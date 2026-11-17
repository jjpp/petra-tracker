# SPDX-License-Identifier: GPL-3.0-or-later
"""Config flow: pick the operator country, paste a token, discover the children."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_COUNTRY,
    CONF_PERSONS,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    OPT_ACTIVE_INTERVAL_S,
    OPT_ASSUMED_SPEED_KMH,
    OPT_ETA_GRANULARITY_MIN,
    OPT_FAILURE_ALERT_MIN,
    OPT_HEARTBEAT_MIN,
    OPT_IDLE_INTERVAL_MIN,
    OPT_LATE_WARNING_MIN,
    OPT_LEAD_TIME_MIN,
    OPT_ROUTING,
    OPT_ROUTING_API_KEY,
    OPT_ROUTING_URL,
    OPT_TELEMETRY,
    OPT_TELEMETRY_RETENTION_DAYS,
    OPT_TRACK_ONBOARD,
    OPT_WAZE_REGION,
    PERSON_DISCOVERY_DAYS,
)
from .core import jwt
from .core.client import (
    COUNTRIES,
    PetraApiKeyError,
    PetraAuthError,
    PetraClient,
    PetraError,
    discover_persons,
    fetch_operator_config,
)
from .core.routing import ENGINES, OPENROUTESERVICE, WAZE_REGIONS

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _check_token(raw: str) -> tuple[str, jwt.TokenInfo | None, str | None]:
    """Returns (normalised token, info, error key)."""
    token = jwt.normalise_token(raw)
    try:
        info = jwt.decode(token)
    except jwt.InvalidToken:
        return token, None, "invalid_token"
    if info.expires_at is not None and info.expires_at <= dt_util.utcnow():
        return token, info, "token_expired"
    return token, info, None


class PetraConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> PetraOptionsFlow:
        return PetraOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token, info, error = _check_token(user_input[CONF_TOKEN])
            if error:
                errors[CONF_TOKEN] = error
            else:
                assert info is not None
                session = async_get_clientsession(self.hass)
                try:
                    op = await fetch_operator_config(session, user_input[CONF_COUNTRY])
                    client = PetraClient(session, op.base_url, op.api_key, token)
                    persons = await discover_persons(
                        client, info.user_id, dt_util.utcnow(), PERSON_DISCOVERY_DAYS
                    )
                except PetraAuthError:
                    errors["base"] = "invalid_auth"
                except PetraApiKeyError:
                    errors["base"] = "operator_config"
                except PetraError as err:
                    LOGGER.debug("Setup failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(f"{op.base_url}#{info.user_id}")
                    self._abort_if_unique_id_configured()
                    names = sorted(n for n in persons.values() if n)
                    title = "Petra trips" + (f" ({', '.join(names)})" if names else "")
                    return self.async_create_entry(
                        title=title,
                        data={
                            CONF_COUNTRY: user_input[CONF_COUNTRY],
                            CONF_BASE_URL: op.base_url,
                            CONF_API_KEY: op.api_key,
                            CONF_TOKEN: token,
                            CONF_USER_ID: info.user_id,
                            CONF_PERSONS: {str(k): v for k, v in persons.items()},
                        },
                        options=dict(DEFAULT_OPTIONS),
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_COUNTRY, default="se"): SelectSelector(
                    SelectSelectorConfig(
                        options=list(COUNTRIES),
                        translation_key="country",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_TOKEN): TOKEN_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token, info, error = _check_token(user_input[CONF_TOKEN])
            if error:
                errors[CONF_TOKEN] = error
            elif info is None or info.user_id != entry.data[CONF_USER_ID]:
                errors[CONF_TOKEN] = "wrong_user"
            else:
                session = async_get_clientsession(self.hass)
                client = PetraClient(
                    session, entry.data[CONF_BASE_URL], entry.data[CONF_API_KEY], token
                )
                try:
                    await discover_persons(client, info.user_id, dt_util.utcnow(), 1)
                except PetraAuthError:
                    errors["base"] = "invalid_auth"
                except PetraError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_TOKEN: token}
                    )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
        )


def _number(minimum: float, maximum: float, step: float = 1, unit: str | None = None) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX
        )
    )


OPTION_SELECTORS = {
    OPT_ACTIVE_INTERVAL_S: _number(10, 300, 1, "s"),
    OPT_IDLE_INTERVAL_MIN: _number(1, 60, 1, "min"),
    OPT_LEAD_TIME_MIN: _number(5, 180, 1, "min"),
    OPT_ETA_GRANULARITY_MIN: _number(1, 30, 1, "min"),
    OPT_HEARTBEAT_MIN: _number(0, 60, 1, "min"),
    OPT_LATE_WARNING_MIN: _number(0, 60, 1, "min"),
    OPT_FAILURE_ALERT_MIN: _number(1, 120, 1, "min"),
    OPT_TRACK_ONBOARD: BooleanSelector(),
    OPT_ASSUMED_SPEED_KMH: _number(5, 100, 1, "km/h"),
    OPT_ROUTING: SelectSelector(
        SelectSelectorConfig(
            options=list(ENGINES), translation_key="routing", mode=SelectSelectorMode.DROPDOWN
        )
    ),
    OPT_WAZE_REGION: SelectSelector(
        SelectSelectorConfig(
            options=list(WAZE_REGIONS), translation_key="waze_region", mode=SelectSelectorMode.DROPDOWN
        )
    ),
    OPT_TELEMETRY: BooleanSelector(),
    OPT_TELEMETRY_RETENTION_DAYS: _number(1, 365, 1, "d"),
}
# Free-text options: a cleared field must stay cleared, so they use a suggested
# value instead of a default (HA omits empty text fields from the submission).
TEXT_OPTIONS = {
    OPT_ROUTING_URL: TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
    OPT_ROUTING_API_KEY: TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
}
INT_OPTIONS = {k for k, v in DEFAULT_OPTIONS.items() if isinstance(v, int) and not isinstance(v, bool)}


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {}
    for key, sel in OPTION_SELECTORS.items():
        fields[vol.Required(key, default=current[key])] = sel
        if key == OPT_ROUTING:  # keep the engine's settings next to it
            for tkey, tsel in TEXT_OPTIONS.items():
                fields[vol.Optional(tkey, description={"suggested_value": current.get(tkey, "")})] = tsel
    return vol.Schema(fields)


class PetraOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = {k: int(v) if k in INT_OPTIONS else v for k, v in user_input.items()}
            for key in TEXT_OPTIONS:
                cleaned[key] = str(cleaned.get(key) or "").strip()
            if (
                cleaned[OPT_ROUTING] == OPENROUTESERVICE
                and not cleaned[OPT_ROUTING_API_KEY]
                and not cleaned[OPT_ROUTING_URL]
            ):
                # The public server needs a (free) key; a self-hosted one does not.
                errors[OPT_ROUTING_API_KEY] = "api_key_required"
            else:
                return self.async_create_entry(data=cleaned)
        current = {**DEFAULT_OPTIONS, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=_options_schema(current), errors=errors)
