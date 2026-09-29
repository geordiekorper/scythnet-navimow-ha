"""The Navimow integration."""
import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
import logging
from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from mower_sdk.api import MowerAPI
from mower_sdk.errors import MowerAPIError
from mower_sdk.sdk import NavimowSDK

# First: whether the mower_sdk on disk is the distribution the manifest
# requires. The modules below may use names only navimow-sdk-community has,
# so they are imported only when it is; otherwise setup refuses with the
# check's message instead of failing here with an ImportError.
from . import sdk_check
from .auth import NavimowOAuth2Implementation
from .const import (
    DOMAIN,
    CLIENT_ID,
    CLIENT_SECRET,
    API_BASE_URL,
    MQTT_BROKER,
    MQTT_PORT,
    MQTT_USERNAME,
    MQTT_PASSWORD,
    MQTT_KEEPALIVE,
    REST_POLL_SECONDS,
    CONF_REST_POLL_SECONDS,
)

if sdk_check.PROBLEM is None:
    from .coordinator import NavimowCoordinator
    from .services import async_setup_services, async_unload_services
    from .location import location_topic, parse_location_message, strip_sdk_envelope
    from .rejected import raw_message_rejection
    from .health import CollectorHealth, device_id_from_topic
    from .rest_poll import RestPoller
    from .session import MqttSession
    from .watchdog import MqttWatchdog

_LOGGER = logging.getLogger(__name__)
_LOGGER.debug("Navimow module imported (__init__.py)")

PLATFORMS: list[Platform] = [Platform.LAWN_MOWER, Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Navimow component."""
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.debug("Navimow async_setup called, registering OAuth2 implementation")
    # Register OAuth2 implementation so config flow can find it.
    config_entry_oauth2_flow.async_register_implementation(
        hass,
        DOMAIN,
        NavimowOAuth2Implementation(
            hass,
            DOMAIN,
            CLIENT_ID,
            CLIENT_SECRET,
        ),
    )
    return True


def _attach_mqtt_hooks(
    sdk: NavimowSDK,
    health: "CollectorHealth",
    devices: list[Any],
    on_connect_fail: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Report the SDK client's connection events to ``health``.

    The client runs these hooks on the event loop, after it has counted the
    event and recorded its reason (health reads both from the client), for
    every paho client it builds, rebuilt ones included. Attach them before
    connecting, so no event is missed. ``on_connect_fail`` runs after a
    refused connection (the broker credentials refresh); a disconnect only
    changes the health, since paho reconnects with the stored credentials.
    """
    mqtt = sdk.mqtt

    async def _on_connected() -> None:
        health.note_connected()
        _LOGGER.info(
            "MQTT connected callback: broker=%s port=%s ws_path=%s client_id=%s",
            mqtt.broker,
            mqtt.port,
            mqtt.ws_path,
            mqtt.client_id,
        )
        for _d in devices:
            _did = getattr(_d, "id", None)
            if _did:
                try:
                    mqtt.client.subscribe(location_topic(_did))
                except Exception as _err:  # noqa: BLE001
                    _LOGGER.warning("Failed to subscribe location topic: %s", _err)

    async def _on_ready() -> None:
        _LOGGER.info(
            "MQTT ready callback: subscribed to downlink topics on broker=%s port=%s client_id=%s",
            mqtt.broker,
            mqtt.port,
            mqtt.client_id,
        )

    async def _on_disconnected() -> None:
        health.note_disconnected()
        _LOGGER.debug(
            "MQTT disconnected callback: broker=%s port=%s ws_path=%s client_id=%s reason=%s",
            mqtt.broker,
            mqtt.port,
            mqtt.ws_path,
            mqtt.client_id,
            mqtt.last_disconnect_reason,
        )

    async def _on_connect_fail(reason: str) -> None:
        # A refused CONNACK, or no CONNACK at all (a network failure, or the
        # bearer token refused at the WebSocket upgrade); paho keeps retrying.
        health.note_connect_failed()
        _LOGGER.debug("MQTT connect failed: %s", reason)
        if on_connect_fail is not None:
            await on_connect_fail()

    @callback
    def _on_raw(topic: str, _payload: bytes) -> None:
        # Every message the client receives, the reconnect-time empty array
        # included; the client has already timed it.
        device_id = device_id_from_topic(topic)
        if device_id:
            health.note_message(device_id)

    mqtt.on_connected = _on_connected
    mqtt.on_ready = _on_ready
    mqtt.on_disconnected = _on_disconnected
    mqtt.on_connect_fail = _on_connect_fail
    sdk.on_raw(_on_raw)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Navimow from a config entry."""
    if sdk_check.PROBLEM is not None:
        # Not retried: an inconsistent installation does not heal by itself.
        _LOGGER.error("Navimow cannot start: %s", sdk_check.PROBLEM)
        raise ConfigEntryError(sdk_check.PROBLEM)
    hass.data.setdefault(DOMAIN, {})

    def _mask_secret(value: str | None) -> str:
        if not value:
            return "<empty>"
        if len(value) <= 4:
            return "*" * len(value)
        return f"{value[:2]}***{value[-2:]}"

    try:
        # 获取 OAuth2 实现
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
        if not isinstance(implementation, NavimowOAuth2Implementation):
            raise ConfigEntryAuthFailed("Invalid OAuth2 implementation")

        # 创建 OAuth2Session
        oauth_session = config_entry_oauth2_flow.OAuth2Session(
            hass, entry, implementation
        )

        token: dict[str, Any] | None = None
        if hasattr(oauth_session, "async_get_valid_token"):
            try:
                token = await oauth_session.async_get_valid_token()
            except AttributeError:
                token = None
        if not token and hasattr(oauth_session, "async_ensure_token_valid"):
            await oauth_session.async_ensure_token_valid()
            token = oauth_session.token
        if not token and hasattr(oauth_session, "async_get_access_token"):
            access_token_value = await oauth_session.async_get_access_token()
            token = {"access_token": access_token_value} if access_token_value else None
        if not token:
            # Final fallback for older HA versions storing token on the entry.
            token = entry.data.get("token")
        if not token:
            raise ConfigEntryAuthFailed("No valid token available")
        access_token = token.get("access_token")
        if not access_token:
            raise ConfigEntryAuthFailed("No access token in token data")

        # 创建 MowerAPI 实例
        api = MowerAPI(
            session=async_get_clientsession(hass),
            token=access_token,
            base_url=entry.data.get("api_base_url", API_BASE_URL),
        )

        # 发现设备
        try:
            devices = await api.async_get_devices()
            _LOGGER.info("Discovered %d Navimow device(s)", len(devices))
        except MowerAPIError as err:
            _LOGGER.error("Failed to discover devices: %s", err)
            raise ConfigEntryNotReady(f"Failed to discover devices: {err}") from err
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            _LOGGER.error("Authentication failed during device discovery: %s", err)
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err

        if not devices:
            _LOGGER.warning("No Navimow devices found")

        # 获取 MQTT 连接信息并创建 SDK
        try:
            mqtt_info = await api.async_get_mqtt_user_info()
        except MowerAPIError as err:
            _LOGGER.error("Failed to get MQTT info: %s", err)
            raise ConfigEntryNotReady(f"Failed to get MQTT info: {err}") from err

        mqtt_host = mqtt_info.get("mqttHost") or entry.data.get(
            "mqtt_broker", MQTT_BROKER
        )
        mqtt_url = mqtt_info.get("mqttUrl")
        mqtt_username = mqtt_info.get("userName") or entry.data.get(
            "mqtt_username", MQTT_USERNAME
        )
        mqtt_password = mqtt_info.get("pwdInfo") or entry.data.get(
            "mqtt_password", MQTT_PASSWORD
        )
        mqtt_port = 443 if mqtt_url else entry.data.get("mqtt_port", MQTT_PORT)
        ws_path = mqtt_url
        if mqtt_url:
            parsed = urlparse(mqtt_url)
            if parsed.scheme in ("ws", "wss") and parsed.hostname:
                if not mqtt_host:
                    mqtt_host = parsed.hostname
                if parsed.port:
                    mqtt_port = parsed.port
                ws_path = parsed.path or "/"
                if parsed.query:
                    ws_path = f"{ws_path}?{parsed.query}"
        auth_headers = {"Authorization": f"Bearer {access_token}"} if ws_path else None

        _LOGGER.info(
            "MQTT connection parameters: broker=%s port=%s mqtt_url=%s ws_path=%s username=%s password=%s auth_header=%s",
            mqtt_host,
            mqtt_port,
            mqtt_url,
            ws_path,
            _mask_secret(mqtt_username),
            _mask_secret(mqtt_password),
            "Bearer <masked>" if auth_headers else "<none>",
        )

        _location_cache: dict[str, dict] = {}
        _location_coordinators: dict[str, Any] = {}
        # Location messages that arrive before the sensors have restored
        # their last states wait here, and are parsed once restore is done:
        # parsed earlier, a late one would fill the cache, restore would
        # then skip the newer restored group, and its time could not guard.
        # (Bounded: a pose every 2 s, and setup takes seconds.)
        _location_backlog: deque[tuple[str, str, str, str]] = deque(maxlen=500)
        _location_ready: list[bool] = [False]

        def _process_location(topic: str, payload_text: str, device_id: str, received_at: str) -> None:
            try:
                _data = strip_sdk_envelope(json.loads(payload_text), device_id)
            except (ValueError, TypeError):
                _data = None
            # One snapshot per entry, published in order, so every
            # pose of a multi-pose message reaches the recorder.
            _coord = _location_coordinators.get(device_id)
            _parsed = parse_location_message(
                _location_cache, device_id, _data, received_at=received_at
            )
            if _coord is not None:
                for _loc in _parsed.snapshots:
                    hass.loop.call_soon_threadsafe(_coord.ingest_location, _loc)
                if _parsed.reason is not None:
                    # The whole message, once, with every reason.
                    hass.loop.call_soon_threadsafe(
                        _coord.record_rejected, "location", topic,
                        _parsed.reason, payload_text, list(_parsed.reasons),
                    )
        _mqtt_refresh_lock = asyncio.Lock()
        # 用列表作为可变标志容器，使 async_unload_entry（不同函数作用域）可以修改它
        _unload_flag: list[bool] = [False]

        def _wrap_on_message(sdk: NavimowSDK) -> None:
            mqtt = sdk.mqtt
            original_on_message = mqtt.on_message

            async def _on_message(topic: str, payload: bytes, device_id: str) -> None:
                payload_text = (payload or b"").decode("utf-8", errors="replace")
                _LOGGER.debug(
                    "MQTT message received: topic=%s bytes=%d device=%s payload=%s",
                    topic,
                    len(payload or b""),
                    device_id,
                    payload_text,
                )
                if device_id and topic.endswith("/realtimeDate/location"):
                    item = (topic, payload_text, device_id, dt_util.utcnow().isoformat())
                    if not _location_ready[0]:
                        _location_backlog.append(item)
                        return
                    _process_location(*item)
                    return
                # The SDK decodes the other channels, but only the fields it
                # knows, and nothing here uses the event or attributes
                # channels; record what would otherwise be lost.
                _coord = _location_coordinators.get(device_id)
                _channel = topic.rsplit("/", 1)[-1]
                _reason = raw_message_rejection(_channel, payload_text)
                if _channel == "state" and _coord is not None:
                    try:
                        _stamp_reason = _coord.check_raw_state(json.loads(payload_text))
                    except ValueError:
                        _stamp_reason = None
                    if _stamp_reason is not None:
                        # Late or implausibly stamped: recorded as received,
                        # and never handed to the SDK, so it cannot become
                        # the current state or the SDK's cached one.
                        _coord.record_rejected(
                            _channel, topic, _stamp_reason, payload_text,
                            [_stamp_reason] + ([_reason] if _reason else []),
                        )
                        return
                if _reason is not None and _coord is not None:
                    hass.loop.call_soon_threadsafe(
                        _coord.record_rejected, _channel, topic, _reason, payload_text
                    )
                if original_on_message is not None:
                    await original_on_message(topic, payload, device_id)

            mqtt.on_message = _on_message

        async def _probe_mqtt_status(sdk: NavimowSDK) -> None:
            await asyncio.sleep(5)
            _LOGGER.info("MQTT status probe (5s): connected=%s", sdk.is_connected)
            await asyncio.sleep(25)
            _LOGGER.info("MQTT status probe (30s): connected=%s", sdk.is_connected)

        def _create_sdk(api: MowerAPI) -> NavimowSDK:
            sdk = NavimowSDK(
                broker=mqtt_host,
                port=mqtt_port,
                username=mqtt_username,
                password=mqtt_password,
                ws_path=ws_path,
                auth_headers=auth_headers,
                loop=hass.loop,
                records=devices,
                keepalive_seconds=MQTT_KEEPALIVE,
                reconnect_min_delay=1,
                reconnect_max_delay=60,
            )
            return sdk

        # Built without connecting (building sets up TLS, which blocks), so
        # the hooks are in place before the first event.
        sdk = await hass.async_add_executor_job(_create_sdk, api)
        health = CollectorHealth(sdk.mqtt)
        session = MqttSession(
            hass, sdk, api, oauth_session, health, _unload_flag, _mqtt_refresh_lock, access_token
        )
        _attach_mqtt_hooks(sdk, health, devices, session.async_refresh_credentials)
        _wrap_on_message(sdk)
        _LOGGER.info(
            "Invoking SDK MQTT connect: broker=%s port=%s ws_path=%s",
            mqtt_host,
            mqtt_port,
            ws_path,
        )
        await session.start()
        hass.async_create_task(_probe_mqtt_status(sdk))

        coordinators: dict[str, NavimowCoordinator] = {}
        for device in devices:
            coordinator = NavimowCoordinator(
                hass=hass,
                sdk=sdk,
                api=api,
                device=device,
                oauth_session=oauth_session,
                config_entry=entry,
                location_cache=_location_cache,
            )
            coordinator.health = health
            coordinator.mqtt_session = session
            await coordinator.async_setup()
            await coordinator.async_config_entry_first_refresh()
            coordinators[device.id] = coordinator
            _location_coordinators[device.id] = coordinator

        # Steady REST status poll for all of this entry's mowers, independent
        # of MQTT health (the coordinators' own fetch is only a fallback).
        rest_poller: RestPoller | None = None
        if coordinators:
            first = next(iter(coordinators.values()))
            rest_poller = RestPoller(
                hass, api, coordinators,
                entry.options.get(CONF_REST_POLL_SECONDS, REST_POLL_SECONDS),
                first._async_ensure_valid_token,
            )
            health.poller = rest_poller
            watchdog = MqttWatchdog(hass, health, coordinators, session.async_rebuild)

            @callback
            def _on_poll_result(ok: bool) -> None:
                health.note_poll()
                if ok:  # the mismatch rule needs this poll's replies
                    watchdog.async_check_after_poll()

            rest_poller.on_result = _on_poll_result
            watchdog.async_start()
            entry.async_on_unload(watchdog.async_stop)
            for coordinator in coordinators.values():
                coordinator.rest_poller = rest_poller
            rest_poller.async_start()
            entry.async_on_unload(rest_poller.async_stop)
            entry.async_on_unload(entry.add_update_listener(_async_options_updated))

        # 存储数据
        hass.data[DOMAIN][entry.entry_id] = {
            "rest_poller": rest_poller,
            "health": health,
            "session": session,
            "sdk": sdk,
            "api": api,
            "devices": devices,
            "coordinators": coordinators,
            "oauth_session": oauth_session,
            "unload_flag": _unload_flag,
            "mqtt_lock": _mqtt_refresh_lock,
        }

        # 转发到平台
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # The sensors have restored their last states: parse what waited.
        _location_ready[0] = True
        for item in _location_backlog:
            _process_location(*item)
        _location_backlog.clear()
        async_setup_services(hass)

        return True

    except (ConfigEntryAuthFailed, ConfigEntryError):
        raise
    except Exception as err:
        _LOGGER.exception("Error setting up Navimow integration: %s", err)
        raise ConfigEntryNotReady(f"Error setting up integration: {err}") from err


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply a changed poll interval to the running poller, without a reload."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    poller = data.get("rest_poller")
    if poller is not None:
        poller.async_set_interval(entry.options.get(CONF_REST_POLL_SECONDS, REST_POLL_SECONDS))
        if data.get("health") is not None:
            data["health"].note_settings_changed()  # collector_status shows it


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # 清理数据
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            data = hass.data[DOMAIN][entry.entry_id]
            # 标记正在卸载，阻止断连回调触发新的凭据刷新
            if "unload_flag" in data:
                data["unload_flag"][0] = True
            sdk = data.get("sdk")
            if sdk:
                # A rebuild or credential refresh in flight holds this lock
                # and may be about to start a new client: let it finish, so
                # the client disconnected here is the last one.
                lock = data.get("mqtt_lock")
                if lock is not None:
                    await lock.acquire()
                try:
                    sdk.disconnect()
                except Exception as err:
                    _LOGGER.warning("Error disconnecting MQTT: %s", err)
                finally:
                    if lock is not None:
                        lock.release()

            hass.data[DOMAIN].pop(entry.entry_id)
        async_unload_services(hass)

    return unload_ok


