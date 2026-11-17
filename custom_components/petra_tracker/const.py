# SPDX-License-Identifier: GPL-3.0-or-later
"""Constants for the Petra trip tracker integration."""

from __future__ import annotations

import logging

DOMAIN = "petra_tracker"
LOGGER = logging.getLogger(__package__)

# Config entry data
CONF_COUNTRY = "country"
CONF_TOKEN = "token"
CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"
CONF_USER_ID = "user_id"
CONF_PERSONS = "persons"  # {person_id (str): first name or None}

# Options (with defaults)
OPT_ACTIVE_INTERVAL_S = "active_interval_s"
OPT_IDLE_INTERVAL_MIN = "idle_interval_min"
OPT_LEAD_TIME_MIN = "lead_time_min"
OPT_ETA_GRANULARITY_MIN = "eta_granularity_min"
OPT_HEARTBEAT_MIN = "heartbeat_min"
OPT_FAILURE_ALERT_MIN = "failure_alert_min"
OPT_TRACK_ONBOARD = "track_onboard"
OPT_ASSUMED_SPEED_KMH = "assumed_speed_kmh"
OPT_TELEMETRY = "telemetry"
OPT_TELEMETRY_RETENTION_DAYS = "telemetry_retention_days"

DEFAULT_OPTIONS: dict[str, int | float | bool] = {
    OPT_ACTIVE_INTERVAL_S: 30,
    OPT_IDLE_INTERVAL_MIN: 10,
    OPT_LEAD_TIME_MIN: 45,
    OPT_ETA_GRANULARITY_MIN: 1,
    OPT_HEARTBEAT_MIN: 0,
    OPT_FAILURE_ALERT_MIN: 3,
    OPT_TRACK_ONBOARD: True,
    OPT_ASSUMED_SPEED_KMH: 25,
    OPT_TELEMETRY: False,
    OPT_TELEMETRY_RETENTION_DAYS: 30,
}

TOKEN_WARN_DAYS = 7
PERSON_DISCOVERY_DAYS = 30

# Events fired on the HA bus (build notifications on these).
EVENT_STATE_CHANGED = f"{DOMAIN}_state_changed"
EVENT_ETA_CHANGED = f"{DOMAIN}_eta_changed"
EVENT_HEARTBEAT = f"{DOMAIN}_heartbeat"
EVENT_SCHEMA_DRIFT = f"{DOMAIN}_schema_drift"
EVENT_TOKEN_EXPIRING = f"{DOMAIN}_token_expiring"
EVENT_CONNECTION_DEGRADED = f"{DOMAIN}_connection_degraded"
EVENT_CONNECTION_RESTORED = f"{DOMAIN}_connection_restored"

STATUS_IDLE = "idle"
