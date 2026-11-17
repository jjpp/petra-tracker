# SPDX-License-Identifier: GPL-3.0-or-later
"""Petra trip tracker: school/service transport trips booked via the Petra platform."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION
from .coordinator import PetraCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

type PetraConfigEntry = ConfigEntry[PetraCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PetraConfigEntry) -> bool:
    coordinator = PetraCoordinator(hass, entry, async_get_clientsession(hass))
    await coordinator.async_restore_state()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PetraConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Flush now: on a reload the new coordinator reads it right away.
        await entry.runtime_data.async_save_state()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PetraConfigEntry) -> None:
    """Delete the saved trip memory when the integration is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _async_options_updated(hass: HomeAssistant, entry: PetraConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
