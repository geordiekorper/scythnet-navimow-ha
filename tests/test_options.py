"""REST poll interval option: form, bounds, and applying it without a reload."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import voluptuous as vol
from homeassistant.core import HomeAssistant

from custom_components.navimow import _async_options_updated
from custom_components.navimow.config_flow import NavimowOptionsFlowHandler
from custom_components.navimow.const import DOMAIN


class OptionsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)

    async def form_schema(self, options):
        flow = NavimowOptionsFlowHandler(SimpleNamespace(options=options))
        flow.hass = self.hass
        flow.handler = "entry-1"
        flow.flow_id = "flow-1"
        result = await flow.async_step_init()
        self.assertEqual(result["type"], "form")
        return result["data_schema"]

    async def test_form_defaults_to_120_then_to_the_saved_value(self):
        self.assertEqual((await self.form_schema({}))({}), {"rest_poll_seconds": 120})
        self.assertEqual(
            (await self.form_schema({"rest_poll_seconds": 300}))({}), {"rest_poll_seconds": 300}
        )

    async def test_interval_is_bounded(self):
        schema = await self.form_schema({})
        self.assertEqual(schema({"rest_poll_seconds": "45"}), {"rest_poll_seconds": 45})
        for bad in (29, 601):
            with self.assertRaises(vol.Invalid):
                schema({"rest_poll_seconds": bad})

    async def test_saving_creates_the_options(self):
        flow = NavimowOptionsFlowHandler(SimpleNamespace(options={}))
        flow.hass, flow.handler, flow.flow_id = self.hass, "entry-1", "flow-1"
        result = await flow.async_step_init({"rest_poll_seconds": 60})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {"rest_poll_seconds": 60})

    async def test_changed_option_reaches_the_running_poller(self):
        poller = SimpleNamespace(async_set_interval=Mock())
        self.hass.data[DOMAIN] = {"entry-1": {"rest_poller": poller}}
        entry = SimpleNamespace(entry_id="entry-1", options={"rest_poll_seconds": 90})
        await _async_options_updated(self.hass, entry)
        poller.async_set_interval.assert_called_once_with(90)

    async def test_listener_without_a_poller_is_a_no_op(self):
        entry = SimpleNamespace(entry_id="gone", options={})
        await _async_options_updated(self.hass, entry)  # does not raise
