# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import aiohttp
import pytest

from custom_components.petra_tracker.core.eta import EtaParams, EtaSource, Leg, estimate_eta
from custom_components.petra_tracker.core.models import LatLon, parse_order
from custom_components.petra_tracker.core.routing import (
    OrsProvider,
    OsrmProvider,
    Route,
    RouteCache,
    RoutingError,
    WazeProvider,
    attach_route,
    make_provider,
)

from .. import synthetic as syn
from .test_client import FakeResponse, FakeSession

T0 = syn.PICKUP_TIME
A = LatLon(0.0200, 0.0100)
B = LatLon(0.0100, 0.0300)


# -- providers ---------------------------------------------------------------------

async def test_osrm_request_uses_lon_lat_and_parses_route():
    session = FakeSession(FakeResponse(200, {"code": "Ok", "routes": [{"duration": 412.5, "distance": 3100.0}]}))
    route = await OsrmProvider(session, "https://osrm.example.invalid/").route(A, B)
    assert route == Route(duration_s=412.5, distance_m=3100.0)
    req = session.requests[0]
    assert req["url"] == "https://osrm.example.invalid/route/v1/driving/0.010000,0.020000;0.030000,0.010000"
    assert req["params"]["overview"] == "false"


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(200, {"code": "NoRoute", "message": "Impossible route"}),
        FakeResponse(200, {"code": "Ok", "routes": []}),
        FakeResponse(429, "Too Many Requests"),
        FakeResponse(200, "<html>"),
        aiohttp.ClientConnectionError(),
    ],
)
async def test_osrm_errors(response):
    with pytest.raises(RoutingError):
        await OsrmProvider(FakeSession(response)).route(A, B)


async def test_ors_request_and_parse():
    body = {"features": [{"properties": {"summary": {"duration": 300.0, "distance": 2500.0}}}]}
    session = FakeSession(FakeResponse(200, body))
    route = await OrsProvider(session, "secret-key").route(A, B)
    assert route == Route(300.0, 2500.0)
    req = session.requests[0]
    assert req["url"] == "https://api.openrouteservice.org/v2/directions/driving-car"
    assert req["params"] == {"start": "0.010000,0.020000", "end": "0.030000,0.010000", "api_key": "secret-key"}


async def test_ors_errors():
    with pytest.raises(RoutingError):
        await OrsProvider(FakeSession(FakeResponse(403, "Access to this API has been disallowed")), "k").route(A, B)
    with pytest.raises(RoutingError):
        await OrsProvider(FakeSession(FakeResponse(200, {"features": []})), "k").route(A, B)


async def test_waze_converts_units_and_wraps_errors():
    calls = []

    class FakeCalculator:
        def __init__(self, region):
            self.region = region

        async def calc_routes(self, start, end, **kw):
            calls.append((self.region, start, end, kw))
            return [SimpleNamespace(duration=7.5, distance=3.2)]  # minutes, km

    route = await WazeProvider("EU", calculator_factory=FakeCalculator).route(A, B)
    assert route == Route(duration_s=450.0, distance_m=3200.0)
    assert calls[0][:3] == ("EU", "0.020000,0.010000", "0.010000,0.030000")  # lat,lon
    assert calls[0][3]["real_time"] is True

    class Broken(FakeCalculator):
        async def calc_routes(self, *a, **kw):
            raise RuntimeError("waze says no")

    with pytest.raises(RoutingError):
        await WazeProvider("EU", calculator_factory=Broken).route(A, B)


def test_make_provider():
    s = FakeSession()
    assert make_provider("straight_line", s) is None
    assert isinstance(make_provider("osrm", s), OsrmProvider)
    assert isinstance(make_provider("openrouteservice", s, api_key="k"), OrsProvider)
    assert isinstance(make_provider("waze", s, waze_region="US"), WazeProvider)


# -- cache ---------------------------------------------------------------------------

def test_cache_reuses_until_car_moves_or_result_ages():
    cache = RouteCache()
    key = (1, "to_pickup")
    cache.put(key, A, T0, Route(100, 1000))
    assert cache.get(key, LatLon(A.lat + 0.0005, A.lon), T0 + timedelta(seconds=30)) == Route(100, 1000)  # ~55 m
    assert cache.get(key, LatLon(A.lat + 0.005, A.lon), T0) is None  # ~550 m away
    assert cache.get(key, A, T0 + timedelta(minutes=3)) is None  # too old
    assert cache.get((1, "to_destination"), A, T0) is None
    cache.prune({2})
    assert cache.get(key, A, T0) is None


# -- attaching routes to trips and using them for the ETA ---------------------------

class CountingProvider:
    name = "test"

    def __init__(self, route: Route | None = None, fail=False):
        self.calls = 0
        self._route, self._fail = route or Route(600, 4000), fail

    async def route(self, origin, dest):
        self.calls += 1
        if self._fail:
            raise RoutingError("down")
        return self._route


def approaching_trip(fix_minutes_before_pickup=8):
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=T0 - timedelta(minutes=9))
    trip = parse_order(o)
    trip.apply_detail(syn.detail(o, syn.approach_track(T0 - timedelta(minutes=9), 10)[:2]))
    return trip


async def test_routed_eta_replaces_straight_line_estimate():
    trip = approaching_trip()
    provider = CountingProvider(Route(duration_s=600, distance_m=4000))
    assert await attach_route(trip, T0, provider, RouteCache(), track_onboard=True) is None
    eta = estimate_eta(trip, T0, EtaParams())
    assert (eta.minutes, eta.distance_m, eta.routing, eta.source, eta.leg) == (
        10, 4000, "test", EtaSource.GPS, Leg.TO_PICKUP
    )


async def test_routing_failure_falls_back_to_straight_line():
    trip = approaching_trip()
    error = await attach_route(trip, T0, CountingProvider(fail=True), RouteCache(), track_onboard=True)
    assert error and "down" in error
    eta = estimate_eta(trip, T0, EtaParams())
    assert eta.routing == "straight_line" and eta.source is EtaSource.GPS and eta.minutes >= 1


async def test_cache_avoids_repeated_requests():
    trip = approaching_trip()
    provider, cache = CountingProvider(), RouteCache()
    for seconds in (0, 20, 40):
        await attach_route(trip, T0 + timedelta(seconds=seconds), provider, cache, track_onboard=True)
    assert provider.calls == 1


async def test_no_routing_when_not_driving_to_a_stop():
    provider = CountingProvider()
    booked = parse_order(syn.order())
    assert await attach_route(booked, T0, provider, RouteCache(), track_onboard=True) is None
    o = syn.order(combined="personIn", state="customer", car_no=1, assigned=T0, started=T0)
    onboard = parse_order(o)
    onboard.apply_detail(syn.detail(o, [(T0, *syn.PICKUP)]))
    await attach_route(onboard, T0, provider, RouteCache(), track_onboard=False)
    assert provider.calls == 0 and onboard.route is None
    await attach_route(onboard, T0, provider, RouteCache(), track_onboard=True)
    assert provider.calls == 1 and onboard.route.leg == "to_destination"


async def test_route_for_other_leg_is_ignored():
    trip = approaching_trip()
    await attach_route(trip, T0, CountingProvider(), RouteCache(), track_onboard=True)
    trip.route = type(trip.route)(leg="to_destination", duration_s=60, distance_m=1, engine="test")
    assert estimate_eta(trip, T0, EtaParams()).routing == "straight_line"
