import pytest

from uniqbot.config import Settings
from uniqbot.redact import redact

TOKEN = "123456:" + "A" * 35  # the shape of a real bot token


def test_redact_replaces_the_exact_token_and_anything_shaped_like_one():
    path = f"/var/lib/telegram-bot-api/{TOKEN}/videos/file_0.mp4"
    assert redact(path, TOKEN) == "/var/lib/telegram-bot-api/<token>/videos/file_0.mp4"
    assert redact(path, None) == "/var/lib/telegram-bot-api/<token>/videos/file_0.mp4"  # by its shape alone
    other = "987654321:" + "b-C_" * 9
    assert redact(f"a {TOKEN} b {other} c", None) == "a <token> b <token> c"


def test_redact_replaces_a_token_that_has_no_token_shape():
    assert redact("url=https://x/botSECRET-token/y", "SECRET-token") == "url=https://x/bot<token>/y"


@pytest.mark.parametrize("text", ["", "no secrets here", "12:34", "id 123456:short", "1234567890"])
def test_redact_leaves_other_text_alone(text):
    assert redact(text, TOKEN) == text
    assert redact(text, None) == text


def test_settings_token_value_is_the_plain_token_or_none():
    assert Settings(bot_token=TOKEN).token_value == TOKEN
    assert Settings(bot_token=None).token_value is None
