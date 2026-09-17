"""Phase 1 tests using HA's real service registry and a mocked mower API.

Run in the HA Python environment: python -m unittest discover -s tests -v
No cloud connection or mower command is made.
"""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from mower_sdk.models import MowerCommand
from custom_components.navimow.services import async_setup_services, async_unload_services


class CommandServicesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.api = SimpleNamespace(async_send_command=AsyncMock(return_value={}))
        self.coordinator = SimpleNamespace(
            _async_ensure_valid_token=AsyncMock(), async_request_refresh=AsyncMock()
        )
        self.data = {'api': self.api, 'coordinators': {'vendor-123': self.coordinator}, 'unload_flag': [False]}
        self.hass.data['navimow'] = {'entry': self.data}
        self.registry = Mock()
        self.registry.async_get.return_value = SimpleNamespace(identifiers={('navimow', 'vendor-123')})
        self.registry_patch = patch('custom_components.navimow.services.dr.async_get', return_value=self.registry)
        self.registry_patch.start()
        async_setup_services(self.hass, self.api)

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        self.registry_patch.stop()
        self.temp.cleanup()

    async def call(self, name, device='ha-device'):
        await self.hass.services.async_call('navimow', name, {'device_id': device}, blocking=True)

    async def test_resume_and_stop_mapping_and_order(self):
        order = []
        self.coordinator._async_ensure_valid_token.side_effect = lambda: order.append('token')
        self.api.async_send_command.side_effect = lambda *args: order.append('send')
        self.coordinator.async_request_refresh.side_effect = lambda: order.append('refresh')
        for service, command in [('resume', MowerCommand.RESUME), ('stop', MowerCommand.STOP)]:
            await self.call(service)
            self.api.async_send_command.assert_awaited_with('vendor-123', command)
        self.assertEqual(order, ['token', 'send', 'refresh'] * 2)
        self.assertEqual(self.api.async_send_command.await_count, 2)
        self.registry.async_get.assert_called_with('ha-device')

    async def test_unknown_and_foreign_devices_are_rejected(self):
        for device in [None, SimpleNamespace(identifiers={('other', 'vendor-123')})]:
            self.registry.async_get.return_value = device
            with self.assertRaises(HomeAssistantError):
                await self.call('resume')
        self.api.async_send_command.assert_not_awaited()

    async def test_unloaded_device_is_rejected(self):
        self.data['unload_flag'][0] = True
        with self.assertRaises(HomeAssistantError):
            await self.call('stop')
        self.api.async_send_command.assert_not_awaited()

    async def test_sdk_failure_is_action_error(self):
        self.api.async_send_command.side_effect = RuntimeError('mock rejected')
        with self.assertRaisesRegex(HomeAssistantError, 'Navimow stop failed'):
            await self.call('stop')
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_auth_failure_prevents_command(self):
        self.coordinator._async_ensure_valid_token.side_effect = HomeAssistantError('reauthenticate')
        with self.assertRaises(HomeAssistantError):
            await self.call('resume')
        self.api.async_send_command.assert_not_awaited()

    async def test_reload_uses_new_api_and_cleans_up(self):
        old_api = self.api
        self.hass.data['navimow'].clear()
        async_unload_services(self.hass)
        self.assertFalse(self.hass.services.has_service('navimow', 'resume'))
        self.assertFalse(self.hass.services.has_service('navimow', 'stop'))
        self.api = SimpleNamespace(async_send_command=AsyncMock(return_value={}))
        self.data['api'] = self.api
        self.hass.data['navimow']['entry'] = self.data
        async_setup_services(self.hass, self.api)
        async_setup_services(self.hass, self.api)
        await self.call('resume')
        self.api.async_send_command.assert_awaited_once_with('vendor-123', MowerCommand.RESUME)
        old_api.async_send_command.assert_not_awaited()

    async def test_other_loaded_entry_preserves_services(self):
        async_unload_services(self.hass)
        self.assertTrue(self.hass.services.has_service('navimow', 'stop'))
