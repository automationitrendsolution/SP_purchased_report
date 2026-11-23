"""Timezone-aware time handling.

Every timestamp is explicit about its zone.  The server's local time is never
used, so the same code behaves the same on a UTC server, an IST laptop, and a
container with no ``/etc/localtime``.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import UTC

__all__ = ["UTC", "now_utc", "now_in", "today_in", "utc_stamp"]


def now_utc() -> datetime:
    """Current instant, in UTC."""
    return datetime.now(tz=UTC)


def now_in(tz: ZoneInfo, *, reference: datetime | None = None) -> datetime:
    """Convert ``reference`` (default: now) into ``tz``."""
    return (reference or now_utc()).astimezone(tz)


def today_in(tz: ZoneInfo, *, reference: datetime | None = None) -> date:
    """The calendar date in ``tz`` -- the advertiser's day, not the server's."""
    return now_in(tz, reference=reference).date()


def utc_stamp(reference: datetime | None = None) -> str:
    """``20261123T101500Z`` -- the prefix on every file the pipeline writes."""
    return (reference or now_utc()).strftime("%Y%m%dT%H%M%SZ")
