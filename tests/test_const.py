"""The mower status to lawn-mower activity table."""
import unittest

from homeassistant.components.lawn_mower import LawnMowerActivity
from mower_sdk.models import MowerStatus

from custom_components.navimow.const import MOWER_STATUS_TO_ACTIVITY


class ActivityTableTest(unittest.TestCase):
    def test_every_sdk_status_has_an_activity(self):
        # A status the SDK adds later must not silently show no activity.
        activities = {activity.value for activity in LawnMowerActivity}
        for status in MowerStatus:
            self.assertIn(status.value, MOWER_STATUS_TO_ACTIVITY, status)
            self.assertIn(MOWER_STATUS_TO_ACTIVITY[status.value], activities, status)

    def test_mapping_and_updating_follow_the_idle_row_and_offline_is_an_error(self):
        idle = MOWER_STATUS_TO_ACTIVITY["idle"]
        self.assertEqual(MOWER_STATUS_TO_ACTIVITY["mapping"], idle)
        self.assertEqual(MOWER_STATUS_TO_ACTIVITY["updating"], idle)
        self.assertEqual(MOWER_STATUS_TO_ACTIVITY["offline"], "error")
