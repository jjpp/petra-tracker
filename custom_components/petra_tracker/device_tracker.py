# SPDX-License-Identifier: GPL-3.0-or-later
"""Device tracker: live position of the car serving the child's current trip."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetraConfigEntry
from .coordinator import PetraCoordinator
from .core.models import ACTIVE_PHASES, GpsFix
from .entity import PetraPersonEntity, add_person_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: PetraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    add_person_entities(entry.runtime_data, async_add_entities, lambda c, pid: (CarTracker(c, pid),))


class CarTracker(PetraPersonEntity, TrackerEntity):
    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: PetraCoordinator, person_id: int) -> None:
        super().__init__(coordinator, person_id, "car")

    @property
    def _fix(self) -> GpsFix | None:
        p = self.person
        if p is None or p.trip is None or p.phase not in ACTIVE_PHASES:
            return None
        return p.trip.last_fix

    @property
    def latitude(self) -> float | None:
        fix = self._fix
        return fix.lat if fix else None

    @property
    def longitude(self) -> float | None:
        fix = self._fix
        return fix.lon if fix else None

    @property
    def location_accuracy(self) -> int:
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fix = self._fix
        p = self.person
        if fix is None or p is None or p.trip is None:
            return {}
        return {"last_fix": fix.time.isoformat(), "car_no": p.trip.car_no}
