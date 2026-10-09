"""Unloading an entry: the client is disconnected, under its own lock."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.navimow import async_unload_entry
from custom_components.navimow.const import DOMAIN

LOGGER = "custom_components.navimow"


class UnloadTest(unittest.IsolatedAsyncioTestCase):
    def make(self, client):
        hass = SimpleNamespace(
            data={DOMAIN: {"e1": {"client": client}}},
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
        )
        return hass, SimpleNamespace(entry_id="e1")

    async def test_the_client_is_disconnected_and_the_data_dropped(self):
        client = SimpleNamespace(async_disconnect=AsyncMock())
        hass, entry = self.make(client)
        with patch("custom_components.navimow.async_unload_services") as unload_services:
            self.assertTrue(await async_unload_entry(hass, entry))
        client.async_disconnect.assert_awaited_once()
        self.assertNotIn("e1", hass.data[DOMAIN])
        unload_services.assert_called_once_with(hass)

    async def test_a_failing_disconnect_still_unloads(self):
        client = SimpleNamespace(async_disconnect=AsyncMock(side_effect=RuntimeError("stuck")))
        hass, entry = self.make(client)
        with patch("custom_components.navimow.async_unload_services"), self.assertLogs(LOGGER, level="WARNING"):
            self.assertTrue(await async_unload_entry(hass, entry))
        self.assertNotIn("e1", hass.data[DOMAIN])

    async def test_platforms_that_do_not_unload_keep_the_client(self):
        client = SimpleNamespace(async_disconnect=AsyncMock())
        hass, entry = self.make(client)
        hass.config_entries.async_unload_platforms.return_value = False
        with patch("custom_components.navimow.async_unload_services") as unload_services:
            self.assertFalse(await async_unload_entry(hass, entry))
        client.async_disconnect.assert_not_awaited()
        self.assertIn("e1", hass.data[DOMAIN])
        unload_services.assert_not_called()
