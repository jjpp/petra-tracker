# SPDX-License-Identifier: GPL-3.0-or-later
"""The polling switch, and reload behaviour of the config entry."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.petra_tracker.const import EVENT_STATE_CHANGED, OPT_ACTIVE_INTERVAL_S

from .. import synthetic as syn
from .conftest import mock_api
from .test_integration import ASSIGNED, CONNECTION, STATUS, TOKEN_EXPIRY, arriving, refresh, setup

SWITCH = "switch.petra_account_polling"


async def set_polling(hass: HomeAssistant, on: bool) -> None:
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()


async def test_switch_off_stops_all_requests(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    assert hass.states.get(SWITCH).state == "on"

    await set_polling(hass, False)
    calls = aioclient_mock.call_count
    for _ in range(3):
        frozen.tick(timedelta(minutes=10))
        await refresh(hass, entry)
    assert aioclient_mock.call_count == calls  # no requests while paused
    assert entry.runtime_data.update_interval is None
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(STATUS).state == STATE_UNAVAILABLE  # would be stale
    assert hass.states.get(TOKEN_EXPIRY).state != STATE_UNAVAILABLE  # account entities stay
    assert hass.states.get(CONNECTION).state != STATE_UNAVAILABLE


async def test_switch_on_polls_immediately(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    await set_polling(hass, False)
    calls = aioclient_mock.call_count
    await set_polling(hass, True)
    assert aioclient_mock.call_count > calls
    assert hass.states.get(STATUS).state == "scheduled"
    assert entry.runtime_data.update_interval is not None


async def test_paused_state_survives_reload_without_any_request(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    await set_polling(hass, False)
    mock_api(aioclient_mock)  # resets the request log
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0  # not even the start-up poll
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(STATUS).state == STATE_UNAVAILABLE


async def test_short_pause_announces_changes_long_pause_does_not(hass, aioclient_mock, frozen):
    frozen.move_to(ASSIGNED + timedelta(minutes=1))
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=1)))
    entry = await setup(hass)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    # Short pause (5 min): the car arrived meanwhile -> announced on resume.
    await set_polling(hass, False)
    frozen.tick(timedelta(minutes=5))
    arrived = syn.order(combined="arrived", state="customer", car_no=syn.CAR_NO,
                        assigned=ASSIGNED, events=[(3, ASSIGNED + timedelta(minutes=5))])
    mock_api(aioclient_mock, syn.order_list(arrived), syn.detail(arrived, []))
    await set_polling(hass, True)
    assert [(e.data["old_status"], e.data["new_status"]) for e in events] == [
        ("approaching", "at_pickup")
    ]

    # Long pause (2 h): the trip finished meanwhile -> recorded silently.
    await set_polling(hass, False)
    frozen.tick(timedelta(hours=2))
    done = syn.order(combined="concluded", car_no=syn.CAR_NO, assigned=ASSIGNED,
                     started=syn.PICKUP_TIME, ended=syn.PICKUP_TIME + timedelta(minutes=12))
    mock_api(aioclient_mock, syn.order_list(done))
    await set_polling(hass, True)
    assert len(events) == 1
    assert hass.states.get(STATUS).state in ("finished", "idle")
    await refresh(hass, entry)  # and normal operation continues
    assert len(events) == 1


async def test_entry_data_change_does_not_reload_but_options_do(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass)
    coordinator = entry.runtime_data
    other = syn.order(book_ref=700009, person_id=502, first_name="SAM")
    mock_api(aioclient_mock, syn.order_list(syn.order(), other))
    await refresh(hass, entry)  # discovers a child -> entry.data updated
    assert entry.data["persons"]["502"] == "Sam"
    assert entry.runtime_data is coordinator  # no reload

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPT_ACTIVE_INTERVAL_S: 20}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is not coordinator  # options changed -> reloaded
