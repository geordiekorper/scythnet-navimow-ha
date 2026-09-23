"""Steady REST status polling.

The MQTT state channel is event-driven and some models only send on a state
change, so the REST status endpoint is the second source of state and
battery, the only source of the descriptive battery level, and the check on
whether MQTT missed a transition. One poll per config entry covers all of its
mowers in a single getVehicleStatus call, independent of MQTT health.

The cloud answers from a cache that lags a minute or two, and its rate limit
is not documented; 120 s is what Scythnet and NaviWatch run without tripping
it. A failed poll backs off (doubling up to REST_POLL_MAX_BACKOFF) until one
succeeds.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from mower_sdk.errors import MowerAPIError

from .const import REST_POLL_MAX_BACKOFF, REST_POLL_MIN_SECONDS

if TYPE_CHECKING:
    from mower_sdk.api import MowerAPI

    from .coordinator import NavimowCoordinator

_LOGGER = logging.getLogger(__name__)

STATUS_ENDPOINT = "/openapi/smarthome/getVehicleStatus"
FIRST_POLL_DELAY = 5


async def async_fetch_statuses(api: MowerAPI, device_ids: list[str]) -> list[dict[str, Any]]:
    """The raw status entries for ``device_ids``, one getVehicleStatus call.

    MowerAPI.async_get_device_statuses keeps only the fields the SDK models,
    so this reads the reply itself: a field the cloud starts sending must
    reach the rest_status sensor. Raises MowerAPIError like the SDK does.
    """
    response = await api._async_request(  # noqa: SLF001 - see docstring
        "POST", STATUS_ENDPOINT, data={"devices": [{"id": d} for d in device_ids]}
    )
    if response.get("code") != 1:
        raise MowerAPIError(f"getVehicleStatus failed: {response.get('desc')}")
    devices = ((response.get("data") or {}).get("payload") or {}).get("devices") or []
    return [d for d in devices if isinstance(d, dict)]


class RestPoller:
    """Polls REST status for one config entry's mowers on a fixed interval."""

    _clock = staticmethod(time.monotonic)

    def __init__(
        self,
        hass: HomeAssistant,
        api: MowerAPI,
        coordinators: dict[str, NavimowCoordinator],
        interval: int,
        ensure_token: Callable[[], Awaitable[Any]],
    ) -> None:
        self.hass = hass
        self.api = api
        self.coordinators = coordinators
        self.interval = max(REST_POLL_MIN_SECONDS, int(interval))
        self._ensure_token = ensure_token
        self._cancel: CALLBACK_TYPE | None = None
        self._due_in: float | None = None
        self._due_at: float | None = None  # monotonic deadline of the pending poll
        self._polling = False
        self._requested_at: float | None = None  # asked for while polling
        self._next_delay: float = self.interval
        self._failures = 0
        self._stopped = True
        # Outcome of the latest poll, for diagnostics, and who to tell.
        self.last_poll_at: str | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        # Called after every poll with whether it succeeded; a successful
        # poll's replies have been applied by then.
        self.on_result: Callable[[bool], None] | None = None

    @callback
    def async_start(self) -> None:
        """Start polling. The first poll runs FIRST_POLL_DELAY seconds from
        now: setup's own status fetch goes through the SDK's model, so until
        this poll the rest_status sensor has no reply as sent to show."""
        self._stopped = False
        self._schedule(FIRST_POLL_DELAY)

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        self._requested_at = None
        self._cancel_timer()

    @callback
    def async_set_interval(self, interval: int) -> None:
        """Change the interval; the next poll is rescheduled from now (or
        from the end of the poll under way)."""
        self.interval = max(REST_POLL_MIN_SECONDS, int(interval))
        if not self._stopped and not self._polling:
            self._schedule(self.interval)

    @callback
    def async_request_poll(self, delay: float) -> None:
        """Poll ``delay`` seconds from now, unless one is already due sooner.
        A request made while a poll is under way is kept for when it ends."""
        if self._stopped:
            return
        due_at = self._clock() + delay
        if self._polling:
            if self._requested_at is None or due_at < self._requested_at:
                self._requested_at = due_at
            return
        if self._due_at is None or due_at < self._due_at:
            self._schedule(delay)

    @property
    def next_delay(self) -> float | None:
        """Delay the pending poll was scheduled with, or None."""
        return self._due_in

    @callback
    def _cancel_timer(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None
        self._due_in = None
        self._due_at = None

    @callback
    def _schedule(self, delay: float) -> None:
        self._cancel_timer()
        self._due_in = delay
        self._due_at = self._clock() + delay
        self._cancel = async_call_later(self.hass, delay, self._fire)

    @callback
    def _fire(self, _now: Any) -> None:
        self._cancel = None
        self._due_in = None
        self._due_at = None
        self.hass.async_create_task(self.async_poll())

    @callback
    def _schedule_after_poll(self, delay: float) -> None:
        """Schedule the next poll, keeping a sooner one requested meanwhile."""
        if self._stopped:
            return
        if self._requested_at is not None:
            delay = min(delay, max(0.0, self._requested_at - self._clock()))
            self._requested_at = None
        self._schedule(delay)

    async def async_poll(self) -> None:
        """Poll once, apply the replies, and schedule the next poll. Only one
        poll runs at a time; a poll due while one is under way runs as soon
        as it ends, so replies are never applied out of order."""
        if self._polling:
            self.async_request_poll(0)
            return
        self._polling = True
        try:
            await self._async_poll_once()
        finally:
            self._polling = False
        self._schedule_after_poll(self._next_delay)

    async def _async_poll_once(self) -> None:
        device_ids = list(self.coordinators)
        try:
            await self._ensure_token()
            statuses = await async_fetch_statuses(self.api, device_ids)
        except Exception as err:  # noqa: BLE001 - any failure backs off
            self._failures += 1
            self.last_error = str(err) or type(err).__name__
            self.last_error_at = dt_util.utcnow().isoformat()
            delay = min(self.interval * 2 ** self._failures, max(self.interval, REST_POLL_MAX_BACKOFF))
            _LOGGER.warning(
                "REST status poll failed (%d in a row), next in %d s: %s",
                self._failures, delay, self.last_error,
            )
            self._next_delay = delay
            if self.on_result is not None:
                self.on_result(False)
            return
        self._failures = 0
        self.last_error = None
        self.last_error_at = None
        self.last_poll_at = dt_util.utcnow().isoformat()
        for raw in statuses:
            coordinator = self.coordinators.get(str(raw.get("id") or raw.get("device_id") or ""))
            if coordinator is None:
                _LOGGER.debug("REST status for an unknown device: %s", raw.get("id"))
                continue
            coordinator.apply_rest_status(raw, self.last_poll_at)
        self._next_delay = self.interval
        if self.on_result is not None:
            self.on_result(True)
