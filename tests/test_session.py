"""The MQTT session: connect once, broker credentials after a refused
connection, the bearer after a token refresh, rebuilds through the SDK."""
import asyncio
import unittest
from types import SimpleNamespace

from mower_sdk.errors import MowerAPIError, MowerRateLimitedError

from custom_components.navimow.session import MqttSession


class FakeHass:
    """Runs executor jobs inline, recording them. Each job yields to the loop
    first, as a real executor job does, and waits while ``gate`` is clear."""

    def __init__(self, calls):
        self.calls = calls
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = asyncio.Event()

    async def async_add_executor_job(self, func, *args):
        self.calls.append("executor")
        self.entered.set()
        await asyncio.sleep(0)
        await self.gate.wait()
        return func(*args)


class FakeSdk:
    def __init__(self, calls):
        self.calls = calls
        self.refresh_result = True
        self.mqtt = SimpleNamespace(rebuild=self._rebuild)

    def connect(self):
        self.calls.append(("connect",))

    def update_mqtt_credentials(self, username=None, password=None, auth_headers=None, *, force_reconnect=False):
        self.calls.append(("update", auth_headers))

    async def async_refresh_broker_credentials(self, api, *, auth_headers=None, force_reconnect=False, cooldown=65.0):
        self.calls.append(("refresh_broker", auth_headers))
        if isinstance(self.refresh_result, Exception):
            raise self.refresh_result
        return self.refresh_result

    def _rebuild(self, username=None, password=None, auth_headers=None, *, reason=None):
        self.calls.append(("rebuild", auth_headers, reason))


class FakeOAuth:
    def __init__(self, calls, token="t2"):
        self.calls = calls
        self.token = {"access_token": token}
        self.fail = False

    async def async_ensure_token_valid(self):
        self.calls.append(("oauth",))
        if self.fail:
            raise RuntimeError("network down")


class FakeHealth:
    def __init__(self, calls):
        self.calls = calls

    def note_credential_refresh(self):
        self.calls.append(("note_refresh",))

    def note_rebuild(self):
        self.calls.append(("note_rebuild",))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class SessionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.sdk = FakeSdk(self.calls)
        self.oauth = FakeOAuth(self.calls)
        self.api = SimpleNamespace(set_token=lambda token: self.calls.append(("set_token", token)))
        self.unload_flag = [False]
        self.lock = asyncio.Lock()
        self.hass = FakeHass(self.calls)
        self.session = MqttSession(
            self.hass, self.sdk, self.api, self.oauth, FakeHealth(self.calls),
            self.unload_flag, self.lock, token="t1",
        )

    def sdk_calls(self):
        return [c for c in self.calls if c != "executor" and c[0] in ("connect", "update", "rebuild", "refresh_broker")]


class StartTest(SessionTestCase):
    async def test_start_without_a_pending_bearer_only_connects(self):
        await self.session.start()
        self.assertEqual(self.calls, ["executor", ("connect",)])
        await self.session.start()  # once
        self.assertEqual(self.sdk_calls(), [("connect",)])

    async def test_a_token_rotated_before_start_is_applied_by_start_and_nothing_before(self):
        await self.session.async_push_bearer("t2")
        self.assertEqual(self.calls, [])  # the client is left alone before start()
        await self.session.start()
        self.assertEqual(self.sdk_calls(), [("update", bearer("t2")), ("connect",)])
        await self.session.async_push_bearer("t2")  # already applied
        self.assertEqual(len(self.sdk_calls()), 2)

    async def test_a_rotation_back_before_start_leaves_nothing_pending(self):
        await self.session.async_push_bearer("t2")
        await self.session.async_push_bearer("t1")
        await self.session.start()
        self.assertEqual(self.sdk_calls(), [("connect",)])

    async def test_an_unloading_entry_does_not_connect(self):
        self.unload_flag[0] = True
        await self.session.start()
        self.assertEqual(self.calls, [])


class PushBearerTest(SessionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.session.start()
        self.calls.clear()

    async def test_a_changed_token_is_pushed_once_for_concurrent_coordinators(self):
        await asyncio.gather(self.session.async_push_bearer("t2"), self.session.async_push_bearer("t2"))
        self.assertEqual(self.sdk_calls(), [("update", bearer("t2"))])
        self.assertIn("executor", self.calls)

    async def test_an_unchanged_token_is_not_pushed(self):
        await self.session.async_push_bearer("t1")
        self.assertEqual(self.calls, [])


class RefreshCredentialsTest(SessionTestCase):
    async def test_a_refused_connection_refreshes_the_token_then_the_broker_credentials(self):
        await self.session.async_refresh_credentials()
        self.assertEqual(self.calls, [
            ("oauth",), ("set_token", "t2"), ("refresh_broker", bearer("t2")),
            ("note_refresh",), ("note_rebuild",),
        ])
        await self.session.async_push_bearer("t2")  # the refresh applied it
        self.assertEqual(len(self.sdk_calls()), 1)

    async def test_nothing_fetched_still_applies_a_refreshed_bearer(self):
        self.sdk.refresh_result = False  # the SDK's cooldown
        await self.session.async_refresh_credentials()
        self.assertNotIn(("note_refresh",), self.calls)
        self.assertEqual(self.sdk_calls(), [("refresh_broker", bearer("t2")), ("update", bearer("t2"))])
        await self.session.async_refresh_credentials()  # the same token: applied once
        self.assertEqual(self.sdk_calls()[-1], ("refresh_broker", bearer("t2")))

    async def test_nothing_fetched_with_an_unchanged_token_touches_nothing(self):
        self.oauth.token = {"access_token": "t1"}
        self.sdk.refresh_result = False
        await self.session.async_refresh_credentials()
        self.assertEqual(self.sdk_calls(), [("refresh_broker", bearer("t1"))])

    async def test_an_api_error_is_logged_and_the_bearer_still_applied(self):
        for error in (MowerRateLimitedError("too early"), MowerAPIError("CODE_OAUTH_INFO_ILLEGAL")):
            self.sdk.refresh_result = error
            with self.assertLogs("custom_components.navimow.session", "WARNING"):
                await self.session.async_refresh_credentials()
        self.assertNotIn(("note_refresh",), self.calls)
        self.assertEqual([c for c in self.sdk_calls() if c[0] == "update"], [("update", bearer("t2"))])
        self.assertFalse(self.lock.locked())

    async def test_a_failed_token_refresh_still_asks_for_the_broker_credentials(self):
        self.oauth.fail = True
        with self.assertLogs("custom_components.navimow.session", "WARNING"):
            await self.session.async_refresh_credentials()
        self.assertIn(("refresh_broker", None), self.calls)

    async def test_unloading_skips_it(self):
        self.unload_flag[0] = True
        await self.session.async_refresh_credentials()
        self.assertEqual(self.calls, [])

    async def test_a_refresh_already_running_is_not_repeated(self):
        async with self.lock:
            await self.session.async_refresh_credentials()
        self.assertEqual(self.calls, [])


class RebuildTest(SessionTestCase):
    async def test_a_rebuild_refreshes_the_token_and_rebuilds_in_the_executor(self):
        await self.session.async_rebuild("watchdog: silence")
        self.assertEqual(self.calls, [
            ("oauth",), ("set_token", "t2"), "executor",
            ("rebuild", bearer("t2"), "watchdog: silence"), ("note_rebuild",),
        ])
        self.assertNotIn("refresh_broker", [c[0] for c in self.calls if c != "executor"])

    async def test_a_rebuild_without_a_token_keeps_the_stored_bearer(self):
        self.oauth.fail = True
        with self.assertLogs("custom_components.navimow.session", "WARNING"):
            await self.session.async_rebuild("watchdog: silence")
        self.assertIn(("rebuild", None, "watchdog: silence"), self.calls)

    async def test_unloading_skips_it(self):
        self.unload_flag[0] = True
        await self.session.async_rebuild("watchdog: silence")
        self.assertEqual(self.calls, [])

    async def test_a_rebuild_in_flight_holds_the_lock_the_unload_waits_for(self):
        self.hass.gate.clear()
        rebuild = asyncio.create_task(self.session.async_rebuild("watchdog: silence"))
        await self.hass.entered.wait()
        self.assertTrue(self.lock.locked())
        await self.session.async_rebuild("again")  # a second request while one runs: dropped
        unload = asyncio.create_task(self.lock.acquire())
        await asyncio.sleep(0)
        self.assertFalse(unload.done())
        self.hass.gate.set()
        await rebuild
        await unload
        self.lock.release()
        self.assertEqual([c for c in self.sdk_calls() if c[0] == "rebuild"],
                         [("rebuild", bearer("t2"), "watchdog: silence")])
