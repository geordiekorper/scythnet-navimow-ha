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

Rebuilds are debounced to one per WATCHDOG_DEBOUNCE, whichever rule asks.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import REST_CACHE_LAG, WATCHDOG_DEBOUNCE
from .health import CollectorHealth

_LOGGER = logging.getLogger(__name__)

# REST states the MQTT state channel never echoes, so disagreeing with them
# says nothing about MQTT: raw strings, and the SDK's catch-all.
IGNORED_REST_RAW = frozenset({"Offline", "offline", "inSoftwareUpdate"})
IGNORED_REST_STATES = frozenset({"unknown"})


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
