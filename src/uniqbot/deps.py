"""Everything the handlers and the worker share, passed to aiogram as workflow data `deps`."""

import asyncio
from dataclasses import dataclass

from .access import AdminAlerts
from .config import Settings
from .profile import UniqRanges
from .repo import Sessions


@dataclass
class Deps:
    settings: Settings
    sessions: Sessions
    ranges: UniqRanges
    wake: asyncio.Event  # set when an order is queued, so the worker does not wait for its poll
    alerts: AdminAlerts
    broadcast_task: asyncio.Task | None = None  # the running broadcast, if any
