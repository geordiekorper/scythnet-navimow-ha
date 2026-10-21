"""The observed MowerAPI: every successful status request is reported."""
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from mower_sdk.api import MowerAPI
from mower_sdk.errors import MowerTransportError
from mower_sdk.models import DeviceStatus

from custom_components.navimow.polls import ObservedMowerAPI
from tests.fakes import REST_PAYLOAD


class ObservedApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = ObservedMowerAPI(session=object(), token="t", base_url="https://api.invalid")
        self.statuses = {"dev-1": DeviceStatus.from_dict(dict(REST_PAYLOAD))}

    async def test_a_reply_reaches_the_observer_with_its_time(self):
        heard = Mock()
        self.api.on_statuses = heard
        with patch.object(MowerAPI, "async_get_device_statuses", AsyncMock(return_value=self.statuses)) as request:
            self.assertIs(await self.api.async_get_device_statuses(["dev-1"]), self.statuses)
        request.assert_awaited_once_with(["dev-1"])
        heard.assert_called_once()
        statuses, polled_at = heard.call_args.args
        self.assertIs(statuses, self.statuses)
        self.assertIsInstance(polled_at, datetime)
        self.assertIsNotNone(polled_at.tzinfo)

    async def test_without_an_observer_the_reply_is_just_returned(self):
        with patch.object(MowerAPI, "async_get_device_statuses", AsyncMock(return_value=self.statuses)):
            self.assertIs(await self.api.async_get_device_statuses(["dev-1"]), self.statuses)

    async def test_a_failure_propagates_and_reports_nothing(self):
        heard = Mock()
        self.api.on_statuses = heard
        with patch.object(MowerAPI, "async_get_device_statuses", AsyncMock(side_effect=MowerTransportError("down"))):
            with self.assertRaises(MowerTransportError):
                await self.api.async_get_device_statuses(["dev-1"])
        heard.assert_not_called()
