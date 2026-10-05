"""MQTT watchdog: rebuild a connection that is up but no longer delivering."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import HassJob, HassJobType, HomeAssistant

from custom_components.navimow.health import CollectorHealth
from custom_components.navimow.watchdog import MqttWatchdog
from tests.fakes import FakeMqtt


class FakeCoordinator:
    def __init__(self, name="Mower"):
        self.view = {
            "name": name, "mqtt_state": "mowing", "mqtt_key": "t1", "mqtt_age": 600.0,
            "rest_state": "docked",
            "shown_state": "mowing", "has_pose": True,
        }

    def get_watch_view(self, now):
        return dict(self.view)


class WatchdogTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.mqtt = FakeMqtt()
        self.mqtt.connects = 1
        self.health = CollectorHealth(self.mqtt)
        self.health.note_connected()
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
        for state in ("offline", "updating", "unknown"):
            self.mower.view.update(rest_state=state)
            self.watchdog.async_check_after_poll()
        self.assertEqual(await self.rebuilds(), [])

    async def test_missing_sources_are_skipped(self):
        self.mower.view["mqtt_state"] = None
        self.watchdog.async_check_after_poll()
        self.mower.view.update(mqtt_state="mowing", rest_state=None)
        self.watchdog.async_check_after_poll()
        self.assertEqual(await self.rebuilds(), [])

    async def test_never_connected_client_is_not_rebuilt(self):
        self.health = CollectorHealth(FakeMqtt())
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


class LocationSilenceTest(WatchdogTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.health.connected_monotonic = self.now - 1000
        self.last_location = self.now - 10
        # The client's age of the last location message, on the test's clock.
        self.health.location_age = lambda device_id: (
            None if self.last_location is None else self.now - self.last_location
        )

    async def test_silence_while_mowing_rebuilds(self):
        self.watchdog.async_check_silence()
        self.assertEqual(await self.rebuilds(), [])
        self.now += 180
        self.watchdog.async_check_silence()
        reasons = await self.rebuilds()
        self.assertEqual(len(reasons), 1)
        self.assertIn("no location message for 190 s while Mower runs", reasons[0])

    async def test_rest_saying_it_runs_is_enough(self):
        self.mower.view.update(shown_state="docked", rest_state="returning")
        self.now += 600
        self.watchdog.async_check_silence()
        self.assertEqual(len(await self.rebuilds()), 1)

    async def test_a_mapping_mower_quiet_for_ten_minutes_is_left_alone(self):
        # A map edit sends poses only where the mower halts, minutes apart.
        self.mower.view.update(shown_state="mapping", rest_state="mapping")
        self.now += 600
        self.watchdog.async_check_silence()
        self.assertEqual(await self.rebuilds(), [])

    async def test_docked_mower_is_quiet_by_design(self):
        self.mower.view.update(shown_state="docked", rest_state="docked")
        self.now += 3600
        self.watchdog.async_check_silence()
        self.assertEqual(await self.rebuilds(), [])

    async def test_mower_without_positions_is_not_watched(self):
        self.mower.view["has_pose"] = False
        self.now += 3600
        self.watchdog.async_check_silence()
        self.assertEqual(await self.rebuilds(), [])

    async def test_disconnected_client_is_left_to_reconnect(self):
        self.health.note_disconnected()
        self.now += 3600
        self.watchdog.async_check_silence()
        self.assertEqual(await self.rebuilds(), [])

    async def test_silence_counts_from_the_connect(self):
        self.last_location = None
        self.health.connected_monotonic = self.now - 100
        self.watchdog.async_check_silence()
        self.assertEqual(await self.rebuilds(), [])
        self.now += 80
        self.watchdog.async_check_silence()
        self.assertEqual(len(await self.rebuilds()), 1)

    async def test_both_rules_share_the_debounce(self):
        self.watchdog.async_check_after_poll()  # rule 1 rebuilds
        self.now += 200
        self.watchdog.async_check_silence()  # silent, but inside 5 min
        self.assertEqual(len(await self.rebuilds()), 1)
        self.now += 100
        self.watchdog.async_check_silence()
        self.assertEqual(len(await self.rebuilds()), 2)

    async def test_start_and_stop_the_periodic_check(self):
        self.watchdog.async_start()
        self.assertIsNotNone(self.watchdog._cancel_timer)
        self.watchdog.async_stop()
        self.assertIsNone(self.watchdog._cancel_timer)

    async def test_timer_runs_the_check_on_the_event_loop(self):
        # A plain function would be an executor job; creating the rebuild
        # task from that thread is refused by HA.
        with patch("custom_components.navimow.watchdog.async_track_time_interval") as track:
            self.watchdog.async_start()
        action = track.call_args.args[1]
        self.assertEqual(HassJob(action).job_type, HassJobType.Callback)
        self.now += 180
        action(None)
        self.assertEqual(len(await self.rebuilds()), 1)
