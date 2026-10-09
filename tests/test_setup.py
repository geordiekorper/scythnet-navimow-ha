"""The setup's glue around the client: the token provider, the error handler
and the status observer."""
import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from mower_sdk.errors import MowerAPIError, MowerAuthRequiredError, MowerTransportError
from mower_sdk.models import DeviceStatus

from custom_components.navimow import (
    PLATFORMS,
    _async_connect_or_undo,
    error_handler,
    status_observer,
    token_provider,
)
from custom_components.navimow.const import DOMAIN
from custom_components.navimow.health import CollectorHealth
from tests.fakes import REST_PAYLOAD

LOGGER = "custom_components.navimow"


class TokenProviderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            token={"access_token": "t1", "expires_at": 1700000000},
            async_ensure_token_valid=AsyncMock(),
        )
        self.health = CollectorHealth()
        self.provider = token_provider(self.session, self.health)

    async def test_a_valid_token_is_returned_and_its_expiry_noted(self):
        self.assertEqual(await self.provider(), "t1")
        self.session.async_ensure_token_valid.assert_awaited_once()
        self.assertTrue(self.health.token_expires_at.startswith("2023-11-14T22:13:20"))

    async def test_a_transient_refresh_failure_keeps_the_cached_token(self):
        self.session.async_ensure_token_valid.side_effect = OSError("dns")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.assertEqual(await self.provider(), "t1")
        self.assertIn("keeping the cached token", logs.output[0])

    async def test_no_token_at_all_gives_none(self):
        self.session.token = None
        self.assertIsNone(await self.provider())
        self.assertIsNone(self.health.token_expires_at)

    async def test_a_refusal_propagates(self):
        self.session.async_ensure_token_valid.side_effect = ConfigEntryAuthFailed("revoked")
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.provider()


class ErrorHandlerTest(unittest.TestCase):
    def setUp(self):
        self.hass = object()
        self.entry = SimpleNamespace(async_start_reauth=Mock())
        self.health = CollectorHealth()
        self.health.connected = True
        self.on_error = error_handler(self.hass, self.entry, self.health)

    def test_a_poll_failure_shows_until_the_next_success(self):
        self.on_error("poll", MowerTransportError("down"))
        self.assertEqual(self.health.status, "poll_failing")
        self.assertEqual(self.health.last_poll_error, "down")
        self.assertIsNotNone(self.health.last_poll_error_at)
        self.entry.async_start_reauth.assert_not_called()
        self.health.note_poll_ok()
        self.assertEqual(self.health.status, "ok")

    def test_the_poll_after_a_command_counts_too_and_other_operations_do_not(self):
        self.on_error("command_poll", RuntimeError("x"))
        self.assertEqual(self.health.last_poll_error, "x")
        self.health.note_poll_ok()
        self.on_error("silence_check", RuntimeError("y"))
        self.on_error("rebuild", RuntimeError("z"))
        self.assertIsNone(self.health.last_poll_error)

    def test_refused_credentials_start_reauthentication(self):
        with self.assertLogs(LOGGER, level="ERROR"):
            self.on_error("poll", MowerAuthRequiredError("expired"))
        self.entry.async_start_reauth.assert_called_once_with(self.hass)
        self.entry.async_start_reauth.reset_mock()
        with self.assertLogs(LOGGER, level="ERROR"):
            self.on_error("recovery", ConfigEntryAuthFailed("refresh refused"))
        self.entry.async_start_reauth.assert_called_once_with(self.hass)


class ConnectOrUndoTest(unittest.IsolatedAsyncioTestCase):
    """The platforms are forwarded and the data stored before the connect;
    a connect that does not complete undoes both."""

    def setUp(self):
        self.hass = SimpleNamespace(
            data={DOMAIN: {"e1": {"client": object()}}},
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
        )
        self.entry = SimpleNamespace(entry_id="e1")
        self.client = SimpleNamespace(async_connect=AsyncMock(), async_disconnect=AsyncMock())

    def undone(self):
        self.hass.config_entries.async_unload_platforms.assert_awaited_once_with(self.entry, PLATFORMS)
        self.assertNotIn("e1", self.hass.data[DOMAIN])

    async def test_a_connect_that_completes_leaves_everything_in_place(self):
        await _async_connect_or_undo(self.hass, self.entry, self.client)
        self.hass.config_entries.async_unload_platforms.assert_not_awaited()
        self.assertIn("e1", self.hass.data[DOMAIN])

    async def test_a_failed_request_undoes_and_retries(self):
        self.client.async_connect.side_effect = MowerAPIError("down")
        with self.assertRaises(ConfigEntryNotReady) as raised:
            await _async_connect_or_undo(self.hass, self.entry, self.client)
        self.assertIsInstance(raised.exception.__cause__, MowerAPIError)
        self.undone()

    async def test_a_credential_reply_the_sdk_refuses_undoes_and_retries(self):
        self.client.async_connect.side_effect = ValueError("no WebSocket path")
        with self.assertRaises(ConfigEntryNotReady):
            await _async_connect_or_undo(self.hass, self.entry, self.client)
        self.undone()

    async def test_refused_credentials_undo_and_start_reauth(self):
        self.client.async_connect.side_effect = MowerAuthRequiredError("expired")
        with self.assertRaises(ConfigEntryAuthFailed):
            await _async_connect_or_undo(self.hass, self.entry, self.client)
        self.undone()
        self.hass.data[DOMAIN]["e1"] = {}
        self.hass.config_entries.async_unload_platforms.reset_mock()
        self.client.async_connect.side_effect = ConfigEntryAuthFailed("refresh refused")
        with self.assertRaises(ConfigEntryAuthFailed) as raised:
            await _async_connect_or_undo(self.hass, self.entry, self.client)
        self.assertIsNone(raised.exception.__cause__)  # Home Assistant's own, as it is
        self.undone()

    async def test_a_connect_the_client_gave_up_undoes_and_stays_cancelled(self):
        self.client.async_connect.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await _async_connect_or_undo(self.hass, self.entry, self.client)
        self.undone()
        self.client.async_disconnect.assert_not_awaited()  # nothing was connected

    async def test_a_setup_cancelled_during_the_connect_lets_it_end_then_disconnects(self):
        # The client's connect works in an executor thread, which a cancelled
        # await cannot stop: the connect is left to finish, then undone.
        started, finished = asyncio.Event(), []

        async def connect():
            started.set()
            await asyncio.sleep(0.02)
            finished.append(True)

        self.client.async_connect = connect
        task = asyncio.create_task(_async_connect_or_undo(self.hass, self.entry, self.client))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(finished, [True])
        self.client.async_disconnect.assert_awaited_once()
        self.undone()

    async def test_a_cancel_that_lands_as_the_connect_completes_still_disconnects(self):
        # The connect returned, our task has not resumed yet, and the cancel
        # arrives: the client is connected and must be disconnected.
        finished = asyncio.Event()

        async def connect():
            await asyncio.sleep(0)
            finished.set()

        self.client.async_connect = connect
        task = asyncio.create_task(_async_connect_or_undo(self.hass, self.entry, self.client))
        await finished.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.async_disconnect.assert_awaited_once()
        self.undone()

    async def test_a_setup_cancelled_twice_lets_the_connect_undo_itself(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def connect():
            started.set()
            await release.wait()

        self.client.async_connect = connect
        task = asyncio.create_task(_async_connect_or_undo(self.hass, self.entry, self.client))
        await started.wait()
        task.cancel()  # the first: the undo waits for the connect
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(task.done())
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            task.cancel()  # the second: the undo stops waiting
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn("disconnects itself", logs.output[0])
        self.undone()
        self.client.async_disconnect.assert_not_awaited()  # nothing completed yet
        release.set()  # the connect completes after everyone gave up
        for _ in range(5):
            await asyncio.sleep(0)
        self.client.async_disconnect.assert_awaited_once()

    async def test_a_setup_cancelled_twice_with_a_connect_that_then_fails_disconnects_nothing(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def connect():
            started.set()
            await release.wait()
            raise MowerAPIError("down")

        self.client.async_connect = connect
        task = asyncio.create_task(_async_connect_or_undo(self.hass, self.entry, self.client))
        await started.wait()
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        with self.assertLogs(LOGGER, level="WARNING"):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        self.client.async_disconnect.assert_not_awaited()
        self.undone()

    async def test_a_setup_cancelled_during_a_connect_that_then_fails_undoes_without_a_disconnect(self):
        started = asyncio.Event()

        async def connect():
            started.set()
            await asyncio.sleep(0.02)
            raise MowerAPIError("down")

        self.client.async_connect = connect
        task = asyncio.create_task(_async_connect_or_undo(self.hass, self.entry, self.client))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.async_disconnect.assert_not_awaited()
        self.undone()


class StatusObserverTest(unittest.TestCase):
    def test_each_reply_reaches_its_coordinator_and_the_poll_is_healthy(self):
        first, second = SimpleNamespace(note_rest_status=Mock()), SimpleNamespace(note_rest_status=Mock())
        health = CollectorHealth()
        health.connected = True
        health.note_poll_error("down")
        observer = status_observer({"dev-1": first, "dev-2": second}, health)
        statuses = {
            "dev-1": DeviceStatus.from_dict(dict(REST_PAYLOAD)),
            "dev-9": DeviceStatus.from_dict({**REST_PAYLOAD, "id": "dev-9"}),
        }
        polled_at = datetime(2026, 9, 17, 20, 2, tzinfo=timezone.utc)
        observer(statuses, polled_at)
        first.note_rest_status.assert_called_once_with(statuses["dev-1"], polled_at)
        second.note_rest_status.assert_not_called()  # the poll left it out
        self.assertEqual(health.status, "ok")
