# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns successive API snapshots into per-person state and change events.

Pure logic: no I/O, no Home Assistant. Feed it parsed trips (with detail/GPS
merged in) and the current time; it returns a snapshot and the events to emit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .eta import (
    END_NEAR_DESTINATION_M,
    Eta,
    EtaParams,
    TripEnding,
    estimate_eta,
    trip_ending,
    waypoint_distance_m,
)
from .models import ACTIVE_PHASES, TERMINAL_PHASES, Phase, Trip

EVENT_STATE_CHANGED = "state_changed"
EVENT_ETA_CHANGED = "eta_changed"
EVENT_HEARTBEAT = "heartbeat"
EVENT_CAR_NOT_ASSIGNED = "car_not_assigned"

STATE_FORMAT_VERSION = 1
DEFAULT_RESTORE_MAX_AGE = timedelta(minutes=30)


@dataclass
class TrackerSettings:
    eta_granularity_min: int = 1
    heartbeat_interval: timedelta | None = None  # periodic heartbeat; None = off
    # Heartbeat only when nothing else was emitted for the trip this long; None = off.
    quiet_heartbeat_interval: timedelta | None = None
    # Warn when a trip is still only booked this long before pickup (None = off).
    late_warning_lead: timedelta | None = timedelta(minutes=1)
    track_onboard: bool = True
    # A trip ended within this distance of the destination counts as arrived.
    end_near_destination_m: float = END_NEAR_DESTINATION_M
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
    ending: TripEnding | None = None  # for a finished trip: where/how it ended


@dataclass
class TrackerEvent:
    type: str
    data: dict[str, Any]


@dataclass
class _Memory:
    phase_by_trip: dict[int, Phase] = field(default_factory=dict)
    announced_eta: dict[int, int] = field(default_factory=dict)
    last_heartbeat: dict[int, datetime] = field(default_factory=dict)
    late_warned: set[int] = field(default_factory=set)
    last_event_at: dict[int, datetime] = field(default_factory=dict)  # any event for the trip

    def prune(self, keep: set[int]) -> None:
        """Forget trips that are no longer in the API window (e.g. yesterday's)."""
        for d in (self.phase_by_trip, self.announced_eta, self.last_heartbeat, self.last_event_at):
            for ref in [r for r in d if r not in keep]:
                del d[ref]
        self.late_warned &= keep


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
        self._endings: dict[int, TripEnding] = {}

    def final_detail_candidates(self, trips: list[Trip]) -> list[Trip]:
        """Trips that have just finished: fetch their detail once more, so the
        last GPS fix tells where the trip really ended."""
        return [
            t
            for t in trips
            if t.phase is Phase.FINISHED
            and t.book_ref not in self._endings
            and self._mem.phase_by_trip.get(t.book_ref) in ACTIVE_PHASES
        ]

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
            for t in ptrips:
                if t.phase is Phase.FINISHED and t.book_ref not in self._endings:
                    ending = trip_ending(t, s.eta_params, s.end_near_destination_m)
                    if ending is not None:
                        self._endings[t.book_ref] = ending
            trip = select_trip(ptrips, now, s)
            eta = estimate_eta(trip, now, s.eta_params, track_onboard=s.track_onboard) if trip else None
            persons[pid] = PersonState(
                person_id=pid,
                name=name,
                trip=trip,
                phase=trip.phase if trip else None,
                eta=eta,
                next_pickup=next_pickup(ptrips, now),
                ending=self._endings.get(trip.book_ref) if trip and trip.phase is Phase.FINISHED else None,
            )
            person_events: list[TrackerEvent] = []
            for t in ptrips:
                person_events.extend(self._phase_events(t, persons[pid] if t is trip else None, name, now))
                person_events.extend(self._late_events(t, name, now))
            if trip is not None:
                person_events.extend(self._eta_events(trip, eta, name, now))
            for ev in person_events:
                self._mem.last_event_at[ev.data["book_ref"]] = now
            if trip is not None:
                person_events.extend(self._quiet_events(trip, eta, name, now))
            events.extend(person_events)

        refs = {t.book_ref for t in trips}
        self._mem.prune(refs)
        self._endings = {r: e for r, e in self._endings.items() if r in refs}
        self.persons = persons
        if not self._warm:
            # First snapshot without restored memory (fresh start, or memory too
            # old): record what we see, announce nothing.
            self._warm = True
            return persons, []
        return persons, events

    def mark_cold(self) -> None:
        """Make the next update silent again (e.g. resuming after a long pause):
        what changed meanwhile is recorded, not announced."""
        self._warm = False

    # -- persistence (so restarts neither repeat nor invent notifications) -----
    def export_state(self, now: datetime) -> dict[str, Any]:
        m = self._mem
        return {
            "version": STATE_FORMAT_VERSION,
            "saved_at": now.isoformat(),
            "phase_by_trip": {str(k): v.value for k, v in m.phase_by_trip.items()},
            "announced_eta": {str(k): v for k, v in m.announced_eta.items()},
            "late_warned": sorted(m.late_warned),
        }

    def restore_state(
        self, data: Any, now: datetime, max_age: timedelta = DEFAULT_RESTORE_MAX_AGE
    ) -> bool:
        """Load memory saved by export_state. Returns False (and keeps the silent
        first snapshot) when the data is missing, malformed or too old."""
        try:
            if not isinstance(data, dict) or data.get("version") != STATE_FORMAT_VERSION:
                return False
            saved_at = datetime.fromisoformat(data["saved_at"])
            if not timedelta(0) <= now - saved_at <= max_age:
                return False
            phases = {int(k): Phase(v) for k, v in data["phase_by_trip"].items()}
            etas = {int(k): int(v) for k, v in data["announced_eta"].items()}
            warned = {int(r) for r in data["late_warned"]}
        except (KeyError, TypeError, ValueError):
            return False
        self._mem = _Memory(phase_by_trip=phases, announced_eta=etas, late_warned=warned)
        self._warm = True
        return True

    # -- events --------------------------------------------------------------
    def _base(self, trip: Trip, name: str | None) -> dict[str, Any]:
        return {
            "person_id": trip.person_id,
            "person_name": name,
            "book_ref": trip.book_ref,
            "car_no": trip.car_no,
            "scheduled_pickup": trip.scheduled_pickup.isoformat() if trip.scheduled_pickup else None,
        }

    def _phase_events(
        self, trip: Trip, state: PersonState | None, name: str | None, now: datetime
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
        distance = waypoint_distance_m(trip)
        data: dict[str, Any] = {
            **self._base(trip, name),
            "old_status": old.value if old else None,
            "new_status": trip.phase.value,
            "raw_status": trip.combined_status,
            "eta_minutes": eta.minutes if eta else None,
            # Car's last known distance to the stop of this phase: "arrived" may be
            # pressed before the car is there.
            "distance_m": round(distance) if distance is not None else None,
        }
        if trip.phase is Phase.FINISHED:
            ending = self._endings.get(trip.book_ref)
            data["ended_at_destination"] = ending.at_destination if ending else None
            data["end_distance_m"] = round(ending.distance_m) if ending else None
            arrival = ending.estimated_arrival if ending else None
            data["estimated_arrival"] = arrival.isoformat() if arrival else None
            if arrival is not None:
                data["eta_minutes"] = max(0, math.ceil((arrival - now).total_seconds() / 60))
        return [TrackerEvent(EVENT_STATE_CHANGED, data)]

    def _late_events(self, trip: Trip, name: str | None, now: datetime) -> list[TrackerEvent]:
        """Once per trip: still only booked shortly before (or after) pickup time."""
        s = self.settings
        pickup = trip.scheduled_pickup
        if (
            s.late_warning_lead is None
            or trip.phase is not Phase.SCHEDULED
            or pickup is None
            or trip.book_ref in self._mem.late_warned
            or now < pickup - s.late_warning_lead
            or now - pickup > s.stale_booking_after
        ):
            return []
        self._mem.late_warned.add(trip.book_ref)
        return [
            TrackerEvent(
                EVENT_CAR_NOT_ASSIGNED,
                {
                    **self._base(trip, name),
                    "minutes_to_pickup": round((pickup - now).total_seconds() / 60),
                    "pickup_passed": now >= pickup,
                    "raw_status": trip.combined_status,
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
                            "reason": "periodic",
                        },
                    )
                )
        return out

    def _quiet_events(
        self, trip: Trip, eta: Eta | None, name: str | None, now: datetime
    ) -> list[TrackerEvent]:
        """Heartbeat when nothing else was emitted for the trip for a while (e.g. the
        car is stuck and the ETA has not changed)."""
        s = self.settings
        ref = trip.book_ref
        if not s.quiet_heartbeat_interval or trip.phase not in ACTIVE_PHASES:
            return []
        last = self._mem.last_event_at.get(ref)
        if last is None:
            self._mem.last_event_at[ref] = now  # start counting from now
            return []
        quiet_for = now - last
        if quiet_for < s.quiet_heartbeat_interval:
            return []
        self._mem.last_event_at[ref] = now
        return [
            TrackerEvent(
                EVENT_HEARTBEAT,
                {
                    **self._base(trip, name),
                    "status": trip.phase.value,
                    "eta_minutes": eta.minutes if eta else None,
                    "leg": eta.leg.value if eta else None,
                    "reason": "quiet",
                    "quiet_for_min": int(quiet_for.total_seconds() // 60),
                },
            )
        ]


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
