#!/usr/bin/env python3
"""Кладёт проход Checko в /root/glavsnab-checko и запускает его. Остальное на сервере не трогает."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import paramiko

LOCAL = Path(__file__).resolve().parents[1]
REMOTE = "/root/glavsnab-checko"


def ensure(sftp: paramiko.SFTPClient, path: str) -> None:
    current = ""
    for part in path.strip("/").split("/"):
        current += "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def main() -> None:
    host = sys.argv[1]
    inns = sys.argv[2]
    password = os.environ["SSH_PASS"]
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
    sftp = client.open_sftp()
    for folder in (
        f"{REMOTE}/models",
        f"{REMOTE}/normalizer",
        f"{REMOTE}/counterparty",
        f"{REMOTE}/out",
    ):
        ensure(sftp, folder)
    mapping = {
        LOCAL / "scripts" / "score_glavsnab.py": f"{REMOTE}/score_glavsnab.py",
        LOCAL / "scripts" / "remote_checko_run.py": f"{REMOTE}/remote_checko_run.py",
        LOCAL / "models" / "company_check_record.py": f"{REMOTE}/models/company_check_record.py",
        LOCAL / "normalizer" / "checko.py": f"{REMOTE}/normalizer/checko.py",
        LOCAL / "counterparty" / "version.py": f"{REMOTE}/counterparty/version.py",
        Path(inns): f"{REMOTE}/inns.txt",
    }
    for src, dst in mapping.items():
        sftp.put(str(src), dst)
    for init in (
        f"{REMOTE}/models/__init__.py",
        f"{REMOTE}/normalizer/__init__.py",
        f"{REMOTE}/counterparty/__init__.py",
    ):
        with sftp.file(init, "w") as handle:
            handle.write("")
    launcher = (
        "import os, signal, subprocess, time, pathlib\n"
        "os.chdir('/root/glavsnab-checko')\n"
        "for line in os.popen('ps -eo pid,args').read().splitlines():\n"
        "    if 'remote_checko_run.py' in line:\n"
        "        pid = line.split(None, 1)[0]\n"
        "        if pid.isdigit():\n"
        "            try:\n"
        "                os.kill(int(pid), signal.SIGTERM)\n"
        "            except ProcessLookupError:\n"
        "                pass\n"
        "time.sleep(1)\n"
        "log = open('run.log', 'a', buffering=1)\n"
        "log.write('\\n--- restart ---\\n')\n"
        "log.flush()\n"
        "proc = subprocess.Popen(\n"
        "    ['python3', '-u', 'remote_checko_run.py'],\n"
        "    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,\n"
        "    start_new_session=True, cwd='/root/glavsnab-checko',\n"
        ")\n"
        "pathlib.Path('run.pid').write_text(str(proc.pid))\n"
        "print('started', proc.pid)\n"
    )
    with sftp.file(f"{REMOTE}/_launch.py", "w") as handle:
        handle.write(launcher)
    sftp.close()
    _stdin, stdout, stderr = client.exec_command(f"python3 {REMOTE}/_launch.py", timeout=25)
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print(err)
    time.sleep(8)
    _stdin, stdout, stderr = client.exec_command(f"tail -n 20 {REMOTE}/run.log", timeout=20)
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print(err)
    client.close()


if __name__ == "__main__":
    main()
