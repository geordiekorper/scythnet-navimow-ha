"""A suggestion on the recorder's retention, as an ignorable repair issue.

The integration's entities are the mower's whole record: every pose, task
reading, state report and status poll. A fresh Home Assistant keeps 10 days
of history (the recorder's purge_keep_days), so on a new install the record
would vanish after ten days with nothing to say why. The integration cannot
change the recorder's settings, but it can read them at setup and suggest
more: a repair issue that is not fixable and only a warning, so the Repairs
page offers Ignore. The issue id is stable per config entry and the issue is
raised again at every setup, which keeps an ignored one ignored (the registry
keeps the dismissal across restarts); it is deleted once the retention
reaches the suggested days, and when the entry is removed.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import DATA_INSTANCE

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SUGGESTED_KEEP_DAYS = 30
ISSUE_TRANSLATION_KEY = "recorder_retention"
LEARN_MORE_URL = "https://www.home-assistant.io/integrations/recorder/#purge_keep_days"


def retention_issue_id(entry: ConfigEntry) -> str:
    """One stable issue id per config entry."""
    return f"recorder_retention_{entry.entry_id}"


@callback
def async_check_recorder_retention(hass: HomeAssistant, entry: ConfigEntry) -> int | None:
    """Raise the suggestion when the recorder keeps too few days, clear it
    otherwise. Returns the days kept, or None when no recorder is loaded."""
    keep_days = getattr(hass.data.get(DATA_INSTANCE), "keep_days", None)
    if keep_days is None:
        return None
    if keep_days < SUGGESTED_KEEP_DAYS:
        _LOGGER.debug(
            "Recorder keeps %s days; suggesting %s or more", keep_days, SUGGESTED_KEEP_DAYS
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            retention_issue_id(entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TRANSLATION_KEY,
            translation_placeholders={
                "days": str(keep_days),
                "suggested": str(SUGGESTED_KEEP_DAYS),
            },
            learn_more_url=LEARN_MORE_URL,
        )
    else:
        async_delete_recorder_retention_issue(hass, entry)
    return keep_days


@callback
def async_delete_recorder_retention_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the suggestion (a no-op when there is none)."""
    ir.async_delete_issue(hass, DOMAIN, retention_issue_id(entry))
