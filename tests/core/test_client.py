# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.petra_tracker.core.client import (
    PetraApiKeyError,
    PetraAuthError,
    PetraClient,
    PetraEndpointError,
    PetraRateLimited,
    PetraTransientError,
    fetch_operator_config,
    local_day_bounds,
)

from .. import synthetic as syn


class FakeResponse:
    def __init__(self, status: int, body, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)
        self.headers = headers or {}

    async def text(self):
        return self._body

    async def json(self, content_type=None):
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Records requests; answers from a queue of responses or exceptions."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


START = datetime(2030, 1, 14, 23, 0, tzinfo=UTC)
END = START + timedelta(days=1)


async def test_list_orders_sends_expected_request():
    session = FakeSession(FakeResponse(200, syn.order_list(syn.order()),
                                       {"X-RateLimit-Remaining": "998", "X-RateLimit-Limit": "1000"}))
    client = PetraClient(session, "https://api.example.invalid/api", "client-key", "JWT tok.en.x")
    res = await client.list_orders(syn.USER_ID, START, END)
    req = session.requests[0]
    assert req["url"] == f"https://api.example.invalid/api/users/{syn.USER_ID}/persons/orders"
    assert req["headers"]["apiKey"] == "client-key"
    assert req["headers"]["Authorization"] == "JWT tok.en.x"
    assert req["params"]["fromDate"] == "2030-01-14T23:00:00.000Z"
    assert req["params"]["toDate"] == "2030-01-15T22:59:59.999Z"
    assert req["params"]["sortOder"] == "ASC"
    assert res.data["count"] == 1
    assert client.last_rate_limit.remaining == 998


async def test_list_orders_follows_pages():
    orders = [syn.order(book_ref=i) for i in range(60)]
    page1 = {"count": 60, "items": orders[:50]}
    page2 = {"count": 60, "items": orders[50:]}
    session = FakeSession(FakeResponse(200, page1), FakeResponse(200, page2))
    res = await PetraClient(session, "https://x.invalid/", "k", "t").list_orders(1, START, END)
    assert len(res.data["items"]) == 60
    assert [r["params"]["index"] for r in session.requests] == ["1", "2"]


@pytest.mark.parametrize(
    ("status", "body", "headers", "exc"),
    [
        (401, "Unauthorized", {}, PetraAuthError),
        (403, '{"message":"Invalid API Key."}', {}, PetraApiKeyError),
        (403, '{"message":"No API Key present."}', {}, PetraApiKeyError),
        (403, '{"message":"endpoint : /orders/1 not found"}', {}, PetraEndpointError),
        (403, '{"message":"Forbidden"}', {}, PetraAuthError),
        (404, "nope", {}, PetraEndpointError),
        (500, "boom", {}, PetraTransientError),
        (200, "<html>not json", {}, PetraTransientError),
    ],
)
async def test_error_mapping(status, body, headers, exc):
    session = FakeSession(FakeResponse(status, body, headers))
    with pytest.raises(exc):
        await PetraClient(session, "https://x.invalid/", "k", "t").order_detail(1)


async def test_rate_limited_carries_retry_after():
    session = FakeSession(FakeResponse(429, "slow down", {"Retry-After": "120"}))
    with pytest.raises(PetraRateLimited) as info:
        await PetraClient(session, "https://x.invalid/", "k", "t").order_detail(1)
    assert info.value.retry_after == timedelta(seconds=120)


@pytest.mark.parametrize("err", [aiohttp.ClientConnectionError("down"), TimeoutError()])
async def test_network_errors_are_transient(err):
    with pytest.raises(PetraTransientError):
        await PetraClient(FakeSession(err), "https://x.invalid/", "k", "t").order_detail(1)


async def test_operator_config():
    body = {"configs": {"globalApiUrl": "https://api.example.invalid/api",
                        "globalApiKey": "abc", "appMinVersion": "0"}}
    session = FakeSession(FakeResponse(200, body))
    cfg = await fetch_operator_config(session, "se")
    assert session.requests[0]["url"].endswith("petra_cab_prod_config_se.json")
    assert (cfg.base_url, cfg.api_key) == ("https://api.example.invalid/api/", "abc")


async def test_operator_config_bad_shape():
    with pytest.raises(PetraEndpointError):
        await fetch_operator_config(FakeSession(FakeResponse(200, {"configs": {}})), "no")


def test_local_day_bounds_across_dst():
    tz = ZoneInfo("Europe/Stockholm")
    # Day DST starts (2030-03-31): local day is 23 h long.
    start, end = local_day_bounds(datetime(2030, 3, 31, 12, 0, tzinfo=UTC), tz)
    assert (start.hour, end.hour, end.day) == (0, 0, 1)
    # (same-tzinfo subtraction ignores offsets in Python, so compare in UTC)
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=23)
