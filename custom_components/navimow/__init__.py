"""The Navimow integration."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

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
from mower_sdk.errors import MowerAPIError

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
    REST_POLL_SECONDS,
    CONF_REST_POLL_SECONDS,
)
from .retention import (
    async_check_recorder_retention,
    async_delete_recorder_retention_issue,
)

if sdk_check.PROBLEM is None:
    from mower_sdk.errors import MowerAuthRequiredError
    from mower_sdk.navimow_client import NavimowClient
    from mower_sdk.watchdog import MqttWatchdog

    from .coordinator import NavimowCoordinator
    from .health import CollectorHealth
    from .polls import ObservedMowerAPI
    from .services import async_setup_services, async_unload_services

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


def token_provider(oauth_session: Any, health: "CollectorHealth") -> Callable[[], Awaitable[str | None]]:
    """The client's token provider: Home Assistant's OAuth session.

    The client awaits it before every operation that sends a token (each
    poll, each command, a connect, a rebuild, a credential refresh), so the
    token is refreshed when due and never on a schedule of the client's own.
    A refresh that fails for a transient reason (a network error, say) keeps
    the cached token, so a flaky refresh does not fail a poll; a refresh Home
    Assistant refuses for good raises ConfigEntryAuthFailed, which fails the
    operation and, through the client's on_error, starts reauthentication.
    """

    async def provider() -> str | None:
        try:
            await oauth_session.async_ensure_token_valid()
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:  # noqa: BLE001 - the cached token may still work
            _LOGGER.warning(
                "Token refresh failed (likely transient), keeping the cached token: %s", err
            )
        token = oauth_session.token
        access_token = token.get("access_token") if token else None
        if access_token:
            health.note_token(token.get("expires_at"))
        return access_token

    return provider


def error_handler(hass: HomeAssistant, entry: ConfigEntry, health: "CollectorHealth") -> Callable[[str, Exception], None]:
    """The client's on_error: a failure on its own schedule.

    The client has logged it with its traceback. A poll failure (the clock's
    poll, or the one after a command) shows on collector_status until the
    next poll succeeds; refused credentials, from the cloud or from Home
    Assistant's own token refresh, start reauthentication, the one failure
    only the integration can act on.
    """

    @callback
    def on_error(operation: str, exc: Exception) -> None:
        if operation in ("poll", "command_poll"):
            health.note_poll_error(str(exc) or type(exc).__name__)
        if isinstance(exc, (MowerAuthRequiredError, ConfigEntryAuthFailed)):
            _LOGGER.error("Navimow credentials refused during %s: %s", operation, exc)
            entry.async_start_reauth(hass)

    return on_error


def facade_hooks(sdk: Any, health: "CollectorHealth", coordinators: dict[str, Any]) -> None:
    """Register on the SDK facade what the client does not forward: every
    message seen on a mower's topics, applied or not, for the last_message
    sensor; and every applied location entry, the coordinator's per-entry
    evidence (note_location_entry says why). The facade calls both on the
    event loop, the entry callback after the client's own handler for the
    same entry, so the state built from it has reached the coordinator first.
    """
    sdk.on_message_seen(lambda device_id, channel, received_at: health.note_message(device_id))

    @callback
    def on_location(message: Any) -> None:
        coordinator = coordinators.get(message.device_id)
        if coordinator is not None:
            coordinator.note_location_entry(message)

    sdk.on_location(on_location)


def watchdog_factory(
    health: "CollectorHealth", coordinators: dict[str, Any]
) -> Callable[[Any], Any]:
    """The client's watchdog factory, which the client calls with the facade
    the moment it has built it, before the feed connects: the one point at
    which hooks can be put on the facade so that no message, not even one
    that arrives while the connect is still under way, is missed. It
    registers the facade hooks and returns the SDK's own watchdog with its
    defaults, as the client would have built it."""

    def factory(sdk: Any) -> Any:
        facade_hooks(sdk, health, coordinators)
        return MqttWatchdog(sdk)

    return factory


def status_observer(
    coordinators: dict[str, Any], health: "CollectorHealth"
) -> Callable[[dict[str, Any], Any], None]:
    """Every successful status poll (polls.py): each reply to its mower's
    coordinator, and the poll shown healthy."""

    @callback
    def on_statuses(statuses: dict[str, Any], polled_at: Any) -> None:
        for device_id, status in statuses.items():
            coordinator = coordinators.get(device_id)
            if coordinator is not None:
                coordinator.note_rest_status(status, polled_at)
        health.note_poll_ok()

    return on_statuses


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Navimow from a config entry."""
    if sdk_check.PROBLEM is not None:
        # Not retried: an inconsistent installation does not heal by itself.
        _LOGGER.error("Navimow cannot start: %s", sdk_check.PROBLEM)
        raise ConfigEntryError(sdk_check.PROBLEM)
    hass.data.setdefault(DOMAIN, {})

    try:
        # Get the OAuth2 implementation
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
        if not isinstance(implementation, NavimowOAuth2Implementation):
            raise ConfigEntryAuthFailed("Invalid OAuth2 implementation")

        # Create the OAuth2Session
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

        # The REST client: the client polls through it, and the integration
        # sees every status reply it gets (polls.py).
        api = ObservedMowerAPI(
            session=async_get_clientsession(hass),
            token=access_token,
            base_url=entry.data.get("api_base_url", API_BASE_URL),
        )

        # Discover devices
        try:
            devices = await api.async_get_devices()
            _LOGGER.info("Discovered %d Navimow device(s)", len(devices))
        except MowerAuthRequiredError as err:
            _LOGGER.error("Credentials refused during device discovery: %s", err)
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except MowerAPIError as err:
            _LOGGER.error("Failed to discover devices: %s", err)
            raise ConfigEntryNotReady(f"Failed to discover devices: {err}") from err
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            _LOGGER.error("Authentication failed during device discovery: %s", err)
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err

        if not devices:
            # The client refuses to connect without a device to subscribe,
            # and the broker has never been seen to deliver on a wildcard.
            # Retried, so a mower added to the account appears on its own.
            raise ConfigEntryNotReady("No Navimow devices found")

        # The client: one for the account. It lists nothing (the devices are
        # given), polls the status every poll_interval seconds, runs the MQTT
        # watchdog, rebuilds the connection on a request, fetches the broker
        # credentials again after a refused connection, and merges the two
        # sources into one state per mower. The token comes from Home
        # Assistant through the provider. Built before its callbacks are
        # registered and connected after, so nothing is missed.
        health = CollectorHealth()
        # device id -> coordinator, filled below; the hooks read it later.
        coordinators: dict[str, NavimowCoordinator] = {}
        poll_interval = entry.options.get(CONF_REST_POLL_SECONDS, REST_POLL_SECONDS)
        client = NavimowClient(
            api,
            devices=devices,
            loop=hass.loop,
            token_provider=token_provider(oauth_session, health),
            poll_interval=poll_interval,
            reconnect_min_delay=1,
            reconnect_max_delay=60,
            watchdog=watchdog_factory(health, coordinators),
        )
        health.client = client
        health.poll_interval = poll_interval
        client.on_connection(health.note_connection)
        client.on_error(error_handler(hass, entry, health))

        @callback
        def _on_rebuild_request(request: Any) -> None:
            _LOGGER.warning("MQTT watchdog: %s", request.reason)

        client.on_rebuild_request(_on_rebuild_request)

        for device in devices:
            coordinator = NavimowCoordinator(
                hass=hass, client=client, device=device, config_entry=entry
            )
            coordinator.async_register()
            coordinators[device.id] = coordinator
        api.on_statuses = status_observer(coordinators, health)

        hass.data[DOMAIN][entry.entry_id] = {
            "client": client,
            "health": health,
            "api": api,
            "devices": devices,
            "coordinators": coordinators,
            "oauth_session": oauth_session,
        }

        # The platforms first: the location sensors restore their last
        # states into the coordinators, which hand the record to the client;
        # the client installs it when it builds the decoder, before the first
        # message can arrive. Its connect polls every mower once, so the
        # entities show a state before the feed opens.
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        for coordinator in coordinators.values():
            coordinator.async_finish_restore()
        await _async_connect_or_undo(hass, entry, client)
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
        async_setup_services(hass)
        # A suggestion, as a repair issue the owner can ignore: the entities
        # are the mower's whole record, and 10 days is the recorder's default.
        async_check_recorder_retention(hass, entry)

        return True

    except (ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady):
        raise
    except Exception as err:
        _LOGGER.exception("Error setting up Navimow integration: %s", err)
        raise ConfigEntryNotReady(f"Error setting up integration: {err}") from err


async def _async_connect_or_undo(hass: HomeAssistant, entry: ConfigEntry, client: Any) -> None:
    """Connect the client; a connect that does not complete undoes the setup so far.

    The platforms were forwarded and the entry's data stored before the
    connect (the restore needs that order), so a connect that fails, or is
    cancelled while Home Assistant shuts down or reloads, unloads them again
    and drops the data before the error goes on: the retry then sets up
    cleanly, and no entity outlives a failed setup. Refused credentials, the
    cloud's or Home Assistant's own, become ConfigEntryAuthFailed; anything
    else the cloud did (a request that failed, a credential reply without a
    WebSocket path) is retried as ConfigEntryNotReady.

    The connect is shielded from the cancellation: part of it runs in an
    executor thread (the facade's build and its connect), which a cancelled
    await cannot stop, and the client marks itself closed when its connect
    is interrupted, after which its disconnect does nothing. So a cancelled
    setup lets the connect end on its own (shielded again, so a second
    cancellation does not interrupt the client either), disconnects the
    client when the connect completed, whether before or after the
    cancellation arrived, and only then unwinds. A setup cancelled a second
    time stops waiting; the connect then undoes itself when it completes.
    """
    loop = asyncio.get_running_loop()
    connect = asyncio.ensure_future(client.async_connect())
    try:
        await asyncio.shield(connect)
    except BaseException as err:
        if not connect.done():
            try:
                await asyncio.shield(connect)
            except BaseException:  # noqa: BLE001 - its failure, or a second cancellation
                pass
        if connect.done():
            if not connect.cancelled() and connect.exception() is None:
                # Completed, before or after the cancellation: stop what it started.
                await asyncio.shield(client.async_disconnect())
        else:
            # Cancelled twice: nobody waits any longer, so the connect takes
            # down what it completes, if it completes.
            def _undo_when_done(done: asyncio.Future) -> None:
                if not done.cancelled() and done.exception() is None:
                    loop.create_task(client.async_disconnect())

            connect.add_done_callback(_undo_when_done)
            _LOGGER.warning(
                "Navimow setup cancelled twice; the connect still under way disconnects itself"
            )
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if isinstance(err, MowerAuthRequiredError):
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        if isinstance(err, (ConfigEntryAuthFailed, ConfigEntryNotReady)) or not isinstance(
            err, Exception
        ):
            raise  # as it is: a refusal, or a cancellation
        raise ConfigEntryNotReady(f"Failed to connect to the Navimow cloud: {err}") from err


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A changed option (the poll interval) reaches the client through a
    reload: the client takes its interval at construction. The feed
    reconnects and the entities keep their state through it."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry is deleted: its retention suggestion goes with it."""
    async_delete_recorder_retention_issue(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None) or {}
        client = data.get("client")
        if client is not None:
            # Under the client's own lock: a rebuild or a credential refresh
            # in flight finishes first, then the clock's tasks are cancelled
            # and the MQTT client disconnected.
            try:
                await client.async_disconnect()
            except Exception as err:  # noqa: BLE001 - the unload goes on
                _LOGGER.warning("Error disconnecting from the Navimow cloud: %s", err)
        async_unload_services(hass)

    return unload_ok
