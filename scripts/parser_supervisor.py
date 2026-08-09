#!/usr/bin/env python3
"""Production supervisor for long-running DNS and M.Video parsers."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
ENV_PATH = PROJECT_ROOT / ".env"

BACKOFF_STEPS_SEC = (15, 30, 60, 120, 300)
STABLE_RUN_RESET_SEC = 600
ALERT_COOLDOWN_SEC = 900
STALL_WARN_SEC = 1800
STALL_WARN_COOLDOWN_SEC = 1800
TERMINATE_TIMEOUT_SEC = 15


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _hostname() -> str:
    return socket.gethostname()


def _load_env() -> None:
    """Load TELEGRAM_* and other vars from project .env (never logged)."""
    if not ENV_PATH.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=False)
    except ImportError:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _configure_stdio_utf8() -> None:
    """Use UTF-8 for supervisor console output (Windows defaults to cp1251)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _child_env() -> dict[str, str]:
    """Environment for child Python processes: force UTF-8 stdio."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class TelegramNotifier:
    """Send Telegram alerts; failures never propagate to caller."""

    def __init__(self, *, token: str | None, chat_id: str | None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._alert_cooldown: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def _cooldown_key(self, kind: str, detail: str) -> str:
        return f"{kind}:{detail}"

    def _should_send(self, key: str, cooldown_sec: int) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._alert_cooldown.get(key)
            if last is not None and now - last < cooldown_sec:
                return False
            self._alert_cooldown[key] = now
            return True

    def send(
        self,
        *,
        parser_name: str,
        kind: str,
        detail: str,
        restart_attempt: int | None = None,
        cooldown_sec: int = ALERT_COOLDOWN_SEC,
        force: bool = False,
    ) -> bool:
        if not self.configured:
            return False

        key = self._cooldown_key(kind, detail)
        if not force and not self._should_send(key, cooldown_sec):
            return False

        lines = [
            f"Parser: {parser_name}",
            f"Host: {_hostname()}",
            f"Time: {_now_iso()}",
            f"Event: {kind}",
            f"Detail: {detail}",
        ]
        if restart_attempt is not None:
            lines.append(f"Restart attempt: {restart_attempt}")

        text = "\n".join(lines)
        try:
            payload = urllib.parse.urlencode(
                {"chat_id": self._chat_id, "text": text}
            ).encode("utf-8")
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"[supervisor] Telegram send failed: {exc}", file=sys.stderr)
            return False


def _telegram_notifier() -> TelegramNotifier:
    return TelegramNotifier(
        token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
    )


class ProcessRunner:
    """Run child process with live stdout/stderr streaming."""

    def __init__(
        self,
        *,
        command: list[str],
        log_path: Path,
        stop_event: threading.Event,
    ) -> None:
        self.command = command
        self.log_path = log_path
        self.stop_event = stop_event
        self._proc: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []
        self._stall_thread: threading.Thread | None = None
        self._touch_output: Callable[[], None] = lambda: None

    def _write_log(self, log_file, text: str) -> None:
        log_file.write(text)
        log_file.flush()
        self._touch_output()

    def _stream_pipe(
        self,
        pipe,
        log_file,
        out_stream,
        prefix: str,
    ) -> None:
        try:
            for line in iter(pipe.readline, ""):
                if self.stop_event.is_set() and not line:
                    break
                if not line:
                    break
                log_line = f"{prefix}{line}" if prefix else line
                out_stream.write(line)
                out_stream.flush()
                self._write_log(log_file, log_line)
        finally:
            pipe.close()

    def _stall_watch(
        self,
        last_output: Callable[[], float],
        telegram: TelegramNotifier,
        parser_name: str,
    ) -> None:
        last_warn = 0.0
        while not self.stop_event.is_set():
            if self._proc is None or self._proc.poll() is not None:
                return
            idle = time.monotonic() - last_output()
            if idle >= STALL_WARN_SEC:
                now = time.monotonic()
                if now - last_warn >= STALL_WARN_COOLDOWN_SEC:
                    telegram.send(
                        parser_name=parser_name,
                        kind="WARNING",
                        detail="no output for 30 min",
                        cooldown_sec=STALL_WARN_COOLDOWN_SEC,
                    )
                    last_warn = now
            time.sleep(30)

    def run(self, *, telegram: TelegramNotifier, parser_name: str) -> int:
        if not self.command:
            raise ValueError("empty command")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        last_output_at = time.monotonic()

        def touch_output() -> None:
            nonlocal last_output_at
            last_output_at = time.monotonic()

        self._touch_output = touch_output

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        with open(self.log_path, "a", encoding="utf-8", newline="") as log_file:
            log_file.write(f"\n--- supervisor run {_now_iso()} ---\n")
            log_file.write(f"command: {' '.join(self.command)}\n")
            log_file.flush()

            self._proc = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=PROJECT_ROOT,
                env=_child_env(),
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )

            touch_output()

            self._threads = [
                threading.Thread(
                    target=self._stream_pipe,
                    args=(self._proc.stdout, log_file, sys.stdout, ""),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._stream_pipe,
                    args=(self._proc.stderr, log_file, sys.stderr, "[stderr] "),
                    daemon=True,
                ),
            ]
            for thread in self._threads:
                thread.start()

            self._stall_thread = threading.Thread(
                target=self._stall_watch,
                args=(lambda: last_output_at, telegram, parser_name),
                daemon=True,
            )
            self._stall_thread.start()

            try:
                while self._proc.poll() is None:
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.2)
            except KeyboardInterrupt:
                self.stop_event.set()
                raise

            if self.stop_event.is_set() and self._proc.poll() is None:
                self._terminate_child()

            for thread in self._threads:
                thread.join(timeout=5)
            if self._stall_thread:
                self._stall_thread.join(timeout=1)

            return self._proc.wait(timeout=5) if self._proc.poll() is None else self._proc.returncode

    def _terminate_child(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        print("[supervisor] terminating child process...", file=sys.stderr)
        self._proc.terminate()
        try:
            self._proc.wait(timeout=TERMINATE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            print("[supervisor] child did not exit; killing...", file=sys.stderr)
            self._proc.kill()
            self._proc.wait(timeout=5)


class ParserSupervisor:
    def __init__(self, *, name: str, command: list[str]) -> None:
        self.name = name
        self.command = command
        self.log_path = LOG_DIR / f"supervisor_{name}.log"
        self.telegram = _telegram_notifier()
        self._manual_stop = False
        self._stop_event = threading.Event()

    def _backoff_delay(self, index: int) -> int:
        if index < len(BACKOFF_STEPS_SEC):
            return BACKOFF_STEPS_SEC[index]
        return BACKOFF_STEPS_SEC[-1]

    def run(self) -> int:
        if not self.telegram.configured:
            print(
                "[supervisor] warning: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set "
                f"(env or {ENV_PATH.name}); alerts disabled",
                file=sys.stderr,
            )

        restart_attempt = 0
        backoff_index = 0

        while True:
            if restart_attempt == 0:
                print(f"[supervisor] starting {self.name}: {' '.join(self.command)}")
            else:
                print(
                    f"[supervisor] restarting {self.name} "
                    f"(attempt {restart_attempt})..."
                )

            started_at = time.monotonic()
            exit_code = 1
            exception_text: str | None = None

            runner = ProcessRunner(
                command=self.command,
                log_path=self.log_path,
                stop_event=self._stop_event,
            )

            try:
                exit_code = runner.run(telegram=self.telegram, parser_name=self.name)
            except KeyboardInterrupt:
                self._manual_stop = True
                self._stop_event.set()
                runner._terminate_child()
                self.telegram.send(
                    parser_name=self.name,
                    kind="stopped manually",
                    detail="KeyboardInterrupt",
                    restart_attempt=restart_attempt,
                    force=True,
                )
                print("[supervisor] stopped manually", file=sys.stderr)
                return 130
            except Exception as exc:
                exception_text = f"{type(exc).__name__}: {exc}"
                self.telegram.send(
                    parser_name=self.name,
                    kind="supervisor exception",
                    detail=exception_text,
                    restart_attempt=restart_attempt,
                )
                print(f"[supervisor] exception: {exception_text}", file=sys.stderr)

            ran_sec = time.monotonic() - started_at
            if ran_sec >= STABLE_RUN_RESET_SEC:
                backoff_index = 0

            if self._manual_stop:
                return 130

            if exit_code == 0 and exception_text is None:
                self.telegram.send(
                    parser_name=self.name,
                    kind="completed",
                    detail="exit code 0",
                    restart_attempt=restart_attempt,
                    force=True,
                )
                print(f"[supervisor] {self.name} completed successfully")
                return 0

            detail = exception_text or f"exit code {exit_code}"
            restart_attempt += 1
            self.telegram.send(
                parser_name=self.name,
                kind="failed",
                detail=detail,
                restart_attempt=restart_attempt,
            )

            delay = self._backoff_delay(backoff_index)
            backoff_index = min(backoff_index + 1, len(BACKOFF_STEPS_SEC) - 1)
            print(
                f"[supervisor] {self.name} failed ({detail}); "
                f"retry in {delay}s (attempt {restart_attempt})",
                file=sys.stderr,
            )
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                self._manual_stop = True
                self.telegram.send(
                    parser_name=self.name,
                    kind="stopped manually",
                    detail="KeyboardInterrupt during backoff",
                    restart_attempt=restart_attempt,
                    force=True,
                )
                print("[supervisor] stopped manually", file=sys.stderr)
                return 130


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    if "--" in argv:
        sep = argv.index("--")
        supervisor_argv = argv[:sep]
        command = argv[sep + 1 :]
    else:
        supervisor_argv = argv
        command = []

    parser = argparse.ArgumentParser(
        description="Production supervisor for DNS / M.Video parsers",
    )
    parser.add_argument(
        "--name",
        help="Parser name for logs and alerts (e.g. dns, mvideo)",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test Telegram message and exit",
    )
    args = parser.parse_args(supervisor_argv[1:])
    return args, command


def _test_telegram() -> int:
    notifier = _telegram_notifier()
    if not notifier.configured:
        print(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
            f"(env or {ENV_PATH.name})",
            file=sys.stderr,
        )
        return 1
    ok = notifier.send(
        parser_name="test",
        kind="test",
        detail="parser_supervisor telegram check",
        restart_attempt=0,
        force=True,
    )
    if ok:
        print("Telegram test message sent")
        return 0
    print("Telegram test message failed", file=sys.stderr)
    return 1


def main() -> None:
    _configure_stdio_utf8()
    _load_env()
    args, command = _parse_args(sys.argv)

    if args.test_telegram:
        sys.exit(_test_telegram())

    if not args.name:
        print("--name is required unless using --test-telegram", file=sys.stderr)
        sys.exit(2)
    if not command:
        print("command required after --", file=sys.stderr)
        sys.exit(2)

    supervisor = ParserSupervisor(name=args.name, command=command)
    sys.exit(supervisor.run())


if __name__ == "__main__":
    main()
