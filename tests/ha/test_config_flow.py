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
    CONF_USERNAME,
    DOMAIN,
    OPT_ACTIVE_INTERVAL_S,
    OPT_TELEMETRY,
)

from .. import synthetic as syn
from .conftest import LOGIN_URL, TOKEN, VERIFY_URL, make_entry, mock_api, mock_login

USER = "parent@example.invalid"
PASSWORD = "not-a-real-password"


async def _start(hass: HomeAssistant, method: str = "login"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "se", "auth_method": method}
    )


def _calls(aioclient_mock, url):
    return [c for c in aioclient_mock.mock_calls if str(c[1]) == url]


# -- username/password + one-time code----------------------------------------------

async def test_login_with_verification_code_creates_entry(hass, aioclient_mock, frozen):
    other = syn.order(book_ref=700009, person_id=502, first_name="SAM")
    mock_api(aioclient_mock, syn.order_list(syn.order(), other))
    mock_login(aioclient_mock)
    result = await _start(hass)
    assert result["step_id"] == "login"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": f" {USER} ", "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "code"
    [login_call] = _calls(aioclient_mock, LOGIN_URL)
    assert login_call[2] == {"username": USER, "password": PASSWORD, "language": "en-GB"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123 456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    [verify_call] = _calls(aioclient_mock, VERIFY_URL)
    assert verify_call[2] == {"userId": syn.USER_ID, "otp": 123456, "language": "EN"}
    data = result["data"]
    assert result["title"] == "Petra trips (Alex, Sam)"
    assert data[CONF_TOKEN] == TOKEN and data[CONF_USER_ID] == syn.USER_ID
    assert data[CONF_USERNAME] == USER and data[CONF_API_KEY] == "test-key"
    assert data[CONF_PERSONS] == {str(syn.PERSON_ID): "Alex", "502": "Sam"}
    assert PASSWORD not in str(data) and "password" not in data  # never stored


async def test_wrong_password(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    mock_login(aioclient_mock, login_status=401,
               login_body={"success": False, "message": "Wrong username or password"})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": USER, "password": "wrong"}
    )
    assert result["step_id"] == "login" and result["errors"] == {"base": "invalid_credentials"}


async def test_wrong_code_then_right_code(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    mock_login(aioclient_mock, verify_status=400, verify_body={"success": False, "message": "Invalid OTP"})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USER, "password": PASSWORD})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["step_id"] == "code" and result["errors"] == {"code": "invalid_code"}
    mock_api(aioclient_mock)
    mock_login(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_code_must_be_digits(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    mock_login(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USER, "password": PASSWORD})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "12ab"})
    assert result["errors"] == {"code": "code_format"}
    assert _calls(aioclient_mock, VERIFY_URL) == []


async def test_resend_code(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    mock_login(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USER, "password": PASSWORD})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "", "resend": True})
    assert result["step_id"] == "code" and result["errors"] == {"base": "code_resent"}
    assert len(_calls(aioclient_mock, LOGIN_URL)) == 2  # the login (and so the code) was sent again
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_account_without_two_factor(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    mock_login(aioclient_mock, login_body={"success": True, "data": {"userId": syn.USER_ID, "token": TOKEN}})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USER, "password": PASSWORD})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _calls(aioclient_mock, VERIFY_URL) == []


async def test_login_cannot_connect(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    aioclient_mock.post(LOGIN_URL, exc=aiohttp.ClientConnectionError())
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USER, "password": PASSWORD})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_operator_config_unreachable(hass, aioclient_mock, frozen):
    aioclient_mock.get("https://store.frogne.com/srv/ftp/petra-configs/petra_cab_prod_config_se.json",
                       exc=aiohttp.ClientConnectionError())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"country": "se", "auth_method": "login"})
    assert result["step_id"] == "user" and result["errors"] == {"base": "cannot_connect"}


# -- pasted token (advanced) ----------------------------------------------------------

async def test_token_flow_creates_entry(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock)
    result = await _start(hass, "token")
    assert result["step_id"] == "token"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": f"JWT {TOKEN}"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == TOKEN and CONF_USERNAME not in result["data"]


async def test_token_errors(hass, aioclient_mock, freezer):
    freezer.move_to(syn.DAY)
    mock_api(aioclient_mock)
    result = await _start(hass, "token")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "hello"})
    assert result["errors"] == {"token": "invalid_token"}
    freezer.move_to(syn.DAY.replace(year=2031))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": TOKEN})
    assert result["errors"] == {"token": "token_expired"}


async def test_server_rejects_token(hass, aioclient_mock, frozen):
    mock_api(aioclient_mock, status=401)
    result = await _start(hass, "token")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": TOKEN})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_account_aborts(hass, aioclient_mock, frozen):
    make_entry().add_to_hass(hass)
    mock_api(aioclient_mock)
    result = await _start(hass, "token")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": TOKEN})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


# -- re-authentication ---------------------------------------------------------------------

async def test_reauth_by_login_prefills_username(hass, aioclient_mock, frozen):
    entry = make_entry(data={CONF_USERNAME: USER})
    entry.add_to_hass(hass)
    new = syn.make_token(iat=syn.DAY)
    mock_api(aioclient_mock)
    mock_login(aioclient_mock, token=new)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == ["login", "token"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
    username_key = next(k for k in result["data_schema"].schema if k == "username")
    assert username_key.default() == USER
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USER, "password": PASSWORD})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_TOKEN] == new and PASSWORD not in str(entry.data)


async def test_reauth_by_token_checks_account(hass, aioclient_mock, frozen):
    entry = make_entry()
    entry.add_to_hass(hass)
    mock_api(aioclient_mock)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "token"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": syn.make_token(user_id=1, iat=syn.DAY)}
    )
    assert result["errors"] == {"base": "wrong_user"}
    new = syn.make_token(iat=syn.DAY)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": new})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()  # reauth reloads the entry in the background
    assert entry.data[CONF_TOKEN] == new


# -- options ----------------------------------------------------------------------------------

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
    await hass.async_block_till_done()


async def test_reconfigure_by_login(hass, aioclient_mock, frozen):
    entry = make_entry()  # e.g. set up with a pasted token
    entry.add_to_hass(hass)
    new = syn.make_token(iat=syn.DAY)
    mock_api(aioclient_mock)
    mock_login(aioclient_mock, token=new)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == ["login", "token"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USER, "password": PASSWORD})
    assert result["step_id"] == "code"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_TOKEN] == new and entry.data[CONF_USERNAME] == USER
    assert PASSWORD not in str(entry.data)


async def test_reconfigure_rejects_other_account(hass, aioclient_mock, frozen):
    entry = make_entry()
    entry.add_to_hass(hass)
    mock_api(aioclient_mock)
    mock_login(aioclient_mock, token=syn.make_token(user_id=1, iat=syn.DAY))
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USER, "password": PASSWORD})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["errors"] == {"base": "wrong_user"}
    assert entry.data[CONF_TOKEN] == TOKEN  # unchanged
