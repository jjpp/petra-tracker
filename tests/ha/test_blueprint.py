# SPDX-License-Identifier: GPL-3.0-or-later
"""The notification blueprint, exercised in a real Home Assistant test instance."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

ROOT = Path(__file__).resolve().parents[2]
BLUEPRINT = ROOT / "blueprints" / "automation" / "petra_tracker" / "trip_notifications.yaml"


@pytest.fixture
async def setup_blueprint(hass: HomeAssistant, tmp_path):
    hass.config.config_dir = str(tmp_path)
    target = tmp_path / "blueprints" / "automation" / "petra_tracker"
    target.mkdir(parents=True)
    shutil.copy(BLUEPRINT, target)
    notify = async_mock_service(hass, "notify", "test_phone")
    tts = async_mock_service(hass, "tts", "speak")

    async def _setup(**inputs):
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "use_blueprint": {
                        "path": "petra_tracker/trip_notifications.yaml",
                        "input": {"notify_action": "notify.test_phone", **inputs},
                    }
                }
            },
        )
        await hass.async_block_till_done()
        return notify, tts

    return _setup


async def fire(hass: HomeAssistant, event_type: str, **data):
    hass.bus.async_fire(f"petra_tracker_{event_type}", {"config_entry_id": "x", **data})
    await hass.async_block_till_done()


def state(new, old="scheduled", **extra):
    return {"person_name": "Alex", "old_status": old, "new_status": new, "eta_minutes": None,
            "car_no": 42, "book_ref": 1, **extra}


async def test_state_changes_are_notified(hass, setup_blueprint):
    notify, _ = await setup_blueprint()
    await fire(hass, "state_changed", **state("approaching", eta_minutes=7))
    await fire(hass, "state_changed", **state("at_pickup", "approaching"))
    await fire(hass, "state_changed", **state("onboard", "at_pickup"))
    await fire(hass, "state_changed", **state("finished", "onboard"))
    assert [c.data["message"] for c in notify] == [
        "Alex's taxi is on its way, about 7 min.",
        "Alex's taxi has arrived.",
        "Alex has been picked up.",
        "Alex has arrived.",
    ]
    assert notify[0].data["title"] == "School transport"


async def test_car_not_assigned_messages(hass, setup_blueprint):
    notify, _ = await setup_blueprint()
    await fire(hass, "car_not_assigned", person_name="Alex", minutes_to_pickup=1, pickup_passed=False)
    await fire(hass, "car_not_assigned", person_name="Alex", minutes_to_pickup=-4, pickup_passed=True)
    assert [c.data["message"] for c in notify] == [
        "No car assigned yet for Alex's pickup in 1 min.",
        "Alex's pickup time has passed and no car is assigned yet.",
    ]


async def test_selection_child_filter_and_account_events(hass, setup_blueprint):
    notify, _ = await setup_blueprint(person_name="Sam")
    await fire(hass, "state_changed", **state("approaching"))  # Alex: filtered out
    await fire(hass, "eta_changed", person_name="Sam", old_eta=5, new_eta=4, leg="to_pickup")  # not selected
    await fire(hass, "state_changed", **{**state("approaching"), "person_name": "SAM"})
    await fire(hass, "connection_degraded", failed_for_s=200, trip_active=True)  # account-level
    await fire(hass, "schema_drift", severity="info", kind="unknown_field", path="$.x", detail="")
    await fire(hass, "schema_drift", severity="warning", kind="enum", path="$.y", detail="")
    assert [c.data["message"] for c in notify] == [
        "SAM's taxi is on its way.",
        "Taxi tracking has had no connection for 3 min.",
        "Taxi tracking: the service's API changed (warning); updates may be wrong.",
    ]


async def test_eta_updates_spoken_only(hass, setup_blueprint):
    notify, tts = await setup_blueprint(
        notify_action="",
        notify_on=["eta_changed"],
        tts_entity="tts.example",
        media_players=["media_player.kitchen", "media_player.hall"],
    )
    await fire(hass, "eta_changed", person_name="Alex", old_eta=5, new_eta=4, leg="to_pickup")
    await fire(hass, "state_changed", **state("approaching"))  # not selected
    assert notify == []
    [call] = tts
    assert call.data["message"] == "Alex's taxi: 4 min to pickup."
    assert call.data["media_player_entity_id"] == ["media_player.kitchen", "media_player.hall"]
    assert call.data["entity_id"] == ["tts.example"]
