# SPDX-License-Identifier: GPL-3.0-or-later
"""DataUpdateCoordinator: polls the Petra API and turns it into entity state + events."""

from __future__ import annotations

import functools
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
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
    EVENT_CONNECTION_DEGRADED,
    EVENT_CONNECTION_RESTORED,
    EVENT_SCHEMA_DRIFT,
    EVENT_TOKEN_EXPIRING,
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
    OPT_QUIET_HEARTBEAT_MIN,
    OPT_ROUTING,
    OPT_ROUTING_API_KEY,
    OPT_ROUTING_URL,
    OPT_TELEMETRY,
    OPT_TELEMETRY_RETENTION_DAYS,
    OPT_TRACK_ONBOARD,
    OPT_WAZE_REGION,
    REAUTH_DAYS,
    STORAGE_VERSION,
    TOKEN_WARN_DAYS,
)
from .core import jwt
from .core.backoff import Backoff
from .core.client import (
    PetraApiKeyError,
    PetraAuthError,
    PetraClient,
    PetraEndpointError,
    PetraError,
    PetraRateLimited,
    PetraTransientError,
    fetch_operator_config,
    local_day_bounds,
)
from .core.eta import EtaParams
from .core.models import ACTIVE_PHASES, Trip, parse_order_list
from .core.routing import RouteCache, attach_route, make_provider
from .core.schema import SchemaIssue, SchemaValidator, Severity, collapse, worst
from .core.telemetry import TelemetryWriter
from .core.tracker import (
    DEFAULT_RESTORE_MAX_AGE,
    PersonState,
    PollSettings,
    TrackerSettings,
    TripTracker,
    plan_next_poll,
    trips_needing_detail,
)

if TYPE_CHECKING:
    from . import PetraConfigEntry

MAX_REPORTED_ISSUES = 20


@dataclass
class PetraData:
    persons: dict[int, PersonState]
    updated_at: datetime
    connection_ok: bool = True
    failing_since: datetime | None = None
    last_error: str | None = None
    schema_severity: Severity | None = None
    schema_issues: list[SchemaIssue] = field(default_factory=list)
    rate_limit_remaining: int | None = None
    paused: bool = False  # polling switched off
    routing_error: str | None = None  # last routing failure (ETA fell back to straight line)


def _opt(entry: PetraConfigEntry, key: str) -> Any:
    return entry.options.get(key, DEFAULT_OPTIONS[key])


class PetraCoordinator(DataUpdateCoordinator[PetraData]):
    config_entry: PetraConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: PetraConfigEntry, session: aiohttp.ClientSession
    ) -> None:
        self.poll_settings = PollSettings(
            active_interval=timedelta(seconds=_opt(entry, OPT_ACTIVE_INTERVAL_S)),
            idle_interval=timedelta(minutes=_opt(entry, OPT_IDLE_INTERVAL_MIN)),
            lead_time=timedelta(
                minutes=max(_opt(entry, OPT_LEAD_TIME_MIN), _opt(entry, OPT_LATE_WARNING_MIN) + 5)
            ),
        )
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=self.poll_settings.active_interval,
        )
        self.session = session
        self.router = make_provider(
            _opt(entry, OPT_ROUTING),
            session,
            url=str(_opt(entry, OPT_ROUTING_URL) or ""),
            api_key=str(_opt(entry, OPT_ROUTING_API_KEY) or ""),
            waze_region=_opt(entry, OPT_WAZE_REGION),
        )
        self.route_cache = RouteCache()
        self._routing_failures = 0
        self._routing_error: str | None = None
        self.client = PetraClient(
            session, entry.data[CONF_BASE_URL], entry.data[CONF_API_KEY], entry.data[CONF_TOKEN]
        )
        self.user_id: int = entry.data[CONF_USER_ID]
        heartbeat = _opt(entry, OPT_HEARTBEAT_MIN)
        self.track_onboard: bool = _opt(entry, OPT_TRACK_ONBOARD)
        self.tracker = TripTracker(
            TrackerSettings(
                eta_granularity_min=int(_opt(entry, OPT_ETA_GRANULARITY_MIN)),
                heartbeat_interval=timedelta(minutes=heartbeat) if heartbeat else None,
                quiet_heartbeat_interval=timedelta(minutes=quiet) if (quiet := _opt(entry, OPT_QUIET_HEARTBEAT_MIN)) else None,
                late_warning_lead=timedelta(minutes=_opt(entry, OPT_LATE_WARNING_MIN)),
                track_onboard=self.track_onboard,
                end_near_destination_m=float(_opt(entry, OPT_END_NEAR_DESTINATION_M)),
                eta_params=EtaParams(assumed_speed_kmh=float(_opt(entry, OPT_ASSUMED_SPEED_KMH))),
            )
        )
        self.failure_alert_after = timedelta(minutes=_opt(entry, OPT_FAILURE_ALERT_MIN))
        self.backoff = Backoff(base=self.poll_settings.active_interval)
        self.known_persons: dict[int, str | None] = {
            int(k): v for k, v in entry.data.get(CONF_PERSONS, {}).items()
        }
        self.telemetry: TelemetryWriter | None = None
        if _opt(entry, OPT_TELEMETRY):
            self.telemetry = TelemetryWriter(
                Path(hass.config.path(DOMAIN, "telemetry")),
                retention_days=int(_opt(entry, OPT_TELEMETRY_RETENTION_DAYS)),
            )
        self._reported_issues: set[tuple[str, str, str]] = set()
        self._degraded_announced = False
        self._token_warned_on: date | None = None
        self._reauth_started_on: date | None = None
        self._pruned_on: date | None = None
        self._api_key_refreshed = False
        # Tracker memory survives restarts/reloads, so events are neither repeated
        # nor invented when Home Assistant starts.
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        # User settings that are not options (changing them must not reload the entry).
        self._settings_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.settings"
        )
        self.polling_enabled = True
        self._last_poll_at: datetime | None = None
        # The options this coordinator was built with; only a change of these reloads.
        self.options = dict(entry.options)

    # -- persistence -------------------------------------------------------------
    async def async_restore_state(self) -> None:
        data = await self._store.async_load()
        if data and self.tracker.restore_state(data, dt_util.utcnow()):
            LOGGER.debug("Restored trip memory saved at %s", data.get("saved_at"))
        settings = await self._settings_store.async_load() or {}
        self.polling_enabled = settings.get("polling_enabled", True) is not False
        if not self.polling_enabled:
            self.update_interval = None
            LOGGER.info("Polling is switched off; not contacting the service")

    async def async_set_polling(self, enabled: bool) -> None:
        """The polling switch: off = no requests at all until switched on again."""
        if enabled == self.polling_enabled:
            return
        self.polling_enabled = enabled
        await self._settings_store.async_save({"polling_enabled": enabled})
        if enabled:
            LOGGER.info("Polling switched on")
            last = self._last_poll_at
            if last is None or dt_util.utcnow() - last > DEFAULT_RESTORE_MAX_AGE:
                self.tracker.mark_cold()  # long pause: do not announce stale changes
            self.update_interval = self.poll_settings.active_interval
            await self.async_refresh()
        else:
            LOGGER.info("Polling switched off")
            self.update_interval = None
            self.backoff.success()
            self._degraded_announced = False
            self.async_set_updated_data(self._paused_data())

    def _paused_data(self) -> PetraData:
        if self.data is not None:
            return replace(self.data, paused=True)
        return PetraData(persons={}, updated_at=dt_util.utcnow(), paused=True)

    async def async_save_state(self) -> None:
        """Write the tracker memory now (on unload/reload)."""
        if self.data is not None:
            await self._store.async_save(self.tracker.export_state(self.data.updated_at))

    # -- helpers -----------------------------------------------------------------
    @property
    def token_info(self) -> jwt.TokenInfo | None:
        try:
            return jwt.decode(self.config_entry.data[CONF_TOKEN])
        except jwt.InvalidToken:
            return None

    def _fire(self, event_type: str, data: dict[str, Any]) -> None:
        self.hass.bus.async_fire(event_type, {"config_entry_id": self.config_entry.entry_id, **data})

    async def _telemetry(self, now: datetime, endpoint: str, key: str, data: Any, status: int | None) -> None:
        if self.telemetry is None:
            return
        try:
            await self.hass.async_add_executor_job(
                lambda: self.telemetry.record(now, endpoint, key, data, http_status=status)
            )
            if self._pruned_on != now.date():
                self._pruned_on = now.date()
                await self.hass.async_add_executor_job(self.telemetry.prune, now.date())
        except OSError as err:
            LOGGER.warning("Could not write telemetry: %s", err)

    def _handle_schema(self, issues: list[SchemaIssue], endpoint: str, now: datetime) -> list[SchemaIssue]:
        issues = collapse(issues)
        for issue in issues:
            key = (issue.kind, issue.path, issue.detail)
            if key in self._reported_issues:
                continue
            self._reported_issues.add(key)
            log = LOGGER.warning if issue.severity >= Severity.WARNING else LOGGER.info
            log("Petra API schema drift on %s: %s %s (%s)", endpoint, issue.kind, issue.path, issue.detail)
            self._fire(EVENT_SCHEMA_DRIFT, {"endpoint": endpoint, **issue.as_dict(), "at": now.isoformat()})
        return issues

    def _check_token(self, now: datetime) -> None:
        info = self.token_info
        if info is None or info.expires_at is None:
            return
        left = info.expires_at - now
        if left <= timedelta(days=TOKEN_WARN_DAYS) and self._token_warned_on != now.date():
            self._token_warned_on = now.date()
            self._fire(
                EVENT_TOKEN_EXPIRING,
                {"expires_at": info.expires_at.isoformat(), "days_left": max(left.days, 0)},
            )
        if left <= timedelta(days=REAUTH_DAYS) and self._reauth_started_on != now.date():
            # Ask for a new login before polling breaks (shows "re-authentication
            # required" in Home Assistant; the token keeps working meanwhile).
            self._reauth_started_on = now.date()
            LOGGER.info("Petra token expires %s; asking for a new login", info.expires_at)
            self.config_entry.async_start_reauth(self.hass)

    async def _note_new_persons(self, trips: list[Trip]) -> None:
        changed = False
        for t in trips:
            if t.person_id not in self.known_persons or (
                self.known_persons[t.person_id] is None and t.person_name
            ):
                self.known_persons[t.person_id] = t.person_name
                changed = True
        if changed:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    CONF_PERSONS: {str(k): v for k, v in self.known_persons.items()},
                },
            )

    def _detail_trips(self, trips: list[Trip]) -> list[Trip]:
        active = trips_needing_detail(trips, self.track_onboard)
        return active + [t for t in self.tracker.final_detail_candidates(trips) if t not in active]

    async def _route(self, trips: list[Trip], detail_trips: list[Trip], now: datetime) -> None:
        """Road-route the active trips; on failure their ETA uses the straight line."""
        assert self.router is not None
        errors = [
            err
            for trip in detail_trips
            if (err := await attach_route(
                trip, now, self.router, self.route_cache, track_onboard=self.track_onboard
            ))
        ]
        self.route_cache.prune({t.book_ref for t in trips})
        if errors:
            self._routing_failures += 1
            self._routing_error = errors[-1][:200]
            if self._routing_failures == 1:
                LOGGER.warning(
                    "Routing engine %s failed (%s); using straight-line ETA until it works again",
                    self.router.name, self._routing_error,
                )
        elif self._routing_failures:
            LOGGER.info("Routing engine %s works again", self.router.name)
            self._routing_failures = 0
            self._routing_error = None

    async def _refresh_operator_config(self) -> bool:
        """The client API key was rejected: maybe the operator rotated it."""
        if self._api_key_refreshed:
            return False
        self._api_key_refreshed = True
        cfg = await fetch_operator_config(self.session, self.config_entry.data[CONF_COUNTRY])
        if cfg.api_key == self.config_entry.data[CONF_API_KEY]:
            return False
        LOGGER.info("Operator API key changed; updating configuration")
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_API_KEY: cfg.api_key, CONF_BASE_URL: cfg.base_url},
        )
        self.client = PetraClient(
            self.session, cfg.base_url, cfg.api_key, self.config_entry.data[CONF_TOKEN]
        )
        return True

    # -- polling -----------------------------------------------------------------
    async def _poll(self, now: datetime) -> PetraData:
        start, end = local_day_bounds(now, dt_util.get_default_time_zone())
        res = await self.client.list_orders(self.user_id, start, end)
        await self._telemetry(now, "list", start.date().isoformat(), res.data, res.status)
        issues = self._handle_schema(SchemaValidator().order_list(res.data), "order_list", now)
        if not isinstance(res.data, dict):
            raise PetraEndpointError("order list is not an object")
        trips = parse_order_list(res.data)
        await self._note_new_persons(trips)

        # Active trips, plus trips that just finished (once): their last GPS fix
        # shows whether the trip was ended at the destination or before it.
        detail_trips = self._detail_trips(trips)
        for trip in detail_trips:
            try:
                det = await self.client.order_detail(trip.book_ref)
            except (PetraTransientError, PetraEndpointError) as err:
                # Keep going without live position; the ETA is unknown meanwhile.
                LOGGER.debug("Detail for a trip failed: %s", err)
                continue
            await self._telemetry(now, "detail", str(trip.book_ref), det.data, det.status)
            issues += self._handle_schema(
                SchemaValidator().order_detail(det.data, now=now), "order_detail", now
            )
            if isinstance(det.data, dict):
                trip.apply_detail(det.data)

        if self.router is not None:
            await self._route(trips, detail_trips, now)
        persons, events = self.tracker.update(trips, now, self.known_persons)
        self._store.async_delay_save(functools.partial(self.tracker.export_state, now), 5)
        for ev in events:
            self._fire(f"{DOMAIN}_{ev.type}", ev.data)
        self._check_token(now)

        interval = plan_next_poll(trips, now, self.poll_settings)
        throttle = self.client.last_rate_limit.throttle_delay()
        if throttle is not None and throttle > interval:
            LOGGER.info("Close to the API rate limit; slowing down to %s", throttle)
            interval = throttle
        self.update_interval = interval

        current = [i for i in issues if i.severity >= Severity.WARNING] or issues
        return PetraData(
            persons=persons,
            updated_at=now,
            schema_severity=worst(issues),
            schema_issues=current[:MAX_REPORTED_ISSUES],
            rate_limit_remaining=self.client.last_rate_limit.remaining,
            routing_error=self._routing_error,
        )

    async def _async_update_data(self) -> PetraData:
        if not self.polling_enabled:
            self.update_interval = None
            return self._paused_data()
        data = await self._async_poll_safely()
        if not self.polling_enabled:  # switched off while a request was in flight
            self.update_interval = None
            return replace(data, paused=True)
        return data

    async def _async_poll_safely(self) -> PetraData:
        now = dt_util.utcnow()
        try:
            data = await self._poll(now)
        except PetraAuthError as err:
            raise ConfigEntryAuthFailed(f"Petra token rejected: {err}") from err
        except PetraApiKeyError as err:
            if await self._refresh_operator_config():
                self.update_interval = timedelta(seconds=5)
                raise UpdateFailed("Operator API key was updated; retrying") from err
            raise UpdateFailed(f"Operator API key rejected: {err}") from err
        except PetraEndpointError as err:
            self._handle_schema(
                [SchemaIssue(Severity.BREAKING, "endpoint", "-", str(err)[:200])], "endpoint", now
            )
            return self._failed(now, err)
        except PetraTransientError as err:
            return self._failed(now, err)
        except PetraError as err:
            return self._failed(now, err)

        if self.backoff.failures:
            if self._degraded_announced:
                self._fire(EVENT_CONNECTION_RESTORED, {"failed_for_s": int(self.backoff.failing_for(now).total_seconds())})
            self._degraded_announced = False
            self.backoff.success()
        self._api_key_refreshed = False
        self._last_poll_at = now
        return data

    def _failed(self, now: datetime, err: Exception) -> PetraData:
        retry_after = err.retry_after if isinstance(err, PetraRateLimited) else None
        self.update_interval = self.backoff.failure(now, retry_after)
        failing_for = self.backoff.failing_for(now)
        LOGGER.debug("Poll failed (%s), retrying in %s", err, self.update_interval)
        trip_active = bool(self.data) and any(
            p.phase in ACTIVE_PHASES for p in self.data.persons.values()
        )
        if failing_for >= self.failure_alert_after and not self._degraded_announced:
            self._degraded_announced = True
            LOGGER.warning("Petra API unreachable for %s: %s", failing_for, err)
            self._fire(
                EVENT_CONNECTION_DEGRADED,
                {"failed_for_s": int(failing_for.total_seconds()), "trip_active": trip_active, "error": str(err)[:200]},
            )
        if self.data is None:
            raise UpdateFailed(str(err)) from err
        # Keep showing the last known state, flagged as stale.
        return replace(
            self.data,
            paused=False,
            connection_ok=False,
            failing_since=self.backoff.first_failure_at,
            last_error=str(err)[:200],
        )
