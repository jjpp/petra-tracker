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
    await hass.config.async_set_time_zone("Europe/Stockholm")
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


PICKUP = "2030-01-15T06:50:00+00:00"  # 07:50 in Europe/Stockholm


def state(new, old="scheduled", **extra):
    return {"person_name": "Alex", "old_status": old, "new_status": new, "eta_minutes": None,
            "car_no": 42, "book_ref": 1, "scheduled_pickup": PICKUP, **extra}


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
    await fire(hass, "car_not_assigned", person_name="Alex", minutes_to_pickup=1,
               pickup_passed=False, scheduled_pickup=PICKUP)
    await fire(hass, "car_not_assigned", person_name="Alex", minutes_to_pickup=-4,
               pickup_passed=True, scheduled_pickup=PICKUP)
    assert [c.data["message"] for c in notify] == [
        "No car assigned yet for Alex's pickup in 1 min.",
        "Alex's pickup time 07:50 has passed and no car is assigned yet.",
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
        "SAM's taxi is on its way, about ? min.",
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


async def test_custom_texts_with_placeholders(hass, setup_blueprint):
    notify, _ = await setup_blueprint(
        title="{child}: taxi",
        msg_approaching="Taxi {car} for {child} ({pickup_time}) arrives in {eta} min",
        msg_car_not_assigned_late="{child}: {minutes} min late, {status}, unknown {old_eta}",
        notify_on=["approaching", "car_not_assigned", "heartbeat"],
        msg_heartbeat="",  # empty text: never sent
    )
    await fire(hass, "state_changed", **state("approaching", eta_minutes=6))
    await fire(hass, "car_not_assigned", person_name="Alex", minutes_to_pickup=-12,
               pickup_passed=True, scheduled_pickup=PICKUP, car_no=None)
    await fire(hass, "heartbeat", person_name="Alex", status="approaching", eta_minutes=3)
    assert [(c.data["title"], c.data["message"]) for c in notify] == [
        ("Alex: taxi", "Taxi 42 for Alex (07:50) arrives in 6 min"),
        ("Alex: taxi", "Alex: 12 min late, ?, unknown ?"),
    ]


async def test_heartbeat_status_in_words(hass, setup_blueprint):
    notify, _ = await setup_blueprint(notify_on=["heartbeat"])
    await fire(hass, "heartbeat", person_name="Alex", status="onboard", eta_minutes=8,
               leg="to_destination", scheduled_pickup=PICKUP)
    assert notify[0].data["message"] == "Alex's taxi: on board, 8 min."


async def test_account_messages_use_their_placeholders(hass, setup_blueprint):
    notify, _ = await setup_blueprint(title="Taxi tracking")
    await fire(hass, "token_expiring", expires_at="2030-01-20T00:00:00+00:00", days_left=5)
    await fire(hass, "connection_degraded", failed_for_s=600, trip_active=False)
    assert [(c.data["title"], c.data["message"]) for c in notify] == [
        ("Taxi tracking", "Taxi tracking: the login token expires in 5 days."),
        ("Taxi tracking", "Taxi tracking has had no connection for 10 min."),
    ]
