"""Unloading an entry while a client rebuild or credential refresh runs."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.navimow import async_unload_entry
from custom_components.navimow.const import DOMAIN


class UnloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_waits_for_a_replacement_in_flight(self):
        lock = asyncio.Lock()
        events = []
        sdk = SimpleNamespace(disconnect=Mock(side_effect=lambda: events.append("disconnect")))
        flag = [False]
        hass = SimpleNamespace(
            data={DOMAIN: {"e1": {"sdk": sdk, "unload_flag": flag, "mqtt_lock": lock}}},
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
        )
        entry = SimpleNamespace(entry_id="e1")
        await lock.acquire()  # a rebuild is replacing the client
        with patch("custom_components.navimow.async_unload_services"):
            unload = asyncio.create_task(async_unload_entry(hass, entry))
            await asyncio.sleep(0.01)
            self.assertTrue(flag[0])  # no new replacement will start
            self.assertEqual(events, [])  # the current one is still being built
            events.append("replacement done")
            lock.release()
            self.assertTrue(await unload)
        self.assertEqual(events, ["replacement done", "disconnect"])
        self.assertNotIn("e1", hass.data[DOMAIN])
        self.assertFalse(lock.locked())
