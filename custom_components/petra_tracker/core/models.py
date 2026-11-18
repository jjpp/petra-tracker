# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed views of Petra API data, plus normalisation of trip status."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Phase(StrEnum):
    """Normalised trip phase. Stable vocabulary for automations, independent of
    the raw API strings (which may change)."""

    SCHEDULED = "scheduled"  # booked, no car yet
    APPROACHING = "approaching"  # car assigned and driving to pickup
    AT_PICKUP = "at_pickup"  # car has arrived at pickup
    ONBOARD = "onboard"  # passenger picked up, driving to destination
    FINISHED = "finished"  # passenger dropped off
    CANCELLED = "cancelled"  # deleted / cancelled
    UNKNOWN = "unknown"  # status we do not recognise


ACTIVE_PHASES = frozenset({Phase.APPROACHING, Phase.AT_PICKUP, Phase.ONBOARD})
TERMINAL_PHASES = frozenset({Phase.FINISHED, Phase.CANCELLED})

_COMBINED_STATUS_TO_PHASE = {
    "booked": Phase.SCHEDULED,
    "arriving": Phase.APPROACHING,
    "arrived": Phase.AT_PICKUP,
    "personIn": Phase.ONBOARD,
    "concluded": Phase.FINISHED,
    "completed": Phase.FINISHED,
    "deleted": Phase.CANCELLED,
    "cancelled": Phase.CANCELLED,
}
_STATE_TO_PHASE = {
    "booked": Phase.SCHEDULED,
    "arriving": Phase.APPROACHING,
    "arrived": Phase.AT_PICKUP,
    "customer": Phase.ONBOARD,
    "concluded": Phase.FINISHED,
    "completed": Phase.FINISHED,
    "deleted": Phase.CANCELLED,
    "cancelled": Phase.CANCELLED,
}

PERSON_EVENT_ARRIVED = 3
PERSON_EVENT_PICKED_UP = 0
PERSON_EVENT_DROPPED_OFF = 1


def phase_of(combined_status: str | None, state: str | None) -> Phase:
    if combined_status in _COMBINED_STATUS_TO_PHASE:
        return _COMBINED_STATUS_TO_PHASE[combined_status]
    if state in _STATE_TO_PHASE:
        return _STATE_TO_PHASE[state]
    return Phase.UNKNOWN


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class LatLon:
    lat: float
    lon: float


def decode_address_coordinates(coords: Any) -> LatLon | None:
    """addresses[].coordinates are integers scaled by 1e7 (x=lon, y=lat)."""
    if not isinstance(coords, dict):
        return None
    x, y = coords.get("x"), coords.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    if x == 0 and y == 0:
        return None
    return LatLon(lat=y / 1e7, lon=x / 1e7)


@dataclass(frozen=True)
class GpsFix:
    """status.gpsPositions entries are plain decimal degrees (x=lon, y=lat)."""

    time: datetime
    lat: float
    lon: float


def decode_gps_positions(raw: Any) -> list[GpsFix]:
    fixes: list[GpsFix] = []
    if not isinstance(raw, list):
        return fixes
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = parse_time(item.get("timeStamp"))
        x, y = item.get("x"), item.get("y")
        if t is None or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        fixes.append(GpsFix(time=t, lat=float(y), lon=float(x)))
    fixes.sort(key=lambda f: f.time)
    return fixes


@dataclass(frozen=True)
class RoutedLeg:
    """Road route from the car's last fix to the next stop (set by routing.py)."""

    leg: str  # eta.Leg value
    duration_s: float
    distance_m: float
    engine: str


@dataclass(frozen=True)
class PersonEvent:
    type: int
    time: datetime


@dataclass
class Trip:
    book_ref: int
    person_id: int
    person_name: str | None
    scheduled_pickup: datetime | None
    scheduled_dropoff: datetime | None
    combined_status: str | None
    state: str | None
    phase: Phase
    car_no: int | None
    trip_assigned: datetime | None
    trip_started: datetime | None
    trip_ended: datetime | None
    pickup: LatLon | None
    destination: LatLon | None
    coordinated_position: int | None
    coordinated_with_other: bool
    events: list[PersonEvent] = field(default_factory=list)
    gps: list[GpsFix] = field(default_factory=list)
    route: RoutedLeg | None = None
    status_time: datetime | None = None  # status.timeStamp: when the status last changed

    @property
    def last_fix(self) -> GpsFix | None:
        return self.gps[-1] if self.gps else None

    @property
    def last_update(self) -> datetime | None:
        """Latest news from the service about this trip: status change, GPS fix
        or pickup/drop-off event."""
        times = [t for t in (self.status_time, self.last_fix.time if self.last_fix else None) if t]
        times += [e.time for e in self.events]
        return max(times, default=None)

    def apply_detail(self, detail: dict[str, Any]) -> None:
        """Merge the order_detail response (which carries gpsPositions)."""
        status = detail.get("status") if isinstance(detail.get("status"), dict) else {}
        self.gps = decode_gps_positions(status.get("gpsPositions"))
        # The detail may be fresher than the list for these fields.
        self.trip_assigned = parse_time(status.get("tripAssigned")) or self.trip_assigned
        self.trip_started = parse_time(status.get("tripStarted")) or self.trip_started
        self.trip_ended = parse_time(status.get("tripEnded")) or self.trip_ended
        self.status_time = parse_time(status.get("timeStamp")) or self.status_time
        car = status.get("carNo")
        if isinstance(car, int) and car > 0:
            self.car_no = car


def _first_name(item: dict[str, Any]) -> str | None:
    name = item.get("firstName")
    if isinstance(name, str) and name.strip():
        return name.strip().title()
    return None


def parse_order(item: dict[str, Any]) -> Trip | None:
    """Parse one order from the order_list response. Returns None if unusable."""
    book_ref = item.get("bookRefNo")
    person_id = item.get("personID")
    if not isinstance(book_ref, int) or not isinstance(person_id, int):
        return None
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    combined = item.get("combinedStatus")
    state = status.get("state")
    pickup = destination = None
    for addr in item.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        coords = decode_address_coordinates(addr.get("coordinates"))
        if addr.get("type") == "pickup":
            pickup = coords
        elif addr.get("type") == "destination":
            destination = coords
    events = []
    for ev in item.get("persons") or []:
        if isinstance(ev, dict) and isinstance(ev.get("type"), int):
            t = parse_time(ev.get("timeStamp"))
            if t is not None:
                events.append(PersonEvent(type=ev["type"], time=t))
    events.sort(key=lambda e: e.time)
    car = status.get("carNo")
    coord_pos = item.get("coordinatedPosition")
    return Trip(
        book_ref=book_ref,
        person_id=person_id,
        person_name=_first_name(item),
        scheduled_pickup=parse_time(item.get("pickUpDateTime")),
        scheduled_dropoff=parse_time(item.get("dropOffTimeDateTime")),
        combined_status=combined if isinstance(combined, str) else None,
        state=state if isinstance(state, str) else None,
        phase=phase_of(combined, state),
        car_no=car if isinstance(car, int) and car > 0 else None,
        trip_assigned=parse_time(status.get("tripAssigned")),
        trip_started=parse_time(status.get("tripStarted")),
        trip_ended=parse_time(status.get("tripEnded")),
        pickup=pickup,
        destination=destination,
        coordinated_position=coord_pos if isinstance(coord_pos, int) else None,
        coordinated_with_other=bool(item.get("coordinatedBookRefnr")),
        events=events,
        status_time=parse_time(status.get("timeStamp")),
    )


def parse_order_list(data: dict[str, Any]) -> list[Trip]:
    trips = []
    for item in data.get("items") or []:
        if isinstance(item, dict):
            trip = parse_order(item)
            if trip is not None:
                trips.append(trip)
    return trips
