"""Developer commands: python -m uniqbot render IN OUT [--seed N]."""

import argparse
import asyncio
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

from uniqbot.config import PROJECT_ROOT
from uniqbot.engine import EngineError, render
from uniqbot.profile import load_ranges

RENDER_TIMEOUT_SEC = 900  # fixed limits: this command deliberately does not read .env


async def _render(src: Path, dest: Path, seed: int | None) -> dict:
    ranges = load_ranges(PROJECT_ROOT / "config" / "uniq.yaml")
    with tempfile.TemporaryDirectory(prefix="uniqbot-") as tmp:
        res = await render(
            src, Path(tmp), ranges=ranges, max_duration=ranges.max_output_sec,
            timeout=RENDER_TIMEOUT_SEC, rng=random.Random(seed),
        )
        shutil.move(res.output, dest)
    return res.params.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uniqbot")
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("render", help="uniquify one video and print the picked parameters as JSON")
    cmd.add_argument("input", type=Path)
    cmd.add_argument("output", type=Path)
    cmd.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        params = asyncio.run(_render(args.input, args.output, args.seed))
    except EngineError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(params))
    return 0


if __name__ == "__main__":
    sys.exit(main())
