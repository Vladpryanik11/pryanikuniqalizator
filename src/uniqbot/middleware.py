"""Records who talks to the bot."""

from aiogram import BaseMiddleware

from .repo import Sessions, TgProfile, touch_user


class TrackUser(BaseMiddleware):
    """Outer middleware on `update`: creates the user, refreshes the profile and last_seen.

    Bots and chats other than private ones are not users of the bot. It runs before the handlers, so orders
    always find their user row.
    """

    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is not None and not user.is_bot and (chat is None or chat.type == "private"):
            await touch_user(
                self._sessions,
                TgProfile(
                    tg_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    language_code=user.language_code,
                    is_premium=user.is_premium,
                ),
            )
        return await handler(event, data)
