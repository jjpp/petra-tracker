# SPDX-License-Identifier: GPL-3.0-or-later
"""Routing engine option in Home Assistant (engines mocked; nothing leaves the test)."""

from __future__ import annotations

import re
from datetime import timedelta

import aiohttp
from homeassistant.data_entry_flow import FlowResultType

from custom_components.petra_tracker.const import (
    OPT_ROUTING,
    OPT_ROUTING_API_KEY,
    OPT_ROUTING_URL,
)
from custom_components.petra_tracker.diagnostics import async_get_config_entry_diagnostics

from .conftest import make_entry, mock_api
from .test_integration import ASSIGNED, ETA, arriving, refresh, setup

OSRM_URL = "https://osrm.example.invalid"
OSRM_ROUTE = re.compile(r"^https://osrm\.example\.invalid/route/v1/driving/")


def mock_osrm(aioclient_mock, duration_s=540.0, distance_m=3500.0, exc=None):
    if exc is not None:
        aioclient_mock.get(OSRM_ROUTE, exc=exc)
    else:
        aioclient_mock.get(OSRM_ROUTE, json={"code": "Ok", "routes": [{"duration": duration_s, "distance": distance_m}]})


async def test_eta_from_routing_engine(hass, aioclient_mock, frozen):
    frozen.move_to(ASSIGNED + timedelta(minutes=1))
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=1)))
    mock_osrm(aioclient_mock, duration_s=540.0, distance_m=3500.0)
    await setup(hass, **{OPT_ROUTING: "osrm", OPT_ROUTING_URL: OSRM_URL})
    eta = hass.states.get(ETA)
    assert eta.state == "9"
    assert eta.attributes["routing"] == "osrm"
    assert eta.attributes["distance_m"] == 3500
    route_calls = [c for c in aioclient_mock.mock_calls if "osrm.example.invalid" in str(c[1])]
    assert len(route_calls) == 1
    assert "/route/v1/driving/" in str(route_calls[0][1])


async def test_routing_failure_falls_back_and_recovers(hass, aioclient_mock, frozen):
    frozen.move_to(ASSIGNED + timedelta(minutes=1))
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=1)))
    mock_osrm(aioclient_mock, exc=aiohttp.ClientConnectionError("routing down"))
    entry = await setup(hass, **{OPT_ROUTING: "osrm", OPT_ROUTING_URL: OSRM_URL})
    eta = hass.states.get(ETA)
    assert eta.attributes["routing"] == "straight_line" and int(eta.state) >= 1
    assert hass.states.get("binary_sensor.petra_account_api_connection").state == "on"  # polling fine
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["routing_engine"] == "osrm" and "routing down" in diag["routing_error"]

    frozen.tick(timedelta(minutes=3))  # past the cache age
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=4)))
    mock_osrm(aioclient_mock, duration_s=240.0)
    await refresh(hass, entry)
    assert hass.states.get(ETA).attributes["routing"] == "osrm"
    assert (await async_get_config_entry_diagnostics(hass, entry))["routing_error"] is None


async def test_default_is_straight_line_and_contacts_no_routing_service(hass, aioclient_mock, frozen):
    frozen.move_to(ASSIGNED + timedelta(minutes=1))
    mock_api(aioclient_mock, *arriving(ASSIGNED + timedelta(minutes=1)))
    await setup(hass)
    assert hass.states.get(ETA).attributes["routing"] == "straight_line"
    hosts = {str(c[1].host) for c in aioclient_mock.mock_calls}
    assert hosts <= {"api.example.invalid", "store.frogne.com"}


async def test_routing_api_key_is_redacted_in_diagnostics(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    entry = await setup(hass, **{OPT_ROUTING: "openrouteservice", OPT_ROUTING_API_KEY: "very-secret"})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "very-secret" not in str(diag)


async def test_options_require_ors_key_unless_own_server(hass, aioclient_mock, frozen):
    entry = make_entry()
    entry.add_to_hass(hass)
    mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    base = dict(entry.options)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, OPT_ROUTING: "openrouteservice", OPT_ROUTING_API_KEY: ""}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {OPT_ROUTING_API_KEY: "api_key_required"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, OPT_ROUTING: "openrouteservice", OPT_ROUTING_API_KEY: " key "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_ROUTING_API_KEY] == "key"
    await hass.async_block_till_done()

    # A cleared text field is omitted by the frontend; it must stay cleared.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    submit = {k: v for k, v in {**entry.options, OPT_ROUTING: "osrm"}.items()
              if k not in (OPT_ROUTING_API_KEY, OPT_ROUTING_URL)}
    result = await hass.config_entries.options.async_configure(result["flow_id"], submit)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_ROUTING_API_KEY] == "" and entry.options[OPT_ROUTING] == "osrm"
    await hass.async_block_till_done()
