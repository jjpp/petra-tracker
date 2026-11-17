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
| 3 | Routing-based ETA (Waze / OSRM / ORS comparison) | planned |
| 4 | Username/password + SMS 2FA login (instead of pasting a token) | planned |
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
   (API address and client key).
2. **Token (JWT)** — for now, copy it from the web client: open the browser's developer tools on
   the operator's web page, find a request to the API and copy the value after
   `Authorization: JWT` (pasting the whole `JWT eyJ…` string is fine). Tokens last ~30 days; HA
   asks for a new one when it expires (and fires an event 7 days before).

The children on the account are discovered automatically from their bookings.

### Entities

Per child (device *"\<name\> transport"*):

| Entity | Meaning |
|---|---|
| `sensor.<name>_transport_trip_status` | `idle`, `scheduled`, `approaching`, `at_pickup`, `onboard`, `finished`, `cancelled`, `unknown` (raw API status in attributes) |
| `sensor.<name>_transport_eta` | minutes to the next waypoint; attributes `leg` (`to_pickup`/`to_destination`), `source` (`schedule`/`gps`/`arrived`), `distance_m`, `approximate` |
| `device_tracker.<name>_transport_car` | live car position during a trip (shows on the HA map) |
| `binary_sensor.<name>_transport_trip_active` | on while a car is on its way / at pickup / onboard |
| `sensor.<name>_transport_next_pickup` | next scheduled pickup |

Account (device *"Petra account"*): `token_expiry`, `api_schema_status`, `api_connection`.

### Notifications (blueprint)

The easiest way to get notified is the included blueprint:

[![Import the blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fjjpp%2Fpetra-tracker%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fpetra_tracker%2Ftrip_notifications.yaml)

(or *Settings → Automations & scenes → Blueprints → Import blueprint* with
`https://github.com/jjpp/petra-tracker/blob/main/blueprints/automation/petra_tracker/trip_notifications.yaml`),
then *Create automation* from it and choose:

- **what to be told about**: car on its way, at pickup, picked up, arrived, cancelled,
  **no car assigned shortly before / after the pickup time**, ETA changes, a periodic update, and
  problems with the tracking itself (no connection, token expiring, API changed);
- optionally **one child** (by first name);
- a **push notification** action (e.g. `notify.mobile_app_my_phone`) and/or a **text-to-speech**
  engine plus **speakers** for spoken announcements.

A common setup is push notifications for the status changes and spoken ETA updates on a kitchen
speaker (two automations from the same blueprint).

**Message texts** are adjustable in the automation's *Message texts* section (defaults are in
English). Placeholders are replaced with the trip's details, also in the title:

| Placeholder | Value |
|---|---|
| `{child}` | first name |
| `{eta}` / `{old_eta}` | minutes to the next stop / previous ETA |
| `{leg}` | `pickup` or `destination` |
| `{status}` | trip status in words (`on its way`, `at pickup`, `on board`, …) |
| `{car}` | car number |
| `{pickup_time}` | scheduled pickup time, `HH:MM` in Home Assistant's time zone |
| `{minutes}` | minutes before / after the pickup time ("no car assigned" messages) |
| `{failed_minutes}`, `{days}`, `{severity}` | connection outage, token expiry, API change |

A placeholder without a value becomes `?`. Empty a text to never send that message.

**Updating the blueprint:** *Settings → Automations & scenes → Blueprints →* ⋮ next to
*Petra Trip Tracker – trip notifications* *→ Re-import blueprint*. Automations created from it use
the new version right away; your chosen options and texts are kept (new options get their
defaults). Automations where you used *Take control* are independent copies and are not updated.
The Marketplace updates the integration, not the blueprint, so re-import it after releases that
mention blueprint changes.

### Events

The blueprint is built on these events; use them directly for your own automations. All trip
events include `person_name`, `book_ref`, `car_no` and `scheduled_pickup`.

| Event | When | Data |
|---|---|---|
| `petra_tracker_state_changed` | trip status changes | `person_name`, `old_status`, `new_status`, `eta_minutes`, `car_no`, `scheduled_pickup`, … |
| `petra_tracker_car_not_assigned` | once per trip: still only *booked* *N* minutes before pickup (default 1) or after the pickup time | `person_name`, `scheduled_pickup`, `minutes_to_pickup` (negative = late), `pickup_passed` |
| `petra_tracker_eta_changed` | ETA changed by ≥ the configured granularity (default 1 min) | `old_eta`, `new_eta`, `leg`, `status` |
| `petra_tracker_heartbeat` | every *N* minutes during a trip (off by default) | `status`, `eta_minutes` |
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
(45 min), ETA event granularity (1 min), heartbeat (off), "no car assigned" warning lead time
(1 min; 0 = at pickup time), connection-alert delay (3 min), track the
car after pickup (on), assumed speed for the straight-line ETA (25 km/h), telemetry (off) and its
retention (30 days).

### Behaviour notes

- **ETA** is currently straight-line distance × 1.4 at the assumed speed. It is rough on real
  roads; routing-based ETA is phase 3.
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
