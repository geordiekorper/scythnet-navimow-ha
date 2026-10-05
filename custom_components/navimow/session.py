"""The entry's MQTT session: when the client connects, and with which credentials.

Three rules, from how the Navimow cloud behaves:

- The broker credentials (userName and pwdInfo from the MQTT user-info
  endpoint) are fetched again only after the broker refused a connection,
  never on a disconnect or a timer: the endpoint allows about one call a
  minute. The SDK's async_refresh_broker_credentials fetches at most once
  per 65 s, one call at a time, and applies the result; paho's thread,
  still retrying, uses it at its next attempt. A disconnect needs nothing:
  paho reconnects with the stored values.
- The OAuth bearer header the WebSocket upgrade carries is pushed to the
  client after every token refresh, so the next connect uses the current
  token. While connected, the SDK sets it on the live client and keeps the
  connection.
- start() is the only place that connects, and nothing is applied to the
  client before it: on the SDK, a credential applied to a client that is
  not connected rebuilds and connects it. A bearer pushed before start()
  waits and is applied by start().

The lock serialises the refresh, the rebuild and the push, and the entry's
unload acquires it too, so an operation in flight finishes before the
client is disconnected; unload_flag[0] is set when the entry unloads.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from homeassistant.core import HomeAssistant
from mower_sdk.errors import MowerAPIError

_LOGGER = logging.getLogger(__name__)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class MqttSession:
    """One config entry's MQTT client lifecycle around the SDK facade."""

    def __init__(
        self,
        hass: HomeAssistant,
        sdk: Any,
        api: Any,
        oauth_session: Any,
        health: Any,
        unload_flag: list[bool],
        lock: asyncio.Lock,
        token: str | None,
    ) -> None:
        self.hass = hass
        self.sdk = sdk
        self.api = api
        self.oauth_session = oauth_session
        self.health = health
        self.unload_flag = unload_flag
        self.lock = lock
        # The access token whose bearer the client carries: the one the SDK
        # was constructed with, then each one applied.
        self._applied_token = token
        self._pending_token: str | None = None
        self._started = False

    @property
    def _unloading(self) -> bool:
        return self.unload_flag[0]

    async def start(self) -> None:
        """Connect, with a bearer pushed before now applied first."""
        async with self.lock:
            if self._unloading or self._started:
                return
            self._started = True
            pending, self._pending_token = self._pending_token, None
            if pending is not None:
                # On a client that never connected this rebuilds and connects.
                await self.hass.async_add_executor_job(
                    partial(self.sdk.update_mqtt_credentials, auth_headers=bearer(pending))
                )
                self._applied_token = pending
                self.health.note_rebuild()
            # A no-op when the update above already connected.
            await self.hass.async_add_executor_job(self.sdk.connect)

    async def _async_fresh_token(self) -> str | None:
        """Refresh the OAuth token if due; the access token, or None if there is none."""
        session = self.oauth_session
        try:
            if hasattr(session, "async_ensure_token_valid"):
                await session.async_ensure_token_valid()
                token = session.token
            elif hasattr(session, "async_get_valid_token"):
                token = await session.async_get_valid_token()
            else:
                token = session.token
        except Exception as err:  # noqa: BLE001 - the broker call below may still work
            _LOGGER.warning("Failed to refresh the OAuth token: %s", err)
            return None
        access_token = token.get("access_token") if token else None
        if access_token:
            self.api.set_token(access_token)
        return access_token

    async def async_refresh_credentials(self) -> None:
        """After a refused connection: refresh the token, then the broker credentials."""
        if self._unloading or self.lock.locked():
            return  # paho retries; the next refused attempt comes back here
        async with self.lock:
            if self._unloading:
                return
            token = await self._async_fresh_token()
            try:
                fetched = await self.sdk.async_refresh_broker_credentials(
                    self.api, auth_headers=bearer(token) if token else None
                )
            except MowerAPIError as err:
                # Rate limited ("too early") included: the next refused
                # connection tries again.
                _LOGGER.warning("Failed to refresh the MQTT credentials: %s", err)
                fetched = False
            if fetched:
                # The helper applied the bearer with the broker credentials.
                if token:
                    self._applied_token = token
                self.health.note_credential_refresh()
                self.health.note_rebuild()  # changed values rebuild a disconnected client
                _LOGGER.info("MQTT credentials refreshed from the server")
            elif token and token != self._applied_token and not self._unloading:
                # Nothing fetched (the cooldown, or an error): a refreshed
                # token must still reach the client, whose refused connection
                # may be the old bearer's.
                await self._async_apply_bearer(token)

    async def async_rebuild(self, reason: str) -> None:
        """Replace the client and connect afresh (the watchdog's action).

        No credential fetch: if the stored broker credentials have gone
        stale, the new client's connect is refused and
        async_refresh_credentials runs.
        """
        if self._unloading or self.lock.locked():
            return
        async with self.lock:
            if self._unloading:
                return
            _LOGGER.warning("Rebuilding the MQTT connection: %s", reason)
            token = await self._async_fresh_token()
            if self._unloading:
                return  # unloaded during the refresh: start nothing
            # paho's teardown and TLS setup block; keep them off the loop.
            await self.hass.async_add_executor_job(
                partial(
                    self.sdk.mqtt.rebuild,
                    auth_headers=bearer(token) if token else None,
                    reason=reason,
                )
            )
            if token:
                self._applied_token = token
            self.health.note_rebuild()

    async def async_push_bearer(self, token: str) -> None:
        """Give the client the current token's bearer header, once per new token."""
        async with self.lock:
            if not self._started:
                self._pending_token = None if token == self._applied_token else token
                return
            if token == self._applied_token or self._unloading:
                return
            await self._async_apply_bearer(token)

    async def _async_apply_bearer(self, token: str) -> None:
        """Apply the bearer; the caller holds the lock."""
        # Connected: set on the live client, the connection kept.
        # Disconnected: the client is rebuilt and connects; that blocks.
        await self.hass.async_add_executor_job(
            partial(self.sdk.update_mqtt_credentials, auth_headers=bearer(token))
        )
        self._applied_token = token
        self.health.note_rebuild()
