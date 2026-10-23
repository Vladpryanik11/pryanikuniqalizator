"""Channel subscription check and rate-limited alerts to the admins."""

import logging
import time
from collections.abc import Callable

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMember

log = logging.getLogger(__name__)

_MEMBER_STATUSES = (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)


class SubscriptionCheckFailed(Exception):
    """Telegram could not say whether the user is in the channel (the bot is not its admin, the network...)."""


def is_member(member: ChatMember) -> bool:
    """Owner, admin and member count; a restricted user counts only while still in the channel."""
    if member.status in _MEMBER_STATUSES:
        return True
    return member.status == ChatMemberStatus.RESTRICTED and bool(member.is_member)


async def check_subscription(bot: Bot, channel_id: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(channel_id, user_id)
    except TelegramAPIError as e:
        raise SubscriptionCheckFailed(str(e)) from e
    return is_member(member)


class AdminAlerts:
    """Messages to every admin. alert() sends a given key at most once per `every` seconds; send errors are only logged."""

    def __init__(self, bot: Bot, admin_ids: list[int], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._bot = bot
        self._admin_ids = admin_ids
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    async def alert(self, key: str, text: str, *, every: float = 600) -> None:
        now = self._clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < every:
            return
        self._last_sent[key] = now  # before the sends: a second caller in the meantime is already limited
        await self.notify(text)

    async def notify(self, text: str) -> None:
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(admin_id, text)
            except Exception as e:  # an admin who blocked the bot must not break the others or the caller
                log.warning("could not alert admin %s: %s", admin_id, e)
