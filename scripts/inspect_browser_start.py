"""Diagnose dedicated-profile startup without navigating or exposing credentials."""

import argparse
import asyncio
import json
import re
import subprocess
import traceback
from pathlib import Path

from ticket_runner.browser import BrowserAdapter
from ticket_runner.config import load_config
from ticket_runner.state import exclusive_run


async def inspect(headful=False):
    directory = Path("/data")
    config = load_config(Path("/config/config.yaml"))
    config.browser.headless = not headful
    with exclusive_run(directory):
        profile = directory / "browser-profile"
        print(
            json.dumps(
                {
                    "profile_markers": {
                        name: {
                            "exists": (profile / name).exists(),
                            "symlink": (profile / name).is_symlink(),
                        }
                        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")
                    }
                }
            ),
            flush=True,
        )
        try:
            async with BrowserAdapter(config, directory):
                print(json.dumps({"browser_started": True, "navigations": 0}))
        except Exception as exc:
            text = str(exc)
            categories = [
                key
                for key in (
                    "ProcessSingleton",
                    "process_singleton",
                    "SingletonLock",
                    "Missing X server",
                    "DISPLAY",
                    "Permission denied",
                    "TargetClosed",
                    "profile appears to be in use",
                    "No space left",
                )
                if key in text
            ]
            print(
                json.dumps(
                    {
                        "browser_started": False,
                        "error_type": type(exc).__name__,
                        "categories": categories,
                        "frames": [
                            {
                                "file": Path(frame.filename).name,
                                "function": frame.name,
                                "line": frame.lineno,
                            }
                            for frame in traceback.extract_tb(exc.__traceback__)[-6:]
                        ],
                        "startup_errors": [
                            re.sub(r"https?://\S+", "[url omitted]", line)[:350]
                            for line in text.splitlines()
                            if "[err]" in line
                        ][:10],
                    }
                ),
                flush=True,
            )
            raise SystemExit(2) from None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()
    display = None
    try:
        if args.headful:
            display = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1440x1000x24", "-nolisten", "tcp"]
            )
            import time

            for _ in range(50):
                if Path("/tmp/.X11-unix/X99").exists():
                    break
                time.sleep(0.1)
        asyncio.run(inspect(args.headful))
    finally:
        if display:
            display.terminate()
            display.wait(timeout=5)
