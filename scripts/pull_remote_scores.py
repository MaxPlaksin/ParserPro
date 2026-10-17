#!/usr/bin/env python3
"""Забирает готовые карточки с серверов и дописывает их в базу."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.score_glavsnab import DEFAULT_XLSX, load_cached, save_workbook  # noqa: E402

CACHE = ROOT / "output" / "glavsnab_checko" / "cache"
HOSTS = sys.argv[1:]
INTERVAL = 10


def connect(host: str) -> paramiko.SSHClient:
    password = os.environ[f"SSH_PASS_{host.replace('.', '_')}"]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username="root",
        password=password,
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def pull_once(clients: list[tuple[str, paramiko.SSHClient]]) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    new = 0
    for host, client in clients:
        try:
            sftp = client.open_sftp()
        except Exception:
            continue
        try:
            for entry in sftp.listdir_attr("/root/glavsnab-checko/out"):
                name = entry.filename
                if not name.endswith(".json"):
                    continue
                dest = CACHE / name
                if dest.exists() and dest.stat().st_size > 0:
                    current = dest.read_text(encoding="utf-8")
                    has_name = '"Название": "' in current and '"Название": ""' not in current
                    blocked = any(token in current.lower() for token in ("429", "403", "blocked", "timed out"))
                    if has_name and not blocked:
                        continue
                temp = dest.with_suffix(".part")
                sftp.get(f"/root/glavsnab-checko/out/{name}", str(temp))
                incoming = temp.read_text(encoding="utf-8")
                if any(token in incoming.lower() for token in ("429", "403", "blocked", "timed out")):
                    temp.unlink(missing_ok=True)
                    continue
                temp.replace(dest)
                new += 1
        finally:
            sftp.close()
    if new:
        by_inn = {}
        for path in CACHE.glob("*.json"):
            inn = path.stem
            cached = load_cached(inn)
            if cached:
                by_inn[inn] = cached
        save_workbook(DEFAULT_XLSX, by_inn)
        print(f"added {new}, workbook {len(by_inn)}", flush=True)
    return new


def main() -> None:
    clients = []
    for host in HOSTS:
        try:
            clients.append((host, connect(host)))
            print("connected", host, flush=True)
        except Exception as exc:  # noqa: BLE001
            print("connect fail", host, type(exc).__name__, flush=True)
    print("puller", ", ".join(host for host, _client in clients), flush=True)
    while True:
        try:
            pull_once(clients)
        except Exception as exc:  # noqa: BLE001
            print("pull error", exc, flush=True)
            clients = []
            for host in HOSTS:
                try:
                    clients.append((host, connect(host)))
                except Exception as reconnect_exc:  # noqa: BLE001
                    print("reconnect", host, reconnect_exc, flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
