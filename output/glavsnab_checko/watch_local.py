from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(r"c:\Project\Parser")
LOG = ROOT / "output" / "glavsnab_checko" / "watch_local.log"


def _ps(command: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result.stdout.strip()


def scorer_running() -> bool:
    out = _ps(
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'score_glavsnab\\.py' } | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    return bool(out)


def count_done() -> str:
    code = r"""
import sys
sys.path.insert(0, r'c:\Project\Parser')
from scripts.score_glavsnab import collect_inns, load_cached, DEFAULT_XLSX
inns = collect_inns(DEFAULT_XLSX)
done = sum(1 for inn in inns if load_cached(inn))
print(f"{done}/{len(inns)}")
"""
    result = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-c", code],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=90,
    )
    return result.stdout.strip() or "unknown"


def start_scorer() -> None:
    subprocess.Popen(
        [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            "-u",
            "scripts\\score_glavsnab.py",
            "--delay",
            "10",
            "--gap",
            "8",
        ],
        cwd=str(ROOT),
        stdout=open(ROOT / "output" / "glavsnab_checko" / "watch_restart.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def write(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def main() -> None:
    write("watcher started")
    while True:
        try:
            if scorer_running():
                write(f"ok {count_done()}")
            else:
                write("scorer missing, restarting")
                start_scorer()
                time.sleep(20)
        except Exception as exc:  # noqa: BLE001
            write(f"watch error: {exc}")
        time.sleep(60)


if __name__ == "__main__":
    main()
