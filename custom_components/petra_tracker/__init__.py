# SPDX-License-Identifier: GPL-3.0-or-later
"""Petra trip tracker: school/service transport trips booked via the Petra platform."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .coordinator import PetraCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

type PetraConfigEntry = ConfigEntry[PetraCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PetraConfigEntry) -> bool:
    coordinator = PetraCoordinator(hass, entry, async_get_clientsession(hass))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PetraConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: PetraConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
