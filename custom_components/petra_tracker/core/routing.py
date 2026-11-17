# SPDX-License-Identifier: GPL-3.0-or-later
"""Road-routing engines for ETA: OSRM, OpenRouteService, Waze.

The default ETA is straight-line based (see eta.py) and needs no external
service. Choosing an engine here sends the car's position and the trip's
pickup/destination coordinates to that service; any failure falls back to the
straight-line estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import aiohttp

from .eta import ARRIVED_RADIUS_M, STRAIGHT_LINE, eta_target, haversine_m
from .models import LatLon, RoutedLeg, Trip

OSRM = "osrm"
OPENROUTESERVICE = "openrouteservice"
WAZE = "waze"
ENGINES = (STRAIGHT_LINE, OSRM, OPENROUTESERVICE, WAZE)
WAZE_REGIONS = ("EU", "US", "IL", "AU")

OSRM_DEFAULT_URL = "https://router.project-osrm.org"
ORS_DEFAULT_URL = "https://api.openrouteservice.org"
TIMEOUT = aiohttp.ClientTimeout(total=15)

# Re-use a route while the car has barely moved and the result is fresh.
CACHE_MAX_AGE = timedelta(minutes=2)
CACHE_MAX_MOVE_M = 150.0


class RoutingError(Exception):
    """The engine could not produce a route (network, quota, bad answer...)."""


@dataclass(frozen=True)
class Route:
    duration_s: float
    distance_m: float


class RoutingProvider(Protocol):
    name: str

    async def route(self, origin: LatLon, dest: LatLon) -> Route: ...


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict[str, str]) -> Any:
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                raise RoutingError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
            return await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise RoutingError(f"request failed: {err!r}") from err


class OsrmProvider:
    """OSRM (project-osrm.org demo server by default; self-hosting recommended)."""

    name = OSRM

    def __init__(self, session: aiohttp.ClientSession, base_url: str = OSRM_DEFAULT_URL) -> None:
        self._session = session
        self._base = base_url.rstrip("/")

    async def route(self, origin: LatLon, dest: LatLon) -> Route:
        # OSRM wants lon,lat order.
        url = (
            f"{self._base}/route/v1/driving/"
            f"{origin.lon:.6f},{origin.lat:.6f};{dest.lon:.6f},{dest.lat:.6f}"
        )
        data = await _get_json(
            self._session, url, {"overview": "false", "alternatives": "false", "steps": "false"}
        )
        try:
            if data.get("code") != "Ok":
                raise RoutingError(f"OSRM: {data.get('code')} {data.get('message', '')}")
            best = data["routes"][0]
            return Route(duration_s=float(best["duration"]), distance_m=float(best["distance"]))
        except (KeyError, IndexError, TypeError, AttributeError) as err:
            raise RoutingError(f"OSRM: unexpected answer {str(data)[:200]}") from err


class OrsProvider:
    """OpenRouteService (free API key at openrouteservice.org, or self-hosted)."""

    name = OPENROUTESERVICE

    def __init__(
        self, session: aiohttp.ClientSession, api_key: str, base_url: str = ORS_DEFAULT_URL
    ) -> None:
        self._session = session
        self._key = api_key
        self._base = base_url.rstrip("/")

    async def route(self, origin: LatLon, dest: LatLon) -> Route:
        params = {
            "start": f"{origin.lon:.6f},{origin.lat:.6f}",
            "end": f"{dest.lon:.6f},{dest.lat:.6f}",
        }
        if self._key:
            params["api_key"] = self._key
        data = await _get_json(self._session, f"{self._base}/v2/directions/driving-car", params)
        try:
            summary = data["features"][0]["properties"]["summary"]
            return Route(duration_s=float(summary["duration"]), distance_m=float(summary["distance"]))
        except (KeyError, IndexError, TypeError) as err:
            raise RoutingError(f"OpenRouteService: unexpected answer {str(data)[:200]}") from err


class WazeProvider:
    """Waze live-traffic routing via pywaze (the library Home Assistant's own
    Waze Travel Time integration uses). Unofficial service; may break."""

    name = WAZE

    def __init__(self, region: str = "EU", calculator_factory: Any = None) -> None:
        self._region = region
        self._factory = calculator_factory

    def _calculator(self) -> Any:
        if self._factory is not None:
            return self._factory(self._region)
        from pywaze.route_calculator import WazeRouteCalculator

        return WazeRouteCalculator(region=self._region, timeout=15)

    async def route(self, origin: LatLon, dest: LatLon) -> Route:
        try:
            routes = await self._calculator().calc_routes(
                f"{origin.lat:.6f},{origin.lon:.6f}",
                f"{dest.lat:.6f},{dest.lon:.6f}",
                real_time=True,
                alternatives=1,
            )
            best = routes[0]
            # pywaze reports minutes and kilometres.
            return Route(duration_s=float(best.duration) * 60, distance_m=float(best.distance) * 1000)
        except RoutingError:
            raise
        except Exception as err:  # pywaze raises its own errors plus network ones
            raise RoutingError(f"Waze: {err!r}") from err


def make_provider(
    engine: str,
    session: aiohttp.ClientSession,
    *,
    url: str = "",
    api_key: str = "",
    waze_region: str = "EU",
) -> RoutingProvider | None:
    """None means: straight-line estimate only."""
    if engine == OSRM:
        return OsrmProvider(session, url or OSRM_DEFAULT_URL)
    if engine == OPENROUTESERVICE:
        return OrsProvider(session, api_key, url or ORS_DEFAULT_URL)
    if engine == WAZE:
        return WazeProvider(waze_region)
    return None


@dataclass
class _Cached:
    origin: LatLon
    at: datetime
    route: Route


class RouteCache:
    """Avoids asking the engine again while the car has barely moved."""

    def __init__(self) -> None:
        self._entries: dict[tuple[int, str], _Cached] = {}

    def get(self, key: tuple[int, str], origin: LatLon, now: datetime) -> Route | None:
        c = self._entries.get(key)
        if c is None or now - c.at > CACHE_MAX_AGE:
            return None
        if haversine_m(c.origin, origin) > CACHE_MAX_MOVE_M:
            return None
        return c.route

    def put(self, key: tuple[int, str], origin: LatLon, now: datetime, route: Route) -> None:
        self._entries[key] = _Cached(origin, now, route)

    def prune(self, keep_refs: set[int]) -> None:
        for key in [k for k in self._entries if k[0] not in keep_refs]:
            del self._entries[key]


async def attach_route(
    trip: Trip,
    now: datetime,
    provider: RoutingProvider,
    cache: RouteCache,
    *,
    track_onboard: bool,
) -> str | None:
    """Route from the car's last fix to the trip's next stop and store it on the
    trip (used by estimate_eta). Returns an error message, or None."""
    trip.route = None
    target = eta_target(trip, track_onboard)
    fix = trip.last_fix
    if target is None or fix is None:
        return None
    leg, dest = target
    origin = LatLon(fix.lat, fix.lon)
    if haversine_m(origin, dest) <= ARRIVED_RADIUS_M:
        return None  # arrived; no route needed
    key = (trip.book_ref, leg.value)
    route = cache.get(key, origin, now)
    if route is None:
        try:
            route = await provider.route(origin, dest)
        except RoutingError as err:
            return str(err)
        cache.put(key, origin, now, route)
    trip.route = RoutedLeg(
        leg=leg.value, duration_s=route.duration_s, distance_m=route.distance_m, engine=provider.name
    )
    return None
