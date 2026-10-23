"""Commands: python -m uniqbot bot | render IN OUT [--seed N] | logout-cloud."""

import argparse
import asyncio
import json
import logging
import random
import shutil
import sys
import tempfile
from pathlib import Path

from aiogram.exceptions import TelegramAPIError

from uniqbot.app import StartupError, logout_cloud, run_bot
from uniqbot.config import PROJECT_ROOT, Settings
from uniqbot.engine import EngineError, render
from uniqbot.profile import load_ranges

RENDER_TIMEOUT_SEC = 900  # fixed limits: the render command deliberately does not read .env


class TokenRedactingFormatter(logging.Formatter):
    """Whatever gets logged (an aiohttp error quotes the request URL, which holds the token), the token does not."""

    def __init__(self, fmt: str | None = None, *, token: str | None = None) -> None:
        super().__init__(fmt)
        self._token = token

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text.replace(self._token, "<token>") if self._token else text


def setup_logging(token: str | None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        TokenRedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", token=token)
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])


async def _render(src: Path, dest: Path, seed: int | None) -> dict:
    ranges = load_ranges(PROJECT_ROOT / "config" / "uniq.yaml")
    with tempfile.TemporaryDirectory(prefix="uniqbot-") as tmp:
        res = await render(
            src, Path(tmp), ranges=ranges, max_duration=ranges.max_output_sec,
            timeout=RENDER_TIMEOUT_SEC, rng=random.Random(seed),
        )
        shutil.move(res.output, dest)
    return res.params.to_dict()


def _render_command(args: argparse.Namespace) -> int:
    try:
        params = asyncio.run(_render(args.input, args.output, args.seed))
    except EngineError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(params))
    return 0


def _bot_command() -> int:
    settings = Settings()
    setup_logging(settings.bot_token.get_secret_value() if settings.bot_token else None)
    try:
        asyncio.run(run_bot(settings))
    except StartupError as e:
        print(e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # Ctrl+C where the polling does not handle the signal itself (Windows)
        pass
    return 0


def _logout_command() -> int:
    try:
        asyncio.run(logout_cloud(Settings()))
    except (StartupError, TelegramAPIError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("Logged out of the cloud Bot API. The bot can start on the local server now; "
          "it cannot go back to the cloud for 10 minutes.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uniqbot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bot", help="run the Telegram bot")
    cmd = commands.add_parser("render", help="uniquify one video and print the picked parameters as JSON")
    cmd.add_argument("input", type=Path)
    cmd.add_argument("output", type=Path)
    cmd.add_argument("--seed", type=int, default=None)
    commands.add_parser(
        "logout-cloud", help="log the bot out of the cloud Bot API (once, before moving to the local server)"
    )
    args = parser.parse_args(argv)
    if args.command == "render":
        return _render_command(args)
    if args.command == "bot":
        return _bot_command()
    return _logout_command()


if __name__ == "__main__":
    sys.exit(main())
