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

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER, Platform.SWITCH]

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
    """Delete the saved trip memory and settings when the integration is removed."""
    for key in (f"{DOMAIN}.{entry.entry_id}", f"{DOMAIN}.{entry.entry_id}.settings"):
        await Store(hass, STORAGE_VERSION, key).async_remove()


async def _async_options_updated(hass: HomeAssistant, entry: PetraConfigEntry) -> None:
    # Home Assistant calls this for any change of the entry, including the data the
    # integration updates itself (e.g. a newly discovered child). Reload only when
    # the user changed the options.
    if dict(entry.options) != entry.runtime_data.options:
        await hass.config_entries.async_reload(entry.entry_id)
