"""Shared command behavior; all mower communication is mocked."""
import logging
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from mower_sdk.errors import MowerAPIError, MowerAuthRequiredError, MowerTransportError
from mower_sdk.models import CommandReceipt, CommandVerdict, MowerCommand

from custom_components.navimow.commands import (
    async_send_command,
    reject_unsupported_command,
)
from custom_components.navimow.lawn_mower import NavimowLawnMower

LOGGER = 'custom_components.navimow.commands'


class CommandsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = SimpleNamespace(async_send_command_receipt=AsyncMock(
            return_value=receipt(CommandVerdict.ACCEPTED)
        ))
        self.coordinator = SimpleNamespace(
            _async_ensure_valid_token=AsyncMock(), async_request_refresh=AsyncMock(),
            last_update_success=True, config_entry=None
        )

    async def send(self):
        await async_send_command(self.api, self.coordinator, 'mower-1', MowerCommand.RESUME)

    async def test_submission_logs_action_and_device(self):
        with self.assertLogs(LOGGER, level='INFO') as logs:
            await self.send()
        self.assertEqual(len(logs.output), 1)
        self.assertIn('Submitted resume command for device mower-1', logs.output[0])
        self.coordinator.async_request_refresh.assert_awaited_once()

    async def test_submission_requests_a_rest_poll_soon(self):
        self.coordinator.rest_poller = SimpleNamespace(async_request_poll=Mock())
        await self.send()
        self.coordinator.rest_poller.async_request_poll.assert_called_once_with(5)

    async def test_failed_submission_still_requests_a_poll(self):
        # A command whose reply was lost may still have acted.
        self.coordinator.rest_poller = SimpleNamespace(async_request_poll=Mock())
        self.api.async_send_command_receipt.side_effect = RuntimeError("timed out")
        with self.assertRaises(HomeAssistantError), self.assertLogs(LOGGER, level="ERROR"):
            await self.send()
        self.coordinator.rest_poller.async_request_poll.assert_called_once_with(5)

    async def test_command_not_sent_requests_no_poll(self):
        self.coordinator.rest_poller = SimpleNamespace(async_request_poll=Mock())
        self.coordinator._async_ensure_valid_token.side_effect = HomeAssistantError("auth")
        with self.assertRaises(HomeAssistantError), self.assertLogs(LOGGER, level="ERROR"):
            await self.send()
        self.coordinator.rest_poller.async_request_poll.assert_not_called()

    async def test_auth_error_is_logged_and_preserved(self):
        error = HomeAssistantError('Authentication required')
        self.coordinator._async_ensure_valid_token.side_effect = error
        with (
            self.assertLogs(LOGGER, level='ERROR') as logs,
            self.assertRaises(HomeAssistantError) as caught,
        ):
            await self.send()
        self.assertIs(caught.exception, error)
        self.assertIn('resume command for device mower-1', logs.output[0])
        self.api.async_send_command_receipt.assert_not_awaited()
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_sdk_error_logs_once_and_keeps_cause(self):
        error = RuntimeError('Rejected')
        self.api.async_send_command_receipt.side_effect = error
        with (
            self.assertLogs(LOGGER, level='INFO') as logs,
            self.assertRaises(HomeAssistantError) as caught,
        ):
            await self.send()
        self.assertEqual(len(logs.output), 1)
        self.assertIn('Failed to submit resume command for device mower-1', logs.output[0])
        self.assertIs(caught.exception.__cause__, error)
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_refresh_failure_does_not_claim_command_rejection(self):
        self.coordinator.async_request_refresh.side_effect = RuntimeError('Offline')
        with self.assertLogs(LOGGER, level='INFO') as logs:
            await self.send()
        self.assertEqual(len(logs.output), 2)
        self.assertIn('Submitted resume', logs.output[0])
        self.assertIn('WARNING', logs.output[1])
        self.assertIn('State refresh failed', logs.output[1])
        self.api.async_send_command_receipt.assert_awaited_once()

    async def test_existing_entity_controls_use_shared_helper(self):
        entity = SimpleNamespace(_api=self.api, coordinator=self.coordinator, _device_id='mower-1')
        with patch('custom_components.navimow.lawn_mower.async_send_command', new_callable=AsyncMock) as send:
            for method, command in [
                ('async_start_mowing', MowerCommand.START),
                ('async_pause', MowerCommand.PAUSE),
                ('async_dock', MowerCommand.DOCK),
                ('async_resume', MowerCommand.RESUME),
                ('async_stop', MowerCommand.STOP),
            ]:
                await getattr(NavimowLawnMower, method)(entity)
                send.assert_awaited_with(self.api, self.coordinator, 'mower-1', command)
            self.assertEqual(send.await_count, 5)

    async def test_all_supported_operations_use_sdk_mapping(self):
        for command in MowerCommand:
            await async_send_command(self.api, self.coordinator, 'mower-1', command)
            self.api.async_send_command_receipt.assert_awaited_with('mower-1', command)

    async def test_unsupported_operation_logs_context_without_auth_or_api(self):
        with (
            self.assertLogs(LOGGER, level='WARNING') as logs,
            self.assertRaisesRegex(HomeAssistantError, 'Blade height adjustment is not supported'),
        ):
            reject_unsupported_command('set_blade_height', 'mower-1', height=30)
        self.assertIn('set_blade_height', logs.output[0])
        self.assertIn('mower-1', logs.output[0])
        self.assertIn('30', logs.output[0])
        self.coordinator._async_ensure_valid_token.assert_not_awaited()
        self.api.async_send_command_receipt.assert_not_awaited()
        self.coordinator.async_request_refresh.assert_not_awaited()


    async def test_auth_failure_starts_reauth(self):
        entry = Mock()
        self.coordinator.config_entry = entry
        self.coordinator.hass = object()
        self.coordinator._async_ensure_valid_token.side_effect = ConfigEntryAuthFailed('Revoked')
        with (
            self.assertLogs(LOGGER, level='ERROR'),
            self.assertRaises(ConfigEntryAuthFailed),
        ):
            await self.send()
        entry.async_start_reauth.assert_called_once_with(self.coordinator.hass)
        self.api.async_send_command_receipt.assert_not_awaited()

    async def test_real_coordinator_refresh_failure_does_not_fail_action(self):
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            coordinator = DataUpdateCoordinator(
                hass, logging.getLogger('test.coordinator'), config_entry=None,
                name='test', update_method=AsyncMock(side_effect=UpdateFailed('Offline')),
            )
            coordinator._async_ensure_valid_token = AsyncMock()
            try:
                with self.assertLogs(LOGGER, level='INFO') as logs:
                    await async_send_command(self.api, coordinator, 'mower-1', MowerCommand.STOP)
                self.assertFalse(coordinator.last_update_success)
                self.assertEqual(len(logs.output), 1)
                self.assertIn('Submitted stop', logs.output[0])
                self.api.async_send_command_receipt.assert_awaited_once()
            finally:
                await coordinator.async_shutdown()
                await hass.async_stop(force=True)


class OutcomeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = SimpleNamespace(async_send_command_receipt=AsyncMock(
            return_value=receipt(CommandVerdict.ACCEPTED)
        ))
        self.poller = SimpleNamespace(async_request_poll=Mock())
        self.entry = SimpleNamespace(async_start_reauth=Mock())
        self.coordinator = SimpleNamespace(
            _async_ensure_valid_token=AsyncMock(), async_request_refresh=AsyncMock(),
            config_entry=self.entry, hass=object(), rest_poller=self.poller,
        )

    async def send(self):
        return await async_send_command(self.api, self.coordinator, "mower-1", MowerCommand.DOCK)

    async def test_accepted_command_reports_its_outcome(self):
        with self.assertLogs(LOGGER, level="INFO"):
            outcome = await self.send()
        self.api.async_send_command_receipt.assert_awaited_once_with("mower-1", MowerCommand.DOCK)
        self.assertEqual(outcome["command"], "dock")
        self.assertEqual(outcome["status"], "accepted")
        self.assertIsNone(outcome["error"])
        self.assertLessEqual(outcome["sent_at"], outcome["recorded_at"])

    async def test_each_verdict_is_the_outcome_status(self):
        for verdict, status in (
            (CommandVerdict.ACCEPTED, "accepted"),
            (CommandVerdict.ALREADY_IN_STATE, "already_in_state"),
            (CommandVerdict.UNKNOWN, "unknown"),
        ):
            self.api.async_send_command_receipt.return_value = receipt(verdict)
            self.assertEqual((await self.send())["status"], status, verdict)

    async def test_no_usable_reply_is_unconfirmed_and_still_polled(self):
        self.api.async_send_command_receipt.side_effect = MowerTransportError("timed out")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            outcome = await self.send()
        self.assertEqual(outcome["status"], "unconfirmed")
        self.assertEqual(outcome["error"], "timed out")
        self.assertIn("may still act", logs.output[0])
        self.assertIsNotNone(outcome["recorded_at"])
        self.poller.async_request_poll.assert_called_once()
        self.entry.async_start_reauth.assert_not_called()

    async def test_refused_credentials_start_reauth_and_raise(self):
        self.api.async_send_command_receipt.side_effect = MowerAuthRequiredError("token expired")
        with self.assertLogs(LOGGER, level="ERROR"), self.assertRaises(HomeAssistantError) as raised:
            await self.send()
        self.entry.async_start_reauth.assert_called_once_with(self.coordinator.hass)
        self.assertIsInstance(raised.exception.__cause__, MowerAuthRequiredError)

    async def test_refusal_still_raises(self):
        self.api.async_send_command_receipt.side_effect = MowerAPIError("COMMAND_FAILED: deviceOffline")
        with self.assertLogs(LOGGER, level="ERROR"), self.assertRaises(HomeAssistantError):
            await self.send()
        self.entry.async_start_reauth.assert_not_called()


def receipt(verdict):
    return CommandReceipt(device_id="mower-1", command=MowerCommand.DOCK, verdict=verdict)
