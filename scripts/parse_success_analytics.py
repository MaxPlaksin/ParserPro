#!/usr/bin/env python3
"""Read-only parsing success analytics across DNS, MVideo, Regard, Citilink."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"

SOURCES = {
    "dns": {"output": PROJECT_ROOT / "output" / "dns", "logs": ["supervisor_dns.log", "dns_full_20260807_173904.log"]},
    "mvideo": {"output": PROJECT_ROOT / "output" / "mvideo", "logs": ["supervisor_mvideo.log", "mvideo_full_20260807_173825.log", "mvideo_full_20260807_173850.log", "mvideo_resume_interrupt_20260807_174831.log"]},
    "regard": {"output": PROJECT_ROOT / "output" / "regard_full", "logs": ["supervisor_regard.log"], "alt_output": PROJECT_ROOT / "output" / "regard"},
    "citilink": {"output": PROJECT_ROOT / "output" / "citilink", "logs": ["supervisor_citilink_priority.log", "supervisor_citilink_discovery.log"]},
}

FAIL_PATTERNS = {
    "dns": re.compile(r"DNS detail failed (\S+):", re.I),
    "mvideo": re.compile(r"failed=(\d+)|detail failed (\S+):|пропущена после блокировок", re.I),
    "regard": re.compile(r"Regard detail failed (\S+):", re.I),
    "citilink": re.compile(r"Citilink detail failed (\S+):|Citilink respec failed (\S+):", re.I),
}

RETRY_PATTERNS = {
    "dns": re.compile(r"retry|Retry|recheck|повтор", re.I),
    "mvideo": re.compile(r"403: короткий retry|Retry category|deferred recheck|retry_count", re.I),
    "regard": re.compile(r"Retry category|retry after backoff|recheck", re.I),
    "citilink": re.compile(r"Retry category|retry after backoff|Restart Citilink session", re.I),
}

BLOCK_PAGE_PATTERNS = [
    re.compile(r"(?<!Image )HTTP 403", re.I),
    re.compile(r"(?<!Image )HTTP 429", re.I),
    re.compile(r"403 при", re.I),
    re.compile(r"Category deferred after blocks", re.I),
    re.compile(r"пропущена после блокировок", re.I),
    re.compile(r"catalog discovery blocked", re.I),
    re.compile(r"TargetClosedError", re.I),
]
BLOCK_IMAGE_PATTERNS = [
    re.compile(r"Image HTTP 403", re.I),
    re.compile(r"Image HTTP 429", re.I),
    re.compile(r"Image download failed", re.I),
]


def local_exists(rel_path: str) -> bool:
    if not rel_path:
        return False
    return (PROJECT_ROOT / rel_path.replace("\\", "/")).is_file()


def iter_checkpoints(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    paths = list(root.rglob("checkpoint.json"))
    alt = SOURCES.get("regard", {}).get("alt_output")
    if alt and alt.is_dir() and alt != root:
        paths.extend(alt.rglob("checkpoint.json"))
    return paths


def product_id_from_pending(item: dict) -> str:
    for key in ("product_id", "id", "source_id"):
        val = item.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def scan_checkpoints(source: str) -> dict:
    cfg = SOURCES[source]
    roots = [cfg["output"]]
    if source == "regard":
        roots.append(cfg.get("alt_output"))

    discovered: set[str] = set()
    completed: set[str] = set()
    pending: set[str] = set()
    checkpoint_files = 0

    seen_paths: set[Path] = set()
    for root in roots:
        if not root:
            continue
        for cp_path in root.rglob("checkpoint.json"):
            if cp_path in seen_paths:
                continue
            seen_paths.add(cp_path)
            try:
                data = json.loads(cp_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            checkpoint_files += 1
            for pid in data.get("completed_ids") or []:
                pid = str(pid).strip()
                if pid:
                    completed.add(pid)
                    discovered.add(pid)
            for item in data.get("pending") or []:
                if isinstance(item, dict):
                    pid = product_id_from_pending(item)
                else:
                    pid = str(item).strip()
                if pid:
                    pending.add(pid)
                    discovered.add(pid)

    global_paths = []
    for root in roots:
        gp = root / "checkpoint_global.json"
        if gp.is_file():
            global_paths.append(gp)

    deferred_categories = 0
    deferred_pending_products = 0
    retry_events_checkpoint = 0
    blocked_403_checkpoint = 0

    for gp in global_paths:
        try:
            g = json.loads(gp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        needs_recheck = g.get("needs_recheck_categories") or []
        deferred_categories += len(needs_recheck)
        blocked = g.get("blocked_categories") or {}
        if isinstance(blocked, dict):
            for meta in blocked.values():
                if not isinstance(meta, dict):
                    continue
                reason = str(meta.get("reason") or "")
                if reason == "403" or "403" in reason:
                    blocked_403_checkpoint += 1
                rc = int(meta.get("retry_count") or 0)
                if rc > 0:
                    retry_events_checkpoint += rc
                deferred_pending_products += int(meta.get("pending_count") or 0)

    return {
        "checkpoint_files": checkpoint_files,
        "discovered_ids": discovered,
        "completed_ids": completed,
        "pending_ids": pending,
        "deferred_categories": deferred_categories,
        "deferred_pending_products": deferred_pending_products,
        "retry_events_checkpoint": retry_events_checkpoint,
        "blocked_403_checkpoint": blocked_403_checkpoint,
    }


def scan_logs(source: str) -> dict:
    failed_ids: set[str] = set()
    retry_events = 0
    blocked_403_429 = 0
    blocked_image = 0
    detail_failed_lines = 0
    deferred_category_lines = 0

    for name in SOURCES[source]["logs"]:
        log_path = PROJECT_ROOT / "logs" / name
        if not log_path.is_file():
            continue
        text = log_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if any(p.search(line) for p in BLOCK_PAGE_PATTERNS):
                blocked_403_429 += 1
            if any(p.search(line) for p in BLOCK_IMAGE_PATTERNS):
                blocked_image += 1
            if RETRY_PATTERNS[source].search(line):
                retry_events += 1
            m = FAIL_PATTERNS[source].search(line)
            if m:
                detail_failed_lines += 1
                for g in m.groups():
                    if g and g.isdigit() is False and len(g) > 2:
                        failed_ids.add(g)
            if "deferred" in line.lower() or "needs_recheck" in line.lower() or "пропущена после блокировок" in line.lower():
                deferred_category_lines += 1

    return {
        "failed_ids": failed_ids,
        "retry_events_logs": retry_events,
        "blocked_403_429_logs": blocked_403_429,
        "blocked_image_logs": blocked_image,
        "detail_failed_lines": detail_failed_lines,
        "deferred_category_lines": deferred_category_lines,
    }


def query_db(conn: sqlite3.Connection, source: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM products WHERE source = ?",
        (source,),
    ).fetchone()
    products = row["c"]

    with_chars = conn.execute(
        """
        SELECT COUNT(*) AS c FROM products p
        JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source = ? AND pc.characteristics_json IS NOT NULL
          AND TRIM(pc.characteristics_json) NOT IN ('', '{}')
        """,
        (source,),
    ).fetchone()["c"]

    with_desc = conn.execute(
        """
        SELECT COUNT(*) AS c FROM products p
        WHERE p.source = ? AND (
            (description IS NOT NULL AND TRIM(description) != '')
            OR (description_detailed IS NOT NULL AND TRIM(description_detailed) != '')
            OR (description_full IS NOT NULL AND TRIM(description_full) != '')
            OR (description_short IS NOT NULL AND TRIM(description_short) != '')
        )
        """,
        (source,),
    ).fetchone()["c"]

    image_rows = conn.execute(
        """
        SELECT pi.path, pi.source_url, p.id AS product_id
        FROM product_images pi
        JOIN products p ON p.id = pi.product_id
        WHERE p.source = ?
        """,
        (source,),
    ).fetchall()

    image_records = len(image_rows)
    urls_found = sum(1 for r in image_rows if (r["source_url"] or "").strip())
    local_ok = 0
    local_missing = 0
    products_with_images: set[int] = set()
    products_with_local: set[int] = set()

    seen_paths: set[tuple[int, str]] = set()
    for r in image_rows:
        products_with_images.add(r["product_id"])
        key = (r["product_id"], (r["path"] or "").replace("\\", "/"))
        if key in seen_paths:
            continue
        seen_paths.add(key)
        if local_exists(r["path"] or ""):
            local_ok += 1
            products_with_local.add(r["product_id"])
        elif (r["source_url"] or r["path"]):
            local_missing += 1

    return {
        "products_db": products,
        "products_with_characteristics": with_chars,
        "products_with_description": with_desc,
        "products_with_image_records": len(products_with_images),
        "products_with_local_images": len(products_with_local),
        "image_records": image_records,
        "image_urls_found": urls_found if urls_found else image_records,
        "image_download_success": local_ok,
        "image_download_failed": local_missing,
    }


def pct(num: float, den: float) -> str:
    if den <= 0:
        return "N/A"
    return f"{num / den * 100:.1f}%"


def build_report() -> dict:
    if not DB_PATH.is_file():
        raise SystemExit(f"DB not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    by_source: dict[str, dict] = {}
    for source in SOURCES:
        cp = scan_checkpoints(source)
        lg = scan_logs(source)
        db = query_db(conn, source)

        discovered = len(cp["discovered_ids"]) or db["products_db"]
        completed_cp = len(cp["completed_ids"])
        pending = len(cp["pending_ids"])

        # detail_attempted = products where detail scrape was tried (not still queued in pending)
        failed_ids = lg["failed_ids"] - cp["completed_ids"]
        detail_failed = len(failed_ids)
        detail_success = db["products_db"]
        detail_attempted = completed_cp + detail_failed
        if detail_attempted < detail_success:
            detail_attempted = detail_success + detail_failed

        # deferred: products in blocked/deferred categories + categories flagged needs_recheck
        deferred_needs_recheck = cp["deferred_pending_products"]
        if deferred_needs_recheck == 0 and cp["deferred_categories"]:
            deferred_needs_recheck = cp["deferred_categories"]

        retry_events = cp["retry_events_checkpoint"]
        if lg["retry_events_logs"]:
            retry_events = max(retry_events, lg["retry_events_logs"])

        blocked = lg["blocked_403_429_logs"]
        if cp["blocked_403_checkpoint"]:
            blocked = max(blocked, cp["blocked_403_checkpoint"])

        metrics = {
            "discovered_products": discovered,
            "detail_attempted": detail_attempted,
            "detail_success": detail_success,
            "detail_failed": detail_failed if detail_failed else max(0, detail_attempted - detail_success),
            "deferred_needs_recheck": deferred_needs_recheck,
            "retry_events": retry_events,
            "blocked_403_429": blocked,
            "blocked_image_403_429": lg["blocked_image_logs"],
            "image_urls_found": db["image_urls_found"],
            "image_download_success": db["image_download_success"],
            "image_download_failed": db["image_download_failed"],
            "products_with_characteristics": db["products_with_characteristics"],
            "products_with_description": db["products_with_description"],
            "products_with_image_records": db["products_with_image_records"],
            "products_with_local_images": db["products_with_local_images"],
            "pending_not_attempted": pending,
            "checkpoint_files": cp["checkpoint_files"],
            "notes": [],
        }

        if discovered == 0 and db["products_db"] > 0:
            metrics["discovered_products"] = db["products_db"]
            metrics["notes"].append("discovered from DB (no checkpoint product ids)")

        if detail_failed == 0 and lg["detail_failed_lines"] == 0 and detail_attempted > detail_success:
            metrics["detail_failed"] = detail_attempted - detail_success

        if cp["retry_events_checkpoint"] == 0 and lg["retry_events_logs"] == 0:
            metrics["notes"].append("retry_events: only explicit log/checkpoint retry markers")

        by_source[source] = metrics

    conn.close()

    totals = defaultdict(float)
    for m in by_source.values():
        for k in (
            "discovered_products", "detail_attempted", "detail_success", "detail_failed",
            "deferred_needs_recheck", "retry_events", "blocked_403_429",
            "image_urls_found", "image_download_success", "image_download_failed",
            "products_with_characteristics", "products_with_description",
            "products_with_image_records", "products_with_local_images",
        ):
            totals[k] += m[k]

    return {"by_source": by_source, "total": dict(totals)}


def print_table(report: dict) -> None:
    cols = [
        ("discovered", "discovered_products"),
        ("attempted", "detail_attempted"),
        ("success", "detail_success"),
        ("failed", "detail_failed"),
        ("deferred", "deferred_needs_recheck"),
        ("retries", "retry_events"),
        ("403/429", "blocked_403_429"),
        ("img_urls", "image_urls_found"),
        ("img_ok", "image_download_success"),
        ("img_fail", "image_download_failed"),
    ]

    header = f"{'source':<10}" + "".join(f"{c[0]:>10}" for c in cols) + f"{'succ_rate':>10}{'char_cov':>10}{'desc_cov':>10}{'photo_cov':>10}{'img_rate':>10}"
    print(header)
    print("-" * len(header))

    rows = list(report["by_source"].items()) + [("TOTAL", report["total"])]

    for name, m in rows:
        disc = m["discovered_products"]
        att = m["detail_attempted"]
        succ = m["detail_success"]
        line = f"{name:<10}"
        for _, key in cols:
            val = m[key]
            line += f"{int(val):>10}"
        line += f"{pct(succ, att):>10}"
        line += f"{pct(m.get('products_with_characteristics', 0), disc):>10}"
        line += f"{pct(m.get('products_with_description', 0), disc):>10}"
        line += f"{pct(m.get('products_with_local_images', m.get('products_with_image_records', 0)), disc):>10}"
        img_rec = m.get("image_urls_found") or m.get("image_records", 0)
        line += f"{pct(m.get('image_download_success', 0), img_rec):>10}"
        print(line)

    print("\nNotes:")
    for src, m in report["by_source"].items():
        extra = []
        if m.get("pending_not_attempted"):
            extra.append(f"pending_not_attempted={m['pending_not_attempted']}")
        if m.get("checkpoint_files"):
            extra.append(f"checkpoints={m['checkpoint_files']}")
        if m.get("blocked_image_403_429"):
            extra.append(f"image_http_blocks={m['blocked_image_403_429']}")
        if m.get("notes"):
            extra.extend(m["notes"])
        if extra:
            print(f"  {src}: {', '.join(extra)}")


def main() -> int:
    report = build_report()
    print_table(report)
    out = PROJECT_ROOT / "output" / "parse_success_analytics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
