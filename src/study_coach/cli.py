"""Command line entry point.

  study-coach         run the MCP server (this is what Claude launches)
  study-coach init    interactive setup that writes your config file
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, LmsConfig, ScheduleConfig, config_path, load_config, save_config


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_int(prompt: str, default: int, minimum: int) -> int:
    while True:
        answer = _ask(prompt, str(default))
        if answer.isdigit() and int(answer) >= minimum:
            return int(answer)
        print(f"  Enter a whole number of at least {minimum}.")


def _ask_time(prompt: str, default: str) -> str:
    while True:
        answer = _ask(prompt, default)
        try:
            time.fromisoformat(answer)
            return answer
        except ValueError:
            print("  Use 24-hour HH:MM, like 09:00 or 22:30.")


def server_command() -> str:
    """Absolute path to the `study-coach` launcher for the Claude Desktop config.

    The launcher sits next to the Python interpreter running us (.venv/bin on
    macOS/Linux, .venv\\Scripts on Windows). Searching PATH alone fails when the
    virtualenv isn't activated, which is how most people run `init`.
    """
    name = "study-coach.exe" if sys.platform == "win32" else "study-coach"
    beside_python = Path(sys.executable).parent / name
    if beside_python.exists():
        return str(beside_python)
    return shutil.which("study-coach") or "study-coach"


def init() -> None:
    path = config_path()
    existing = load_config() if path.exists() else Config()
    print("Study Coach setup -- press Enter to keep the value in brackets.\n")

    base_url = _ask("Canvas URL (e.g. https://yourschool.instructure.com)", existing.lms.base_url)
    while True:
        tz = _ask("Time zone", existing.timezone)
        try:
            ZoneInfo(tz)
            break
        except Exception:
            print("  Unknown time zone. Use a name like America/Chicago.")

    s = existing.schedule
    study_start = _ask_time("Earliest time you'll study (HH:MM)", s.study_start.strftime("%H:%M"))
    study_end = _ask_time("Latest time you'll study (HH:MM)", s.study_end.strftime("%H:%M"))
    max_block = _ask_int("Longest study block in minutes", s.max_block_minutes, minimum=15)
    feed = _ask("Private calendar feed URL (optional, Enter to skip)", "")
    feeds = existing.calendar_feeds + ([feed] if feed and feed not in existing.calendar_feeds else [])

    config = existing.model_copy(
        update={
            "timezone": tz,
            "lms": LmsConfig(base_url=base_url),
            "schedule": ScheduleConfig.model_validate(
                s.model_dump()
                | {"study_start": study_start, "study_end": study_end, "max_block_minutes": max_block}
            ),
            "calendar_feeds": feeds,
        }
    )
    saved = save_config(config)
    print(f"\nSaved {saved}")
    print("Add work/practice times and course notes later by just telling Claude.\n")

    snippet = {
        "mcpServers": {
            "study-coach": {"command": server_command(), "env": {"CANVAS_TOKEN": "<paste your Canvas token>"}}
        }
    }
    print("Add this to your Claude Desktop config (Settings -> Developer -> Edit Config):\n")
    print(json.dumps(snippet, indent=2))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init()
        return
    from .server import run

    run()


if __name__ == "__main__":
    main()
