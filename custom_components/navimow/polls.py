"""Every REST status reply the SDK's client receives, seen by the integration.

NavimowClient polls getVehicleStatus on its clock and tells its consumer
about a reply only when that reply becomes a mower's shown state. Two of
this integration's promises need every reply: the rest_status sensor writes
one recorder row per poll (Scythnet reads that series), and collector_status
leaves poll_failing the moment a poll succeeds. The client polls through the
MowerAPI it is given, so a MowerAPI that reports each status request gives
the integration both, with no timer of its own and no second request.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util
from mower_sdk.api import MowerAPI
from mower_sdk.models import DeviceStatus

StatusesCallback = Callable[[dict[str, DeviceStatus], datetime], None]


class ObservedMowerAPI(MowerAPI):
    """A MowerAPI that reports every successful status request.

    ``on_statuses(statuses, polled_at)`` runs on the event loop after each
    reply, with the statuses by device id as the SDK read them and the UTC
    time the reply was read. A failed request is the client's to report,
    through its on_error callbacks.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.on_statuses: StatusesCallback | None = None

    async def async_get_device_statuses(self, device_ids: list[str]) -> dict[str, DeviceStatus]:
        statuses = await super().async_get_device_statuses(device_ids)
        if self.on_statuses is not None:
            self.on_statuses(statuses, dt_util.utcnow())
        return statuses
