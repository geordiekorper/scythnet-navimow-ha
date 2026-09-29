"""The entry's hooks on the real SDK client, driven through paho's own callbacks.

The SDK builds its paho client with callback API version 2, so paho calls
on_connect and on_disconnect with five arguments. These tests call the
callbacks the paho client carries, as paho's thread would, and check what the
entry's health shows once the SDK has run the hooks on the loop. Nothing
connects: the broker name does not resolve and connect() is never called.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import paho.mqtt.client as paho

from mower_sdk.sdk import NavimowSDK
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.navimow import _attach_location_callbacks, _attach_mqtt_hooks
from custom_components.navimow.health import CollectorHealth

DEVICE = SimpleNamespace(id="dev-1")


class HookWiringTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sdk = NavimowSDK(
            broker="wss://broker.example.invalid", port=443, ws_path="/mqtt/1",
            username="1", password="p", auth_headers={"Authorization": "Bearer t"},
            loop=asyncio.get_running_loop(), records=[],
        )
        self.health = CollectorHealth(self.sdk.mqtt)
        self.refreshes = []

        async def on_connect_fail():
            self.refreshes.append(self.health.connect_failures)

        _attach_mqtt_hooks(self.sdk, self.health, [DEVICE], on_connect_fail)
        self.client = self.sdk.mqtt.client

    async def settle(self):
        # The SDK hands each hook to the loop as a task: let it run.
        for _ in range(3):
            await asyncio.sleep(0)

    async def test_a_successful_connect_marks_the_health_connected(self):
        self.client.on_connect(self.client, None, {}, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        await self.settle()
        self.assertTrue(self.health.connected)
        self.assertEqual(self.health.connects, 1)
        self.assertEqual(self.health.client_id, self.sdk.mqtt.client_id)
        self.assertEqual(self.health.status, "ok")

    async def test_a_refused_connect_is_a_failure_with_pahos_reason(self):
        self.client.on_connect(self.client, None, {}, ReasonCode(PacketTypes.CONNACK, "Not authorized"), None)
        await self.settle()
        self.assertFalse(self.health.connected)
        self.assertEqual(self.health.connect_failures, 1)
        self.assertIn("Not authorized", self.health.connect_fail_reason)
        self.assertIsNotNone(self.health.connect_failed_at)
        self.assertEqual(self.health.status, "disconnected")
        self.assertEqual(self.refreshes, [1])  # the credential refresh runs after it

    async def test_a_disconnect_is_reported_and_starts_no_refresh(self):
        self.client.on_connect(self.client, None, {}, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        await self.settle()
        self.client.on_disconnect(
            self.client, None, {}, ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"), None
        )
        await self.settle()
        self.assertFalse(self.health.connected)
        self.assertEqual(self.health.disconnects, 1)
        self.assertEqual(self.health.disconnect_reason, "Unspecified error")
        self.assertEqual(self.refreshes, [])  # paho reconnects with the stored values

    async def test_no_connack_at_all_is_a_failure(self):
        self.client.on_connect_fail(self.client, None)
        await self.settle()
        self.assertEqual(self.health.connect_failures, 1)
        self.assertEqual(self.health.connect_fail_reason, "connection failed before CONNACK")
        self.assertEqual(self.refreshes, [1])

    async def test_a_rebuilt_client_reports_too(self):
        # rebuild() connects the new client: keep paho off the network.
        with patch.object(paho.Client, "connect_async"), patch.object(paho.Client, "loop_start"):
            self.sdk.mqtt.rebuild(reason="test")
        client = self.sdk.mqtt.client
        self.assertIsNot(client, self.client)
        client.on_connect(client, None, {}, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        await self.settle()
        self.assertTrue(self.health.connected)
        self.assertEqual((self.health.rebuilds, self.health.last_rebuild_reason), (1, "test"))
        self.assertEqual(self.health.client_id, self.sdk.mqtt.client_id)

    async def test_every_message_reaches_the_message_listeners(self):
        heard = []
        self.health.async_add_message_listener(heard.append)
        for topic, payload in (
            ("/downlink/vehicle/dev-1/realtimeDate/state", b'{"state":"idle"}'),
            ("/downlink/vehicle/dev-1/realtimeDate/location", b"[]"),
            ("/some/other/topic", b"x"),
        ):
            self.client.on_message(self.client, None, SimpleNamespace(topic=topic, payload=payload))
        await self.settle()
        self.assertEqual(heard, ["dev-1", "dev-1"])
        self.assertIsNotNone(self.health.last_message_at("dev-1"))
        self.assertIsNotNone(self.health.location_age("dev-1"))


class LocationCallbackTest(unittest.IsolatedAsyncioTestCase):
    """Location messages through the real SDK facade to the coordinator."""

    async def asyncSetUp(self):
        self.sdk = NavimowSDK(
            broker="wss://broker.example.invalid", port=443, ws_path="/mqtt/1",
            loop=asyncio.get_running_loop(), records=[], subscribe_location=True,
        )
        self.coordinator = SimpleNamespace(ingested=[], rejected=[])
        self.coordinator.ingest_location = self.coordinator.ingested.append
        self.coordinator.record_rejected = lambda *args: self.coordinator.rejected.append(args)
        _attach_location_callbacks(self.sdk, {"dev-1": self.coordinator})
        self.client = self.sdk.mqtt.client

    async def deliver(self, device_id, channel, payload):
        topic = f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"
        self.client.on_message(self.client, None, SimpleNamespace(topic=topic, payload=payload))
        for _ in range(3):
            await asyncio.sleep(0)

    async def test_each_applied_entry_reaches_the_coordinator(self):
        await self.deliver("dev-1", "location", (
            b'[{"type":1,"postureX":"1.5","postureY":"0.25","time":1700000000000},'
            b'{"type":1,"postureX":"2.5","postureY":"0.25","time":1700000002000}]'
        ))
        self.assertEqual([m.location.x for m in self.coordinator.ingested], [1.5, 2.5])
        self.assertEqual(self.coordinator.rejected, [])

    async def test_a_location_rejection_is_recorded_with_its_reasons(self):
        payload = b'[{"type":3,"partitionIds":[2],"time":5,"extra":1}]'
        await self.deliver("dev-1", "location", payload)
        self.assertEqual(self.coordinator.ingested, [])
        ((channel, topic, reason, text, reasons),) = self.coordinator.rejected
        self.assertEqual((channel, reason), ("location", "implausible_time"))
        self.assertEqual(topic, "/downlink/vehicle/dev-1/realtimeDate/location")
        self.assertEqual(text, payload.decode())
        self.assertEqual(set(reasons), {"implausible_time", "unknown_field"})

    async def test_other_devices_and_other_channels_are_not_recorded_here(self):
        await self.deliver("dev-2", "location", b"not json")
        await self.deliver("dev-1", "state", b"not json")
        self.assertEqual(self.coordinator.rejected, [])
