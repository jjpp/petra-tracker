# SPDX-License-Identifier: GPL-3.0-or-later
"""Async client for the Petra GlobalApi (aiohttp)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import aiohttp

from .backoff import RateLimitInfo
from .jwt import normalise_token
from .schema import SchemaValidator, Severity

OPERATOR_CONFIG_URL = (
    "https://store.frogne.com/srv/ftp/petra-configs/petra_cab_prod_config_{country}.json"
)
COUNTRIES = ("se", "no")
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
PAGE_SIZE = 50
MAX_PAGES = 10


class PetraError(Exception):
    """Base error."""


class PetraAuthError(PetraError):
    """Token missing/invalid/expired: the user must provide a new token."""


class PetraApiKeyError(PetraError):
    """The operator API key was rejected: operator config is stale or wrong."""


class PetraEndpointError(PetraError):
    """The endpoint does not exist anymore: API contract changed."""


class PetraTransientError(PetraError):
    """Network problem, timeout, 5xx: retry with backoff."""


class PetraRateLimited(PetraTransientError):
    def __init__(self, retry_after: timedelta | None) -> None:
        super().__init__(f"rate limited (retry after {retry_after})")
        self.retry_after = retry_after


@dataclass(frozen=True)
class OperatorConfig:
    country: str
    base_url: str
    api_key: str
    app_min_version: str | None = None


@dataclass
class ApiResult:
    data: Any
    status: int
    rate_limit: RateLimitInfo


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def local_day_bounds(now: datetime, tz: tzinfo) -> tuple[datetime, datetime]:
    """Start of today and start of tomorrow in the given local timezone."""
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Aware arithmetic with zoneinfo is wall-clock based, so this is the next
    # local midnight even across DST changes.
    return start, start + timedelta(days=1)


async def fetch_operator_config(session: aiohttp.ClientSession, country: str) -> OperatorConfig:
    """Fetch the public per-operator config (API base URL + client API key)."""
    if country not in COUNTRIES:
        raise ValueError(f"unsupported country {country!r}")
    url = OPERATOR_CONFIG_URL.format(country=country)
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                raise PetraError(f"operator config returned HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError) as err:
        raise PetraTransientError(f"cannot fetch operator config: {err}") from err
    issues = SchemaValidator().operator_config(data)
    if any(i.severity is Severity.BREAKING for i in issues):
        raise PetraEndpointError(f"unexpected operator config shape: {[i.as_dict() for i in issues]}")
    cfg = data["configs"]
    base = cfg["globalApiUrl"]
    return OperatorConfig(
        country=country,
        base_url=base if base.endswith("/") else base + "/",
        api_key=cfg["globalApiKey"],
        app_min_version=cfg.get("appMinVersion"),
    )


class PetraClient:
    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, api_key: str, token: str
    ) -> None:
        self._session = session
        self._base = base_url if base_url.endswith("/") else base_url + "/"
        self._api_key = api_key
        self._token = normalise_token(token)
        self.last_rate_limit = RateLimitInfo()

    def set_token(self, token: str) -> None:
        self._token = normalise_token(token)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> ApiResult:
        headers = {
            "apiKey": self._api_key,
            "Authorization": f"JWT {self._token}",
            "Accept": "application/json",
        }
        try:
            async with self._session.get(
                self._base + path, params=params, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                rate = RateLimitInfo.from_headers({k.lower(): v for k, v in resp.headers.items()})
                self.last_rate_limit = rate
                text = await resp.text()
                status = resp.status
                retry_after = resp.headers.get("Retry-After")
        except (TimeoutError, aiohttp.ClientError) as err:
            raise PetraTransientError(f"request failed: {err!r}") from err

        if status == 200:
            try:
                return ApiResult(json.loads(text), status, rate)
            except ValueError as err:
                raise PetraTransientError("response is not JSON") from err
        message = text[:300]
        if status == 429:
            delay = None
            if retry_after and retry_after.strip().isdigit():
                delay = timedelta(seconds=int(retry_after.strip()))
            raise PetraRateLimited(delay)
        if status >= 500:
            raise PetraTransientError(f"HTTP {status}")
        if status == 401:
            raise PetraAuthError(message)
        if status == 403:
            low = message.lower()
            if "api key" in low:
                raise PetraApiKeyError(message)
            if "not found" in low and "endpoint" in low:
                raise PetraEndpointError(message)
            raise PetraAuthError(message)
        if status == 404:
            raise PetraEndpointError(message)
        raise PetraError(f"HTTP {status}: {message}")

    async def list_orders(self, user_id: int, start: datetime, end: datetime) -> ApiResult:
        """All orders of all persons of the user in [start, end). Follows pagination."""
        items: list[Any] = []
        first: ApiResult | None = None
        for page in range(1, MAX_PAGES + 1):
            res = await self._get(
                f"users/{user_id}/persons/orders",
                {
                    "fromDate": _iso_z(start),
                    "toDate": _iso_z(end - timedelta(milliseconds=1)),
                    "itemsPerPage": str(PAGE_SIZE),
                    "index": str(page),
                    "sortOder": "ASC",  # sic: spelled like this by the official clients
                    "grouping": "true",
                },
            )
            first = first or res
            page_items = res.data.get("items") if isinstance(res.data, dict) else None
            if not isinstance(page_items, list):
                return res  # let the schema validator report it
            items.extend(page_items)
            count = res.data.get("count")
            if len(page_items) < PAGE_SIZE or (isinstance(count, int) and len(items) >= count):
                break
        assert first is not None
        merged = dict(first.data)
        merged["items"] = items
        return ApiResult(merged, first.status, self.last_rate_limit)

    async def order_detail(self, book_ref: int) -> ApiResult:
        return await self._get(f"orders/{book_ref}")


async def discover_persons(
    client: PetraClient, user_id: int, now: datetime, days: int = 30
) -> dict[int, str | None]:
    """Find the persons (children) on the account from orders around `now`."""
    res = await client.list_orders(user_id, now - timedelta(days=days), now + timedelta(days=days))
    persons: dict[int, str | None] = {}
    items = res.data.get("items") if isinstance(res.data, dict) else None
    for item in items or []:
        if not isinstance(item, dict) or not isinstance(item.get("personID"), int):
            continue
        name = item.get("firstName")
        name = name.strip().title() if isinstance(name, str) and name.strip() else None
        if persons.get(item["personID"]) is None:
            persons[item["personID"]] = name
    return persons
