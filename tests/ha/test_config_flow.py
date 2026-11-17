# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import aiohttp
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.petra_tracker.const import (
    CONF_API_KEY,
    CONF_PERSONS,
    CONF_TOKEN,
    CONF_USER_ID,
    DOMAIN,
    OPT_ACTIVE_INTERVAL_S,
    OPT_TELEMETRY,
)

from .. import synthetic as syn
from .conftest import TOKEN, make_entry, mock_api


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_creates_entry_and_discovers_children(hass, aioclient_mock, frozen):
    other = syn.order(book_ref=700009, person_id=502, first_name="SAM")
    mock_api(aioclient_mock, syn.order_list(syn.order(), other))
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "se", "token": f"JWT {TOKEN}"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Petra trips (Alex, Sam)"
    data = result["data"]
    assert data[CONF_TOKEN] == TOKEN  # "JWT " prefix stripped
    assert data[CONF_USER_ID] == syn.USER_ID
    assert data[CONF_API_KEY] == "test-key"  # fetched from operator config, not hardcoded
    assert data[CONF_PERSONS] == {str(syn.PERSON_ID): "Alex", "502": "Sam"}


async def test_user_flow_rejects_garbage_token(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "se", "token": "hello"}
    )
    assert result["errors"] == {"token": "invalid_token"}


async def test_user_flow_expired_token(hass, aioclient_mock, freezer):
    freezer.move_to(syn.DAY.replace(year=2031))
    mock_api(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "se", "token": TOKEN}
    )
    assert result["errors"] == {"token": "token_expired"}


async def test_user_flow_server_rejects_token(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock, status=401)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "se", "token": TOKEN}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock, exc=aiohttp.ClientConnectionError())
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "se", "token": TOKEN}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_account_aborts(hass, aioclient_mock, frozen):
    make_entry().add_to_hass(hass)
    mock_api(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "se", "token": TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth_flow(hass, aioclient_mock, frozen):
    entry = make_entry()
    entry.add_to_hass(hass)
    mock_api(aioclient_mock)
    result = await entry.start_reauth_flow(hass)
    other_user = syn.make_token(user_id=1, iat=syn.DAY)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": other_user})
    assert result["errors"] == {"token": "wrong_user"}
    new = syn.make_token(iat=syn.DAY)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": new})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()  # reauth reloads the entry in the background
    assert entry.data[CONF_TOKEN] == new


async def test_options_flow(hass, aioclient_mock, frozen):
    entry = make_entry()
    entry.add_to_hass(hass)
    mock_api(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    user_input = {**entry.options, OPT_ACTIVE_INTERVAL_S: 20.0, OPT_TELEMETRY: True}
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_ACTIVE_INTERVAL_S] == 20 and isinstance(entry.options[OPT_ACTIVE_INTERVAL_S], int)
    assert entry.options[OPT_TELEMETRY] is True
