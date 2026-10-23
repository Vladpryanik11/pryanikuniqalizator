"""Keeps the bot token out of everything that leaves the process: logs, the orders table, alerts to the admins."""

import re

PLACEHOLDER = "<token>"
# the shape of any Telegram bot token, so that a token we do not know about (another bot's, an old one) goes too
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def redact(text: str, token: str | None = None) -> str:
    """`text` with the exact `token` (when given) and anything shaped like a bot token replaced by <token>."""
    if token:
        text = text.replace(token, PLACEHOLDER)
    return _TOKEN_RE.sub(PLACEHOLDER, text)
