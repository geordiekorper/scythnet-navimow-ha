"""Steady REST poll: one call for all mowers, backoff on failure."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
from mower_sdk.errors import MowerAPIError

from custom_components.navimow.const import REST_POLL_MAX_BACKOFF
from custom_components.navimow.rest_poll import RestPoller

DOCKED = {"id": "dev-1", "vehicleState": "isDocked",
          "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 100}],
          "descriptiveCapacityRemaining": "FULL"}


def reply(*devices):
    """The status entries as MowerAPI.async_get_vehicle_status_raw returns them."""
    return list(devices)


class RestPollerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.api = SimpleNamespace(async_get_vehicle_status_raw=AsyncMock(return_value=reply(DOCKED)))
        self.coordinators = {
            "dev-1": SimpleNamespace(apply_rest_status=Mock()),
            "dev-2": SimpleNamespace(apply_rest_status=Mock()),
        }
        self.token = AsyncMock()
        self.poller = RestPoller(self.hass, self.api, self.coordinators, 120, self.token)
        self.poller.async_start()
        self.addCleanup(self.poller.async_stop)

    async def test_one_call_covers_every_mower(self):
        await self.poller.async_poll()
        self.token.assert_awaited_once()
        self.api.async_get_vehicle_status_raw.assert_awaited_once_with(["dev-1", "dev-2"])

    async def test_reply_goes_to_its_mower_whole(self):
        raw = {**DOCKED, "newField": 7}
        self.api.async_get_vehicle_status_raw.return_value = reply(raw, {"id": "stranger"})
        await self.poller.async_poll()
        args = self.coordinators["dev-1"].apply_rest_status.call_args.args
        self.assertEqual(args[0], raw)
        self.assertEqual(args[1], self.poller.last_poll_at)
        self.coordinators["dev-2"].apply_rest_status.assert_not_called()
        self.assertEqual(self.poller.next_delay, 120)

    async def test_failures_back_off_and_recover(self):
        self.api.async_get_vehicle_status_raw.side_effect = MowerAPIError("HTTP 429")
        delays = []
        for _ in range(4):
            await self.poller.async_poll()
            delays.append(self.poller.next_delay)
        self.assertEqual(delays, [240, 480, REST_POLL_MAX_BACKOFF, REST_POLL_MAX_BACKOFF])
        self.assertIn("429", self.poller.last_error)
        self.assertIsNotNone(self.poller.last_error_at)
        self.api.async_get_vehicle_status_raw.side_effect = None
        await self.poller.async_poll()
        self.assertEqual(self.poller.next_delay, 120)
        self.assertIsNone(self.poller.last_error)

    async def test_cloud_error_code_is_a_failure(self):
        # The SDK checks the reply's envelope and raises for an error code.
        self.api.async_get_vehicle_status_raw.side_effect = MowerAPIError("rate limited")
        await self.poller.async_poll()
        self.assertIn("rate limited", self.poller.last_error)
        self.coordinators["dev-1"].apply_rest_status.assert_not_called()

    async def test_token_failure_is_a_failure(self):
        self.token.side_effect = RuntimeError("refresh failed")
        await self.poller.async_poll()
        self.api.async_get_vehicle_status_raw.assert_not_awaited()
        self.assertEqual(self.poller.next_delay, 240)

    async def test_outcome_is_reported_after_the_replies_are_applied(self):
        seen = []
        self.coordinators["dev-1"].apply_rest_status.side_effect = lambda *a: seen.append("applied")
        self.poller.on_result = lambda ok: seen.append(("result", ok))
        await self.poller.async_poll()
        self.assertEqual(seen, ["applied", ("result", True)])

    async def test_first_poll_comes_soon_after_start(self):
        self.assertEqual(self.poller.next_delay, 5)

    async def test_requested_poll_only_moves_sooner(self):
        await self.poller.async_poll()  # now due in a full interval
        self.poller.async_request_poll(5)
        self.assertEqual(self.poller.next_delay, 5)
        self.poller.async_request_poll(60)
        self.assertEqual(self.poller.next_delay, 5)

    async def test_interval_is_clamped_and_reschedules(self):
        self.poller.async_set_interval(10)
        self.assertEqual(self.poller.interval, 30)
        self.assertEqual(self.poller.next_delay, 30)

    async def test_stopped_poller_schedules_nothing(self):
        self.poller.async_stop()
        self.assertIsNone(self.poller.next_delay)
        await self.poller.async_poll()
        self.poller.async_request_poll(5)
        self.assertIsNone(self.poller.next_delay)

    async def test_every_poll_reports_its_outcome(self):
        self.poller.on_result = Mock()
        await self.poller.async_poll()
        self.api.async_get_vehicle_status_raw.side_effect = MowerAPIError("HTTP 500")
        await self.poller.async_poll()
        self.assertEqual([c.args for c in self.poller.on_result.call_args_list], [(True,), (False,)])
        self.api.async_get_vehicle_status_raw.side_effect = None
        await self.poller.async_poll()
        self.assertIsNone(self.poller.last_error_at)


class SchedulingTest(unittest.IsolatedAsyncioTestCase):
    """Deadlines are absolute, and a request made during a poll survives it."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.gate = asyncio.Event()
        self.gate.set()
        self.calls = 0

        async def request(*args, **kwargs):
            self.calls += 1
            await self.gate.wait()
            return reply(DOCKED)

        self.api = SimpleNamespace(async_get_vehicle_status_raw=request)
        self.coordinators = {"dev-1": SimpleNamespace(apply_rest_status=Mock())}
        self.poller = RestPoller(self.hass, self.api, self.coordinators, 120, AsyncMock())
        self.now = 1000.0
        self.poller._clock = lambda: self.now
        self.poller.async_start()
        self.addCleanup(self.poller.async_stop)
        await self.poller.async_poll()  # next poll due at 1120

    async def test_request_keeps_a_poll_that_is_due_sooner(self):
        self.now = 1119.0  # the regular poll is one second away
        self.poller.async_request_poll(5)
        self.assertEqual(self.poller._due_at, 1120.0)
        self.poller.async_request_poll(0.5)
        self.assertEqual(self.poller._due_at, 1119.5)

    async def test_request_during_a_poll_survives_its_end(self):
        self.gate.clear()
        poll = asyncio.create_task(self.poller.async_poll())
        await asyncio.sleep(0)
        self.poller.async_request_poll(5)  # a command while the request is out
        self.now += 2
        self.gate.set()
        await poll
        self.assertEqual(self.poller.next_delay, 3.0)  # not the full interval

    async def test_overlapping_poll_waits_for_the_one_under_way(self):
        self.gate.clear()
        first = asyncio.create_task(self.poller.async_poll())
        await asyncio.sleep(0)
        await self.poller.async_poll()  # a timer firing meanwhile
        self.assertEqual(self.calls, 2)  # the setUp poll and the first only
        self.gate.set()
        await first
        self.assertEqual(self.poller.next_delay, 0.0)  # runs right after

    async def test_interval_change_during_a_poll_applies_at_its_end(self):
        self.gate.clear()
        poll = asyncio.create_task(self.poller.async_poll())
        await asyncio.sleep(0)
        self.poller.async_set_interval(300)
        self.gate.set()
        await poll
        self.assertEqual(self.poller.next_delay, 300)
