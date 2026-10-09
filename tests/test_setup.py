"""The setup's glue around the client: the token provider, the error handler
and the status observer."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import ConfigEntryAuthFailed
from mower_sdk.errors import MowerAuthRequiredError, MowerTransportError
from mower_sdk.models import DeviceStatus

from custom_components.navimow import error_handler, status_observer, token_provider
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
