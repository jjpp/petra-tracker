# SPDX-License-Identifier: GPL-3.0-or-later
"""Username/password + one-time code login (synthetic responses shaped like the real ones)."""

from __future__ import annotations

import aiohttp
import pytest

from custom_components.petra_tracker.core.client import (
    PetraApiKeyError,
    PetraEndpointError,
    PetraLoginError,
    PetraOtpError,
    PetraRateLimited,
    PetraTransientError,
    login,
    otp_digits,
    verify_2fa,
)

from .. import synthetic as syn
from .test_client import FakeResponse, FakeSession

BASE = "https://api.example.invalid/api"
TOKEN = syn.make_token()


def login_ok(user_id=syn.USER_ID):
    return {"httpStatusCode": 200, "success": True, "data": {"userId": user_id}, "message": "2FA Success"}


def verify_ok(token=TOKEN):
    return {"httpStatusCode": 200, "success": True, "data": {
        "username": "parent@example.invalid", "email": "parent@example.invalid", "fullname": "Test Parent",
        "firstName": "Test", "lastName": "Parent", "userType": 8, "token": token,
        "userRoles": [{"person": True, "account": False, "agreement": False}], "companyNo": 1,
    }, "error": "", "message": "Success"}


async def test_login_then_verify_sends_what_the_web_client_sends():
    session = FakeSession(FakeResponse(201, login_ok()), FakeResponse(201, verify_ok()))
    start = await login(session, BASE, "client-key", " parent@example.invalid ", "pw-123")
    assert (start.user_id, start.token) == (syn.USER_ID, None)
    token = await verify_2fa(session, BASE, "client-key", start.user_id, "012 345")
    assert token == TOKEN
    first, second = session.requests
    assert first["url"] == f"{BASE}/authentication/login"
    assert first["json"] == {"username": "parent@example.invalid", "password": "pw-123", "language": "en-GB"}
    assert first["headers"]["apiKey"] == "client-key" and "Authorization" not in first["headers"]
    assert second["url"] == f"{BASE}/authentication/verify2factor"
    assert second["json"] == {"userId": syn.USER_ID, "otp": 12345, "language": "EN"}  # a number


async def test_account_without_two_factor_gets_token_directly():
    body = {"success": True, "data": {"userId": syn.USER_ID, "token": f"JWT {TOKEN}"}}
    start = await login(FakeSession(FakeResponse(201, body)), BASE, "k", "u", "p")
    assert start.token == TOKEN


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(401, {"success": False, "message": "Wrong username or password"}),
        FakeResponse(400, {"statusCode": 400, "message": "Bad Request"}),
        FakeResponse(201, {"success": False, "message": "User blocked"}),
        FakeResponse(200, "not json"),
    ],
)
async def test_rejected_login(response):
    with pytest.raises(PetraLoginError):
        await login(FakeSession(response), BASE, "k", "u", "secret-password")


async def test_errors_never_contain_password_or_code():
    for exc in (aiohttp.ClientConnectionError("boom"), TimeoutError()):
        with pytest.raises(PetraTransientError) as info:
            await login(FakeSession(exc), BASE, "k", "u", "secret-password")
        assert "secret-password" not in str(info.value)
    with pytest.raises(PetraOtpError) as info:
        await verify_2fa(FakeSession(FakeResponse(401, "")), BASE, "k", 1, "987654")
    assert "987654" not in str(info.value)


async def test_other_failures():
    with pytest.raises(PetraApiKeyError):
        await login(FakeSession(FakeResponse(403, '{"message":"Invalid API Key."}')), BASE, "k", "u", "p")
    with pytest.raises(PetraRateLimited):
        await login(FakeSession(FakeResponse(429, "slow", {"Retry-After": "60"})), BASE, "k", "u", "p")
    with pytest.raises(PetraTransientError):
        await login(FakeSession(FakeResponse(502, "bad gateway")), BASE, "k", "u", "p")
    with pytest.raises(PetraEndpointError):
        await login(FakeSession(FakeResponse(201, {"success": True, "data": {}})), BASE, "k", "u", "p")


async def test_wrong_code():
    bad = FakeResponse(400, {"success": False, "message": "Invalid OTP"})
    with pytest.raises(PetraOtpError, match="Invalid OTP"):
        await verify_2fa(FakeSession(bad), BASE, "k", 1, "111111")
    with pytest.raises(PetraEndpointError):
        await verify_2fa(FakeSession(FakeResponse(201, {"success": True, "data": {}})), BASE, "k", 1, "111111")


def test_code_format():
    assert otp_digits(" 123 456 ") == "123456"
    assert otp_digits("123-456") == "123456"
    for bad in ("", "12", "12ab56", "123456789"):  # hygiene: allow (too-long code, not data)
        assert otp_digits(bad) is None
