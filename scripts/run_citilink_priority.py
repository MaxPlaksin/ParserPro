#!/usr/bin/env python3
"""Sequential Citilink priority catalog run from a manifest file."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PARSER = PROJECT_ROOT / "citilink_parser.py"
DEFAULT_MANIFEST = PROJECT_ROOT / "output" / "citilink" / "priority_manifest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "citilink"


def main() -> None:
    p = argparse.ArgumentParser(description="Run Citilink priority categories sequentially")
    p.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="Priority manifest JSON (default: output/citilink/priority_manifest.json)",
    )
    p.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--delay", type=float, default=4.0)
    p.add_argument("--category-delay", type=float, default=20.0)
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--workers", type=int, default=1, help="Fixed to 1 for sequential run")
    p.add_argument(
        "--profile-suffix",
        default="_run",
        help="Browser profile suffix passed to citilink_parser (default: _run)",
    )
    args = p.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")

    cmd = [
        sys.executable,
        str(PARSER),
        "-o",
        str(args.output),
        "--manifest",
        str(manifest_path),
        "--delay",
        str(args.delay),
        "--category-delay",
        str(args.category_delay),
        "--workers",
        "1",
        "--profile-suffix",
        str(args.profile_suffix),
    ]
    if args.headless:
        cmd.append("--headless")

    print(">>>", " ".join(cmd))
    raise SystemExit(subprocess.call(cmd, cwd=str(PROJECT_ROOT)))


if __name__ == "__main__":
    main()
