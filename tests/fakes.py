"""Stand-ins shared by the tests."""
from datetime import datetime


class FakeMqtt:
    """The parts of the SDK's NavimowMQTT that CollectorHealth reads: the
    counters, reasons and client id, and the per-device message times, all
    set by the test."""

    def __init__(self) -> None:
        self.connects = 0
        self.disconnects = 0
        self.connect_failures = 0
        self.rebuilds = 0
        self.last_rebuild_reason: str | None = None
        self.client_id = "web_user_1"
        self.last_disconnect_reason: str | None = None
        self.last_connect_fail_reason: str | None = None
        self.message_at: dict[str, datetime] = {}
        self.location_age: dict[str, float] = {}

    def last_message_at(self, device_id, channel=None):
        assert channel is None
        return self.message_at.get(device_id)

    def last_message_age(self, device_id, channel=None):
        assert channel == "location"
        return self.location_age.get(device_id)
