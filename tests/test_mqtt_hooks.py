"""The entry's wiring on the real SDK client, driven through paho's own callbacks.

A NavimowClient on a fake MowerAPI builds the real NavimowSDK, whose paho
client is kept off the network (connect_async and loop_start do nothing).
The tests call the callbacks the paho client carries, as paho's thread
would, and check what the entry's health and coordinator show once the
client has run its hooks on the loop.
"""
import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import paho.mqtt.client as paho
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from homeassistant.core import HomeAssistant
import tempfile

from mower_sdk.errors import MowerTransportError
from mower_sdk.models import Device, DeviceStatus, MqttConnectionInfo
from mower_sdk.navimow_client import NavimowClient

from custom_components.navimow import error_handler, facade_hooks, status_observer
from custom_components.navimow.coordinator import NavimowCoordinator
from custom_components.navimow.health import CollectorHealth
from tests.fakes import REST_PAYLOAD

DEVICE = Device(id="dev-1", name="Mower", model="X430", firmware_version="1.0", serial_number="SN1")
INFO = MqttConnectionInfo(
    broker="wss://broker.example.invalid", port=443, ws_path="/mqtt/1", username="1", password="p"
)


class FakeAPI:
    """The MowerAPI calls the client makes, answered from the test; reports
    each status reply as ObservedMowerAPI does."""

    def __init__(self):
        self.token = "t"
        self.statuses = {"dev-1": DeviceStatus.from_dict(dict(REST_PAYLOAD))}
        self.fail_polls = False
        self.polls = 0
        self.info_requests = 0
        self.on_statuses = None

    def set_token(self, token):
        self.token = token

    async def async_get_devices(self):
        return [DEVICE]

    async def async_get_mqtt_connection_info(self):
        self.info_requests += 1
        return INFO

    async def async_get_mqtt_user_info(self):
        # The facade's credential refresh reads the raw reply.
        self.info_requests += 1
        return {"mqttHost": INFO.broker, "mqttUrl": INFO.ws_path, "userName": "2", "pwdInfo": "p2"}

    async def async_get_device_statuses(self, device_ids):
        self.polls += 1
        if self.fail_polls:
            raise MowerTransportError("down")
        if self.on_statuses is not None:
            self.on_statuses(self.statuses, datetime.now(timezone.utc))
        return self.statuses


class ClientWiringTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.api = FakeAPI()
        self.health = CollectorHealth()
        self.entry = SimpleNamespace(async_start_reauth=lambda hass: None)
        self.client = NavimowClient(
            self.api, devices=[DEVICE], loop=asyncio.get_running_loop(),
            poll_interval=None, reconnect_min_delay=1, reconnect_max_delay=60,
        )
        self.health.client = self.client
        self.client.on_connection(self.health.note_connection)
        self.client.on_error(error_handler(self.hass, self.entry, self.health))
        self.coordinator = NavimowCoordinator(
            hass=self.hass, client=self.client, device=DEVICE, config_entry=None
        )
        self.coordinator.async_register()
        self.api.on_statuses = status_observer({"dev-1": self.coordinator}, self.health)
        self.patches = [
            patch.object(paho.Client, "connect_async"),
            patch.object(paho.Client, "loop_start"),
            patch.object(paho.Client, "loop_stop"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(self.client.async_disconnect)

    async def connect(self):
        await self.client.async_connect()
        self.paho = self.client.mqtt.client
        facade_hooks(self.client, self.health, {"dev-1": self.coordinator})

    async def settle(self):
        # The SDK hands each hook to the loop as a task: let it run.
        for _ in range(4):
            await asyncio.sleep(0)

    def connack(self, text):
        self.paho.on_connect(self.paho, None, {}, ReasonCode(PacketTypes.CONNACK, text), None)

    async def deliver(self, channel, payload, device_id="dev-1"):
        topic = f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"
        self.paho.on_message(self.paho, None, SimpleNamespace(topic=topic, payload=payload))
        await self.settle()

    async def test_the_connect_polls_once_and_the_entities_show_the_reply(self):
        await self.connect()
        self.assertEqual(self.api.polls, 1)
        state = self.coordinator.get_device_state()
        self.assertEqual(state.status.value, "docked")
        self.assertEqual(self.coordinator.get_data_source(), "http_fallback")
        self.assertEqual(self.coordinator.get_rest_details()[0], "isDocked")
        self.assertEqual(self.health.status, "starting")  # the feed is not up yet

    async def test_a_successful_connect_marks_the_health_connected(self):
        await self.connect()
        self.connack("Success")
        await self.settle()
        self.assertTrue(self.health.connected)
        self.assertEqual(self.health.connects, 1)
        self.assertEqual(self.health.client_id, self.client.mqtt.client_id)
        self.assertEqual(self.health.status, "ok")

    async def test_a_refused_connect_is_a_failure_and_the_client_recovers(self):
        await self.connect()
        self.assertEqual(self.api.info_requests, 1)
        self.connack("Not authorized")
        await self.settle()
        self.assertFalse(self.health.connected)
        self.assertEqual(self.health.connect_failures, 1)
        self.assertIn("Not authorized", self.health.connect_fail_reason)
        self.assertIsNotNone(self.health.connect_failed_at)
        self.assertEqual(self.health.status, "disconnected")
        # the recovery: the broker credentials fetched again by the client
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(self.api.info_requests, 2)

    async def test_a_disconnect_is_reported(self):
        await self.connect()
        self.connack("Success")
        await self.settle()
        self.paho.on_disconnect(
            self.paho, None, {}, ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"), None
        )
        await self.settle()
        self.assertFalse(self.health.connected)
        self.assertEqual(self.health.disconnects, 1)
        self.assertEqual(self.health.disconnect_reason, "Unspecified error")
        self.assertEqual(self.api.info_requests, 1)  # paho reconnects with the stored values

    async def test_every_message_reaches_the_message_listeners(self):
        await self.connect()
        heard = []
        self.health.async_add_message_listener(heard.append)
        await self.deliver("state", b'{"state":"isRunning","timestamp":1700000000000}')
        await self.deliver("location", b"[]")
        self.paho.on_message(self.paho, None, SimpleNamespace(topic="/some/other/topic", payload=b"x"))
        await self.settle()
        self.assertEqual(heard, ["dev-1", "dev-1"])
        self.assertIsNotNone(self.health.last_message_at("dev-1"))
        self.assertIsNotNone(self.health.location_age("dev-1"))

    async def test_a_state_message_becomes_the_shown_state(self):
        await self.connect()
        await self.deliver("state", b'{"state":"isRunning","timestamp":1700000000000,"battery":80}')
        state = self.coordinator.get_device_state()
        self.assertEqual((state.status.value, state.battery), ("mowing", 80))
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_push")
        self.assertEqual(self.coordinator.get_source_details()["mqtt_raw_state"], "isRunning")

    async def test_each_applied_pose_reaches_the_coordinator_with_its_record(self):
        await self.connect()
        seen = []
        self.coordinator.async_add_listener(lambda: seen.append(self.coordinator.get_device_location().x))
        await self.deliver("location", (
            b'[{"type":1,"postureX":"1.5","postureY":"0.25","time":1700000000000},'
            b'{"type":1,"postureX":"2.5","postureY":"0.25","time":1700000002000}]'
        ))
        self.assertEqual(seen, [1.5, 2.5])
        self.assertEqual(self.coordinator.get_rejected()[0], 0)

    async def test_a_rejected_message_is_recorded_with_its_reasons(self):
        await self.connect()
        payload = b'[{"type":3,"partitionIds":[2],"time":5,"extra":1}]'
        await self.deliver("location", payload)
        count, latest = self.coordinator.get_rejected()
        self.assertEqual(count, 1)
        self.assertEqual((latest["channel"], latest["reason"]), ("location", "implausible_time"))
        self.assertEqual(latest["topic"], "/downlink/vehicle/dev-1/realtimeDate/location")
        self.assertEqual(latest["payload"], payload.decode())
        self.assertEqual(set(latest["reasons"]), {"implausible_time", "unknown_field"})

    async def test_another_mowers_rejection_is_not_recorded_here(self):
        await self.connect()
        await self.deliver("location", b"not json", device_id="dev-2")
        self.assertEqual(self.coordinator.get_rejected()[0], 0)

    async def test_a_late_state_message_is_recorded_and_not_applied(self):
        await self.connect()
        await self.deliver("state", b'{"state":"isDocked","timestamp":1700000060000}')
        await self.deliver("state", b'{"state":"isRunning","timestamp":1700000000000,"signal":-60}')
        self.assertEqual(self.coordinator.get_device_state().status.value, "docked")
        count, latest = self.coordinator.get_rejected()
        self.assertEqual(count, 1)
        self.assertEqual((latest["channel"], latest["reason"]), ("state", "stale"))
        self.assertEqual(json.loads(latest["payload"])["timestamp"], 1700000000000)

    async def test_an_event_message_is_recorded_whole(self):
        await self.connect()
        await self.deliver("event", b'{"event":"bladeBlocked"}')
        count, latest = self.coordinator.get_rejected()
        self.assertEqual((count, latest["channel"], latest["reason"]), (1, "event", "unknown_channel"))

    async def test_a_restored_record_is_installed_before_the_connect(self):
        self.coordinator.restore_location("pose", {"x": 2.0, "y": 0.3, "pose_at": 1700000000000})
        self.coordinator.async_finish_restore()
        await self.connect()
        record = self.client.sdk.get_cached_location("dev-1")
        self.assertEqual((record.x, record.marks.get(1)), (2.0, 1700000000000))
        self.assertTrue(self.coordinator.is_group_restored("pose"))
        # the connect's poll carried the installed record: still restored
        self.assertEqual(self.coordinator.get_device_location().x, 2.0)
        # a late pose is rejected as stale; a newer one ends the restore
        await self.deliver("location", b'[{"type":1,"postureX":"9.0","postureY":"9.0","time":1699999999000}]')
        self.assertEqual(self.coordinator.get_rejected()[1]["reason"], "stale")
        self.assertTrue(self.coordinator.is_group_restored("pose"))
        await self.deliver("location", b'[{"type":1,"postureX":"1.5","postureY":"0.25","time":1700000002000}]')
        self.assertFalse(self.coordinator.is_group_restored("pose"))
        self.assertEqual(self.coordinator.get_device_location().x, 1.5)

    async def test_poses_before_any_status_reach_the_sensors(self):
        # The connect's poll fails; the feed delivers poses before any state
        # message: the entries are applied from the facade's entry stream.
        self.api.fail_polls = True
        await self.connect()
        self.assertEqual(self.health.status, "starting")
        self.assertIsNone(self.coordinator.get_device_state())
        seen = []
        self.coordinator.async_add_listener(lambda: seen.append(self.coordinator.get_device_location().x))
        await self.deliver("location", (
            b'[{"type":1,"postureX":"1.0","postureY":"0.25","time":1700000000000},'
            b'{"type":1,"postureX":"2.0","postureY":"0.25","time":1700000002000},'
            b'{"type":1,"postureX":"3.0","postureY":"0.25","time":1700000004000}]'
        ))
        self.assertEqual(seen, [1.0, 2.0, 3.0])
        self.assertIsNone(self.coordinator.get_device_state())  # still no status half
        self.api.fail_polls = False
        await self.client.async_poll()
        self.assertEqual(self.coordinator.get_device_state().status.value, "docked")
        self.assertEqual(self.coordinator.get_device_location().x, 3.0)
        # the poll's own row (rest_status) and the state, both with the same record
        self.assertEqual(seen, [1.0, 2.0, 3.0, 3.0, 3.0])

    async def test_a_poll_failure_shows_until_the_next_success(self):
        await self.connect()
        self.connack("Success")
        await self.settle()
        self.api.fail_polls = True
        with self.assertRaises(MowerTransportError):
            await self.client.async_poll()
        # a manual poll raises; the clock's poll reports through on_error
        self.client._report("poll", MowerTransportError("down"))
        self.assertEqual(self.health.status, "poll_failing")
        self.api.fail_polls = False
        await self.client.async_poll()
        self.assertEqual(self.health.status, "ok")
        self.assertEqual(self.api.polls, 3)
