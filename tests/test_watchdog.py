"""MQTT watchdog: rebuild a connection that is up but no longer delivering."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant

from custom_components.navimow.health import CollectorHealth
from custom_components.navimow.watchdog import MqttWatchdog


class FakeCoordinator:
    def __init__(self, name="Mower"):
        self.view = {
            "name": name, "mqtt_state": "mowing", "mqtt_key": "t1", "mqtt_age": 600.0,
            "rest_state": "docked", "rest_raw_state": "isDocked",
        }

    def get_watch_view(self, now):
        return dict(self.view)


class WatchdogTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.health = CollectorHealth()
        self.health.note_connected("c")
        self.mower = FakeCoordinator()
        self.rebuild = AsyncMock()
        self.now = 10_000.0
        self.watchdog = MqttWatchdog(
            self.hass, self.health, {"dev-1": self.mower}, self.rebuild, clock=lambda: self.now
        )

    async def rebuilds(self):
        await asyncio.sleep(0)
        return [call.args[0] for call in self.rebuild.await_args_list]


class RestMismatchTest(WatchdogTestCase):
    async def test_missed_transition_rebuilds_once_per_report(self):
        self.watchdog.async_check_after_poll()
        reasons = await self.rebuilds()
        self.assertEqual(len(reasons), 1)
        self.assertIn("REST says docked but MQTT last said mowing for Mower", reasons[0])
        self.now += 1000  # past the debounce, same MQTT report
        self.watchdog.async_check_after_poll()
        self.assertEqual(len(await self.rebuilds()), 1)
        self.mower.view["mqtt_key"] = "t2"  # a newer report that still disagrees
        self.watchdog.async_check_after_poll()
        self.assertEqual(len(await self.rebuilds()), 2)

    async def test_agreement_rearms_the_rule(self):
        self.watchdog.async_check_after_poll()
        self.mower.view["rest_state"] = "mowing"
        self.watchdog.async_check_after_poll()  # agree: forget the report
        self.mower.view["rest_state"] = "docked"
        self.now += 1000
        self.watchdog.async_check_after_poll()
        self.assertEqual(len(await self.rebuilds()), 2)

    async def test_young_mqtt_report_is_not_contradicted(self):
        self.mower.view["mqtt_age"] = 60.0  # REST may lag behind it
        self.watchdog.async_check_after_poll()
        self.assertEqual(await self.rebuilds(), [])

    async def test_states_mqtt_never_echoes_are_ignored(self):
        for raw, state in (("Offline", "unknown"), ("inSoftwareUpdate", "paused"), ("weird", "unknown")):
            self.mower.view.update(rest_raw_state=raw, rest_state=state)
            self.watchdog.async_check_after_poll()
        self.assertEqual(await self.rebuilds(), [])

    async def test_missing_sources_are_skipped(self):
        self.mower.view["mqtt_state"] = None
        self.watchdog.async_check_after_poll()
        self.mower.view.update(mqtt_state="mowing", rest_state=None)
        self.watchdog.async_check_after_poll()
        self.assertEqual(await self.rebuilds(), [])

    async def test_never_connected_client_is_not_rebuilt(self):
        self.health = CollectorHealth()
        self.watchdog.health = self.health
        self.watchdog.async_check_after_poll()
        self.assertEqual(await self.rebuilds(), [])

    async def test_debounce_keeps_the_mismatch_live(self):
        self.watchdog.async_check_after_poll()
        other = FakeCoordinator("Other")
        other.view["mqtt_key"] = "o1"
        self.watchdog.coordinators["dev-2"] = other
        self.now += 60  # inside the debounce window
        self.watchdog.async_check_after_poll()
        self.assertEqual(len(await self.rebuilds()), 1)
        self.now += 300  # window over: dev-2's mismatch is still acted on
        self.watchdog.async_check_after_poll()
        reasons = await self.rebuilds()
        self.assertEqual(len(reasons), 2)
        self.assertIn("for Other", reasons[1])
