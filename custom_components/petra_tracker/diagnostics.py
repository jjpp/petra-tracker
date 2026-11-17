# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostics download (Settings -> Devices & services -> ... -> Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import PetraConfigEntry
from .const import CONF_API_KEY, CONF_PERSONS, CONF_TOKEN, CONF_USER_ID, OPT_ROUTING_API_KEY
from .core.schema import schema_version

TO_REDACT = {
    CONF_TOKEN, CONF_API_KEY, CONF_USER_ID, CONF_PERSONS, OPT_ROUTING_API_KEY, "unique_id", "title"
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PetraConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    token = coordinator.token_info
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "schema_version": schema_version(),
        "token_expires_at": token.expires_at.isoformat() if token and token.expires_at else None,
        "update_interval_s": coordinator.update_interval.total_seconds() if coordinator.update_interval else None,
        "backoff_failures": coordinator.backoff.failures,
        "polling_enabled": coordinator.polling_enabled,
        "routing_engine": coordinator.router.name if coordinator.router else "straight_line",
        "routing_error": coordinator.data.routing_error if coordinator.data else None,
        "state": None
        if data is None
        else {
            "updated_at": data.updated_at.isoformat(),
            "connection_ok": data.connection_ok,
            "last_error": data.last_error,
            "rate_limit_remaining": data.rate_limit_remaining,
            "schema_issues": [i.as_dict() for i in data.schema_issues],
            "persons": [
                {
                    "phase": p.phase.value if p.phase else None,
                    "eta_minutes": p.eta.minutes if p.eta else None,
                    "eta_source": p.eta.source.value if p.eta else None,
                    "gps_fixes": len(p.trip.gps) if p.trip else 0,
                }
                for p in data.persons.values()
            ],
        },
    }
