# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from datetime import datetime, timedelta

from custom_components.petra_tracker.core.eta import EtaParams, EtaSource, Leg, estimate_eta
from custom_components.petra_tracker.core.models import Phase, parse_order, parse_order_list
from custom_components.petra_tracker.core.tracker import (
    EVENT_CAR_CHANGED,
    EVENT_CAR_NOT_ASSIGNED,
    EVENT_ETA_CHANGED,
    EVENT_HEARTBEAT,
    EVENT_STATE_CHANGED,
    PollSettings,
    TrackerSettings,
    TripTracker,
    plan_next_poll,
    trips_needing_detail,
)

from .. import synthetic as syn

T0 = syn.PICKUP_TIME
ASSIGNED = T0 - timedelta(minutes=9)
ARRIVED = T0 + timedelta(minutes=1)
STARTED = T0 + timedelta(minutes=2)
ENDED = T0 + timedelta(minutes=12)


def trip_with_gps(order, gps):
    trip = parse_order(order)
    trip.apply_detail(syn.detail(order, gps))
    return trip


def test_eta_schedule_before_car_assigned():
    trip = parse_order(syn.order())
    eta = estimate_eta(trip, T0 - timedelta(minutes=30), EtaParams())
    assert (eta.minutes, eta.source, eta.leg) == (30, EtaSource.SCHEDULE, Leg.TO_PICKUP)


def test_eta_from_gps_decreases_and_hits_zero():
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    track = syn.approach_track(ASSIGNED, 8)
    params = EtaParams(assumed_speed_kmh=25, road_factor=1.4)
    far = trip_with_gps(o, track[:1])
    near = trip_with_gps(o, track[:-2])
    there = trip_with_gps(o, track)
    e_far = estimate_eta(far, ASSIGNED, params)
    e_near = estimate_eta(near, ASSIGNED + timedelta(minutes=7), params)
    assert e_far.source is EtaSource.GPS and 6 <= e_far.minutes <= 8  # 2 km * 1.4 @ 25 km/h
    assert e_near.minutes < e_far.minutes
    assert estimate_eta(there, ASSIGNED + timedelta(minutes=8), params).source is EtaSource.ARRIVED


def test_eta_onboard_targets_destination_and_can_be_disabled():
    o = syn.order(combined="personIn", state="customer", car_no=syn.CAR_NO,
                  assigned=ASSIGNED, started=STARTED)
    trip = trip_with_gps(o, [(STARTED, *syn.PICKUP)])
    eta = estimate_eta(trip, STARTED, EtaParams())
    assert eta.leg is Leg.TO_DESTINATION and eta.minutes > 0
    assert estimate_eta(trip, STARTED, EtaParams(), track_onboard=False) is None


def test_pooled_trip_is_marked_approximate():
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED, coordinated_position=2)
    trip = trip_with_gps(o, syn.approach_track(ASSIGNED, 4)[:1])
    assert estimate_eta(trip, ASSIGNED, EtaParams()).approximate


def run_timeline(settings: TrackerSettings):
    """Replays a whole trip like the real capture: booked -> arriving -> arrived
    -> personIn -> concluded, polling every 30 s."""
    tracker = TripTracker(settings)
    track = syn.approach_track(ASSIGNED, 10)  # arrives at T0 + 1 min
    events = []
    now = ASSIGNED - timedelta(minutes=5)
    while now <= ENDED + timedelta(minutes=5):
        if now < ASSIGNED:
            o = syn.order()
            gps = None
        elif now < ARRIVED:
            o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
            gps = [g for g in track if g[0] <= now]
        elif now < STARTED:
            o = syn.order(combined="arrived", state="customer", car_no=syn.CAR_NO,
                          assigned=ASSIGNED, events=[(3, ARRIVED)])
            gps = track
        elif now < ENDED:
            o = syn.order(combined="personIn", state="customer", car_no=syn.CAR_NO,
                          assigned=ASSIGNED, started=STARTED, events=[(3, ARRIVED), (0, STARTED)])
            gps = track + [(now, syn.PICKUP[0], syn.PICKUP[1] + 0.02 * (now - STARTED) / (ENDED - STARTED))]
        else:
            o = syn.order(combined="concluded", car_no=syn.CAR_NO, assigned=ASSIGNED,
                          started=STARTED, ended=ENDED,
                          events=[(3, ARRIVED), (0, STARTED), (1, ENDED)])
            gps = None
        trips = parse_order_list(syn.order_list(o))
        for t in trips_needing_detail(trips, settings.track_onboard):
            t.apply_detail(syn.detail(o, gps or []))
        persons, evs = tracker.update(trips, now)
        events.extend((now, e) for e in evs)
        now += timedelta(seconds=30)
    return tracker, events


def test_full_trip_state_events():
    _, events = run_timeline(TrackerSettings())
    transitions = [(e.data["old_status"], e.data["new_status"])
                   for _, e in events if e.type == EVENT_STATE_CHANGED]
    assert transitions == [
        ("scheduled", "approaching"),
        ("approaching", "at_pickup"),
        ("at_pickup", "onboard"),
        ("onboard", "finished"),
    ]
    first = next(e for _, e in events if e.type == EVENT_STATE_CHANGED)
    assert first.data["person_name"] == "Alex" and first.data["car_no"] == syn.CAR_NO


def test_eta_events_respect_granularity():
    _, fine = run_timeline(TrackerSettings(eta_granularity_min=1))
    _, coarse = run_timeline(TrackerSettings(eta_granularity_min=3))
    n_fine = sum(1 for _, e in fine if e.type == EVENT_ETA_CHANGED)
    n_coarse = sum(1 for _, e in coarse if e.type == EVENT_ETA_CHANGED)
    assert n_fine > n_coarse > 0
    for _, e in coarse:
        if e.type == EVENT_ETA_CHANGED:
            assert abs(e.data["new_eta"] - e.data["old_eta"]) >= 3


def test_heartbeat_off_by_default_and_periodic_when_enabled():
    _, off = run_timeline(TrackerSettings())
    assert not any(e.type == EVENT_HEARTBEAT for _, e in off)
    _, on = run_timeline(TrackerSettings(heartbeat_interval=timedelta(minutes=2)))
    times = [t for t, e in on if e.type == EVENT_HEARTBEAT]
    assert len(times) >= 5
    assert all(b - a >= timedelta(minutes=2) for a, b in zip(times, times[1:], strict=False))


def test_no_events_on_first_snapshot_mid_trip():
    tracker = TripTracker()
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    _, evs = tracker.update(parse_order_list(syn.order_list(o)), ASSIGNED)
    assert evs == []  # e.g. HA restarted during a trip: do not re-announce


def test_trip_first_seen_in_motion_is_announced_after_warmup():
    tracker = TripTracker()
    tracker.update([], ASSIGNED - timedelta(minutes=1))  # warm-up, nothing today yet
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    _, evs = tracker.update(parse_order_list(syn.order_list(o)), ASSIGNED)
    assert [(e.data["old_status"], e.data["new_status"]) for e in evs if e.type == EVENT_STATE_CHANGED] == [
        (None, "approaching")
    ]


def test_selection_prefers_active_then_recent_then_next():
    morning = syn.order(combined="concluded", book_ref=1, car_no=1, assigned=T0, ended=ENDED)
    afternoon = syn.order(book_ref=2, pickup_time=T0 + timedelta(hours=7),
                          dropoff_time=T0 + timedelta(hours=7, minutes=15))
    trips = parse_order_list(syn.order_list(morning, afternoon))
    tracker = TripTracker()
    persons, _ = tracker.update(trips, ENDED + timedelta(minutes=5))
    assert persons[syn.PERSON_ID].trip.book_ref == 1  # just finished: still shown
    persons, _ = tracker.update(trips, ENDED + timedelta(minutes=30))
    p = persons[syn.PERSON_ID]
    assert p.trip.book_ref == 2 and p.phase is Phase.SCHEDULED
    assert p.next_pickup == T0 + timedelta(hours=7)


def test_known_persons_without_trips_are_idle():
    persons, _ = TripTracker().update([], T0, known_persons={77: "Sam"})
    assert persons[77].phase is None and persons[77].name == "Sam"


def test_poll_plan():
    p = PollSettings()
    booked = parse_order_list(syn.order_list(syn.order()))
    assert plan_next_poll(booked, T0 - timedelta(hours=3), p) == p.idle_interval
    assert plan_next_poll(booked, T0 - timedelta(minutes=50), p) == timedelta(minutes=5)
    assert plan_next_poll(booked, T0 - timedelta(minutes=30), p) == p.active_interval
    assert plan_next_poll(booked, T0 + timedelta(minutes=20), p) == p.active_interval  # car late
    active = parse_order_list(syn.order_list(syn.order(combined="arriving", car_no=1, assigned=T0)))
    assert plan_next_poll(active, T0 - timedelta(hours=5), p) == p.active_interval
    assert plan_next_poll([], T0, p) == p.idle_interval


# -- "car not assigned" warning ---------------------------------------------------

def booked(now_trips=None):
    return parse_order_list(syn.order_list(syn.order()))


def warm_tracker(settings: TrackerSettings | None = None) -> TripTracker:
    tracker = TripTracker(settings)
    tracker.update([], T0 - timedelta(hours=1))  # first (silent) snapshot
    return tracker


def late_events(evs):
    return [e for e in evs if e.type == EVENT_CAR_NOT_ASSIGNED]


def test_car_not_assigned_fires_once_at_lead_time():
    tracker = warm_tracker()
    assert late_events(tracker.update(booked(), T0 - timedelta(minutes=2))[1]) == []
    [ev] = late_events(tracker.update(booked(), T0 - timedelta(seconds=50))[1])
    assert ev.data["minutes_to_pickup"] == 1 and ev.data["pickup_passed"] is False
    assert ev.data["person_name"] == "Alex" and ev.data["raw_status"] == "booked"
    for minutes in (0, 1, 5):
        assert late_events(tracker.update(booked(), T0 + timedelta(minutes=minutes))[1]) == []


def test_car_not_assigned_when_trip_first_seen_after_pickup_time():
    tracker = warm_tracker()
    [ev] = late_events(tracker.update(booked(), T0 + timedelta(minutes=5))[1])
    assert ev.data["pickup_passed"] is True and ev.data["minutes_to_pickup"] == -5


def test_car_not_assigned_lead_is_configurable_and_can_be_off():
    tracker = warm_tracker(TrackerSettings(late_warning_lead=timedelta(minutes=10)))
    assert late_events(tracker.update(booked(), T0 - timedelta(minutes=9))[1])
    tracker = warm_tracker(TrackerSettings(late_warning_lead=timedelta(0)))
    assert not late_events(tracker.update(booked(), T0 - timedelta(seconds=10))[1])
    assert late_events(tracker.update(booked(), T0)[1])
    tracker = warm_tracker(TrackerSettings(late_warning_lead=None))
    assert not late_events(tracker.update(booked(), T0 + timedelta(minutes=5))[1])


def test_no_car_not_assigned_once_car_is_assigned():
    tracker = warm_tracker()
    tracker.update(booked(), T0 - timedelta(minutes=5))
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=T0 - timedelta(minutes=3))
    _, evs = tracker.update(parse_order_list(syn.order_list(o)), T0 - timedelta(seconds=30))
    assert not late_events(evs)


def test_cold_start_after_pickup_time_does_not_warn():
    tracker = TripTracker()
    assert tracker.update(booked(), T0 + timedelta(minutes=2))[1] == []  # e.g. HA (re)start
    assert late_events(tracker.update(booked(), T0 + timedelta(minutes=3))[1]) == []


# -- restart persistence -----------------------------------------------------------

def arriving_trips():
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    return parse_order_list(syn.order_list(o))


def at_pickup_trips():
    o = syn.order(combined="arrived", state="customer",
                  car_no=syn.CAR_NO, assigned=ASSIGNED, events=[(3, ARRIVED)])
    return parse_order_list(syn.order_list(o))


def test_restored_memory_prevents_repeats_after_restart():
    before = warm_tracker()
    before.update(booked(), T0 - timedelta(seconds=30))  # late warning sent
    before.update(arriving_trips(), ASSIGNED + timedelta(minutes=10))  # "approaching" sent
    saved = json.loads(json.dumps(before.export_state(ASSIGNED + timedelta(minutes=10))))

    after = TripTracker()
    assert after.restore_state(saved, ASSIGNED + timedelta(minutes=11))
    assert after.update(arriving_trips(), ASSIGNED + timedelta(minutes=11))[1] == []


def test_change_during_restart_is_announced_once():
    before = warm_tracker()
    before.update(arriving_trips(), ASSIGNED + timedelta(minutes=1))
    saved = before.export_state(ASSIGNED + timedelta(minutes=1))

    after = TripTracker()
    assert after.restore_state(saved, ARRIVED + timedelta(seconds=30))
    _, evs = after.update(at_pickup_trips(), ARRIVED + timedelta(seconds=30))
    assert [(e.data["old_status"], e.data["new_status"])
            for e in evs if e.type == EVENT_STATE_CHANGED] == [("approaching", "at_pickup")]


def test_stale_or_bad_saved_state_is_ignored():
    before = warm_tracker()
    before.update(arriving_trips(), ASSIGNED)
    saved = before.export_state(ASSIGNED)
    after = TripTracker()
    assert not after.restore_state(saved, ASSIGNED + timedelta(minutes=31))
    assert after.update(at_pickup_trips(), ASSIGNED + timedelta(minutes=31))[1] == []  # silent
    for bad in (None, {}, {"version": 99}, {**saved, "saved_at": "nonsense"},
                {**saved, "phase_by_trip": {"1": "teleporting"}}):
        assert not TripTracker().restore_state(bad, ASSIGNED)


def test_memory_only_keeps_current_trips():
    tracker = warm_tracker()
    tracker.update(arriving_trips(), ASSIGNED)
    assert tracker.export_state(ASSIGNED)["phase_by_trip"] == {str(syn.BOOK_REF): "approaching"}
    tracker.update([], ASSIGNED + timedelta(days=1))  # next day: yesterday's trip gone
    assert tracker.export_state(ASSIGNED)["phase_by_trip"] == {}


def test_all_trip_events_carry_scheduled_pickup():
    _, events = run_timeline(TrackerSettings(heartbeat_interval=timedelta(minutes=2)))
    assert events
    for _, e in events:
        assert e.data["scheduled_pickup"] == syn.PICKUP_TIME.isoformat()


# -- driver-pressed statuses vs. where the car really is -------------------------

def _onboard(at, lat, lon):
    o = syn.order(combined="personIn", state="customer", car_no=syn.CAR_NO, assigned=ASSIGNED,
                  started=at, events=[(3, at), (0, at)])
    return o, [(at, lat, lon)]


def _concluded(ended):
    return syn.order(combined="concluded", car_no=syn.CAR_NO, assigned=ASSIGNED,
                     started=STARTED, ended=ended, events=[(3, ARRIVED), (0, STARTED), (1, ended)])


def _poll(tracker, order, gps, now, *, with_final_detail=True):
    trips = parse_order_list(syn.order_list(order))
    wanted = trips_needing_detail(trips, True) + (tracker.final_detail_candidates(trips) if with_final_detail else [])
    for t in wanted:
        t.apply_detail(syn.detail(order, gps))
    return tracker.update(trips, now)


def test_trip_ended_before_destination_is_reported_with_estimated_arrival():
    tracker = warm_tracker()
    o, gps = _onboard(STARTED, *syn.PICKUP)
    _poll(tracker, o, gps, STARTED)
    ended = STARTED + timedelta(minutes=2)
    done = _concluded(ended)
    # Car still at the pickup when the driver ended the trip (seen in real data).
    assert tracker.final_detail_candidates(parse_order_list(syn.order_list(done)))
    persons, evs = _poll(tracker, done, gps + [(ended, *syn.PICKUP)], ended + timedelta(seconds=20))
    [ev] = [e for e in evs if e.type == EVENT_STATE_CHANGED]
    d = ev.data
    assert d["new_status"] == "finished" and d["ended_at_destination"] is False
    assert 2000 < d["end_distance_m"] < 2400  # pickup -> destination is ~2.2 km
    arrival = datetime.fromisoformat(d["estimated_arrival"])
    assert ended + timedelta(minutes=5) < arrival < ended + timedelta(minutes=12)
    assert d["eta_minutes"] >= 5
    assert persons[syn.PERSON_ID].ending.at_destination is False
    # Fetched once only.
    assert tracker.final_detail_candidates(parse_order_list(syn.order_list(done))) == []


def test_trip_ended_at_destination_is_normal():
    tracker = warm_tracker()
    o, gps = _onboard(STARTED, *syn.DESTINATION)
    _poll(tracker, o, gps, STARTED)
    persons, evs = _poll(tracker, _concluded(ENDED), [(ENDED, syn.DESTINATION[0] + 0.002, syn.DESTINATION[1])], ENDED)
    [ev] = [e for e in evs if e.type == EVENT_STATE_CHANGED]
    assert ev.data["ended_at_destination"] is True and ev.data["estimated_arrival"] is None
    assert ev.data["end_distance_m"] < 500


def test_trip_ending_unknown_without_final_gps():
    tracker = warm_tracker()
    o, gps = _onboard(STARTED, *syn.PICKUP)
    _poll(tracker, o, gps, STARTED)
    _, evs = _poll(tracker, _concluded(ENDED), [], ENDED, with_final_detail=False)
    [ev] = [e for e in evs if e.type == EVENT_STATE_CHANGED]
    assert ev.data["ended_at_destination"] is None and ev.data["end_distance_m"] is None


def test_at_pickup_event_tells_how_far_the_car_still_is():
    tracker = warm_tracker()
    approach = syn.approach_track(ASSIGNED, 10)
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    tracker.update(parse_order_list(syn.order_list(o)), ASSIGNED)
    # Driver presses "arrived" while the car is still ~1 km away.
    a = syn.order(combined="arrived", state="customer", car_no=syn.CAR_NO, assigned=ASSIGNED,
                  events=[(3, ASSIGNED + timedelta(minutes=5))])
    half_way = [g for g in approach if g[0] <= ASSIGNED + timedelta(minutes=5)]
    _, evs = _poll(tracker, a, half_way, ASSIGNED + timedelta(minutes=5))
    [ev] = [e for e in evs if e.type == EVENT_STATE_CHANGED]
    assert ev.data["new_status"] == "at_pickup" and 900 < ev.data["distance_m"] < 1100


def test_eta_keeps_counting_when_arrived_is_pressed_early():
    a = syn.order(combined="arrived", state="customer", car_no=syn.CAR_NO, assigned=ASSIGNED,
                  events=[(3, ASSIGNED + timedelta(minutes=5))])
    track = syn.approach_track(ASSIGNED, 10)
    early = parse_order(a)
    early.apply_detail(syn.detail(a, [g for g in track if g[0] <= ASSIGNED + timedelta(minutes=5)]))
    eta = estimate_eta(early, ASSIGNED + timedelta(minutes=5), EtaParams())
    assert eta.source is EtaSource.GPS and eta.minutes >= 2 and eta.leg is Leg.TO_PICKUP
    there = parse_order(a)
    there.apply_detail(syn.detail(a, track))
    assert estimate_eta(there, ASSIGNED + timedelta(minutes=10), EtaParams()).source is EtaSource.ARRIVED
    nogps = parse_order(a)
    assert estimate_eta(nogps, ASSIGNED, EtaParams()).minutes == 0


def test_ended_at_destination_distance_is_configurable():
    seven_hundred_m_before = (syn.DESTINATION[0] + 700 / 111_200, syn.DESTINATION[1])
    for near, expected in ((1000, True), (500, False)):
        tracker = warm_tracker(TrackerSettings(end_near_destination_m=near))
        o, gps = _onboard(STARTED, *syn.PICKUP)
        _poll(tracker, o, gps, STARTED)
        _, evs = _poll(tracker, _concluded(ENDED), [(ENDED, *seven_hundred_m_before)], ENDED)
        [ev] = [e for e in evs if e.type == EVENT_STATE_CHANGED]
        assert ev.data["ended_at_destination"] is expected, near
        assert 650 < ev.data["end_distance_m"] < 750


# -- quiet heartbeat ---------------------------------------------------------------

def _drive(tracker, gps_at, minutes, step_s=30):
    """Poll an approaching trip; gps_at(now) gives the car's track up to now."""
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    out = []
    now = ASSIGNED
    while now <= ASSIGNED + timedelta(minutes=minutes):
        trips = parse_order_list(syn.order_list(o))
        for t in trips_needing_detail(trips, True):
            t.apply_detail(syn.detail(o, gps_at(now)))
        _, evs = tracker.update(trips, now)
        out += [(now, e) for e in evs]
        now += timedelta(seconds=step_s)
    return out


def test_quiet_heartbeat_when_car_stands_still():
    stuck = [(ASSIGNED, syn.PICKUP[0] + 0.02, syn.PICKUP[1])]  # ~2 km away, not moving
    tracker = warm_tracker(TrackerSettings(quiet_heartbeat_interval=timedelta(minutes=3)))
    evs = _drive(tracker, lambda now: stuck, minutes=10)
    beats = [(t, e) for t, e in evs if e.type == EVENT_HEARTBEAT]
    assert len(beats) >= 2 and all(e.data["reason"] == "quiet" for _, e in beats)
    assert all(b[0] - a[0] >= timedelta(minutes=3) for a, b in zip(beats, beats[1:], strict=False))
    assert beats[0][1].data["quiet_for_min"] >= 3


def test_no_quiet_heartbeat_while_eta_keeps_changing():
    track = syn.approach_track(ASSIGNED, 10, from_km=4.0)
    tracker = warm_tracker(TrackerSettings(quiet_heartbeat_interval=timedelta(minutes=3)))
    evs = _drive(tracker, lambda now: [g for g in track if g[0] <= now], minutes=9)
    assert any(e.type == EVENT_ETA_CHANGED for _, e in evs)
    assert not any(e.type == EVENT_HEARTBEAT for _, e in evs)


def test_periodic_and_quiet_heartbeats_are_labelled():
    stuck = [(ASSIGNED, syn.PICKUP[0] + 0.02, syn.PICKUP[1])]
    tracker = warm_tracker(TrackerSettings(heartbeat_interval=timedelta(minutes=2)))
    evs = _drive(tracker, lambda now: stuck, minutes=5)
    assert {e.data["reason"] for _, e in evs if e.type == EVENT_HEARTBEAT} == {"periodic"}


# -- location not known / car changed -----------------------------------------------

def driving(car_no: int, gps, now, tracker: TripTracker, **kw):
    o = syn.order(combined=kw.pop("combined", "arriving"), car_no=car_no, assigned=ASSIGNED, **kw)
    trips = parse_order_list(syn.order_list(o))
    for t in trips_needing_detail(trips, True):
        t.apply_detail(syn.detail(o, gps))
    return tracker.update(trips, now)


def test_eta_unknown_for_car_on_its_way_without_position():
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=ASSIGNED)
    trip = trip_with_gps(o, [])
    assert estimate_eta(trip, ASSIGNED, EtaParams()) is None  # not the schedule
    onboard = syn.order(combined="personIn", state="customer", car_no=syn.CAR_NO,
                        assigned=ASSIGNED, started=STARTED)
    assert estimate_eta(trip_with_gps(onboard, []), STARTED, EtaParams()) is None


def test_approaching_without_position_says_location_unknown():
    tracker = warm_tracker()
    persons, evs = driving(syn.CAR_NO, [], ASSIGNED, tracker)
    (st,) = [e for e in evs if e.type == EVENT_STATE_CHANGED]
    assert st.data["new_status"] == "approaching"
    assert st.data["eta_minutes"] is None and st.data["location_known"] is False
    assert persons[syn.PERSON_ID].eta is None
    # The first position gives the ETA.
    track = syn.approach_track(ASSIGNED, 8)
    _, evs = driving(syn.CAR_NO, track[:1], ASSIGNED + timedelta(seconds=30), tracker)
    (eta,) = [e for e in evs if e.type == EVENT_ETA_CHANGED]
    assert eta.data["old_eta"] is None and eta.data["new_eta"] > 0 and eta.data["location_known"]


def test_lost_position_is_announced_once_then_eta_again():
    tracker = warm_tracker()
    track = syn.approach_track(ASSIGNED, 8)
    driving(syn.CAR_NO, track[:1], ASSIGNED, tracker)
    _, evs = driving(syn.CAR_NO, [], ASSIGNED + timedelta(seconds=30), tracker)
    (lost,) = [e for e in evs if e.type == EVENT_ETA_CHANGED]
    assert lost.data["new_eta"] is None and lost.data["location_known"] is False
    assert lost.data["leg"] == "to_pickup" and lost.data["old_eta"] > 0
    _, evs = driving(syn.CAR_NO, [], ASSIGNED + timedelta(seconds=60), tracker)
    assert not [e for e in evs if e.type == EVENT_ETA_CHANGED]  # no repeat
    _, evs = driving(syn.CAR_NO, track[:2], ASSIGNED + timedelta(seconds=90), tracker)
    (back,) = [e for e in evs if e.type == EVENT_ETA_CHANGED]
    assert back.data["old_eta"] is None and back.data["new_eta"] > 0


def test_car_change_is_announced_without_a_schedule_eta():
    """Like a real afternoon trip: the car was replaced a minute after assignment,
    and the new car had no position yet."""
    tracker = warm_tracker()
    track = syn.approach_track(ASSIGNED, 8)
    driving(1, track[:1], ASSIGNED, tracker)
    persons, evs = driving(2, [], ASSIGNED + timedelta(seconds=45), tracker)
    assert [e.type for e in evs] == [EVENT_CAR_CHANGED]  # no "location unknown" on top
    d = evs[0].data
    assert (d["old_car_no"], d["car_no"], d["status"]) == (1, 2, "approaching")
    assert d["eta_minutes"] is None and d["location_known"] is False
    _, evs = driving(2, track[1:2], ASSIGNED + timedelta(seconds=60), tracker)
    (eta,) = [e for e in evs if e.type == EVENT_ETA_CHANGED]
    assert eta.data["old_eta"] is None and 1 <= eta.data["new_eta"] <= 10


def test_car_number_on_a_booking_is_not_a_car_change():
    tracker = warm_tracker()
    o = syn.order(car_no=5)  # booked, with a planned car
    tracker.update(parse_order_list(syn.order_list(o)), ASSIGNED - timedelta(minutes=5))
    _, evs = driving(7, syn.approach_track(ASSIGNED, 8)[:1], ASSIGNED, tracker)
    assert EVENT_CAR_CHANGED not in [e.type for e in evs]


def test_car_change_memory_survives_restart():
    before = warm_tracker()
    driving(1, syn.approach_track(ASSIGNED, 8)[:1], ASSIGNED, before)
    saved = json.loads(json.dumps(before.export_state(ASSIGNED)))
    after = TripTracker()
    assert after.restore_state(saved, ASSIGNED + timedelta(minutes=1))
    _, evs = driving(2, [], ASSIGNED + timedelta(minutes=1), after)
    assert [e.data["old_car_no"] for e in evs if e.type == EVENT_CAR_CHANGED] == [1]


def test_restore_accepts_unknown_eta_and_older_state_without_cars():
    before = warm_tracker()
    driving(syn.CAR_NO, [], ASSIGNED, before)  # ETA announced as unknown
    saved = json.loads(json.dumps(before.export_state(ASSIGNED)))
    assert saved["announced_eta"] == {str(syn.BOOK_REF): None}
    assert TripTracker().restore_state(saved, ASSIGNED + timedelta(minutes=1))
    del saved["car_by_trip"]  # saved by 0.9
    assert TripTracker().restore_state(saved, ASSIGNED + timedelta(minutes=1))
