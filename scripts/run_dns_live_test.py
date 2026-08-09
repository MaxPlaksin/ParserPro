#!/usr/bin/env python3
"""Live DNS smoke test: 2 products in visible browser + DB verification."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
DNS_CATEGORY = "17a8a01d16404e77"


def run(cmd: list[str]) -> int:
    print(">>>", " ".join(cmd))
    return subprocess.call(cmd, cwd=PROJECT_ROOT)


def verify_db(limit: int = 2) -> tuple[bool, list[str]]:
    if not DB_PATH.exists():
        return False, [f"Database not found: {DB_PATH}"]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    issues: list[str] = []

    rows = conn.execute(
        """
        SELECT p.source_id, p.name, p.price, p.price_min_observed, p.price_max_observed,
               LENGTH(p.description_detailed) AS desc_detailed_len,
               LENGTH(p.raw_full_text) AS raw_len,
               LENGTH(p.description_full) AS desc_full_len,
               p.reliability_percent, p.display, p.dimensions, p.equipment,
               (SELECT COUNT(*) FROM product_characteristics pc WHERE pc.product_id = p.id) AS has_chars,
               (SELECT COUNT(*) FROM price_history ph WHERE ph.product_id = p.id) AS hist_count
        FROM products p
        WHERE p.source = 'dns'
        ORDER BY p.last_seen_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if len(rows) < limit:
        issues.append(f"Expected {limit} DNS products in DB, found {len(rows)}")

    for row in rows:
        sid = row["source_id"]
        chars = conn.execute(
            """
            SELECT characteristics_json FROM product_characteristics pc
            JOIN products p ON p.id = pc.product_id
            WHERE p.source='dns' AND p.source_id=?
            """,
            (sid,),
        ).fetchone()
        char_count = len(json.loads(chars["characteristics_json"])) if chars else 0

        checks = [
            (row["name"], "name empty"),
            (row["price"], "price missing"),
            (row["desc_detailed_len"] and row["desc_detailed_len"] > 50, "description_detailed too short"),
            (char_count >= 10, f"characteristics too few ({char_count})"),
            (row["raw_len"] and row["raw_len"] > 100, "raw_full_text too short"),
            (row["desc_full_len"] and row["desc_full_len"] > row["desc_detailed_len"], "description_full missing extras"),
            (row["display"], "display empty"),
        ]
        for ok, msg in checks:
            if not ok:
                issues.append(f"{sid}: {msg}")

        hist = conn.execute(
            """
            SELECT ph.price, ph.recorded_at FROM price_history ph
            JOIN products p ON p.id = ph.product_id
            WHERE p.source='dns' AND p.source_id=?
            ORDER BY ph.id
            """,
            (sid,),
        ).fetchall()
        if not hist:
            issues.append(f"{sid}: price_history empty")
        elif len(hist) > 3:
            issues.append(f"{sid}: suspicious price_history count ({len(hist)}) — min/max must not be in history")

    conn.close()
    return not issues, issues


def reset_category_checkpoint(output: str) -> None:
    global_cp = PROJECT_ROOT / output / "checkpoint_global.json"
    if global_cp.exists():
        data = json.loads(global_cp.read_text(encoding="utf-8"))
        data["completed_categories"] = [
            c for c in data.get("completed_categories", []) if c != DNS_CATEGORY
        ]
        global_cp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    cat_cp = PROJECT_ROOT / output / "smartfony" / "checkpoint.json"
    rows_cp = cat_cp.with_suffix(".rows.json")
    for path in (cat_cp, rows_cp):
        if path.exists():
            path.unlink()


def checkpoint_rows(output: str) -> list[dict]:
    rows_path = PROJECT_ROOT / output / "smartfony" / "checkpoint.rows.json"
    if not rows_path.exists():
        return []
    return json.loads(rows_path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="DNS live test on 2 products")
    parser.add_argument("-o", "--output", default="output/dns")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--reset-checkpoint", action="store_true", help="Re-parse category from scratch")
    parser.add_argument("--skip-parse", action="store_true", help="Only verify DB")
    args = parser.parse_args()

    py = sys.executable
    if not args.skip_parse:
        if args.reset_checkpoint:
            reset_category_checkpoint(args.output)
        rc = run(
            [
                py,
                "dns_parser.py",
                "--category",
                DNS_CATEGORY,
                "--limit",
                str(args.limit),
                "--skip-images",
                "-o",
                args.output,
            ]
        )
        if rc != 0:
            sys.exit(rc)
        rows = checkpoint_rows(args.output)
        if len(rows) < args.limit:
            print(
                f"\nERROR: checkpoint has {len(rows)} rows, expected {args.limit}. "
                "DNS likely returned 403 — wait 30-60 min and retry in visible browser."
            )
            sys.exit(2)
        run([py, "scripts/show_db.py"])

    ok, issues = verify_db(args.limit)
    print("\n=== LIVE TEST RESULT ===")
    if ok:
        print("PASS: structured DNS fields look complete")
    else:
        print("FAIL:")
        for item in issues:
            print(f"  - {item}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
