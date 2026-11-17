# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared entity plumbing."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PetraCoordinator
from .core.tracker import PersonState


class PetraEntity(CoordinatorEntity[PetraCoordinator]):
    _attr_has_entity_name = True


class PetraAccountEntity(PetraEntity):
    def __init__(self, coordinator: PetraCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_account")},
            name="Petra account",
            manufacturer="Petra (unofficial)",
            entry_type=DeviceEntryType.SERVICE,
        )


class PetraPersonEntity(PetraEntity):
    def __init__(self, coordinator: PetraCoordinator, person_id: int, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.person_id = person_id
        self._attr_unique_id = f"{entry.unique_id}_{person_id}_{key}"
        self._attr_translation_key = key
        name = coordinator.known_persons.get(person_id) or f"Person {person_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_{person_id}")},
            name=f"{name} transport",
            manufacturer="Petra (unofficial)",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and not (data and data.paused)

    @property
    def person(self) -> PersonState | None:
        data = self.coordinator.data
        return data.persons.get(self.person_id) if data else None


def add_person_entities(
    coordinator: PetraCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[PetraCoordinator, int], Iterable[Entity]],
) -> None:
    """Create entities for every known person now, and for persons that appear later."""
    added: set[int] = set()

    @callback
    def _sync() -> None:
        ids = set(coordinator.known_persons)
        if coordinator.data:
            ids |= set(coordinator.data.persons)
        new = sorted(ids - added)
        if not new:
            return
        added.update(new)
        async_add_entities([e for pid in new for e in factory(coordinator, pid)])

    _sync()
    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(_sync))
