# SPDX-License-Identifier: GPL-3.0-or-later
"""ETA estimation.

There is no ETA in the API, so we compute one. The MVP uses straight-line
distance * a road factor at an assumed average speed. Proper routing engines
(OSRM / Waze / ...) plug in later behind the same `estimate_eta` result shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .models import GpsFix, LatLon, Phase, Trip

STRAIGHT_LINE = "straight_line"

EARTH_RADIUS_M = 6_371_000.0
ARRIVED_RADIUS_M = 60.0
GPS_STALE_AFTER = timedelta(minutes=5)


class Leg(StrEnum):
    TO_PICKUP = "to_pickup"
    TO_DESTINATION = "to_destination"


class EtaSource(StrEnum):
    SCHEDULE = "schedule"  # no car position yet: minutes until scheduled pickup
    GPS = "gps"  # computed from the car's last GPS fix
    ARRIVED = "arrived"  # car is at the waypoint


@dataclass(frozen=True)
class EtaParams:
    assumed_speed_kmh: float = 25.0
    road_factor: float = 1.4


@dataclass(frozen=True)
class Eta:
    minutes: int
    leg: Leg
    source: EtaSource
    distance_m: float | None = None
    last_fix: datetime | None = None
    gps_stale: bool = False
    approximate: bool = False  # e.g. pooled trip: car may stop elsewhere first
    routing: str | None = None  # engine used for a GPS-based ETA


def haversine_m(a: LatLon | GpsFix, b: LatLon | GpsFix) -> float:
    p = math.pi / 180
    dlat = (b.lat - a.lat) * p
    dlon = (b.lon - a.lon) * p
    h = math.sin(dlat / 2) ** 2 + math.cos(a.lat * p) * math.cos(b.lat * p) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def drive_minutes(distance_m: float, params: EtaParams) -> float:
    speed_ms = max(params.assumed_speed_kmh, 1.0) / 3.6
    return distance_m * params.road_factor / speed_ms / 60.0


def eta_target(trip: Trip, track_onboard: bool = True) -> tuple[Leg, LatLon] | None:
    """The next stop the car is driving to, if it is driving to one."""
    if trip.phase is Phase.APPROACHING and trip.pickup is not None:
        return Leg.TO_PICKUP, trip.pickup
    if trip.phase is Phase.ONBOARD and track_onboard and trip.destination is not None:
        return Leg.TO_DESTINATION, trip.destination
    return None


def _driving_eta(trip: Trip, leg: Leg, dest: LatLon, fix: GpsFix, now: datetime,
                 params: EtaParams, pooled: bool) -> Eta:
    stale = now - fix.time > GPS_STALE_AFTER
    straight = haversine_m(fix, dest)
    if straight <= ARRIVED_RADIUS_M:
        return Eta(0, leg, EtaSource.ARRIVED, straight, fix.time, stale, pooled)
    route = trip.route
    if route is not None and route.leg == leg.value:
        minutes, distance, routing = route.duration_s / 60, route.distance_m, route.engine
    else:
        minutes, distance, routing = drive_minutes(straight, params), straight, STRAIGHT_LINE
    return Eta(max(round(minutes), 1), leg, EtaSource.GPS, distance, fix.time, stale, pooled, routing)


def estimate_eta(
    trip: Trip, now: datetime, params: EtaParams, *, track_onboard: bool = True
) -> Eta | None:
    """Return the ETA to the next waypoint of the trip, or None if not meaningful."""
    pooled = (trip.coordinated_position or 0) > 1
    fix = trip.last_fix
    target = eta_target(trip, track_onboard)
    if target is not None and fix is not None:
        return _driving_eta(trip, *target, fix, now, params, pooled)

    if trip.phase in (Phase.SCHEDULED, Phase.APPROACHING):
        if trip.scheduled_pickup is not None:
            minutes = math.ceil((trip.scheduled_pickup - now).total_seconds() / 60)
            return Eta(max(minutes, 0), Leg.TO_PICKUP, EtaSource.SCHEDULE, approximate=pooled)
        return None
    if trip.phase is Phase.AT_PICKUP:
        return Eta(0, Leg.TO_PICKUP, EtaSource.ARRIVED, last_fix=fix.time if fix else None)
    if trip.phase is Phase.ONBOARD and track_onboard and trip.scheduled_dropoff is not None:
        minutes = math.ceil((trip.scheduled_dropoff - now).total_seconds() / 60)
        return Eta(max(minutes, 0), Leg.TO_DESTINATION, EtaSource.SCHEDULE, approximate=pooled)
    return None
