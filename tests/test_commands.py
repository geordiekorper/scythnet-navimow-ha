"""Shared command behavior; the client is a stand-in."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from mower_sdk.errors import MowerAPIError, MowerAuthRequiredError, MowerTransportError
from mower_sdk.models import CommandReceipt, CommandVerdict, MowerCommand

from custom_components.navimow.commands import (
    async_send_command,
    reject_unsupported_command,
)
from custom_components.navimow.lawn_mower import NavimowLawnMower

LOGGER = 'custom_components.navimow.commands'


def receipt(verdict, command=MowerCommand.DOCK):
    return CommandReceipt(device_id="mower-1", command=command, verdict=verdict)


class CommandsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            async_send_command=AsyncMock(return_value=receipt(CommandVerdict.ACCEPTED, MowerCommand.RESUME))
        )
        self.entry = SimpleNamespace(async_start_reauth=Mock())
        self.coordinator = SimpleNamespace(config_entry=self.entry, hass=object())

    async def send(self):
        return await async_send_command(self.client, self.coordinator, 'mower-1', MowerCommand.RESUME)

    async def test_submission_goes_through_the_client_and_logs_action_and_device(self):
        with self.assertLogs(LOGGER, level='INFO') as logs:
            await self.send()
        self.client.async_send_command.assert_awaited_once_with('mower-1', MowerCommand.RESUME)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('Submitted resume command for device mower-1', logs.output[0])

    async def test_a_refused_token_refresh_starts_reauth_and_is_preserved(self):
        error = ConfigEntryAuthFailed('Revoked')
        self.client.async_send_command.side_effect = error
        with (
            self.assertLogs(LOGGER, level='ERROR') as logs,
            self.assertRaises(ConfigEntryAuthFailed) as caught,
        ):
            await self.send()
        self.assertIs(caught.exception, error)
        self.assertIn('resume command on device mower-1', logs.output[0])
        self.entry.async_start_reauth.assert_called_once_with(self.coordinator.hass)

    async def test_sdk_error_logs_once_and_keeps_cause(self):
        error = RuntimeError('Rejected')
        self.client.async_send_command.side_effect = error
        with (
            self.assertLogs(LOGGER, level='INFO') as logs,
            self.assertRaises(HomeAssistantError) as caught,
        ):
            await self.send()
        self.assertEqual(len(logs.output), 1)
        self.assertIn('Failed to submit resume command for device mower-1', logs.output[0])
        self.assertIs(caught.exception.__cause__, error)

    async def test_a_home_assistant_error_from_the_client_passes_through(self):
        error = HomeAssistantError('nope')
        self.client.async_send_command.side_effect = error
        with self.assertLogs(LOGGER, level='ERROR'), self.assertRaises(HomeAssistantError) as caught:
            await self.send()
        self.assertIs(caught.exception, error)

    async def test_existing_entity_controls_use_shared_helper(self):
        entity = SimpleNamespace(_client=self.client, coordinator=self.coordinator, _device_id='mower-1')
        with patch('custom_components.navimow.lawn_mower.async_send_command', new_callable=AsyncMock) as send:
            for method, command in [
                ('async_start_mowing', MowerCommand.START),
                ('async_pause', MowerCommand.PAUSE),
                ('async_dock', MowerCommand.DOCK),
                ('async_resume', MowerCommand.RESUME),
                ('async_stop', MowerCommand.STOP),
            ]:
                await getattr(NavimowLawnMower, method)(entity)
                send.assert_awaited_with(self.client, self.coordinator, 'mower-1', command)
            await NavimowLawnMower.async_navimow_command(entity, "dock")
            send.assert_awaited_with(self.client, self.coordinator, 'mower-1', MowerCommand.DOCK)
            self.assertEqual(send.await_count, 6)

    async def test_all_supported_operations_use_sdk_mapping(self):
        for command in MowerCommand:
            self.client.async_send_command.return_value = receipt(CommandVerdict.ACCEPTED, command)
            await async_send_command(self.client, self.coordinator, 'mower-1', command)
            self.client.async_send_command.assert_awaited_with('mower-1', command)

    async def test_unsupported_operation_logs_context_without_the_client(self):
        with (
            self.assertLogs(LOGGER, level='WARNING') as logs,
            self.assertRaisesRegex(HomeAssistantError, 'Blade height adjustment is not supported'),
        ):
            reject_unsupported_command('set_blade_height', 'mower-1', height=30)
        self.assertIn('set_blade_height', logs.output[0])
        self.assertIn('mower-1', logs.output[0])
        self.assertIn('30', logs.output[0])
        self.client.async_send_command.assert_not_awaited()


class OutcomeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            async_send_command=AsyncMock(return_value=receipt(CommandVerdict.ACCEPTED))
        )
        self.entry = SimpleNamespace(async_start_reauth=Mock())
        self.coordinator = SimpleNamespace(config_entry=self.entry, hass=object())

    async def send(self):
        return await async_send_command(self.client, self.coordinator, "mower-1", MowerCommand.DOCK)

    async def test_accepted_command_reports_its_outcome(self):
        with self.assertLogs(LOGGER, level="INFO"):
            outcome = await self.send()
        self.client.async_send_command.assert_awaited_once_with("mower-1", MowerCommand.DOCK)
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
            self.client.async_send_command.return_value = receipt(verdict)
            self.assertEqual((await self.send())["status"], status, verdict)

    async def test_no_usable_reply_is_unconfirmed(self):
        self.client.async_send_command.side_effect = MowerTransportError("timed out")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            outcome = await self.send()
        self.assertEqual(outcome["status"], "unconfirmed")
        self.assertEqual(outcome["error"], "timed out")
        self.assertIn("may still act", logs.output[0])
        self.assertIsNotNone(outcome["recorded_at"])
        self.entry.async_start_reauth.assert_not_called()

    async def test_refused_credentials_start_reauth_and_raise(self):
        self.client.async_send_command.side_effect = MowerAuthRequiredError("token expired")
        with self.assertLogs(LOGGER, level="ERROR"), self.assertRaises(HomeAssistantError) as raised:
            await self.send()
        self.entry.async_start_reauth.assert_called_once_with(self.coordinator.hass)
        self.assertIsInstance(raised.exception.__cause__, MowerAuthRequiredError)

    async def test_refusal_still_raises(self):
        self.client.async_send_command.side_effect = MowerAPIError("COMMAND_FAILED: deviceOffline")
        with self.assertLogs(LOGGER, level="ERROR"), self.assertRaises(HomeAssistantError):
            await self.send()
        self.entry.async_start_reauth.assert_not_called()

    async def test_without_an_entry_no_reauth_is_started(self):
        self.coordinator.config_entry = None
        self.client.async_send_command.side_effect = MowerAuthRequiredError("token expired")
        with self.assertLogs(LOGGER, level="ERROR"), self.assertRaises(HomeAssistantError):
            await self.send()
