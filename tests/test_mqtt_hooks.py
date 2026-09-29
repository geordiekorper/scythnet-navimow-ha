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

from custom_components.navimow import _attach_mqtt_hooks
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
        self.disconnects_seen = []

        async def after_disconnect():
            self.disconnects_seen.append(self.health.connected)

        _attach_mqtt_hooks(self.sdk, self.health, [DEVICE], after_disconnect)
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

    async def test_a_disconnect_is_reported_and_then_handed_on(self):
        self.client.on_connect(self.client, None, {}, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        await self.settle()
        self.client.on_disconnect(
            self.client, None, {}, ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"), None
        )
        await self.settle()
        self.assertFalse(self.health.connected)
        self.assertEqual(self.health.disconnects, 1)
        self.assertEqual(self.health.disconnect_reason, "Unspecified error")
        self.assertEqual(self.disconnects_seen, [False])  # after the health was noted

    async def test_no_connack_at_all_is_a_failure(self):
        self.client.on_connect_fail(self.client, None)
        await self.settle()
        self.assertEqual(self.health.connect_failures, 1)
        self.assertEqual(self.health.connect_fail_reason, "connection failed before CONNACK")

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
