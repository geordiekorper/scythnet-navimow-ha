"""MQTT watchdog: rebuild a connection that is up but no longer delivering.

The keepalive catches a link that died outright. It cannot catch a broker
that has stopped delivering to a client whose link is still up, which the
Navimow broker has been seen to do. Two rules from Scythnet (collector.py)
detect that from the data itself:

1. After a REST poll: REST is current (the last MQTT state report is older
   than the REST cache lag), it disagrees with that report, and its state is
   one MQTT could have echoed. MQTT missed a transition. Acted on once per
   MQTT report, so a mower that stays offline does not cause a rebuild on
   every poll.
2. Every CHECK_SECONDS: a mower that is out sends a pose every two seconds,
   so LOCATION_SILENCE seconds without a location message, from a mower
   that has reported a position before, while it mows or returns (shown or
   REST state) and the client says it is connected, means the broker has
   stopped delivering. The state channel is too quiet to tell. A docked
   mower is quiet by design and never triggers this.

Rebuilds are debounced to one per WATCHDOG_DEBOUNCE, whichever rule asks.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from datetime import timedelta

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import LOCATION_SILENCE, REST_CACHE_LAG, WATCHDOG_DEBOUNCE
from .health import CollectorHealth

_LOGGER = logging.getLogger(__name__)

# REST states the MQTT state channel never echoes, so disagreeing with them
# says nothing about MQTT: raw strings, and the SDK's catch-all.
IGNORED_REST_RAW = frozenset({"Offline", "offline", "inSoftwareUpdate"})
IGNORED_REST_STATES = frozenset({"unknown"})
# States in which a mower that is out sends a pose every two seconds.
MOVING_STATES = frozenset({"mowing", "returning"})
CHECK_SECONDS = 30


class MqttWatchdog:
    """Watches one config entry's mowers and asks for a rebuild when MQTT
    has evidently stopped delivering."""

    def __init__(
        self,
        hass: HomeAssistant,
        health: CollectorHealth,
        coordinators: dict[str, Any],
        rebuild: Callable[[str], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hass = hass
        self.health = health
        self.coordinators = coordinators
        self._rebuild = rebuild
        self._clock = clock
        self._last_rebuild: float | None = None
        # device id -> the MQTT report a rebuild was already made for
        self._acted_on: dict[str, Any] = {}
        self._cancel_timer: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        """Start the periodic location-silence check (rule 2)."""
        # The handler must be a @callback: HA runs a plain function passed
        # to a timer in an executor thread, where the rebuild task could
        # not be created.
        self._cancel_timer = async_track_time_interval(
            self.hass, self._on_timer, timedelta(seconds=CHECK_SECONDS)
        )

    @callback
    def _on_timer(self, _now: Any) -> None:
        self.async_check_silence()

    @callback
    def async_stop(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    def _debounced(self, now: float) -> bool:
        return self._last_rebuild is not None and now - self._last_rebuild < WATCHDOG_DEBOUNCE

    @callback
    def _request_rebuild(self, now: float, reason: str) -> None:
        self._last_rebuild = now
        _LOGGER.warning("MQTT watchdog: %s", reason)
        self.hass.async_create_task(self._rebuild(reason))

    @callback
    def async_check_after_poll(self) -> None:
        """Rule 1, run after every REST poll."""
        now = self._clock()
        mismatches: list[tuple[str, Any, str]] = []
        for device_id, coordinator in self.coordinators.items():
            view = coordinator.get_watch_view(now)
            mqtt_state, rest_state = view["mqtt_state"], view["rest_state"]
            if mqtt_state is None or rest_state is None:
                continue
            if mqtt_state == rest_state:
                self._acted_on.pop(device_id, None)
                continue
            if rest_state in IGNORED_REST_STATES or view["rest_raw_state"] in IGNORED_REST_RAW:
                continue
            if view["mqtt_age"] is None or view["mqtt_age"] < REST_CACHE_LAG:
                continue  # REST may simply not have caught up yet
            if self._acted_on.get(device_id) == view["mqtt_key"]:
                continue
            mismatches.append((
                device_id, view["mqtt_key"],
                f"REST says {rest_state} but MQTT last said {mqtt_state} for {view['name']}",
            ))
        if not mismatches or not self.health.connects or self._debounced(now):
            # Not acted on inside the debounce window: the mismatch stays
            # live for the next poll, since the missed transition may never
            # come to clear it.
            return
        for device_id, key, _ in mismatches:
            self._acted_on[device_id] = key
        self._request_rebuild(now, f"missed a state change ({mismatches[0][2]})")

    @callback
    def async_check_silence(self) -> None:
        """Rule 2: a connected client that delivers nothing while a mower runs."""
        health = self.health
        now = self._clock()
        if not health.connected or health.connected_monotonic is None or self._debounced(now):
            return  # not connected is the reconnect path's business
        for device_id, coordinator in self.coordinators.items():
            view = coordinator.get_watch_view(now)
            if not view["has_pose"]:
                continue
            if view["shown_state"] not in MOVING_STATES and view["rest_state"] not in MOVING_STATES:
                continue
            # Nothing can have arrived before this client connected, and no
            # location message yet counts as quiet since the connect.
            quiet_for = now - health.connected_monotonic
            location_age = health.location_age(device_id)
            if location_age is not None:
                quiet_for = min(quiet_for, location_age)
            if quiet_for >= LOCATION_SILENCE:
                self._request_rebuild(
                    now, f"no location message for {int(quiet_for)} s while {view['name']} runs"
                )
                return
