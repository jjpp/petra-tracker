# SPDX-License-Identifier: GPL-3.0-or-later
"""Sensors: trip status, ETA, next pickup (per child); token expiry and schema status (account)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetraConfigEntry
from .const import STATUS_IDLE
from .coordinator import PetraCoordinator
from .core.models import Phase
from .core.schema import schema_version
from .entity import PetraAccountEntity, PetraPersonEntity, add_person_entities

STATUS_OPTIONS = [STATUS_IDLE, *(p.value for p in Phase)]
SCHEMA_OPTIONS = ["ok", "info", "warning", "breaking"]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def async_setup_entry(
    hass: HomeAssistant, entry: PetraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [TokenExpirySensor(coordinator), SchemaStatusSensor(coordinator), LastPollSensor(coordinator)]
    )
    add_person_entities(
        coordinator,
        async_add_entities,
        lambda c, pid: (
            TripStatusSensor(c, pid),
            EtaSensor(c, pid),
            NextPickupSensor(c, pid),
            TripLastUpdateSensor(c, pid),
        ),
    )


class TripStatusSensor(PetraPersonEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS

    def __init__(self, coordinator: PetraCoordinator, person_id: int) -> None:
        super().__init__(coordinator, person_id, "trip_status")

    @property
    def native_value(self) -> str:
        p = self.person
        return p.phase.value if p and p.phase else STATUS_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.person
        t = p.trip if p else None
        if t is None:
            return {}
        return {
            "raw_status": t.combined_status,
            "raw_state": t.state,
            "book_ref": t.book_ref,
            "scheduled_pickup": _iso(t.scheduled_pickup),
            "scheduled_dropoff": _iso(t.scheduled_dropoff),
            "car_no": t.car_no,
            "trip_assigned": _iso(t.trip_assigned),
            "trip_started": _iso(t.trip_started),
            "trip_ended": _iso(t.trip_ended),
            "coordinated_position": t.coordinated_position,
            "data_stale": not self.coordinator.data.connection_ok,
            **self._ending(p),
        }

    @staticmethod
    def _ending(p) -> dict[str, Any]:
        """For a finished trip: was it ended at the destination? (Drivers sometimes
        end it early; the car's position is no longer tracked after that.)"""
        e = p.ending if p else None
        if e is None:
            return {}
        return {
            "ended_at_destination": e.at_destination,
            "end_distance_m": round(e.distance_m),
            "estimated_arrival": _iso(e.estimated_arrival),
        }


class EtaSensor(PetraPersonEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: PetraCoordinator, person_id: int) -> None:
        super().__init__(coordinator, person_id, "eta")

    @property
    def native_value(self) -> int | None:
        p = self.person
        return p.eta.minutes if p and p.eta else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.person
        e = p.eta if p else None
        if e is None:
            return {}
        return {
            "leg": e.leg.value,
            "source": e.source.value,
            "distance_m": round(e.distance_m) if e.distance_m is not None else None,
            "last_fix": _iso(e.last_fix),
            "gps_stale": e.gps_stale,
            "approximate": e.approximate,
            "routing": e.routing,
        }


class NextPickupSensor(PetraPersonEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PetraCoordinator, person_id: int) -> None:
        super().__init__(coordinator, person_id, "next_pickup")

    @property
    def native_value(self) -> datetime | None:
        p = self.person
        return p.next_pickup if p else None


class TokenExpirySensor(PetraAccountEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PetraCoordinator) -> None:
        super().__init__(coordinator, "token_expiry")

    @property
    def native_value(self) -> datetime | None:
        info = self.coordinator.token_info
        return info.expires_at if info else None


class SchemaStatusSensor(PetraAccountEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SCHEMA_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PetraCoordinator) -> None:
        super().__init__(coordinator, "schema_status")

    @property
    def native_value(self) -> str:
        sev = self.coordinator.data.schema_severity if self.coordinator.data else None
        return sev.name.lower() if sev else "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "schema_version": schema_version(),
            "issues": [i.as_dict() for i in data.schema_issues] if data else [],
        }


class TripLastUpdateSensor(PetraPersonEntity, SensorEntity):
    """When the service last had news about the child's current trip (status change,
    GPS fix, pickup/drop-off) - shows how fresh the car's data is."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PetraCoordinator, person_id: int) -> None:
        super().__init__(coordinator, person_id, "trip_last_update")

    @property
    def native_value(self) -> datetime | None:
        p = self.person
        return p.trip.last_update if p and p.trip else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.person
        t = p.trip if p else None
        data = self.coordinator.data
        return {
            "last_gps_fix": _iso(t.last_fix.time) if t and t.last_fix else None,
            "last_status_change": _iso(t.status_time) if t else None,
            "last_poll": _iso(data.updated_at) if data else None,
        }


class LastPollSensor(PetraAccountEntity, SensorEntity):
    """Last successful poll of the service (stays put while it is unreachable)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PetraCoordinator) -> None:
        super().__init__(coordinator, "last_poll")

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None  # useful precisely during an outage

    @property
    def native_value(self) -> datetime | None:
        data = self.coordinator.data
        return data.updated_at if data else None
