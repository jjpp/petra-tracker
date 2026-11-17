# SPDX-License-Identifier: GPL-3.0-or-later
"""Binary sensors: trip active (per child), API connectivity (account)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetraConfigEntry
from .coordinator import PetraCoordinator
from .core.models import ACTIVE_PHASES
from .entity import PetraAccountEntity, PetraPersonEntity, add_person_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: PetraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([ConnectionSensor(coordinator)])
    add_person_entities(coordinator, async_add_entities, lambda c, pid: (TripActiveSensor(c, pid),))


class TripActiveSensor(PetraPersonEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: PetraCoordinator, person_id: int) -> None:
        super().__init__(coordinator, person_id, "trip_active")

    @property
    def is_on(self) -> bool:
        p = self.person
        return bool(p and p.phase in ACTIVE_PHASES)


class ConnectionSensor(PetraAccountEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PetraCoordinator) -> None:
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        return True  # this sensor reports the outage itself

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return bool(self.coordinator.last_update_success and data and data.connection_ok)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        return {
            "failing_since": data.failing_since.isoformat() if data.failing_since else None,
            "last_error": data.last_error,
            "last_success": data.updated_at.isoformat(),
            "rate_limit_remaining": data.rate_limit_remaining,
            "poll_interval_s": int(self.coordinator.update_interval.total_seconds())
            if self.coordinator.update_interval
            else None,
        }
