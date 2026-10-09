"""REST poll interval option: form, bounds, and applying it through a reload."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import voluptuous as vol

from custom_components.navimow import _async_options_updated
from custom_components.navimow.config_flow import NavimowOptionsFlowHandler
from tests.fakes import hass_for


class OptionsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = hass_for(self)

    async def form_schema(self, options):
        flow = NavimowOptionsFlowHandler(SimpleNamespace(options=options))
        flow.hass = self.hass
        flow.handler = "entry-1"
        flow.flow_id = "flow-1"
        result = await flow.async_step_init()
        self.assertEqual(result["type"], "form")
        return result["data_schema"]

    async def test_form_defaults_to_300_then_to_the_saved_value(self):
        self.assertEqual((await self.form_schema({}))({}), {"rest_poll_seconds": 300})
        self.assertEqual(
            (await self.form_schema({"rest_poll_seconds": 120}))({}), {"rest_poll_seconds": 120}
        )

    async def test_interval_is_bounded(self):
        schema = await self.form_schema({})
        self.assertEqual(schema({"rest_poll_seconds": "75"}), {"rest_poll_seconds": 75})
        for bad in (59, 601):
            with self.assertRaises(vol.Invalid):
                schema({"rest_poll_seconds": bad})

    async def test_saving_creates_the_options(self):
        flow = NavimowOptionsFlowHandler(SimpleNamespace(options={}))
        flow.hass, flow.handler, flow.flow_id = self.hass, "entry-1", "flow-1"
        result = await flow.async_step_init({"rest_poll_seconds": 60})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {"rest_poll_seconds": 60})

    async def test_a_changed_option_reloads_the_entry(self):
        # The client takes its poll interval at construction; a reload
        # rebuilds it with the new one, and the entities keep their state.
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_reload=AsyncMock()))
        entry = SimpleNamespace(entry_id="entry-1", options={"rest_poll_seconds": 90})
        await _async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once_with("entry-1")

