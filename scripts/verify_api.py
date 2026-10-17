#!/usr/bin/env python3
"""Smoke-test parser API contract."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

API_KEY = os.getenv("PARSER_API_KEY", "")
BASE_LOCAL = "http://127.0.0.1:8765"
BASE_EXT = "http://92.255.108.89:8765"
INN = sys.argv[1] if len(sys.argv) > 1 else "9717108903"

REQUIRED_COMPLETED = {
    "inn",
    "status",
    "source",
    "name",
    "checked_at",
    "financials",
    "enforcements",
    "arbitration",
    "error",
}


def req(method: str, url: str, body: dict | None = None, auth: bool = True) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if auth and API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"raw": payload}
        return exc.code, parsed


def validate_completed(payload: dict) -> list[str]:
    issues: list[str] = []
    missing = REQUIRED_COMPLETED - set(payload.keys())
    if missing:
        issues.append(f"missing top-level keys: {sorted(missing)}")
    if payload.get("status") != "completed":
        issues.append(f"expected status=completed, got {payload.get('status')!r}")
    if payload.get("source") != "checko_web":
        issues.append(f"expected source=checko_web, got {payload.get('source')!r}")
    fin = payload.get("financials") or {}
    for block in ("revenue", "profit"):
        if block not in fin:
            issues.append(f"financials.{block} missing")
            continue
        years = [k for k in fin[block] if k.isdigit()]
        if len(years) < 1:
            issues.append(f"financials.{block} has no year keys")
        if "change_percent" not in fin[block]:
            issues.append(f"financials.{block}.change_percent missing")
    arb = payload.get("arbitration") or {}
    if "defendant_total_count" not in arb:
        issues.append("arbitration.defendant_total_count missing")
    ts = payload.get("checked_at") or ""
    if "+03:00" not in ts and "+03:" not in ts:
        issues.append(f"checked_at not MSK timezone: {ts!r}")
    return issues


def main() -> int:
    print("=== health local ===")
    code, body = req("GET", f"{BASE_LOCAL}/api/v1/health", auth=False)
    print(code, body)

    print("\n=== auth without key ===")
    code, body = req("GET", f"{BASE_LOCAL}/api/v1/checks/{INN}", auth=False)
    print(code, body)

    print("\n=== GET cached ===")
    t0 = time.perf_counter()
    code, body = req("GET", f"{BASE_LOCAL}/api/v1/checks/{INN}")
    dt = time.perf_counter() - t0
    print(f"{code} in {dt:.3f}s status={body.get('status')} source={body.get('source')}")

    print("\n=== contract validation ===")
    if body.get("status") == "completed":
        issues = validate_completed(body)
        if issues:
            print("FAIL:")
            for item in issues:
                print(" -", item)
        else:
            print("OK — response matches expected contract")
            print(json.dumps(body, ensure_ascii=False, indent=2))
    else:
        print("SKIP validation — not completed:", body)

    print("\n=== health external ===")
    try:
        code, body = req("GET", f"{BASE_EXT}/api/v1/health", auth=False)
        print(code, body)
    except Exception as exc:
        print("FAIL:", type(exc).__name__, exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
