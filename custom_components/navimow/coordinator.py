"""DataUpdateCoordinator for Navimow integration."""
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from mower_sdk.api import MowerAPI
from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceStateMessage,
    DeviceStatus,
)
from mower_sdk.sdk import NavimowSDK

from .const import (
    DOMAIN,
    HTTP_FALLBACK_MIN_INTERVAL,
    MQTT_STALE_SECONDS,
    UPDATE_INTERVAL,
)
from .location import DOCKED_STATES, update_dock_estimate

_LOGGER = logging.getLogger(__name__)


class NavimowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Navimow data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        sdk: NavimowSDK,
        api: MowerAPI,
        device: Device,
        oauth_session: config_entry_oauth2_flow.OAuth2Session | None = None,
        config_entry: ConfigEntry | None = None,
        location_cache: dict[str, dict] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.sdk = sdk
        self.api = api
        self.device = device
        self.oauth_session = oauth_session
        # The per-device merge cache the MQTT location parser writes to;
        # restored sensor states are seeded into it so live entries merge
        # over them field by field.
        self.location_cache = location_cache
        self.data: dict[str, Any] = {}
        self._last_state: DeviceStateMessage | None = None
        self._last_attributes: DeviceAttributesMessage | None = None
        self._last_location: dict[str, Any] | None = None
        self._dock: dict[str, Any] | None = None  # learned {"x","y","n"}
        self._last_mqtt_update: float | None = None
        self._last_http_fetch: float | None = None
        # Source of _last_state: "mqtt_push", "mqtt_cache" or "http_fallback".
        self._last_data_source: str | None = None
        # The two sources, kept apart. _mqtt_state is the last MQTT message
        # adopted (by identity, so a poll that finds the same object in the
        # SDK cache does not adopt it again); _rest_status is the raw REST
        # result including the fields the SDK keeps in DeviceStatus.extra.
        self._mqtt_state: DeviceStateMessage | None = None
        self._mqtt_received_at: str | None = None
        self._rest_status: DeviceStatus | None = None
        self._rest_polled_at: str | None = None

    async def async_setup(self) -> None:
        """Register callbacks from SDK."""
        self.sdk.on_state(self._handle_state)
        self.sdk.on_attributes(self._handle_attributes)

    def _build_data(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "state": self._last_state,
            "attributes": self._last_attributes,
            "location": self._last_location,
            "meta": {
                "last_data_source": self._last_data_source,
                "last_mqtt_update_monotonic": self._last_mqtt_update,
                "last_http_fetch_monotonic": self._last_http_fetch,
            },
        }

    def _device_status_to_state(self, status: DeviceStatus) -> DeviceStateMessage:
        error: dict[str, Any] | None = None
        if status.error_code and status.error_code.value != "none":
            error = {
                "code": status.error_code.value,
                "message": status.error_message,
            }
        return DeviceStateMessage(
            device_id=status.device_id,
            timestamp=status.timestamp,
            state=status.status.value,
            battery=status.battery,
            signal_strength=status.signal_strength,
            position=status.position,
            error=error,
            metrics=None,
        )

    async def _async_ensure_valid_token(self) -> str | None:
        if not self.oauth_session:
            return None
        try:
            token: dict[str, Any] | None
            if hasattr(self.oauth_session, "async_ensure_token_valid"):
                await self.oauth_session.async_ensure_token_valid()
                token = self.oauth_session.token
            elif hasattr(self.oauth_session, "async_get_valid_token"):
                token = await self.oauth_session.async_get_valid_token()
            else:
                token = self.oauth_session.token
        except ConfigEntryAuthFailed:
            # 确定性认证失败（refresh_token 缺失或被服务端拒绝）→ 直接上报，让 HA 引导用户重新认证
            raise
        except Exception as err:
            # 瞬态错误（网络超时、DNS 等）→ 不立即触发重新认证流程。
            # 尝试沿用缓存中的 access_token；若缓存也不可用才升级为认证失败。
            _LOGGER.warning(
                "Token refresh failed (likely transient), falling back to cached token: %s", err
            )
            cached = getattr(self.oauth_session, "token", None)
            if cached and cached.get("access_token"):
                token = cached
            else:
                raise ConfigEntryAuthFailed(
                    f"Token refresh failed and no cached token available: {err}"
                ) from err
        if not token or not token.get("access_token"):
            raise ConfigEntryAuthFailed("No access token after refresh")
        access_token = token["access_token"]
        self.api.set_token(access_token)
        return access_token

    async def _async_update_data(self) -> dict[str, Any]:
        # 每次 update 都主动刷新 token，确保 api._token 与 oauth_session 保持同步。
        # 若仅在 HTTP fallback 时刷新，MQTT 正常推数据期间 token 长期不更新，
        # 过期后用户下发指令会立即收到 CODE_OAUTH_INFO_ILLEGAL。
        try:
            await self._async_ensure_valid_token()
        except ConfigEntryAuthFailed:
            raise

        cached_state = self.sdk.get_cached_state(self.device.id)
        if cached_state is not None and cached_state is not self._mqtt_state:
            # A message the callback did not deliver (it arrived before the
            # callback was registered). Adopt it once; later polls that find
            # the same object leave the state and its source label alone.
            self._adopt_mqtt_state(cached_state, "mqtt_cache")

        cached_attrs = self.sdk.get_cached_attributes(self.device.id)
        if cached_attrs is not None:
            self._last_attributes = cached_attrs

        now = time.monotonic()
        is_mqtt_stale = (
            self._last_mqtt_update is None
            or now - self._last_mqtt_update > MQTT_STALE_SECONDS
        )
        can_http_fetch = (
            self._last_http_fetch is None
            or now - self._last_http_fetch > HTTP_FALLBACK_MIN_INTERVAL
        )
        if is_mqtt_stale and can_http_fetch:
            try:
                status = await self.api.async_get_device_status(self.device.id)
                self._rest_status = status
                self._rest_polled_at = dt_util.utcnow().isoformat()
                self._last_state = self._device_status_to_state(status)
                self._last_http_fetch = now
                self._last_data_source = "http_fallback"
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "HTTP fallback failed for device %s: %s", self.device.id, err
                )

        _LOGGER.debug(
            "Coordinator update: device=%s source=%s mqtt_ts=%s http_ts=%s",
            self.device.id,
            self._last_data_source,
            self._last_mqtt_update,
            self._last_http_fetch,
        )
        self.data = self._build_data()
        return self.data

    def _handle_state(self, state: DeviceStateMessage) -> None:
        if state.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT state received: device=%s state=%s battery=%s",
            state.device_id,
            state.state,
            state.battery,
        )
        self._last_mqtt_update = time.monotonic()
        received_at = dt_util.utcnow().isoformat()
        self.hass.loop.call_soon_threadsafe(
            self._update_from_state, state, received_at
        )

    def _handle_attributes(self, attrs: DeviceAttributesMessage) -> None:
        if attrs.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT attributes received: device=%s keys=%d",
            attrs.device_id,
            len(getattr(attrs, "__dict__", {}) or {}),
        )
        self._last_mqtt_update = time.monotonic()
        self.hass.loop.call_soon_threadsafe(self._update_from_attributes, attrs)

    def _update_from_state(
        self, state: DeviceStateMessage, received_at: str | None = None
    ) -> None:
        self._adopt_mqtt_state(state, "mqtt_push", received_at)
        self.async_set_updated_data(self._build_data())

    def _adopt_mqtt_state(
        self, state: DeviceStateMessage, source: str, received_at: str | None = None
    ) -> None:
        """Make an MQTT state message the current device state.

        ``received_at`` is when HA received the message; for a message found
        in the SDK cache it is the poll that found it.
        """
        self._mqtt_state = state
        self._mqtt_received_at = received_at or dt_util.utcnow().isoformat()
        self._last_state = state
        self._last_data_source = source

    def get_data_source(self) -> str:
        """Source of the current device state: mqtt_push, mqtt_cache, http_fallback or none."""
        return self._last_data_source or "none"

    def get_source_details(self) -> dict[str, Any]:
        """The last MQTT state message and the last REST poll, side by side.

        Device timestamps (``mqtt_timestamp``, ``rest_timestamp``) are what
        the source supplied and stay None when it supplied nothing; HA's
        own clock is only ever in ``mqtt_received_at`` / ``rest_polled_at``.
        """
        mqtt = self._mqtt_state
        rest = self._rest_status
        extra = (rest.extra if rest else None) or {}
        return {
            "mqtt_state": mqtt.state if mqtt else None,
            "mqtt_raw_state": (mqtt.metrics or {}).get("raw_state") if mqtt else None,
            "mqtt_battery": mqtt.battery if mqtt else None,
            "mqtt_timestamp": mqtt.timestamp if mqtt else None,
            "mqtt_received_at": self._mqtt_received_at,
            "rest_status": rest.status.value if rest else None,
            "rest_vehicle_state": extra.get("vehicleState"),
            "rest_battery": rest.battery if rest else None,
            "rest_battery_level": extra.get("descriptiveCapacityRemaining"),
            "rest_timestamp": rest.timestamp if rest else None,
            "rest_polled_at": self._rest_polled_at,
        }

    def _update_from_attributes(self, attrs: DeviceAttributesMessage) -> None:
        self._last_attributes = attrs
        self.async_set_updated_data(self._build_data())

    def ingest_location(self, location: dict) -> None:
        if not isinstance(location, dict):
            return
        if location.get("device_id") not in (None, self.device.id):
            return
        self._last_location = location
        self._maybe_learn_dock(location)
        self.async_set_updated_data(self._build_data())

    def restore_location(self, group: str, fields: dict[str, Any]) -> None:
        """Seed the shared location cache from a sensor's last recorded state.

        Live data wins: nothing is written for a group whose keys are already
        in the cache. A restored group carries ``<group>_restored: True``
        until the parser sees the first live entry of that type.
        """
        cache = self.location_cache
        if cache is None or not fields:
            return
        loc = dict(cache.get(self.device.id) or {"device_id": self.device.id})
        if any(key in loc for key in fields):
            return
        loc.update(fields)
        loc[f"{group}_restored"] = True
        cache[self.device.id] = loc
        self._last_location = loc
        self.async_set_updated_data(self._build_data())

    def _maybe_learn_dock(self, location: dict) -> None:
        """Average pose samples into the dock estimate while docked/charging."""
        if location.get("pose_restored"):
            return  # a restored pose is not a fresh sample
        state = self._last_state
        status = (state.state or "").lower() if state else ""
        x, y = location.get("x"), location.get("y")
        if status in DOCKED_STATES and x is not None and y is not None:
            self._dock = update_dock_estimate(self._dock, x, y)

    def get_dock_position(self) -> dict | None:
        """Learned dock position {"x","y","n"}, or None if never seen docked."""
        return self._dock

    def get_device_location(self) -> dict | None:
        return self.data.get("location")

    def get_device_state(self) -> DeviceStateMessage | None:
        return self.data.get("state")

    def get_device_attributes(self) -> DeviceAttributesMessage | None:
        return self.data.get("attributes")

    def get_device_info(self) -> Any | None:
        return self.data.get("device")
