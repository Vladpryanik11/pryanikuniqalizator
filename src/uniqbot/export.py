"""The users table as a CSV that Excel opens without setup. Spec 3, /users."""

import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import User

# the order of the `users` fields in spec 5
COLUMNS = (
    "tg_id",
    "username",
    "first_name",
    "last_name",
    "language_code",
    "is_premium",
    "source",
    "first_seen",
    "last_seen",
    "is_subscribed",
    "subscription_checked_at",
    "videos_done",
    "blocked",
    "blocked_at",
)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object, tz: ZoneInfo) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, datetime):
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value  # a profile name like =HYPERLINK(...) must not run as a formula in Excel
    return value


def users_csv(users: list[User], tz: ZoneInfo) -> bytes:
    """UTF-8 with a BOM and `;` as the separator; times are shown in `tz`."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(COLUMNS)
    for user in users:
        writer.writerow(_cell(getattr(user, column), tz) for column in COLUMNS)
    return out.getvalue().encode("utf-8-sig")
