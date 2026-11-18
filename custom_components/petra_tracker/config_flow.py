# SPDX-License-Identifier: GPL-3.0-or-later
"""Config flow: operator country, then username/password + SMS code (or a pasted
token); the children are discovered from their bookings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult, OptionsFlow
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
    CONF_USERNAME,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    OPT_ACTIVE_INTERVAL_S,
    OPT_ASSUMED_SPEED_KMH,
    OPT_END_NEAR_DESTINATION_M,
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
    OperatorConfig,
    PetraApiKeyError,
    PetraAuthError,
    PetraClient,
    PetraError,
    PetraLoginError,
    PetraOtpError,
    discover_persons,
    fetch_operator_config,
    login,
    otp_digits,
    verify_2fa,
)
from .core.routing import ENGINES, OPENROUTESERVICE, WAZE_REGIONS

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
USERNAME_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username"))
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
CODE_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code"))

CONF_AUTH_METHOD = "auth_method"
CONF_PASSWORD = "password"
CONF_CODE = "code"
CONF_RESEND = "resend"
AUTH_LOGIN = "login"
AUTH_TOKEN = "token"


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
    """Set-up and re-authentication share the steps login -> code (or token)."""

    VERSION = 1

    def __init__(self) -> None:
        self._op: OperatorConfig | None = None
        self._username: str | None = None
        self._password: str | None = None  # in memory only while this dialog is open
        self._user_id: int | None = None
        self._direct_token: str | None = None  # account without two-factor login

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> PetraOptionsFlow:
        return PetraOptionsFlow()

    @property
    def _session(self):
        return async_get_clientsession(self.hass)

    # -- first set-up ------------------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._op = await fetch_operator_config(self._session, user_input[CONF_COUNTRY])
            except PetraError as err:
                LOGGER.debug("Operator config failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if user_input.get(CONF_AUTH_METHOD) == AUTH_TOKEN:
                    return await self.async_step_token()
                return await self.async_step_login()
        schema = vol.Schema(
            {
                vol.Required(CONF_COUNTRY, default="se"): SelectSelector(
                    SelectSelectorConfig(
                        options=list(COUNTRIES), translation_key="country", mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(CONF_AUTH_METHOD, default=AUTH_LOGIN): SelectSelector(
                    SelectSelectorConfig(
                        options=[AUTH_LOGIN, AUTH_TOKEN], translation_key="auth_method",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # -- re-authentication ---------------------------------------------------------
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        self._op = OperatorConfig(
            country=entry.data[CONF_COUNTRY],
            base_url=entry.data[CONF_BASE_URL],
            api_key=entry.data[CONF_API_KEY],
        )
        self._username = entry.data.get(CONF_USERNAME)
        return self.async_show_menu(step_id="reauth", menu_options=[AUTH_LOGIN, AUTH_TOKEN])

    # -- shared steps ------------------------------------------------------------------
    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            error = await self._send_code()
            if error is None:
                if self._direct_token:
                    return await self._finish_or_form("login", self._direct_token)
                return await self.async_step_code()
            errors["base"] = error
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self._username or ""): USERNAME_SELECTOR,
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(step_id="login", data_schema=schema, errors=errors)

    async def _send_code(self) -> str | None:
        """POST login (the server sends an SMS). Returns an error key or None."""
        assert self._op is not None and self._username is not None and self._password is not None
        try:
            start = await login(
                self._session, self._op.base_url, self._op.api_key, self._username, self._password
            )
        except PetraLoginError as err:
            LOGGER.debug("Login rejected: %s", err)
            return "invalid_credentials"
        except PetraApiKeyError:
            return "operator_config"
        except PetraError as err:
            LOGGER.debug("Login failed: %s", err)
            return "cannot_connect"
        self._user_id = start.user_id
        self._direct_token = start.token
        return None

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_RESEND):
                errors["base"] = await self._send_code() or "code_resent"
            elif otp_digits(user_input.get(CONF_CODE, "")) is None:
                errors[CONF_CODE] = "code_format"
            else:
                assert self._op is not None and isinstance(self._user_id, int)
                try:
                    token = await verify_2fa(
                        self._session, self._op.base_url, self._op.api_key, self._user_id,
                        user_input[CONF_CODE],
                    )
                except PetraOtpError as err:
                    LOGGER.debug("Code rejected: %s", err)
                    errors[CONF_CODE] = "invalid_code"
                except PetraError as err:
                    LOGGER.debug("Code verification failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    return await self._finish_or_form("code", token)
        schema = vol.Schema(
            {
                vol.Optional(CONF_CODE, default=""): CODE_SELECTOR,
                vol.Optional(CONF_RESEND, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="code", data_schema=schema, errors=errors)

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            return await self._finish_or_form("token", user_input[CONF_TOKEN])
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
        )

    # -- completion ----------------------------------------------------------------------
    async def _finish_or_form(self, step_id: str, raw_token: str) -> ConfigFlowResult:
        """Validate the token and create/update the entry; on failure show the step again."""
        result = await self._finish(raw_token)
        if not isinstance(result, str):
            return result
        field = CONF_TOKEN if step_id == "token" and result in ("invalid_token", "token_expired") else "base"
        if step_id == "token":
            schema = vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR})
        elif step_id == "code":
            schema = vol.Schema(
                {vol.Optional(CONF_CODE, default=""): CODE_SELECTOR,
                 vol.Optional(CONF_RESEND, default=False): BooleanSelector()}
            )
        else:
            schema = vol.Schema(
                {vol.Required(CONF_USERNAME, default=self._username or ""): USERNAME_SELECTOR,
                 vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}
            )
        return self.async_show_form(step_id=step_id, data_schema=schema, errors={field: result})

    async def _finish(self, raw_token: str) -> ConfigFlowResult | str:
        token, info, error = _check_token(raw_token)
        if error:
            return error
        assert info is not None and self._op is not None
        reauth = self.source == SOURCE_REAUTH
        if reauth and info.user_id != self._get_reauth_entry().data[CONF_USER_ID]:
            return "wrong_user"
        client = PetraClient(self._session, self._op.base_url, self._op.api_key, token)
        try:
            persons = await discover_persons(
                client, info.user_id, dt_util.utcnow(), 1 if reauth else PERSON_DISCOVERY_DAYS
            )
        except PetraAuthError:
            return "invalid_auth"
        except PetraApiKeyError:
            return "operator_config"
        except PetraError as err:
            LOGGER.debug("Setup failed: %s", err)
            return "cannot_connect"
        self._password = None
        if reauth:
            updates: dict[str, Any] = {CONF_TOKEN: token}
            if self._username:
                updates[CONF_USERNAME] = self._username
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=updates)

        await self.async_set_unique_id(f"{self._op.base_url}#{info.user_id}")
        self._abort_if_unique_id_configured()
        names = sorted(n for n in persons.values() if n)
        data: dict[str, Any] = {
            CONF_COUNTRY: self._op.country,
            CONF_BASE_URL: self._op.base_url,
            CONF_API_KEY: self._op.api_key,
            CONF_TOKEN: token,
            CONF_USER_ID: info.user_id,
            CONF_PERSONS: {str(k): v for k, v in persons.items()},
        }
        if self._username:
            data[CONF_USERNAME] = self._username
        return self.async_create_entry(
            title="Petra trips" + (f" ({', '.join(names)})" if names else ""),
            data=data,
            options=dict(DEFAULT_OPTIONS),
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
    OPT_END_NEAR_DESTINATION_M: _number(100, 5000, 50, "m"),
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
        # Saved by 0.4.0-0.7.0 as upper case ("EU"); the selector keys are lowercase.
        current[OPT_WAZE_REGION] = str(current[OPT_WAZE_REGION]).lower()
        return self.async_show_form(step_id="init", data_schema=_options_schema(current), errors=errors)
