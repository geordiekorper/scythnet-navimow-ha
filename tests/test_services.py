"""Actions tested through HA's native entity routing with mocked mower APIs."""
import logging
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.lawn_mower import LawnMowerEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from mower_sdk.models import MowerCommand

from custom_components.navimow.lawn_mower import NavimowLawnMower
from custom_components.navimow.services import (
    async_setup_services,
    async_unload_services,
)


class TestMower(LawnMowerEntity):
    _attr_should_poll = False
    async_resume = NavimowLawnMower.async_resume
    async_stop = NavimowLawnMower.async_stop

    def __init__(self, hass, entity_id, vendor_id):
        super().__init__()
        self.hass = hass
        self.entity_id = entity_id
        self._device_id = vendor_id
        self._api = SimpleNamespace(async_send_command=AsyncMock())
        self.coordinator = SimpleNamespace(
            _async_ensure_valid_token=AsyncMock(), async_request_refresh=AsyncMock(),
            last_update_success=True
        )


class CommandServicesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        self.first = TestMower(self.hass, 'lawn_mower.navimow_x430', 'vendor-1')
        self.second = TestMower(self.hass, 'lawn_mower.navimow_x430_2', 'vendor-2')
        self.platforms = []
        self.platform = self.make_platform('navimow', 'lawn_mower')
        await self.platform.async_add_entities([self.first, self.second])
        self.foreign = TestMower(self.hass, 'lawn_mower.other_brand', 'foreign')
        await self.make_platform('other_brand', 'lawn_mower').async_add_entities([self.foreign])
        self.sensor = TestMower(self.hass, 'sensor.battery', 'sensor')
        await self.make_platform('navimow', 'sensor').async_add_entities([self.sensor])
        self.assertEqual(len(self.platform.entities), 2)
        self.hass.data['navimow'] = {'entry': {}}
        async_setup_services(self.hass)

    def make_platform(self, name, domain):
        platform = EntityPlatform(
            hass=self.hass, logger=logging.getLogger(__name__), domain=domain,
            platform_name=name, platform=None, scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        self.platforms.append(platform)
        return platform

    async def asyncTearDown(self):
        for platform in self.platforms:
            await platform.async_reset()

    async def call(self, name, entity_id):
        await self.hass.services.async_call(
            'navimow', name, {}, target={'entity_id': entity_id}, blocking=True
        )

    async def test_each_entity_routes_to_its_own_mower(self):
        await self.call('resume', self.first.entity_id)
        await self.call('stop', self.second.entity_id)
        self.first._api.async_send_command.assert_awaited_once_with('vendor-1', MowerCommand.RESUME)
        self.second._api.async_send_command.assert_awaited_once_with('vendor-2', MowerCommand.STOP)

    async def test_multiple_entities(self):
        await self.call('resume', [self.first.entity_id, self.second.entity_id])
        for e in (self.first, self.second):
            e._api.async_send_command.assert_awaited_once_with(e._device_id, MowerCommand.RESUME)

    async def test_renamed_entity(self):
        await self.platform.async_remove_entity(self.first.entity_id)
        self.first = TestMower(self.hass, 'lawn_mower.front_lawn', 'vendor-1')
        await self.platform.async_add_entities([self.first])
        await self.call('resume', self.first.entity_id)
        self.first._api.async_send_command.assert_awaited_once_with('vendor-1', MowerCommand.RESUME)

    async def test_unknown_foreign_sensor_and_unavailable_targets_do_not_send(self):
        self.first._attr_available = False
        for target in ['lawn_mower.missing', 'lawn_mower.other_brand', 'sensor.battery', self.first.entity_id]:
            await self.call('resume', target)
        for e in (self.first, self.second, self.foreign, self.sensor):
            e._api.async_send_command.assert_not_awaited()

    async def test_sdk_failure_is_action_error(self):
        self.first._api.async_send_command.side_effect = RuntimeError('Rejected')
        with self.assertRaises(HomeAssistantError):
            await self.call('stop', self.first.entity_id)

    async def test_reload_resolves_current_entity(self):
        old = self.first
        self.hass.data['navimow'].clear()
        async_unload_services(self.hass)
        self.assertFalse(self.hass.services.has_service('navimow', 'resume'))
        self.assertFalse(self.hass.services.has_service('navimow', 'stop'))
        await self.platform.async_remove_entity(old.entity_id)
        self.first = TestMower(self.hass, old.entity_id, 'vendor-1')
        await self.platform.async_add_entities([self.first])
        self.hass.data['navimow']['entry'] = {}
        async_setup_services(self.hass)
        async_setup_services(self.hass)
        await self.call('resume', self.first.entity_id)
        self.first._api.async_send_command.assert_awaited_once()
        old._api.async_send_command.assert_not_awaited()
        self.assertTrue(self.hass.services.has_service('navimow', 'stop'))
        await self.call('stop', self.first.entity_id)
        self.first._api.async_send_command.assert_awaited_with('vendor-1', MowerCommand.STOP)

    async def test_blade_height_remains_unsupported(self):
        with (
            self.assertLogs('custom_components.navimow.commands', level='WARNING'),
            self.assertRaises(HomeAssistantError),
        ):
            await self.hass.services.async_call(
                'navimow', 'set_blade_height', {'device_id': 'vendor-1', 'height': 30}, blocking=True
            )
        self.first._api.async_send_command.assert_not_awaited()

    async def test_other_loaded_entry_preserves_services(self):
        self.hass.data['navimow']['other_entry'] = {}
        del self.hass.data['navimow']['entry']
        async_unload_services(self.hass)
        for name in ('resume', 'stop', 'set_blade_height'):
            self.assertTrue(self.hass.services.has_service('navimow', name))
        await self.call('stop', self.second.entity_id)
        self.second._api.async_send_command.assert_awaited_once_with('vendor-2', MowerCommand.STOP)
