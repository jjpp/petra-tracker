# School-transport tracker — proposal & architecture

Status: design (not yet implemented). Target backend: Cabonline / Finn Frogne "Petra"
GlobalApi (`https://skoltransport.cabonline.com/api`). Based on static analysis of
`com.cabonline.persontransport` v1.8.2 and one captured live trip.

---

## 1. Goals

Two **fully independent, distributable** products that share only a data **contract**
(no runtime dependency on each other):

- **A) Home Assistant integration** — an installable custom component other parents can add to
  their *own* HA. Tracks rides, exposes **status** + **ETA (minutes)** as entities, and fires
  **events** on change and on a periodic heartbeat so standard HA notifications can be built on
  top. Validates every response against a schema and alerts on drift. **Telemetry collection is
  opt-in** (the author enables it for analysis; other users can leave it off).
- **B) Android app** — a standalone app any parent can install and log into with their *own*
  credentials, **no access to anyone's HA**. Live map + ETA, configurable state/ETA
  notifications, optional onboard-leg tracking. Validates the schema and **fails loudly** on
  incompatible changes; it does **not** collect telemetry.

Both are built so the same codebase can serve other Petra-based transport services by changing
`base_url` + `api_key` (these are per-operator config, see §2).

Cross-cutting: resilience to schema/API change, and capture (HA-side, opt-in) of a real
**pooled** trip — today's ride was single-child, so the pooled shape is still unknown.

---

## 2. Key findings that drive the design (from the live capture)

- **Auth:** `apiKey` + `Authorization: JWT <token>`. The `apiKey` is a per-operator **client**
  identifier (not per-user); the per-user secret is the JWT from login
  (`POST authentication/login` → `verify 2FA`). JWT lifetime **30 days**. The web `access_token`
  is NOT needed.
- **Per-operator config is PUBLIC and fetchable** at
  `https://store.frogne.com/srv/ftp/petra-configs/petra_cab_prod_config_<country>.json`
  (country `se`,`no`; lowercase). Confirmed live contents:
  - SE: `globalApiUrl https://skoltransport.cabonline.com/api/`, `globalApiKey <operator key>`
  - NO: `globalApiUrl https://skoleturer.cabonline.com/api/`, `globalApiKey <operator key>`
  Also carries ToS/privacy links and `appMinVersion`. → **Apps should auto-fetch this at setup**
  (pick country → get the right url+key) rather than hardcode; this is how we support other
  operators/countries cleanly.
  Note: in testing the SE backend also accepted the NO key (apiKey not strictly tenant-bound),
  but the correct, stable choice is the key from the operator's own config.
- **Two endpoints are complementary:**
  - `GET /users/{id}/persons/orders?fromDate&toDate&personIds&grouping=true` — **list**:
    `combinedStatus` + `persons[]` events, **no GPS**.
  - `GET /orders/{bookRefNo}` — **detail**: `status.gpsPositions` (live car track),
    **no person-event array**.
- **State machine** (use list `combinedStatus`):
  `booked → arriving → arrived → personIn → concluded` (+ `deleted`, `cancelled`, `noshow`).
  `persons[].type`: 3 = arrived@pickup, 0 = picked up (=`tripStarted`), 1 = dropped off (=`tripEnded`).
- **GPS:** `status.gpsPositions = [{timeStamp, x=lon, y=lat}]`, **plain decimal degrees**,
  a fix every ~30–60 s, cumulative, covering **approach AND onboard** legs.
- **Coordinate trap:** `addresses[].coordinates` are **×1e7 integers**; `gpsPositions` are
  **plain decimals**.
- **No server ETA** — compute it from GPS → waypoint via a routing engine.
- **Privacy:** responses carry name/phone/address; `GET /users/{id}` even leaks a bcrypt
  password hash — never call it, never store it.


---

## 3. Architecture: two independent clients, one shared contract

There is **no hub**. Each product talks to the Petra API directly with the user's own JWT.
What they share is a **versioned contract** kept in a small language-neutral spec, so both
validate the same shapes and can be updated together when drift is found:

```
            ┌──────────────────────── shared contract (versioned) ────────────────────────┐
            │  endpoints · field/type schema · enums (combinedStatus/state/persons.type)   │
            │  coordinate decode rules · state→notification mapping · schema_version        │
            └───────────────┬──────────────────────────────────────────┬───────────────────┘
                            │ implemented in Python                     │ implemented in Kotlin
                 ┌──────────▼───────────┐                   ┌───────────▼────────────┐
   Petra API ◀──┤ A) HA integration     │     Petra API ◀───┤ B) Android app          │
   (JWT+apiKey) │  poll · validate+alert │     (JWT+apiKey)  │  poll · validate+FAIL   │
                │  ETA · entities/events │                   │  ETA · map · notify     │
                │  telemetry (opt-in)    │                   │  (no telemetry)         │
                └────────────────────────┘                   └─────────────────────────┘
```

The shared logic ("PETRA CORE", §4) is a **design spec implemented twice**, not a shared
service. The contract lives in version control; the HA telemetry is what lets us *detect* drift
and publish a new contract version that both implementations then adopt.

---

## 4. PETRA CORE — the shared behaviour (implemented in each product)

### 4.1 Config
`base_url`, `api_key`, `user_id`, `person_ids[]`, `jwt`, home/school coords (or auto from
`addresses`), poll cadence, ETA-announce granularity, routing provider. **Auto-populate
`base_url`+`api_key` by fetching `petra_cab_prod_config_<country>.json`** (see §2); let the user
pick country at setup.

### 4.2 AuthManager
- Holds JWT; decodes `exp`; exposes `expires_at`; alerts at **T-7 days**.
- On 401/auth-fail → stop, surface re-auth.
- **v1:** manual JWT paste. **Early follow-up (required for other users):** full
  `POST /login` → `verifyTwoFactor(smsCode)` flow; the SMS step is user-assisted (a login
  screen on Android; a re-auth dialog in HA).

### 4.3 Poller (adaptive)
- **Discovery** (list endpoint): slow when idle (configurable, e.g. 5–10 min); ramps up within
  a lead time of a scheduled `pickUpDateTime`. Detects trips + `combinedStatus` changes.
- **Tracking** (detail endpoint): only while active (`arriving/arrived/customer/personIn`);
  default **30 s**; pulls `gpsPositions`; stops at `concluded` + buffer.
- Jitter + backoff; tolerate transient `HTTP 000`/timeouts without faking a state change.

### 4.3a Backoff & rate-limit policy (be a good citizen)
The server advertises `x-ratelimit-limit: 1000` / `x-ratelimit-remaining` / `x-ratelimit-reset`
(express-rate-limit); exceeding it yields **429 + Retry-After**. The Petra app itself has **no**
client backoff, so we add our own. Our normal cadence (30 s active ≈ 120 req/h, idle far less) is
well under 1000/window, so this is for resilience, not throttle-dodging.
- **Success:** poll at the configured cadence; reset any backoff.
- **Transient failure** (timeout, `HTTP 000`, connection reset, 5xx): exponential backoff with
  **full jitter** — base = active poll interval, ×2 each failure, cap ≈ 5 min. Do **not** emit a
  state/ETA change from a failed poll.
- **429:** honour `Retry-After` if present, else back off to the cap; log loudly.
- **Proactive:** if `x-ratelimit-remaining` falls below a threshold, lengthen the interval until
  `x-ratelimit-reset` (parse defensively — the server sometimes reports `reset: 0`).
- **401 / auth failure:** stop polling and raise re-auth — not a backoff case.
- **Failure notification:** if no successful poll for `failure_alert_after` (configurable,
  default **3 min**) while a trip is active, fire a "tracking degraded / connection lost"
  event/notification; clear it on the next success. (HA: event + a `sensor.taxi_connection`
  state; Android: a low-priority notification.)

### 4.4 SchemaGuard (versioned contract)
- Validates every response vs the contract: field presence/types + enum membership
  (`combinedStatus`, `state`, `persons.type`).
- Drift classes: unknown-new-field (benign), missing-field/type-change (breaking),
  **new enum value** (we may mishandle).
- **HA:** alert (event + sensor) and keep operating where safe; retain raw payload (if telemetry on).
- **Android:** on a **breaking/incompatible** change, **fail loudly** — show "this service
  changed, app update needed", stop rather than display wrong data. Benign additions are ignored.
- Both carry a `schema_version`; a server shape that doesn't match the known version is the signal.

### 4.5 CoordDecode
`addresses[].coordinates`: `lat=y/1e7, lon=x/1e7`. `gpsPositions`: `lat=y, lon=x` (decimal).
Centralised so the trap can't recur.

### 4.6 ETAEngine
- Target by leg: `arriving/arrived` → **pickup**; `personIn/customer` → **destination (school)**.
- Routed drive-time via a pluggable provider (§7); fallback haversine + smoothed speed.
- Output `eta_minutes` (int), `distance_m`, `leg`, `speed_kmh`, `last_fix_ts`.
- Pre-assignment (no GPS): `eta_minutes = null` → show scheduled `pickUpDateTime`.
- Smoothing/EMA so the announced minute doesn't flap; dedupe repeated fixes.

### 4.7 TelemetryStore — **HA only, opt-in**
- When enabled: append distinct raw responses to `telemetry/YYYY-MM-DD/<bookRefNo>.jsonl`,
  **PII-redacted** (keys kept; values of `firstName,lastName,phone,email,comment` dropped).
- Purpose: catch a real pooled trip, back-test ETA, diagnose drift → feed new contract versions.
- Default off for distributed installs; the author turns it on. Android never collects.

---

## 5. Deliverable A — Home Assistant integration

HACS custom component `custom_components/petra_tracker`, UI config flow,
`DataUpdateCoordinator` with dynamic interval (idle ↔ active).

### Entities (per child)
- `sensor.taxi_<child>_status` = `combinedStatus` (+ car_no, driver_no, book_ref, pickup/dropoff times, leg).
- `sensor.taxi_<child>_eta` = `eta_minutes` (+ distance_m, leg, speed_kmh, last_fix, provider).
- `device_tracker.taxi_<child>_car` = car lat/lon (`source_type: gps`) → native HA map + companion app.
- `binary_sensor.taxi_<child>_active`; `sensor.taxi_<child>_next_trip`.
- `sensor.taxi_token_expiry`; `sensor.taxi_schema_status` (ok / drift).

### Events (build notifications on these)
- `petra_tracker_state_changed` `{child, book_ref, old, new, car_no}`
- `petra_tracker_eta_changed` `{child, book_ref, old_eta, new_eta, leg}` — fires when the announced
  minute changes (granularity configurable, default 1 min)
- `petra_tracker_heartbeat` `{child, book_ref, state, eta_minutes}` — periodic "still going"
  signal while active (see §8 on how this relates to poll frequency)
- `petra_tracker_car_not_assigned` `{child, book_ref, scheduled_pickup, minutes_to_pickup,
  pickup_passed}` — once per trip, when it is still only booked a configurable time before pickup
  (default 1 min) or after the pickup time
- `petra_tracker_schema_drift`, `petra_tracker_token_expiring`

**Restart safety:** the tracker's memory (last announced phase and ETA per trip, warnings sent) is
persisted in HA storage and restored on start/reload, so events are neither repeated nor invented;
changes that happened during a short downtime are announced once. Memory older than 30 min is
ignored and the first snapshot is then silent. A blueprint
(`blueprints/automation/petra_tracker/trip_notifications.yaml`) turns the events into push and/or
TTS notifications.

**Polling switch:** `switch.petra_account_polling` (state kept in HA storage, not in the config
entry, so toggling it does not reload the entry). Off = no requests; children's entities are
unavailable; on = immediate poll, silent if the pause exceeded 30 min.

**Routing (implemented):** option `routing` = straight_line (default) | osrm | openrouteservice | waze
(`core/routing.py`). The route from the car's last fix to the next stop is attached to the trip
before ETA estimation; failures fall back to straight line. A small cache re-uses a route while the
car moved < 150 m and the result is < 2 min old. `tools/compare_eta.py` compares engines on captures.

### Services & Options
Services: `set_token`, `export_telemetry`, `force_poll`, `reauth` (follow-up).
Options: poll cadences, pickup lead time, heartbeat interval, ETA-announce granularity,
routing provider + endpoint/key, home/school coords, tracked `person_ids`, **telemetry on/off**.

---

## 6. Deliverable B — Android app (standalone)

Native **Kotlin / Jetpack Compose**; **MapLibre** + OSM tiles. Self-contained: user logs in
with their own credentials (v1: paste JWT; soon: username/password + SMS 2FA), no HA anywhere.

### Features
- **Login/account** with the user's own credentials; JWT stored in Android Keystore-encrypted prefs.
- **Live map:** car + pickup + school markers + path polyline; ETA badge; auto-follow.
- **Status card:** state timeline with timestamps.
- **Notifications (configurable):** on state change / on ETA-minute change / both / off;
  foreground service or WorkManager for background polling.
- **Onboard-leg toggle:** after `personIn`, keep tracking and switch ETA target to the school;
  off = stop at pickup. (Data supports both.)
- **SchemaGuard:** on incompatible change, fail loudly ("service changed — update the app"),
  never show wrong data. No telemetry collection.
- **Config:** base_url + api_key (defaulted, editable to support other operators), home/school coords, routing, cadence.

**Sequencing:** build Android *after* the HA integration is implemented, deployed, and used for
a few days, so the contract and ETA logic are proven on a real backend first.

---

## 7. Routing / ETA engine — comparison (your open decision)

**Is Waze usable in a standalone Android app? No, not cleanly.** Waze has no official routing
SDK/API. HA's "Waze Travel Time" works by an *unofficial* Python scraper
(`WazeRouteCalculator`) — fine inside HA, but shipping scraping logic in a distributed Android
app is fragile and ToS-dubious. So Waze is effectively an **HA-side** option; for Android,
traffic-aware means a keyed provider (Mapbox/Google).

| Provider | Live traffic | Key needed | Local/offline | Android-usable | Notes |
|---|---|---|---|---|---|
| **OSRM** (self-host) | No (static speeds) | No | Yes (your server) | Yes (HTTP) | Fully local; fast; no SLA on public demo |
| **OpenRouteService** | No | Yes (free tier) | Self-hostable | Yes | Keyless only if self-hosted |
| **Valhalla / GraphHopper** | Limited (if you feed traffic) | No (self-host) | Yes | Yes | Heavier to run |
| **Mapbox Directions** | **Yes** (driving-traffic) | Yes | No | Yes | Free tier; good Android fit |
| **Google Routes** | **Yes** | Yes + billing | No | Yes | Best traffic; cost/ToS |
| **Waze** (via scraper) | **Yes** | No | No | **Not for distribution** | HA-only, fragile |

**Plan:** make routing **pluggable** behind one interface.
- **HA:** default **Waze** (traffic, keyless) with **OSRM/ORS fallback**; you compare live-traffic
  Waze vs open engines on your own install and we pick. If the open ones are "good enough",
  default to them and keep Waze as optional.
- **Android:** default **OSRM/ORS** (works for everyone, no key, no live traffic); optional
  user-supplied **Mapbox/Google** key to enable traffic. Finalised after the HA comparison.

---

## 8. Defaults & the heartbeat/poll question

Three *independent* knobs (all adjustable), with your chosen defaults:

- **Poll frequency (active):** how often we hit the API during a trip → **30 s** default.
  (Idle poll is separate and slower.)
- **ETA-announce granularity:** announce only when the rounded ETA changes by ≥ this → **1 min** default.
- **State change:** always announced.
- **Heartbeat (optional):** a periodic "still en route, ETA N min" re-announcement even when
  nothing changed — this is the original "notify every X minutes". It is **separate** from poll
  frequency (you can poll every 30 s but re-announce every, say, 2–5 min). Default: your call —
  suggest 0 (off) and rely on change-events, or a modest 3–5 min. In HA it's one event you hook
  or ignore; the 30 s poll does not spam notifications by itself.

So: *poll 30 s, announce on state change, announce when ETA minute changes, optional heartbeat
every N min.*

---

## 9. Resilience & risks

- **Schema/API change:** SchemaGuard in both; HA alerts + (opt-in) keeps raw payloads to
  re-analyse and publish a new contract version; Android fails safe. Shared `schema_version`.
- **Pooled trips (unknown shape):** flagged as expected-unknown; onboard ETA labelled
  approximate when `coordinatedPosition > 1`; first real pooled ride captured via HA telemetry.
- **JWT expiry / multi-user auth:** 30-day token; v1 manual paste + T-7d alert; **full
  username/password + SMS 2FA login is a required early follow-up** (unavoidable for other users).
- **Distributability:** `api_key`/`base_url` configurable (per operator); each user logs in for
  their own JWT; no shared secrets beyond the public client `api_key`.
- **Rate-limit / good-citizen:** server exposes `x-ratelimit-*` (1000/window, 429 + Retry-After);
  app has no built-in backoff, so we add exponential-backoff-with-jitter, honour Retry-After, and
  alert after N min of failures (§4.3a). Normal cadence is well within budget.
- **Privacy:** no PII at rest (HA telemetry redacts; Android stores none); `/users/{id}` never called.
- **Legal/ToS:** undocumented private API used with each user's own account for personal use; may
  change without notice — the design assumes that.

---

## 10. Phasing

1. **Shared contract + PETRA CORE in Python** (poller, SchemaGuard, ETA, coord decode),
   validated against the captured sample.
2. **HA integration MVP:** status + eta + device_tracker + change/heartbeat/schema events +
   token sensor + opt-in telemetry. Haversine ETA to start. → deploy, use a few days.
3. **ETAEngine comparison in HA:** Waze vs OSRM/ORS; pick defaults; onboard-leg target switching.
4. **Full login (username/password + SMS 2FA)** — once the core is proven; needed before wider sharing.
5. **Capture a pooled trip** (HA telemetry) → model multi-stop ETA.
6. **Android app (standalone):** login, map, configurable notifications, onboard-leg toggle, fail-safe SchemaGuard.
7. **Routing on Android:** OSRM/ORS default + optional Mapbox/Google key.

---

## 11. Settled decisions (this round)

- Two **independent, distributable** products; shared **contract**, no hub. ✔
- Android: standalone login, schema-check-and-fail, **no** telemetry; **postponed** until HA is proven. ✔
- HA: opt-in telemetry; schema alert + keep raw. ✔
- Routing: pluggable; HA compares Waze (traffic) vs open engines; Android keyless OSRM/ORS default
  + optional keyed traffic provider. Waze not shippable in standalone Android. ✔
- Defaults: poll **30 s**, ETA announce granularity **1 min**, both adjustable; heartbeat optional/separate. ✔
- Auth: v1 raw JWT; full user/pass + 2FA login as the first major follow-up. ✔
