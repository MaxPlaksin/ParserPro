#!/usr/bin/env python3
"""Prepare M.Video checkpoints for detail backfill (requeue listing-only rows)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mvideo_parser import CheckpointState, requeue_failed_details_in_checkpoint  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Requeue M.Video rows missing descriptions/details")
    parser.add_argument(
        "-o",
        "--output",
        default=str(PROJECT_ROOT / "output" / "mvideo"),
        help="M.Video output root (contains checkpoint_global.json)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only report counts, do not write")
    args = parser.parse_args()

    output_root = Path(args.output)
    global_path = output_root / "checkpoint_global.json"
    global_cp: dict = {}
    if global_path.exists():
        global_cp = json.loads(global_path.read_text(encoding="utf-8"))

    completed = set(global_cp.get("completed_categories", []))
    total_requeued = 0
    categories_touched = 0
    pending_total = 0

    for cp_path in sorted(output_root.rglob("checkpoint.json")):
        checkpoint = CheckpointState.load(cp_path)
        if checkpoint is None:
            continue
        before_pending = len(checkpoint.pending)
        requeued = requeue_failed_details_in_checkpoint(checkpoint)
        if requeued <= 0 and before_pending == len(checkpoint.pending):
            continue
        categories_touched += 1
        total_requeued += requeued
        pending_total += len(checkpoint.pending)
        cat_key = str(checkpoint.category.category_id) if checkpoint.category else cp_path.parent.name
        if cat_key in completed and checkpoint.pending:
            completed.discard(cat_key)
        if args.dry_run:
            print(f"[dry-run] {cp_path.parent.name}: requeued={requeued}, pending={len(checkpoint.pending)}")
            continue
        checkpoint.save(cp_path)
        print(f"{cp_path.parent.name}: requeued={requeued}, pending={len(checkpoint.pending)}")

    if not args.dry_run and global_path.exists():
        global_cp["completed_categories"] = sorted(completed)
        global_path.write_text(json.dumps(global_cp, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"Done: categories={categories_touched}, requeued={total_requeued}, "
        f"pending_total={pending_total}, dry_run={args.dry_run}"
    )


if __name__ == "__main__":
    main()
