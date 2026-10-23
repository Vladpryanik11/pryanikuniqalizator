import csv
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from uniqbot.db import User
from uniqbot.export import users_csv

MOSCOW = ZoneInfo("Europe/Moscow")
HEADER = (
    "tg_id;username;first_name;last_name;language_code;is_premium;source;first_seen;last_seen;"
    "is_subscribed;subscription_checked_at;videos_done;blocked;blocked_at"
)
AT_9 = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
AT_10 = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)


def make_user(tg_id: int, **fields) -> User:
    defaults = dict(
        is_premium=False, first_seen=AT_9, last_seen=AT_9, videos_done=0, blocked=False,
    )
    return User(tg_id=tg_id, **{**defaults, **fields})


def rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))


def test_users_csv_format():
    full = make_user(
        1, username="vlad", first_name="Vlad; the Great", last_name="I", language_code="ru", is_premium=True,
        source="reels", last_seen=AT_10, is_subscribed=True, subscription_checked_at=AT_10, videos_done=3,
        blocked=True, blocked_at=AT_10,
    )
    bare = make_user(2)  # what a user who only wrote /start looks like: the optional columns are empty
    data = users_csv([full, bare], MOSCOW)

    assert data.startswith("﻿".encode("utf-8"))
    text = data.decode("utf-8")
    assert text.startswith("﻿" + HEADER + "\r\n")
    assert '"Vlad; the Great"' in text  # a ; inside a value is quoted, not a new column

    header, first, second = rows(data)
    assert ";".join(header) == HEADER
    assert first == [
        "1", "vlad", "Vlad; the Great", "I", "ru", "да", "reels", "2026-10-23 12:00", "2026-10-23 13:30",
        "да", "2026-10-23 13:30", "3", "да", "2026-10-23 13:30",
    ]
    assert second == ["2", "", "", "", "", "нет", "", "2026-10-23 12:00", "2026-10-23 12:00", "", "", "0", "нет", ""]


def test_users_csv_times_follow_the_given_zone():
    [_, row] = rows(users_csv([make_user(1)], ZoneInfo("America/New_York")))
    assert row[7:9] == ["2026-10-23 05:00", "2026-10-23 05:00"]  # 09:00 UTC, whatever the zone of this machine is


def test_users_csv_of_nobody_is_the_header():
    assert rows(users_csv([], MOSCOW)) == [HEADER.split(";")]


def test_users_csv_keeps_a_profile_text_from_running_as_a_formula():
    evil = make_user(3, first_name="=HYPERLINK(\"http://x\")", last_name="@sum", username="-cmd", source="ok-1")
    [_, row] = rows(users_csv([evil], MOSCOW))
    assert row[1:4] == ["'-cmd", "'=HYPERLINK(\"http://x\")", "'@sum"]
    assert row[6] == "ok-1"  # an ordinary text is left alone
