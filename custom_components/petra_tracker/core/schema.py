# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate Petra API responses against the shared contract (contract.json).

The API is undocumented, so every response is checked against the shape we
assume. Findings are classified so callers can decide what to do:

- BREAKING: a required field is missing or has the wrong type. Parsing may give
  wrong results; the Android app refuses to show data, HA raises an alert.
- WARNING: an enum value we have never seen (e.g. a new trip status). We can
  usually keep going but may mis-handle that state.
- INFO: an unknown extra field. Harmless, but worth knowing about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import ACTIVE_PHASES, parse_time, phase_of

GPS_GRACE = timedelta(minutes=3)

CONTRACT_PATH = Path(__file__).with_name("contract.json")


class Severity(IntEnum):
    INFO = 1
    WARNING = 2
    BREAKING = 3


@dataclass(frozen=True)
class SchemaIssue:
    severity: Severity
    kind: str  # missing | type | enum | unknown_field
    path: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.name.lower(),
            "kind": self.kind,
            "path": self.path,
            "detail": self.detail,
        }


@lru_cache(maxsize=1)
def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def schema_version() -> int:
    return int(load_contract()["schema_version"])


def _type_ok(value: Any, spec: str) -> bool:
    for t in spec.split("|"):
        if t == "any":
            return True
        if t == "null" and value is None:
            return True
        if t == "int" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "bool" and isinstance(value, bool):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "object" and isinstance(value, dict):
            return True
    return False


class SchemaValidator:
    """Collects issues while walking a response."""

    def __init__(self, contract: dict[str, Any] | None = None) -> None:
        self.contract = contract or load_contract()
        self.issues: list[SchemaIssue] = []

    # -- generic -----------------------------------------------------------
    def _object(self, value: Any, name: str, path: str) -> bool:
        spec = self.contract["objects"][name]
        if not isinstance(value, dict):
            self.issues.append(
                SchemaIssue(Severity.BREAKING, "type", path, f"expected object ({name})")
            )
            return False
        for field, tspec in spec["required"].items():
            if field not in value:
                self.issues.append(
                    SchemaIssue(Severity.BREAKING, "missing", f"{path}.{field}", f"required by {name}")
                )
            elif not _type_ok(value[field], tspec):
                self.issues.append(
                    SchemaIssue(
                        Severity.BREAKING,
                        "type",
                        f"{path}.{field}",
                        f"expected {tspec}, got {type(value[field]).__name__}",
                    )
                )
        for field, tspec in spec["optional"].items():
            if field in value and not _type_ok(value[field], tspec):
                # Optional fields we rely on less; still worth a warning.
                self.issues.append(
                    SchemaIssue(
                        Severity.WARNING,
                        "type",
                        f"{path}.{field}",
                        f"expected {tspec}, got {type(value[field]).__name__}",
                    )
                )
        known = set(spec["required"]) | set(spec["optional"])
        for field in value:
            if field not in known:
                self.issues.append(
                    SchemaIssue(Severity.INFO, "unknown_field", f"{path}.{field}", f"not in {name}")
                )
        return True

    def _enum(self, value: Any, enum_name: str, path: str) -> None:
        if value is None:
            return
        if value not in self.contract["enums"][enum_name]:
            self.issues.append(
                SchemaIssue(Severity.WARNING, "enum", path, f"unknown {enum_name} value {value!r}")
            )

    def _just_assigned(self, status: dict[str, Any]) -> bool:
        """The GPS list appears only with the first position, seconds after the car
        is assigned (seen in real data); no warning during that grace period."""
        now = getattr(self, "_now", None)
        assigned = parse_time(status.get("tripAssigned"))
        return now is not None and assigned is not None and now - assigned <= GPS_GRACE

    # -- endpoint-specific -------------------------------------------------
    def operator_config(self, data: Any) -> list[SchemaIssue]:
        if self._object(data, "operatorConfig", "$"):
            self._object(data.get("configs"), "operatorConfigBody", "$.configs")
        return self.issues

    def order_list(self, data: Any) -> list[SchemaIssue]:
        if self._object(data, "orderList", "$") and isinstance(data.get("items"), list):
            for i, item in enumerate(data["items"]):
                self._order(item, f"$.items[{i}]", detail=False)
        return self.issues

    def order_detail(self, data: Any, now: datetime | None = None) -> list[SchemaIssue]:
        """`now` enables the grace period after assignment (see GPS_GRACE)."""
        self._now = now
        self._order(data, "$", detail=True)
        return self.issues

    def _order(self, item: Any, path: str, *, detail: bool) -> None:
        if not self._object(item, "order", path):
            return
        self._enum(item.get("combinedStatus"), "combinedStatus", f"{path}.combinedStatus")
        status = item.get("status")
        if isinstance(status, dict) and self._object(status, "status", f"{path}.status"):
            self._enum(status.get("state"), "state", f"{path}.status.state")
            gps = status.get("gpsPositions")
            if isinstance(gps, list):
                for j, fix in enumerate(gps):
                    self._object(fix, "gpsPosition", f"{path}.status.gpsPositions[{j}]")
            elif (
                detail
                and phase_of(item.get("combinedStatus"), status.get("state")) in ACTIVE_PHASES
                and not self._just_assigned(status)
            ):
                # Before a car is assigned (and after the trip) there is no live
                # position; only a car on its way without one is unexpected.
                self.issues.append(
                    SchemaIssue(
                        Severity.WARNING,
                        "missing",
                        f"{path}.status.gpsPositions",
                        "detail response without gpsPositions (no live position available)",
                    )
                )
        addresses = item.get("addresses")
        if isinstance(addresses, list):
            for j, addr in enumerate(addresses):
                if self._object(addr, "address", f"{path}.addresses[{j}]"):
                    self._object(
                        addr.get("coordinates"),
                        "scaledCoordinates",
                        f"{path}.addresses[{j}].coordinates",
                    )
        attributes = item.get("attributes")
        if isinstance(attributes, list):
            for j, attr in enumerate(attributes):
                self._object(attr, "bookingAttribute", f"{path}.attributes[{j}]")
        persons = item.get("persons")
        if isinstance(persons, list):
            for j, ev in enumerate(persons):
                if self._object(ev, "personEvent", f"{path}.persons[{j}]"):
                    self._enum(ev.get("type"), "personEventType", f"{path}.persons[{j}].type")


def worst(issues: list[SchemaIssue]) -> Severity | None:
    return max((i.severity for i in issues), default=None)


def collapse(issues: list[SchemaIssue]) -> list[SchemaIssue]:
    """Merge issues that differ only by array index, e.g. items[0] / items[1]."""
    import re

    seen: dict[tuple[str, str, str], SchemaIssue] = {}
    for issue in issues:
        generic = re.sub(r"\[\d+\]", "[]", issue.path)
        key = (issue.kind, generic, issue.detail)
        if key not in seen:
            seen[key] = SchemaIssue(issue.severity, issue.kind, generic, issue.detail)
    return list(seen.values())
