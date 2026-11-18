# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.petra_tracker.const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_COUNTRY,
    CONF_PERSONS,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
)

from .. import synthetic as syn

BASE = "https://api.example.invalid/api/"
CONFIG_URL = "https://store.frogne.com/srv/ftp/petra-configs/petra_cab_prod_config_se.json"
LIST_URL = f"{BASE}users/{syn.USER_ID}/persons/orders"
DETAIL_URL = f"{BASE}orders/{syn.BOOK_REF}"
TOKEN = syn.make_token(iat=syn.DAY - timedelta(days=1))
RATE_HEADERS = {"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "990", "x-ratelimit-reset": "0"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def frozen(freezer):
    freezer.move_to(syn.PICKUP_TIME - timedelta(minutes=30))
    return freezer


def mock_api(aioclient_mock: AiohttpClientMocker, list_body=None, detail_body=None, *, status=200, exc=None):
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        CONFIG_URL, json={"configs": {"globalApiUrl": BASE, "globalApiKey": "test-key"}}
    )
    if exc is not None:
        aioclient_mock.get(LIST_URL, exc=exc)
        aioclient_mock.get(DETAIL_URL, exc=exc)
        return
    aioclient_mock.get(
        LIST_URL,
        json=list_body if list_body is not None else syn.order_list(syn.order()),
        status=status,
        headers=RATE_HEADERS,
    )
    if detail_body is not None:
        aioclient_mock.get(DETAIL_URL, json=detail_body, headers=RATE_HEADERS)


LOGIN_URL = f"{BASE}authentication/login"
VERIFY_URL = f"{BASE}authentication/verify2factor"


def mock_login(aioclient_mock, *, login_status=201, login_body=None, verify_status=201, verify_body=None,
               token=None):
    """Register login + verify2factor answers (call after mock_api, which clears mocks)."""
    aioclient_mock.post(LOGIN_URL, status=login_status, json=login_body if login_body is not None else {
        "httpStatusCode": 200, "success": True, "data": {"userId": syn.USER_ID}, "message": "2FA Success"})
    aioclient_mock.post(VERIFY_URL, status=verify_status, json=verify_body if verify_body is not None else {
        "httpStatusCode": 200, "success": True, "message": "Success", "error": "",
        "data": {"username": "parent@example.invalid", "firstName": "Test", "lastName": "Parent",
                 "userType": 8, "token": token or TOKEN, "companyNo": 1}})


def make_entry(*, data: dict | None = None, **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Petra trips (Alex)",
        unique_id=f"{BASE}#{syn.USER_ID}",
        data={
            CONF_COUNTRY: "se",
            CONF_BASE_URL: BASE,
            CONF_API_KEY: "test-key",
            CONF_TOKEN: TOKEN,
            CONF_USER_ID: syn.USER_ID,
            CONF_PERSONS: {str(syn.PERSON_ID): "Alex"},
            **(data or {}),
        },
        options={**DEFAULT_OPTIONS, **options},
    )
