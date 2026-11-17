# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from pathlib import Path

from custom_components.petra_tracker.core import schema
from custom_components.petra_tracker.core.models import (
    Phase,
    decode_address_coordinates,
    decode_gps_positions,
    parse_order_list,
    phase_of,
)
from custom_components.petra_tracker.core.schema import SchemaValidator, Severity

from .. import synthetic as syn

ROOT = Path(__file__).resolve().parents[2]


def test_contract_copy_matches_canonical():
    canonical = json.loads((ROOT / "contract" / "petra-contract.v1.json").read_text())
    assert schema.load_contract() == canonical


def test_valid_list_and_detail_have_no_problems():
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=syn.PICKUP_TIME)
    assert SchemaValidator().order_list(syn.order_list(o)) == []
    d = syn.detail(o, syn.approach_track(syn.PICKUP_TIME, 2))
    assert SchemaValidator().order_detail(d) == []


def test_missing_required_field_is_breaking():
    o = syn.order()
    del o["pickUpDateTime"]
    issues = SchemaValidator().order_list(syn.order_list(o))
    assert schema.worst(issues) is Severity.BREAKING
    assert any(i.kind == "missing" and i.path.endswith("pickUpDateTime") for i in issues)


def test_wrong_type_is_breaking():
    o = syn.order()
    o["bookRefNo"] = "700001"
    issues = SchemaValidator().order_list(syn.order_list(o))
    assert [i.kind for i in issues if i.severity is Severity.BREAKING] == ["type"]


def test_unknown_status_is_warning():
    o = syn.order(combined="teleporting")
    issues = SchemaValidator().order_list(syn.order_list(o))
    assert schema.worst(issues) is Severity.WARNING
    assert issues[0].kind == "enum"


def test_unknown_field_is_info_and_collapsed():
    a, b = syn.order(), syn.order(book_ref=700002)
    a["newThing"] = b["newThing"] = 1
    issues = schema.collapse(SchemaValidator().order_list(syn.order_list(a, b)))
    assert len(issues) == 1
    assert issues[0].severity is Severity.INFO
    assert issues[0].path == "$.items[].newThing"


def test_detail_without_gps_warns():
    o = syn.order(combined="arriving", car_no=syn.CAR_NO, assigned=syn.PICKUP_TIME)
    issues = SchemaValidator().order_detail(o)
    assert any(i.path.endswith("gpsPositions") for i in issues)


def test_coordinate_encodings():
    # Addresses: integers scaled by 1e7. GPS: plain decimal degrees.
    ll = decode_address_coordinates({"x": 100000, "y": 200000})
    assert (round(ll.lat, 6), round(ll.lon, 6)) == (0.02, 0.01)
    assert decode_address_coordinates({"x": 0, "y": 0}) is None
    fixes = decode_gps_positions(
        [{"timeStamp": "2030-01-15T07:00:30.000Z", "x": 0.01, "y": 0.02},
         {"timeStamp": "2030-01-15T07:00:00.000Z", "x": 0.0, "y": 0.0}]
    )
    assert [f.lat for f in fixes] == [0.0, 0.02]  # sorted by time


def test_phase_mapping_prefers_combined_status():
    assert phase_of("arrived", "customer") is Phase.AT_PICKUP
    assert phase_of("personIn", "customer") is Phase.ONBOARD
    assert phase_of(None, "customer") is Phase.ONBOARD
    assert phase_of("weird", "weirder") is Phase.UNKNOWN


def test_parse_order_list():
    o = syn.order(combined="personIn", car_no=syn.CAR_NO, assigned=syn.PICKUP_TIME,
                  events=[(3, syn.PICKUP_TIME), (0, syn.PICKUP_TIME)])
    [trip] = parse_order_list(syn.order_list(o))
    assert trip.phase is Phase.ONBOARD
    assert trip.person_name == "Alex"
    assert trip.car_no == syn.CAR_NO
    assert round(trip.pickup.lat, 4) == syn.PICKUP[0]
    assert round(trip.destination.lon, 4) == syn.DESTINATION[1]
    assert [e.type for e in trip.events] == [3, 0]


def test_detail_without_gps_is_normal_before_a_car_is_assigned():
    assert SchemaValidator().order_detail(syn.order()) == []  # booked
    done = syn.order(combined="concluded", car_no=syn.CAR_NO, assigned=syn.PICKUP_TIME)
    assert SchemaValidator().order_detail(done) == []
