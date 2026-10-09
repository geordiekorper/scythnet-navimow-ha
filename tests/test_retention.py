"""The recorder-retention suggestion: an ignorable repair issue."""
import tempfile
import unittest
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import DATA_INSTANCE

from custom_components.navimow import async_remove_entry
from custom_components.navimow.const import DOMAIN
from custom_components.navimow.retention import (
    SUGGESTED_KEEP_DAYS,
    async_check_recorder_retention,
    retention_issue_id,
)


class RetentionIssueTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        await ir.async_load(self.hass)
        self.registry = ir.async_get(self.hass)
        self.entry = SimpleNamespace(entry_id="entry-1")

    def recorder_keeps(self, days):
        self.hass.data[DATA_INSTANCE] = SimpleNamespace(keep_days=days)

    def issue(self):
        return self.registry.async_get_issue(DOMAIN, retention_issue_id(self.entry))

    async def test_a_short_retention_raises_an_ignorable_warning(self):
        self.recorder_keeps(10)
        self.assertEqual(async_check_recorder_retention(self.hass, self.entry), 10)
        issue = self.issue()
        self.assertIsNotNone(issue)
        self.assertFalse(issue.is_fixable)
        self.assertEqual(issue.severity, ir.IssueSeverity.WARNING)
        self.assertEqual(issue.translation_key, "recorder_retention")
        self.assertEqual(
            issue.translation_placeholders, {"days": "10", "suggested": str(SUGGESTED_KEEP_DAYS)}
        )
        self.assertIsNone(issue.dismissed_version)

    async def test_enough_retention_raises_nothing_and_clears_an_earlier_issue(self):
        self.recorder_keeps(10)
        async_check_recorder_retention(self.hass, self.entry)
        self.assertIsNotNone(self.issue())
        self.recorder_keeps(SUGGESTED_KEEP_DAYS)
        self.assertEqual(async_check_recorder_retention(self.hass, self.entry), 30)
        self.assertIsNone(self.issue())
        self.recorder_keeps(90)
        async_check_recorder_retention(self.hass, self.entry)  # nothing to clear: no error
        self.assertIsNone(self.issue())

    async def test_without_a_recorder_nothing_is_raised(self):
        self.assertIsNone(async_check_recorder_retention(self.hass, self.entry))
        self.assertIsNone(self.issue())

    async def test_an_ignored_issue_stays_ignored_when_raised_again(self):
        self.recorder_keeps(10)
        async_check_recorder_retention(self.hass, self.entry)
        self.registry.async_ignore(DOMAIN, retention_issue_id(self.entry), True)
        self.assertIsNotNone(self.issue().dismissed_version)
        async_check_recorder_retention(self.hass, self.entry)  # the next setup
        self.assertIsNotNone(self.issue().dismissed_version)

    async def test_one_issue_per_entry(self):
        self.recorder_keeps(10)
        other = SimpleNamespace(entry_id="entry-2")
        async_check_recorder_retention(self.hass, self.entry)
        async_check_recorder_retention(self.hass, other)
        self.assertNotEqual(retention_issue_id(self.entry), retention_issue_id(other))
        self.assertIsNotNone(self.registry.async_get_issue(DOMAIN, retention_issue_id(other)))

    async def test_removing_the_entry_deletes_the_issue(self):
        self.recorder_keeps(10)
        async_check_recorder_retention(self.hass, self.entry)
        await async_remove_entry(self.hass, self.entry)
        self.assertIsNone(self.issue())
