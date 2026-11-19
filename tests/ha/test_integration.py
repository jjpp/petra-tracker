# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.petra_tracker.const import (
    EVENT_CAR_NOT_ASSIGNED,
    EVENT_CONNECTION_DEGRADED,
    EVENT_CONNECTION_RESTORED,
    EVENT_ETA_CHANGED,
    EVENT_SCHEMA_DRIFT,
    EVENT_STATE_CHANGED,
    OPT_TELEMETRY,
)
from custom_components.petra_tracker.core.telemetry import REDACTED

from .. import synthetic as syn
from .conftest import make_entry, mock_api

STATUS = "sensor.alex_transport_trip_status"
ETA = "sensor.alex_transport_eta"
CAR = "device_tracker.alex_transport_car"
ACTIVE = "binary_sensor.alex_transport_trip_active"
CONNECTION = "binary_sensor.petra_account_api_connection"
SCHEMA = "sensor.petra_account_api_schema_status"
TOKEN_EXPIRY = "sensor.petra_account_token_expiry"

ASSIGNED = syn.PICKUP_TIME - timedelta(minutes=9)


async def setup(hass: HomeAssistant, **options):
    entry = make_entry(**options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def arriving(now):
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    track = [g for g in syn.approach_track(ASSIGNED, 10) if g[0] <= now]
    return syn.order_list(o), syn.detail(o, track)


async def refresh(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_setup_scheduled_trip(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(STATUS).state == "scheduled"
    eta = hass.states.get(ETA)
    assert eta.state == "30" and eta.attributes["source"] == "schedule"
    status = hass.states.get(STATUS).attributes
    assert status["pooling_allowed"] is True and status["coordinated_position"] == 1
    assert status["booking_attributes"] == ["SKOLTUR"]  # only the ones that are set
    assert hass.states.get(ACTIVE).state == "off"
    assert hass.states.get(CONNECTION).state == "on"
    assert hass.states.get(SCHEMA).state == "ok"
    assert hass.states.get(TOKEN_EXPIRY).state.startswith("2030-02-13")
    # Outside an active trip we poll slowly-ish (inside the lead window here: fast).
    assert entry.runtime_data.update_interval == timedelta(seconds=30)


async def test_trip_progress_updates_entities_and_fires_events(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    state_events = async_capture_events(hass, EVENT_STATE_CHANGED)
    eta_events = async_capture_events(hass, EVENT_ETA_CHANGED)

    frozen.move_to(ASSIGNED + timedelta(minutes=1))
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=1)))
    await refresh(hass, entry)
    assert hass.states.get(STATUS).state == "approaching"
    assert hass.states.get(ACTIVE).state == "on"
    car = hass.states.get(CAR)
    assert car.attributes["latitude"] > syn.PICKUP[0]  # north of pickup, driving south
    assert hass.states.get(ETA).attributes["source"] == "gps"
    assert [(e.data["old_status"], e.data["new_status"]) for e in state_events] == [
        ("scheduled", "approaching")
    ]
    assert state_events[0].data["person_name"] == "Alex"
    assert state_events[0].data["config_entry_id"] == entry.entry_id

    first_eta = int(hass.states.get(ETA).state)
    frozen.move_to(ASSIGNED + timedelta(minutes=6))
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=6)))
    await refresh(hass, entry)
    assert int(hass.states.get(ETA).state) < first_eta
    assert eta_events and eta_events[-1].data["new_eta"] == int(hass.states.get(ETA).state)

    frozen.move_to(syn.PICKUP_TIME + timedelta(minutes=12))
    done = syn.order(combined="concluded", car_no=syn.CAR_NO, assigned=ASSIGNED,
                     started=syn.PICKUP_TIME, ended=syn.PICKUP_TIME + timedelta(minutes=12))
    at_school = [(syn.PICKUP_TIME + timedelta(minutes=12), *syn.DESTINATION)]
    mock_api(aioclient_mock, syn.order_list(done), syn.detail(done, at_school))
    await refresh(hass, entry)
    assert hass.states.get(STATUS).state == "finished"
    assert hass.states.get(ACTIVE).state == "off"
    assert hass.states.get(CAR).attributes.get("latitude") is None
    assert state_events[-1].data["new_status"] == "finished"


async def test_backoff_degraded_and_restored(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    coordinator = entry.runtime_data
    degraded = async_capture_events(hass, EVENT_CONNECTION_DEGRADED)
    restored = async_capture_events(hass, EVENT_CONNECTION_RESTORED)

    mock_api(aioclient_mock, exc=aiohttp.ClientConnectionError("down"))
    intervals = []
    for _ in range(4):
        frozen.tick(timedelta(minutes=1))
        await refresh(hass, entry)
        intervals.append(coordinator.update_interval)
    # Entities keep the last known state but the connection sensor reports the outage.
    assert hass.states.get(STATUS).state == "scheduled"
    assert hass.states.get(CONNECTION).state == "off"
    assert coordinator.backoff.failures == 4
    assert max(intervals) <= timedelta(minutes=5)
    assert len(degraded) == 1  # failing >= 3 min, announced once
    assert degraded[0].data["trip_active"] is False

    mock_api(aioclient_mock)
    await refresh(hass, entry)
    assert hass.states.get(CONNECTION).state == "on"
    assert len(restored) == 1
    assert coordinator.backoff.failures == 0


async def test_rate_limited_honours_retry_after(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"https://api.example.invalid/api/users/{syn.USER_ID}/persons/orders",
        status=429, text="Too many requests", headers={"Retry-After": "240"},
    )
    await refresh(hass, entry)
    assert entry.runtime_data.update_interval == timedelta(seconds=240)


async def test_auth_failure_starts_reauth(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    mock_api(aioclient_mock, status=401)
    await refresh(hass, entry)
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)


async def test_schema_drift_reported_once(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    drift = async_capture_events(hass, EVENT_SCHEMA_DRIFT)
    weird = syn.order(combined="teleporting")
    mock_api(aioclient_mock, syn.order_list(weird))
    await refresh(hass, entry)
    await refresh(hass, entry)
    assert hass.states.get(SCHEMA).state == "warning"
    assert hass.states.get(STATUS).state == "unknown"
    # Two distinct drifts (combinedStatus and status.state), each reported once
    # despite two polls.
    assert sorted(e.data["path"] for e in drift) == [
        "$.items[].combinedStatus", "$.items[].status.state"
    ]
    assert all(e.data["kind"] == "enum" and "teleporting" in e.data["detail"] for e in drift)


async def test_telemetry_is_opt_in_and_redacted(hass, aioclient_mock, frozen, tmp_path):
    hass.config.config_dir = str(tmp_path)
    mock_api(aioclient_mock)
    await setup(hass)
    assert not (tmp_path / "petra_tracker").exists()  # off by default

    await hass.config_entries.async_unload(hass.config_entries.async_entries()[0].entry_id)
    hass.config_entries.async_update_entry(
        hass.config_entries.async_entries()[0],
        options={**hass.config_entries.async_entries()[0].options, OPT_TELEMETRY: True},
    )
    await hass.config_entries.async_setup(hass.config_entries.async_entries()[0].entry_id)
    await hass.async_block_till_done()
    files = list((tmp_path / "petra_tracker" / "telemetry").rglob("*.jsonl"))
    assert files
    line = json.loads(files[0].read_text().splitlines()[0])
    item = line["data"]["items"][0]
    assert item["firstName"] == REDACTED and item["phone"] == REDACTED
    assert item["bookRefNo"] == syn.BOOK_REF


async def test_new_child_gets_entities_later(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    assert hass.states.get("sensor.sam_transport_trip_status") is None
    other = syn.order(book_ref=700009, person_id=502, first_name="SAM")
    mock_api(aioclient_mock, syn.order_list(syn.order(), other))
    await refresh(hass, entry)
    assert hass.states.get("sensor.sam_transport_trip_status").state == "scheduled"
    assert entry.data["persons"]["502"] == "Sam"


async def test_unload(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


# -- "car not assigned" warning and restarts ---------------------------------------

async def test_car_not_assigned_event_fires_once(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)  # still only booked
    entry = await setup(hass)
    late = async_capture_events(hass, EVENT_CAR_NOT_ASSIGNED)
    frozen.move_to(syn.PICKUP_TIME - timedelta(minutes=2))
    await refresh(hass, entry)
    assert late == []
    frozen.move_to(syn.PICKUP_TIME - timedelta(seconds=50))
    await refresh(hass, entry)
    frozen.move_to(syn.PICKUP_TIME + timedelta(minutes=3))
    await refresh(hass, entry)
    assert len(late) == 1
    assert late[0].data["person_name"] == "Alex" and late[0].data["pickup_passed"] is False


async def test_restart_does_not_repeat_or_invent_notifications(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    frozen.move_to(syn.PICKUP_TIME - timedelta(seconds=30))
    await refresh(hass, entry)  # car_not_assigned sent
    frozen.move_to(ASSIGNED + timedelta(minutes=10))
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=10)))
    await refresh(hass, entry)  # approaching sent

    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    late = async_capture_events(hass, EVENT_CAR_NOT_ASSIGNED)
    eta = async_capture_events(hass, EVENT_ETA_CHANGED)
    frozen.tick(timedelta(seconds=30))
    assert await hass.config_entries.async_reload(entry.entry_id)  # like a restart
    await hass.async_block_till_done()
    assert (events, late, eta) == ([], [], [])
    assert hass.states.get(STATUS).state == "approaching"

    # A real change after the restart is still announced, relative to before.
    arrived = syn.order(combined="arrived", state="customer", car_no=syn.CAR_NO,
                        assigned=ASSIGNED, events=[(3, syn.PICKUP_TIME)])
    mock_api(aioclient_mock, syn.order_list(arrived), syn.detail(arrived, []))
    await refresh(hass, entry)
    assert [(e.data["old_status"], e.data["new_status"]) for e in events] == [
        ("approaching", "at_pickup")
    ]


async def test_change_during_downtime_is_announced_once(hass, aioclient_mock, frozen):
    frozen.move_to(ASSIGNED + timedelta(minutes=2))
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=2)))
    entry = await setup(hass)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    frozen.move_to(syn.PICKUP_TIME + timedelta(minutes=3))  # HA was off for ~8 minutes
    onboard = syn.order(combined="personIn", state="customer", car_no=syn.CAR_NO,
                        assigned=ASSIGNED, started=syn.PICKUP_TIME + timedelta(minutes=2),
                        events=[(3, syn.PICKUP_TIME), (0, syn.PICKUP_TIME + timedelta(minutes=2))])
    mock_api(aioclient_mock, syn.order_list(onboard), syn.detail(onboard, []))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [(e.data["old_status"], e.data["new_status"]) for e in events] == [
        ("approaching", "onboard")
    ]


async def test_cold_start_after_pickup_time_is_silent(hass, aioclient_mock, freezer):
    freezer.move_to(syn.PICKUP_TIME + timedelta(minutes=5))  # no saved memory at all
    mock_api(aioclient_mock)  # still only booked: would be "late"
    late = async_capture_events(hass, EVENT_CAR_NOT_ASSIGNED)
    entry = await setup(hass)
    await refresh(hass, entry)
    assert late == []
    assert hass.states.get(STATUS).state == "scheduled"


async def test_trip_ended_away_from_destination(hass, aioclient_mock, frozen):
    started = syn.PICKUP_TIME + timedelta(minutes=1)
    frozen.move_to(started)
    onboard = syn.order(combined="personIn", state="customer", car_no=syn.CAR_NO, assigned=ASSIGNED,
                        started=started, events=[(3, started), (0, started)])
    mock_api(aioclient_mock, syn.order_list(onboard), syn.detail(onboard, [(started, *syn.PICKUP)]))
    entry = await setup(hass)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    ended = started + timedelta(minutes=2)  # driver ends the trip still at the pickup
    frozen.move_to(ended + timedelta(seconds=15))
    done = syn.order(combined="concluded", car_no=syn.CAR_NO, assigned=ASSIGNED, started=started,
                     ended=ended, events=[(3, started), (0, started), (1, ended)])
    mock_api(aioclient_mock, syn.order_list(done),
             syn.detail(done, [(started, *syn.PICKUP), (ended, *syn.PICKUP)]))
    await refresh(hass, entry)
    [ev] = events
    assert ev.data["new_status"] == "finished" and ev.data["ended_at_destination"] is False
    assert ev.data["estimated_arrival"] and ev.data["eta_minutes"] > 0
    attrs = hass.states.get(STATUS).attributes
    assert attrs["ended_at_destination"] is False and attrs["end_distance_m"] > 2000

    def detail_calls():
        return sum("/orders/" in str(c[1]) and "/persons/" not in str(c[1]) for c in aioclient_mock.mock_calls)

    before = detail_calls()
    frozen.tick(timedelta(seconds=30))
    await refresh(hass, entry)
    assert detail_calls() == before  # the extra detail fetch happens once
    assert hass.states.get(STATUS).attributes["ended_at_destination"] is False


async def test_reauth_is_requested_before_the_token_expires(hass, aioclient_mock, freezer):
    freezer.move_to(syn.DAY + timedelta(days=28))  # token (issued DAY-1, 30 days) expires in ~1 day
    mock_api(aioclient_mock)
    entry = await setup(hass)
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" and f["context"].get("entry_id") == entry.entry_id
               for f in flows)
    assert hass.states.get(STATUS).state != "unavailable"  # still working meanwhile


async def test_ended_near_destination_option(hass, aioclient_mock, frozen):
    from custom_components.petra_tracker.const import OPT_END_NEAR_DESTINATION_M

    started = syn.PICKUP_TIME + timedelta(minutes=1)
    frozen.move_to(started)
    onboard = syn.order(combined="personIn", state="customer", car_no=syn.CAR_NO, assigned=ASSIGNED,
                        started=started, events=[(3, started), (0, started)])
    mock_api(aioclient_mock, syn.order_list(onboard), syn.detail(onboard, [(started, *syn.PICKUP)]))
    entry = await setup(hass, **{OPT_END_NEAR_DESTINATION_M: 300})
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    ended = started + timedelta(minutes=8)
    frozen.move_to(ended + timedelta(seconds=10))
    done = syn.order(combined="concluded", car_no=syn.CAR_NO, assigned=ASSIGNED, started=started,
                     ended=ended, events=[(3, started), (0, started), (1, ended)])
    before_school = (syn.DESTINATION[0] + 500 / 111_200, syn.DESTINATION[1])  # 500 m away
    mock_api(aioclient_mock, syn.order_list(done), syn.detail(done, [(ended, *before_school)]))
    await refresh(hass, entry)
    [ev] = events
    assert ev.data["ended_at_destination"] is False  # 500 m > configured 300 m


async def test_last_update_sensors(hass, aioclient_mock, frozen):
    from homeassistant.util import dt as dt_util

    frozen.move_to(ASSIGNED + timedelta(minutes=3))
    lst, det = arriving(ASSIGNED + timedelta(minutes=3))
    mock_api(aioclient_mock, lst, det)
    entry = await setup(hass)
    account = hass.states.get("sensor.petra_account_last_update")
    trip = hass.states.get("sensor.alex_transport_last_update")
    assert dt_util.parse_datetime(account.state) == ASSIGNED + timedelta(minutes=3)  # the poll
    # Latest news about the trip = the newest GPS fix (status changed at assignment).
    assert dt_util.parse_datetime(trip.state) == ASSIGNED + timedelta(minutes=3)
    assert dt_util.parse_datetime(trip.attributes["last_status_change"]) == ASSIGNED

    # During an outage the account sensor keeps the last successful poll.
    mock_api(aioclient_mock, exc=aiohttp.ClientConnectionError())
    frozen.tick(timedelta(minutes=2))
    await refresh(hass, entry)
    assert dt_util.parse_datetime(hass.states.get("sensor.petra_account_last_update").state) == ASSIGNED + timedelta(minutes=3)


async def test_trip_last_update_without_trip(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock, syn.order_list())
    await setup(hass)
    assert hass.states.get("sensor.alex_transport_last_update").state == "unknown"


async def test_no_schema_alert_for_missing_gps_at_assignment(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    drift = async_capture_events(hass, EVENT_SCHEMA_DRIFT)
    frozen.move_to(ASSIGNED)
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    mock_api(aioclient_mock, syn.order_list(o), o)  # detail without gpsPositions, same second
    await refresh(hass, entry)
    assert drift == [] and hass.states.get(SCHEMA).state == "ok"
    assert hass.states.get(STATUS).state == "approaching"
