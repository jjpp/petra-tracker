# Petra Trip Tracker

Track school / service transport trips booked through the **Petra** platform (Finn Frogne A/S),
as used by e.g. *Cabonline Serviceresor / Skoltransport* (Sweden) and *Skoleturer* (Norway).

The official app shows little more than a status and does not refresh on its own. This project
polls the same backend, follows the trip as it happens and makes it available for automations:
status, **ETA in minutes**, and the **car's live position**.

> **Unofficial.** Not affiliated with Cabonline or Finn Frogne. It uses an undocumented API with
> your own account, for personal use. The API can change at any time; the integration validates
> every response and tells you when it changed (see *Schema watchdog*).

Contents:

- `custom_components/petra_tracker/` — Home Assistant integration (HACS-compatible)
- `custom_components/petra_tracker/core/` — HA-independent core (API client, parsing, ETA, schema guard)
- `contract/` — the assumed API contract, shared with the (planned) Android app
- `docs/architecture.md` — design
- `android/` — planned standalone app (not started)

## Status

| Phase | What | State |
|---|---|---|
| 1 | Core: client, schema guard, ETA (straight-line), tracker, backoff, telemetry | done |
| 2 | Home Assistant integration MVP | done |
| 3 | Routing-based ETA (Waze / OSRM / ORS) | done; engine comparison on real trips ongoing |
| 4 | Username/password + verification code login (instead of pasting a token) | done |
| 5 | Pooled-trip handling (needs a real pooled trip captured via telemetry) | planned |
| 6 | Android app | planned |

## Home Assistant

### Install

- **Home Assistant 2026.11 or newer:** *Settings → Marketplace → Add from link* →
  `https://github.com/jjpp/petra-tracker` → *Add*, then open it and install. (Adding a repository
  from a link asks you to connect GitHub once.)
- **Older Home Assistant with HACS:** HACS → *Custom repositories* → add
  `https://github.com/jjpp/petra-tracker` as an *Integration*, install, restart.
- **Manually:** copy `custom_components/petra_tracker` into your HA `config/custom_components/`
  and restart.

Then add it under *Settings → Devices & services*.

### Set up

*Settings → Devices & services → Add integration → Petra Trip Tracker*

1. **Operator country** — the integration downloads that operator's public configuration
   (API address and client key). Choose **Username and password**.
2. **Log in** with the same username (e-mail) and password as in the operator's app or web page.
3. **Verification code** — enter the code you receive by e-mail or SMS, depending on the account
   (*Send a new code* if it does not arrive; check the spam folder).

The children on the account are discovered automatically from their bookings.

**The password is not stored** — only the username (to pre-fill the form) and the login token,
which is valid for about 30 days. Three days before it expires, Home Assistant shows
*Re-authentication required*: log in again (password + verification code); tracking keeps working
meanwhile, and if the token runs out anyway, polling pauses until you log in again. Seven days
before expiry the `petra_tracker_token_expiring` event fires as well.

**Log in again at any time** (e.g. to test it, or to switch from a pasted token to logging in):
*Settings → Devices & services → Petra Trip Tracker →* ⋮ *→ Reconfigure*.

*Advanced:* instead of logging in you can paste a token (JWT) copied from the web client (browser
developer tools → a request to the API → the value after `Authorization: JWT`).

### Entities

Per child (device *"\<name\> transport"*):

| Entity | Meaning |
|---|---|
| `sensor.<name>_transport_trip_status` | `idle`, `scheduled`, `approaching`, `at_pickup`, `onboard`, `finished`, `cancelled`, `unknown`; attributes include the raw API status, `pooling_allowed` (operator may share the car), `coordinated_position` (> 1 = other stops first) and `booking_attributes` (e.g. `SKOLTUR`) |
| `sensor.<name>_transport_eta` | minutes to the next waypoint; attributes `leg` (`to_pickup`/`to_destination`), `source` (`schedule` before a car is assigned, `gps`, `arrived`), `distance_m`, `approximate`, `location_known`; unknown while a car on its way has no position |
| `device_tracker.<name>_transport_car` | live car position during a trip (shows on the HA map) |
| `binary_sensor.<name>_transport_trip_active` | on while a car is on its way / at pickup / onboard |
| `sensor.<name>_transport_next_pickup` | next scheduled pickup |
| `sensor.<name>_transport_last_update` | when the service last had news about the current trip (status change, GPS fix, pickup/drop-off); attributes `last_gps_fix`, `last_status_change`, `last_poll` |

Account (device *"Petra account"*): `last_update` (last successful poll), `token_expiry`,
`api_schema_status`, `api_connection`, and the
**`switch.petra_account_polling`** switch (see *Turning it off*).

### Notifications (blueprint)

The easiest way to get notified is the included blueprint:

[![Import the blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fjjpp%2Fpetra-tracker%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fpetra_tracker%2Ftrip_notifications.yaml)

(or *Settings → Automations & scenes → Blueprints → Import blueprint* with
`https://github.com/jjpp/petra-tracker/blob/main/blueprints/automation/petra_tracker/trip_notifications.yaml`),
then *Create automation* from it and choose:

- **what to be told about**: car on its way, at pickup, picked up, arrived, cancelled,
  **no car assigned shortly before / after the pickup time**, **the car was changed** during the
  trip, ETA changes, a periodic update, and
  problems with the tracking itself (no connection, token expiring, API changed);
- optionally **one child** (by first name);
- a **push notification** action (e.g. `notify.mobile_app_my_phone`) and/or a **text-to-speech**
  engine plus **speakers** for spoken announcements.

A common setup is push notifications for the status changes and spoken ETA updates on a kitchen
speaker (two automations from the same blueprint). Further options:

- **Announce ETA changes after pickup** (on): turn off to hear ETA updates only while the car is on
  its way to the pickup; status changes after that are still announced.
- **"Arrived" while still this far away** (300 m): drivers sometimes press "arrived" well before
  reaching the pickup (seen: 1.8 km, 10 minutes early). If the car is still farther away than
  this, you get "Alex's taxi reports it has arrived, but is still 1.8 km away (about 6 min)"
  instead of "Alex's taxi has arrived". 0 = off.
- **Update one notification instead of adding new ones** (off): for the companion app — each
  child's messages replace the previous notification (same `tag`), status changes still make a
  sound, ETA updates and heartbeats update it silently. Leave off for other notification services.
  If your push action is a **script**, it receives `title`, `message` and `data`; it must pass
  `data` on to the notify action, otherwise the notifications pile up:

  ```yaml
  script:
    notify_family:
      fields: {title: {}, message: {}, data: {}}
      sequence:
        - action: notify.mobile_app_your_phone
          data:
            title: "{{ title }}"
            message: "{{ message }}"
            data: "{{ data | default({}) }}"   # or: dict(data | default({}), channel='Taxi')
  ```

### The car on a map

`device_tracker.<name>_transport_car` has the car's position while a trip is active (positions
arrive about once a minute), so it appears on Home Assistant's **Map** (sidebar). For a dashboard,
add a *Map* card, e.g. with the car's recent path and your own zones (home, school):

```yaml
type: map
entities:
  - entity: device_tracker.alex_transport_car
  - zone.home
hours_to_show: 1
```

Outside a trip the tracker has no position and is not shown.

**Message texts** are adjustable in the automation's *Message texts* section (defaults are in
English). Placeholders are replaced with the trip's details, also in the title:

| Placeholder | Value |
|---|---|
| `{child}` | first name |
| `{eta}` / `{old_eta}` | minutes to the next stop / previous ETA |
| `{leg}` | `pickup` or `destination` |
| `{status}` | trip status in words (`on its way`, `at pickup`, `on board`, …) |
| `{car}` / `{old_car}` | car number / the previous car ("car changed" message) |
| `{distance}` | how far the car still is from the stop (`850 m`, `3.2 km`; decimal separator configurable) |
| `{arrival_time}` | estimated arrival when a trip was ended before the destination |
| `{pickup_time}` | scheduled pickup time, `HH:MM` in Home Assistant's time zone |
| `{minutes}` | minutes before / after the pickup time ("no car assigned" messages) |
| `{failed_minutes}`, `{days}`, `{severity}` | connection outage, token expiry, API change |

A placeholder without a value becomes `?`. Empty a text to never send that message. When the car
is on its way but its location is not known (no ETA), the "on its way", ETA and periodic messages
use their own *location not known* texts instead.

To **translate** the messages, change the texts and, in the *Words and formats* section, the words
used for `{status}`, `{leg}` and `{severity}`, the missing-value text (`?`) and the time format of
`{pickup_time}` (`%H:%M`; e.g. `%H.%M` or `%-I:%M %p`).

**Updating the blueprint:** *Settings → Automations & scenes → Blueprints →* ⋮ next to
*Petra Trip Tracker – trip notifications* *→ Re-import blueprint*. Automations created from it use
the new version right away; your chosen options and texts are kept (new options get their
defaults). Automations where you used *Take control* are independent copies and are not updated.
The Marketplace updates the integration, not the blueprint, so re-import it after releases that
mention blueprint changes.

### Events

The blueprint is built on these events; use them directly for your own automations. All trip
events include `person_name`, `book_ref`, `car_no` and `scheduled_pickup`.

**Location not known:** while a car is on its way but has not sent a position (right after it is
assigned or changed, or when the service's position data fails), there is no ETA: the ETA sensor
is unknown and events carry `location_known: false` and no ETA. Before a car is assigned, the ETA
is the time until the booked pickup.

| Event | When | Data |
|---|---|---|
| `petra_tracker_state_changed` | trip status changes | `person_name`, `old_status`, `new_status`, `eta_minutes`, `distance_m`, `car_no`, `scheduled_pickup`, …; for `finished` also `ended_at_destination`, `end_distance_m`, `estimated_arrival` |
| `petra_tracker_car_not_assigned` | once per trip: still only *booked* *N* minutes before pickup (default 1) or after the pickup time | `person_name`, `scheduled_pickup`, `minutes_to_pickup` (negative = late), `pickup_passed` |
| `petra_tracker_car_changed` | another car took over a trip that is under way | `old_car_no`, `car_no` (the new car), `status`, `eta_minutes`, `location_known` |
| `petra_tracker_eta_changed` | ETA changed by ≥ the configured granularity (default 1 min), or became unknown / known again | `old_eta`, `new_eta` (`null` = location not known), `leg`, `status`, `location_known` |
| `petra_tracker_heartbeat` | during a trip: every *N* minutes (`reason: periodic`) and/or when nothing else was emitted for *M* minutes (`reason: quiet`, e.g. the car is stuck); both off by default | `status`, `eta_minutes`, `leg`, `reason`, `quiet_for_min` |
| `petra_tracker_connection_degraded` / `_restored` | API failing for ≥ *N* minutes (default 3) / back | `failed_for_s`, `trip_active` |
| `petra_tracker_schema_drift` | a response no longer matches the assumed contract | `severity`, `kind`, `path`, `detail` |
| `petra_tracker_token_expiring` | daily, from 7 days before token expiry | `expires_at`, `days_left` |

**Restarts do not cause notifications.** The integration remembers what it has already announced
(saved in Home Assistant's storage), so after a restart or reload nothing is announced twice; a
change that happened *while* Home Assistant was down is announced once. If Home Assistant was off
for more than 30 minutes, the first update after starting is silent.

If you build your own automations, trigger on these **events** rather than on the entities'
state: state triggers also fire when an entity comes back after a restart (e.g. from
`unavailable` to `approaching`). If you do use state triggers, add
`not_from: [unavailable, unknown]`.

### Options

Poll interval during a trip (30 s) and when idle (10 min), fast-poll lead time before a pickup
(45 min), ETA event granularity (1 min), periodic heartbeat (off), heartbeat after *M* quiet minutes (off), "no car assigned" warning lead time
(1 min; 0 = at pickup time), connection-alert delay (3 min), track the
car after pickup (on), assumed speed for the straight-line ETA (25 km/h), ETA routing engine (straight
line) with its server URL / API key / Waze region, telemetry (off) and its retention (30 days).

### ETA routing engines

| Engine | Live traffic | Needs | Notes |
|---|---|---|---|
| **Straight line** (default) | no | nothing | distance × 1.4 at the assumed speed; no external service |
| **OSRM** | no | nothing (or your own server) | road routing; the public demo server is for light use — self-host for regular use |
| **OpenRouteService** | no | free API key (or your own server) | road routing |
| **Waze** | **yes** | nothing | unofficial service (same library as Home Assistant's Waze Travel Time); may break |

Choosing an engine other than *straight line* sends the car's position and the trip's pickup and
destination locations to that service. If it fails, the ETA falls back to the straight-line
estimate (the ETA sensor's `routing` attribute shows which one was used) and the integration logs
a warning once. Results are reused while the car has moved less than 150 m (max. 2 minutes), so
the engine is asked at most about once per poll.

`tools/compare_eta.py` replays a recorded trip through several engines and compares their ETAs
with the actual arrival times.

### Turning it off

- **Notifications only:** turn off the automation(s) created from the blueprint
  (*Settings → Automations & scenes*).
- **Pause (e.g. school holidays):** turn off `switch.petra_account_polling`. No requests are sent
  while it is off, and the children's entities are unavailable. It stays off across restarts;
  turning it on polls immediately (after a pause of more than 30 minutes, changes that happened
  meanwhile are not announced). Because it is a switch, automations can control it, e.g. from a
  holiday calendar:

  ```yaml
  triggers:
    - trigger: calendar
      event: start
      entity_id: calendar.school_holidays
      id: holiday
    - trigger: calendar
      event: end
      entity_id: calendar.school_holidays
      id: school
  actions:
    - action: "switch.turn_{{ 'off' if trigger.id == 'holiday' else 'on' }}"
      target:
        entity_id: switch.petra_account_polling
  ```
- **All contact with the service (emergency stop):** *Settings → Devices & services → Petra Trip
  Tracker →* ⋮ *→ Disable*. Polling stops immediately and the entities become unavailable; the
  configuration and token are kept. *Enable* resumes.
- **Remove completely:** delete the integration entry (removes the token and saved state), then
  uninstall it in *Settings → Marketplace*.
- **If Home Assistant has problems starting** because of a custom integration: restart in safe mode
  (*Settings →* ⋮ *→ Restart Home Assistant → Advanced options → Restart Home Assistant in safe
  mode*); custom integrations are then not loaded.

### Behaviour notes

- **Statuses are what the driver presses, not where the car is.** In real trips, "arrived" was
  pressed about 1 km before the pickup, and a trip was ended right after pickup, 3 km before the
  destination — and the operator stops tracking the car at that moment. So:
  - the "car at pickup" event carries `distance_m` (use `{distance}` in the message), and the ETA
    keeps counting down until the GPS shows the car really there;
  - a finished trip is checked against the car's last position: farther from the destination
    than the *counts as arrived* distance (option, default 1000 m — drivers were seen ending the
    trip 300–550 m before the school while still driving there) gives `ended_at_destination: false`, an `estimated_arrival` (from the last position,
    using the chosen ETA engine) and, in the blueprint, the separate *Trip ended before the
    destination* message instead of "has arrived".

- **ETA**: straight-line distance × 1.4 at the assumed speed by default, which is rough on real
  roads; choose a routing engine for road-based ETAs (see above).
- **Polling** speeds up only around trips; failures back off exponentially with jitter (honouring
  `Retry-After` and the server's rate-limit headers), and stale data stays visible meanwhile.
- **Schema watchdog**: every response is checked against `contract/`. New fields are noted,
  unknown status values and missing/retyped fields raise `petra_tracker_schema_drift` and change
  `api_schema_status`.
- **Telemetry** (opt-in) writes responses to `config/petra_tracker/telemetry/` on your HA host
  with names, phone numbers, addresses and comments removed. It helps diagnose API changes; it
  never leaves your machine.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest-homeassistant-custom-component ruff
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

All test data is synthetic (`tests/synthetic.py`). **Never commit real API responses, tokens,
identifiers or locations** — derive synthetic fixtures instead.
`python tools/check_public_hygiene.py` (also run in CI) flags things that look like real data.

### Releasing

1. Bump `version` in `custom_components/petra_tracker/manifest.json` **and** `pyproject.toml`
   (CI checks they agree).
2. Commit, push, then create a GitHub release with tag `v<version>` (CI fails if the tag and the
   manifest differ). Home Assistant's Marketplace offers releases, not commits, as updates; the
   release notes are shown to users under *Settings → Updates*.

## Contributing

Issues and pull requests are welcome. Because this is about children's transport, please be
careful with data: when reporting a problem, describe it or attach the integration's
*Download diagnostics* output (which removes tokens and personal data), never raw API responses,
screenshots with names/addresses, or tokens. The same applies to test fixtures in pull requests.

## License

Copyright (C) 2026 Jaak Pruulmann-Vengerfeldt

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version. See [LICENSE](LICENSE).

GPL-3.0 (rather than GPL-2.0) because Home Assistant, aiohttp and the Android libraries this
project builds on are Apache-2.0 licensed, which the FSF lists as compatible with GPLv3 but not
with GPLv2.

*Cabonline, Serviceresor, Skoltransport and Petra are names/trademarks of their respective owners
and are used here only to describe compatibility.*
