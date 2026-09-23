"""Steady REST poll: one call for all mowers, backoff on failure."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
from mower_sdk.errors import MowerAPIError

from custom_components.navimow.const import REST_POLL_MAX_BACKOFF
from custom_components.navimow.rest_poll import RestPoller, async_fetch_statuses

DOCKED = {"id": "dev-1", "vehicleState": "isDocked",
          "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 100}],
          "descriptiveCapacityRemaining": "FULL"}


def reply(*devices, code=1):
    return {"code": code, "desc": "ok" if code == 1 else "rate limited",
            "data": {"payload": {"devices": list(devices)}}}


class RestPollerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.api = SimpleNamespace(_async_request=AsyncMock(return_value=reply(DOCKED)))
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
        self.api._async_request.assert_awaited_once_with(
            "POST", "/openapi/smarthome/getVehicleStatus",
            data={"devices": [{"id": "dev-1"}, {"id": "dev-2"}]},
        )

    async def test_reply_goes_to_its_mower_whole(self):
        raw = {**DOCKED, "newField": 7}
        self.api._async_request.return_value = reply(raw, {"id": "stranger"})
        await self.poller.async_poll()
        args = self.coordinators["dev-1"].apply_rest_status.call_args.args
        self.assertEqual(args[0], raw)
        self.assertEqual(args[1], self.poller.last_poll_at)
        self.coordinators["dev-2"].apply_rest_status.assert_not_called()
        self.assertEqual(self.poller.next_delay, 120)

    async def test_failures_back_off_and_recover(self):
        self.api._async_request.side_effect = MowerAPIError("HTTP 429")
        delays = []
        for _ in range(4):
            await self.poller.async_poll()
            delays.append(self.poller.next_delay)
        self.assertEqual(delays, [240, 480, REST_POLL_MAX_BACKOFF, REST_POLL_MAX_BACKOFF])
        self.assertIn("429", self.poller.last_error)
        self.assertIsNotNone(self.poller.last_error_at)
        self.api._async_request.side_effect = None
        await self.poller.async_poll()
        self.assertEqual(self.poller.next_delay, 120)
        self.assertIsNone(self.poller.last_error)

    async def test_cloud_error_code_is_a_failure(self):
        self.api._async_request.return_value = reply(code=0)
        await self.poller.async_poll()
        self.assertIn("rate limited", self.poller.last_error)
        self.coordinators["dev-1"].apply_rest_status.assert_not_called()

    async def test_token_failure_is_a_failure(self):
        self.token.side_effect = RuntimeError("refresh failed")
        await self.poller.async_poll()
        self.api._async_request.assert_not_awaited()
        self.assertEqual(self.poller.next_delay, 240)

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
        self.api._async_request.side_effect = MowerAPIError("HTTP 500")
        await self.poller.async_poll()
        self.assertEqual(self.poller.on_result.call_count, 2)
        self.api._async_request.side_effect = None
        await self.poller.async_poll()
        self.assertIsNone(self.poller.last_error_at)


class FetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_dict_entries_are_skipped(self):
        api = SimpleNamespace(_async_request=AsyncMock(return_value=reply(DOCKED, "junk")))
        self.assertEqual(await async_fetch_statuses(api, ["dev-1"]), [DOCKED])

    async def test_missing_payload_is_empty(self):
        api = SimpleNamespace(_async_request=AsyncMock(return_value={"code": 1}))
        self.assertEqual(await async_fetch_statuses(api, ["dev-1"]), [])
