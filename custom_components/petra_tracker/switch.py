# SPDX-License-Identifier: GPL-3.0-or-later
"""Polling switch: pause all contact with the service (e.g. during school holidays)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetraConfigEntry
from .coordinator import PetraCoordinator
from .entity import PetraAccountEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PetraConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([PollingSwitch(entry.runtime_data)])


class PollingSwitch(PetraAccountEntity, SwitchEntity):
    """On: poll the service as configured. Off: no requests at all.

    The state is kept across restarts. While off, the children's entities are
    unavailable (their last state would be stale).
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PetraCoordinator) -> None:
        super().__init__(coordinator, "polling")

    @property
    def available(self) -> bool:
        return True  # must stay usable while paused or while the service is down

    @property
    def is_on(self) -> bool:
        return self.coordinator.polling_enabled

    @property
    def icon(self) -> str:
        return "mdi:sync" if self.is_on else "mdi:sync-off"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_polling(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_polling(False)
        self.async_write_ha_state()
