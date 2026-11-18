# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic Petra API responses for tests.

Everything here is invented: ids, names, dates and coordinates (near Null
Island, lat/lon ~ 0). The *shape* mirrors real responses so the schema and
parsing code are exercised realistically. Do not paste real captures here.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

DAY = datetime(2030, 1, 15, tzinfo=UTC)
PICKUP_TIME = DAY.replace(hour=7, minute=0)
DROPOFF_TIME = DAY.replace(hour=7, minute=15)

USER_ID = 9001
PERSON_ID = 501
PERSON_NAME = "ALEX"
BOOK_REF = 700001
CAR_NO = 42

# Pickup and destination ~2.2 km apart near (0, 0).
PICKUP = (0.0100, 0.0100)  # (lat, lon)
DESTINATION = (0.0100, 0.0300)


def scaled(lat: float, lon: float) -> dict[str, int]:
    return {"x": round(lon * 1e7), "y": round(lat * 1e7)}


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def address(kind: str, lat: float, lon: float) -> dict[str, Any]:
    return {
        "streetID": 1, "houseNo": 1, "houseLetter": "", "floor": "", "door": "",
        "pOIId": 0, "comment": "", "district": 0, "pOIName": "", "streetName": "TESTVÄGEN",
        "city": "TESTSTAD", "zipCode": "00000", "countryCode": "", "fullAddress": "TESTVÄGEN 1",
        "type": kind, "poiname": "", "coordinates": scaled(lat, lon),
    }


def order(
    *,
    combined: str = "booked",
    state: str | None = None,
    book_ref: int = BOOK_REF,
    person_id: int = PERSON_ID,
    first_name: str = PERSON_NAME,
    pickup_time: datetime = PICKUP_TIME,
    dropoff_time: datetime = DROPOFF_TIME,
    car_no: int | None = None,
    assigned: datetime | None = None,
    started: datetime | None = None,
    ended: datetime | None = None,
    events: list[tuple[int, datetime]] | None = None,
    coordinated_position: int = 1,
) -> dict[str, Any]:
    status: dict[str, Any] = {"state": state or combined, "provider": None}
    if car_no is not None:
        status.update(
            {
                "carNo": car_no, "driverNo": 1234, "operator": ".test",
                "timeStamp": iso(assigned), "price": 0, "tripAssigned": iso(assigned),
                "tripStarted": iso(started), "tripEnded": iso(ended),
            }
        )
    return {
        "bookRefNo": book_ref, "bookSeqNo": 1, "companyNo": 1,
        "createdDateTime": iso(DAY - timedelta(days=20)), "pickUpDateTime": iso(pickup_time),
        "operator": ".test", "scheduleID": 1, "firstName": first_name, "lastName": "TESTSSON",
        "phone": "000000", "email": "", "customerRefNo": "", "projectID": "T1", "fixedPrice": 0,
        "comment": "synthetic", "fixedCar": 0, "coordinable": True, "urgent": False,
        "manual": False, "account": 1, "fixedDriver": 0, "productID": 1, "agreementID": 1,
        "dropOffTimeDateTime": iso(dropoff_time), "personID": person_id,
        "combinedStatus": combined, "coordinatedBookRefnr": book_ref + 1,
        "coordinatedPosition": coordinated_position,
        "addresses": [address("pickup", *PICKUP), address("destination", *DESTINATION)],
        "attributes": [
            {"attributeID": 1, "amount": 1, "abbreviation": "SK", "description": "SKOLTUR"},
            {"attributeID": 2, "amount": 0, "abbreviation": "EN", "description": "ENSAMÅKNING"},
        ], "status": status,
        "persons": [{"tripRefID": 1, "personIndex": 1, "type": t, "timeStamp": iso(ts)}
                    for t, ts in (events or [])] or None,
        "permissions": [], "metaData": {"isEditable": False, "isReopenable": False}, "Car": [],
    }


def order_list(*orders: dict[str, Any]) -> dict[str, Any]:
    return {"count": len(orders), "items": list(orders)}


def detail(base: dict[str, Any], gps: list[tuple[datetime, float, float]]) -> dict[str, Any]:
    d = json.loads(json.dumps(base))
    d.pop("persons", None)
    d.update({"editTime": None, "editOperator": None, "commentOperator": None,
              "externalCodesString": None})
    d["status"]["gpsPositions"] = [{"timeStamp": iso(t), "x": lon, "y": lat} for t, lat, lon in gps]
    return d


def approach_track(start: datetime, minutes: int, from_km: float = 2.0) -> list[tuple[datetime, float, float]]:
    """Car driving north->south straight onto the pickup point, one fix per 30 s."""
    deg_per_km = 1 / 111.2
    steps = minutes * 2
    lat0 = PICKUP[0] + from_km * deg_per_km
    out = []
    for i in range(steps + 1):
        frac = i / steps
        out.append((start + timedelta(seconds=30 * i), lat0 + (PICKUP[0] - lat0) * frac, PICKUP[1]))
    return out


def make_token(user_id: int = USER_ID, iat: datetime = DAY, days: int = 30) -> str:
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    header = b64({"alg": "RS256", "typ": "JWT"})
    payload = b64({"id": user_id, "iat": int(iat.timestamp()),
                   "exp": int((iat + timedelta(days=days)).timestamp())})
    return f"{header}.{payload}.c2lnbmF0dXJl"
