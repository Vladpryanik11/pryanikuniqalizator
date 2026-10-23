import logging

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetChatMember
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    User,
)
from fakes import ADMIN_IDS, FakeBot

from uniqbot.access import AdminAlerts, SubscriptionCheckFailed, check_subscription, is_member

USER = User(id=5, is_bot=False, first_name="x")


@pytest.mark.parametrize(
    "member, expected",
    [
        (ChatMemberOwner.model_construct(user=USER), True),
        (ChatMemberAdministrator.model_construct(user=USER), True),
        (ChatMemberMember.model_construct(user=USER), True),
        (ChatMemberRestricted.model_construct(user=USER, is_member=True), True),
        (ChatMemberRestricted.model_construct(user=USER, is_member=False), False),
        (ChatMemberLeft.model_construct(user=USER), False),
        (ChatMemberBanned.model_construct(user=USER), False),
    ],
    ids=["creator", "administrator", "member", "restricted-in", "restricted-out", "left", "kicked"],
)
def test_member_statuses(member, expected):
    assert is_member(member) is expected


async def test_check_subscription_asks_the_channel_about_the_user():
    bot = FakeBot()
    assert await check_subscription(bot, "-1001", 5) is True
    assert bot.called("get_chat_member") == [{"chat_id": "-1001", "user_id": 5}]
    bot.member_status = "left"
    assert await check_subscription(bot, "-1001", 5) is False


@pytest.mark.parametrize("error_type", [TelegramBadRequest, TelegramNetworkError])
async def test_check_subscription_wraps_api_errors(error_type):
    bot = FakeBot()
    bot.member_error = error_type(method=GetChatMember(chat_id=1, user_id=5), message="chat not found")
    with pytest.raises(SubscriptionCheckFailed, match="chat not found"):
        await check_subscription(bot, "-1001", 5)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_alerts_rate_limited():
    bot, clock = FakeBot(), Clock()
    alerts = AdminAlerts(bot, list(ADMIN_IDS), clock=clock)

    await alerts.alert("sub", "first")
    await alerts.alert("sub", "second")
    assert [(kw["chat_id"], kw["text"]) for kw in bot.called("send_message")] == [(11, "first"), (22, "first")]

    clock.now += 599
    await alerts.alert("sub", "third")
    assert bot.texts() == ["first", "first"]

    clock.now += 2  # 601 s after the first one
    await alerts.alert("sub", "fourth")
    assert bot.texts() == ["first", "first", "fourth", "fourth"]


async def test_alert_keys_are_limited_separately_and_every_can_differ():
    bot, clock = FakeBot(), Clock()
    alerts = AdminAlerts(bot, [11], clock=clock)

    await alerts.alert("a", "a1")
    await alerts.alert("b", "b1")
    clock.now += 30
    await alerts.alert("a", "a2", every=10)
    assert bot.texts() == ["a1", "b1", "a2"]


async def test_notify_is_not_limited():
    bot = FakeBot()
    alerts = AdminAlerts(bot, list(ADMIN_IDS))
    await alerts.notify("one")
    await alerts.notify("one")
    assert bot.texts(11) == ["one", "one"] and bot.texts(22) == ["one", "one"]


async def test_alert_send_errors_are_swallowed_and_logged(caplog):
    bot = FakeBot()
    bot.send_errors[11] = RuntimeError("bot was blocked by the user")
    alerts = AdminAlerts(bot, list(ADMIN_IDS))

    with caplog.at_level(logging.WARNING):
        await alerts.notify("hello")  # must not raise

    assert bot.texts(22) == ["hello"]  # the other admin still gets it
    assert "11" in caplog.text and "blocked" in caplog.text
