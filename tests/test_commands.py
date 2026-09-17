"""Shared command behavior; all mower communication is mocked."""
import logging
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from mower_sdk.models import MowerCommand

from custom_components.navimow.commands import (
    async_send_command,
    reject_unsupported_command,
)
from custom_components.navimow.lawn_mower import NavimowLawnMower

LOGGER = 'custom_components.navimow.commands'


class CommandsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = SimpleNamespace(async_send_command=AsyncMock())
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
        self.api.async_send_command.assert_not_awaited()
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_sdk_error_logs_once_and_keeps_cause(self):
        error = RuntimeError('Rejected')
        self.api.async_send_command.side_effect = error
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
        self.api.async_send_command.assert_awaited_once()

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
            self.api.async_send_command.assert_awaited_with('mower-1', command)

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
        self.api.async_send_command.assert_not_awaited()
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
        self.api.async_send_command.assert_not_awaited()

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
                self.api.async_send_command.assert_awaited_once()
            finally:
                await coordinator.async_shutdown()
                await hass.async_stop(force=True)
