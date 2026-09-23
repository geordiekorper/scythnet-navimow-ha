"""Rejected input: the record kept for input that was received but not applied."""
import json
import unittest

from custom_components.navimow.rejected import (
    PAYLOAD_LIMIT,
    REASONS,
    raw_message_rejection,
    rejection_record,
)

RECEIVED = "2026-09-23T20:00:00+00:00"


class RejectionRecordTest(unittest.TestCase):
    def test_text_payload_is_kept_as_received(self):
        rec = rejection_record("location", "/t", "stale", '[{"type":1}]', RECEIVED)
        self.assertEqual(rec, {
            "channel": "location", "topic": "/t", "reason": "stale",
            "reasons": ["stale"], "received_at": RECEIVED,
            "payload": '[{"type":1}]', "truncated": False,
        })

    def test_other_payloads_become_json(self):
        rec = rejection_record("state", None, "stale", {"state": "isDocked", "timestamp": 1}, RECEIVED)
        self.assertEqual(json.loads(rec["payload"]), {"state": "isDocked", "timestamp": 1})

    def test_all_reasons_are_listed_with_the_deciding_one(self):
        rec = rejection_record("location", "/t", "implausible_time", "x", RECEIVED,
                               reasons=["implausible_time", "unknown_field"])
        self.assertEqual(rec["reason"], "implausible_time")
        self.assertEqual(rec["reasons"], ["implausible_time", "unknown_field"])

    def test_long_payload_is_truncated_and_marked(self):
        rec = rejection_record("event", "/t", "unknown_channel", "a" * (PAYLOAD_LIMIT + 5), RECEIVED)
        self.assertEqual(len(rec["payload"]), PAYLOAD_LIMIT)
        self.assertTrue(rec["truncated"])
        exact = rejection_record("event", "/t", "unknown_channel", "a" * PAYLOAD_LIMIT, RECEIVED)
        self.assertFalse(exact["truncated"])

    def test_unknown_reason_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            rejection_record("state", None, "odd", "x", RECEIVED)

    def test_record_stays_under_the_recorder_attribute_limit(self):
        # The recorder measures attributes as UTF-8 JSON (orjson), where a
        # quote or backslash costs two bytes and an accented letter two.
        for filler in ('"', "é", "\\", "a"):
            rec = rejection_record("location", "/downlink/vehicle/x/realtimeDate/location",
                                   "unknown_field", filler * (PAYLOAD_LIMIT * 2), RECEIVED,
                                   reasons=list(REASONS))
            self.assertTrue(rec["truncated"])
            size = len(json.dumps(rec, ensure_ascii=False).encode())
            self.assertLess(size, 16384, filler)
            self.assertGreater(size, PAYLOAD_LIMIT, filler)


class RawMessageTest(unittest.TestCase):
    STATE = {"state": "isDocked", "battery": 100, "timestamp": 1700000000, "device_id": "dev-1"}

    def test_known_state_fields_are_not_recorded(self):
        self.assertIsNone(raw_message_rejection("state", json.dumps(self.STATE)))
        rest_shape = {"vehicleState": "isDocked", "capacityRemaining": [], "status": "x"}
        self.assertIsNone(raw_message_rejection("state", json.dumps(rest_shape)))

    def test_unknown_state_field_is_recorded(self):
        text = json.dumps({**self.STATE, "signal": -60})
        self.assertEqual(raw_message_rejection("state", text), "unknown_field")
