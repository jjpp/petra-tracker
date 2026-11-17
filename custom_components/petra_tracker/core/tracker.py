# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns successive API snapshots into per-person state and change events.

Pure logic: no I/O, no Home Assistant. Feed it parsed trips (with detail/GPS
merged in) and the current time; it returns a snapshot and the events to emit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .eta import Eta, EtaParams, estimate_eta
from .models import ACTIVE_PHASES, TERMINAL_PHASES, Phase, Trip

EVENT_STATE_CHANGED = "state_changed"
EVENT_ETA_CHANGED = "eta_changed"
EVENT_HEARTBEAT = "heartbeat"


@dataclass
class TrackerSettings:
    eta_granularity_min: int = 1
    heartbeat_interval: timedelta | None = None  # None/0 = off
    track_onboard: bool = True
    show_finished_for: timedelta = timedelta(minutes=15)
    stale_booking_after: timedelta = timedelta(hours=3)
    eta_params: EtaParams = field(default_factory=EtaParams)


@dataclass
class PersonState:
    person_id: int
    name: str | None
    trip: Trip | None  # the trip currently of interest (active / just finished / next)
    phase: Phase | None  # None = idle (nothing today)
    eta: Eta | None
    next_pickup: datetime | None


@dataclass
class TrackerEvent:
    type: str
    data: dict[str, Any]


@dataclass
class _Memory:
    phase_by_trip: dict[int, Phase] = field(default_factory=dict)
    announced_eta: dict[int, int] = field(default_factory=dict)
    last_heartbeat: dict[int, datetime] = field(default_factory=dict)


def select_trip(trips: list[Trip], now: datetime, s: TrackerSettings) -> Trip | None:
    """Pick the trip most relevant right now for one person."""
    def pickup_key(t: Trip) -> datetime:
        return t.scheduled_pickup or datetime.max.replace(tzinfo=now.tzinfo)

    active = sorted((t for t in trips if t.phase in ACTIVE_PHASES), key=pickup_key)
    if active:
        return active[0]
    recently_done = [
        t
        for t in trips
        if t.phase in TERMINAL_PHASES
        and (t.trip_ended or t.scheduled_pickup)
        and now - (t.trip_ended or t.scheduled_pickup) <= s.show_finished_for
        and (t.trip_ended or t.scheduled_pickup) <= now
    ]
    if recently_done:
        return max(recently_done, key=lambda t: t.trip_ended or t.scheduled_pickup)
    upcoming = sorted(
        (
            t
            for t in trips
            if t.phase in (Phase.SCHEDULED, Phase.UNKNOWN)
            and t.scheduled_pickup is not None
            and t.scheduled_pickup >= now - s.stale_booking_after
        ),
        key=pickup_key,
    )
    return upcoming[0] if upcoming else None


def next_pickup(trips: list[Trip], now: datetime) -> datetime | None:
    times = [
        t.scheduled_pickup
        for t in trips
        if t.phase is Phase.SCHEDULED and t.scheduled_pickup and t.scheduled_pickup >= now
    ]
    return min(times, default=None)


class TripTracker:
    def __init__(self, settings: TrackerSettings | None = None) -> None:
        self.settings = settings or TrackerSettings()
        self._mem = _Memory()
        self._warm = False  # suppress events for the very first snapshot (e.g. after restart)
        self.persons: dict[int, PersonState] = {}

    def update(
        self, trips: list[Trip], now: datetime, known_persons: dict[int, str | None] | None = None
    ) -> tuple[dict[int, PersonState], list[TrackerEvent]]:
        s = self.settings
        events: list[TrackerEvent] = []
        by_person: dict[int, list[Trip]] = {pid: [] for pid in (known_persons or {})}
        for t in trips:
            by_person.setdefault(t.person_id, []).append(t)

        persons: dict[int, PersonState] = {}
        for pid, ptrips in by_person.items():
            name = next((t.person_name for t in ptrips if t.person_name), None)
            if name is None and known_persons:
                name = known_persons.get(pid)
            trip = select_trip(ptrips, now, s)
            eta = estimate_eta(trip, now, s.eta_params, track_onboard=s.track_onboard) if trip else None
            persons[pid] = PersonState(
                person_id=pid,
                name=name,
                trip=trip,
                phase=trip.phase if trip else None,
                eta=eta,
                next_pickup=next_pickup(ptrips, now),
            )
            for t in ptrips:
                events.extend(self._phase_events(t, persons[pid] if t is trip else None, name))
            if trip is not None:
                events.extend(self._eta_events(trip, eta, name, now))

        self.persons = persons
        if not self._warm:
            self._warm = True
            return persons, []
        return persons, events

    # -- events --------------------------------------------------------------
    def _base(self, trip: Trip, name: str | None) -> dict[str, Any]:
        return {
            "person_id": trip.person_id,
            "person_name": name,
            "book_ref": trip.book_ref,
            "car_no": trip.car_no,
        }

    def _phase_events(
        self, trip: Trip, state: PersonState | None, name: str | None
    ) -> list[TrackerEvent]:
        old = self._mem.phase_by_trip.get(trip.book_ref)
        self._mem.phase_by_trip[trip.book_ref] = trip.phase
        if old == trip.phase:
            return []
        if old is None and not (self._warm and trip.phase in ACTIVE_PHASES):
            # A trip seen for the first time is only announced if it is already
            # in motion (we might have missed its earlier transitions).
            return []
        eta = state.eta if state else None
        return [
            TrackerEvent(
                EVENT_STATE_CHANGED,
                {
                    **self._base(trip, name),
                    "old_status": old.value if old else None,
                    "new_status": trip.phase.value,
                    "raw_status": trip.combined_status,
                    "eta_minutes": eta.minutes if eta else None,
                },
            )
        ]

    def _eta_events(
        self, trip: Trip, eta: Eta | None, name: str | None, now: datetime
    ) -> list[TrackerEvent]:
        s = self.settings
        out: list[TrackerEvent] = []
        ref = trip.book_ref
        if eta is not None and trip.phase in ACTIVE_PHASES:
            last = self._mem.announced_eta.get(ref)
            if last is None:
                self._mem.announced_eta[ref] = eta.minutes
            elif abs(eta.minutes - last) >= max(s.eta_granularity_min, 1):
                self._mem.announced_eta[ref] = eta.minutes
                out.append(
                    TrackerEvent(
                        EVENT_ETA_CHANGED,
                        {
                            **self._base(trip, name),
                            "status": trip.phase.value,
                            "old_eta": last,
                            "new_eta": eta.minutes,
                            "leg": eta.leg.value,
                            "eta_source": eta.source.value,
                        },
                    )
                )
        if s.heartbeat_interval and trip.phase in ACTIVE_PHASES:
            last_hb = self._mem.last_heartbeat.get(ref)
            if last_hb is None:
                self._mem.last_heartbeat[ref] = now
            elif now - last_hb >= s.heartbeat_interval:
                self._mem.last_heartbeat[ref] = now
                out.append(
                    TrackerEvent(
                        EVENT_HEARTBEAT,
                        {
                            **self._base(trip, name),
                            "status": trip.phase.value,
                            "eta_minutes": eta.minutes if eta else None,
                            "leg": eta.leg.value if eta else None,
                        },
                    )
                )
        return out


# -- polling plan ---------------------------------------------------------------


@dataclass
class PollSettings:
    active_interval: timedelta = timedelta(seconds=30)
    idle_interval: timedelta = timedelta(minutes=10)
    lead_time: timedelta = timedelta(minutes=45)


def plan_next_poll(trips: list[Trip], now: datetime, p: PollSettings) -> timedelta:
    """How long to wait before the next poll, given what we know."""
    if any(t.phase in ACTIVE_PHASES for t in trips):
        return p.active_interval
    nxt: timedelta = p.idle_interval
    for t in trips:
        if t.phase is not Phase.SCHEDULED or t.scheduled_pickup is None:
            continue
        window_start = t.scheduled_pickup - p.lead_time
        # Inside the window (including a car that is late): poll fast.
        if window_start <= now <= t.scheduled_pickup + timedelta(hours=2):
            return p.active_interval
        if now < window_start:
            nxt = min(nxt, window_start - now)
    return max(nxt, p.active_interval)


def trips_needing_detail(trips: list[Trip], track_onboard: bool) -> list[Trip]:
    """Trips for which we fetch order_detail (the only source of GPS)."""
    wanted = {Phase.APPROACHING, Phase.AT_PICKUP}
    if track_onboard:
        wanted.add(Phase.ONBOARD)
    return [t for t in trips if t.phase in wanted]
