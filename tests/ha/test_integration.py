# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.petra_tracker.const import (
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
    mock_api(aioclient_mock, syn.order_list(done))
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
