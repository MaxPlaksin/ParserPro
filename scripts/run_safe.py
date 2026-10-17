#!/usr/bin/env python3
"""Не даёт компьютеру уснуть и добирает каталог Лемана ПРО.

Петрович в этот запуск не стартует. Если свободной памяти меньше 0.7 ГБ,
Лемана останавливается и запускается снова, когда память освободится.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
LOG_PATH = ROOT / "output" / "system_load.log"
PID_PATH = ROOT / "output" / "parser_pids.json"

# Стройматериалы (245635348) уже собраны.
PETROVICH_SECTIONS = [
    "1336",        # Крепеж
    "13381",       # Сад и досуг
    "285396560",   # Товары для дома
    "1366",        # Сантехника
    "1533",        # Финишная отделка
    "1313",        # Инструмент
    "12088",       # Инженерные системы
    "1384",        # Электрика
]

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040

LOW_RAM_GB = 0.7
RESUME_RAM_GB = 1.5
# Если в логе парсера тишина дольше этого времени, браузер завис: процесс убиваем и поднимаем снова.
STALL_SEC = 8 * 60


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def setup_log() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("run_safe")


def prevent_sleep() -> None:
    commands = [
        ["powercfg", "/change", "standby-timeout-ac", "0"],
        ["powercfg", "/change", "standby-timeout-dc", "0"],
        ["powercfg", "/change", "hibernate-timeout-ac", "0"],
        ["powercfg", "/change", "hibernate-timeout-dc", "0"],
        ["powercfg", "/change", "monitor-timeout-ac", "0"],
        ["powercfg", "/change", "monitor-timeout-dc", "0"],
        ["powercfg", "/change", "disk-timeout-ac", "0"],
        ["powercfg", "/change", "disk-timeout-dc", "0"],
    ]
    for cmd in commands:
        subprocess.run(cmd, check=False, capture_output=True)
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED | ES_AWAYMODE_REQUIRED
    )


def memory_gb() -> tuple[float, float]:
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    total = stat.ullTotalPhys / (1024 ** 3)
    free = stat.ullAvailPhys / (1024 ** 3)
    return total, free


def hold_awake() -> None:
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED | ES_AWAYMODE_REQUIRED
    )


def running(proc: subprocess.Popen | None) -> bool:
    return proc is not None and proc.poll() is None


def stop_proc(proc: subprocess.Popen | None) -> None:
    if not running(proc):
        return
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False, capture_output=True)


def start_petrovich(section: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            PYTHON, "-u", str(ROOT / "petrovich_parser.py"),
            "--section", section,
            "--skip-existing",
            "--no-headless",
            "-o", str(ROOT / "output" / "petrovich"),
        ],
        cwd=str(ROOT),
    )


LEMANA_LOG = ROOT / "output" / "lemana_run.log"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return str(pid) in (result.stdout or "")


def single_instance(log: logging.Logger) -> bool:
    """Второй супервизор только мешает: два браузера дерутся за один профиль и оба падают."""
    lock = ROOT / "output" / "run_safe.lock"
    pid = os.getpid()
    if lock.exists():
        try:
            old = int(lock.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            old = 0
        if old and old != pid and pid_alive(old):
            log.warning("Сбор уже ведёт процесс %s. Второй запуск закрываю.", old)
            return False
    lock.write_text(str(pid), encoding="utf-8")
    return True


def start_lemana() -> subprocess.Popen:
    LEMANA_LOG.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LEMANA_LOG, "a", encoding="utf-8")
    handle.write("\n----- старт -----\n")
    handle.flush()
    proc = subprocess.Popen(
        [
            PYTHON, "-u", str(ROOT / "lemana_parser.py"),
            "--url", "https://lemanapro.ru/catalogue/santehnika/",
            "--url", "https://lemanapro.ru/catalogue/elektrotovary/",
            "--skip-existing",
            "--follow-catalogue",
            "--no-headless",
            "--delay", "7",
            "-o", str(ROOT / "output" / "lemana"),
        ],
        cwd=str(ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    proc._log_handle = handle  # не закрывать, пока процесс пишет лог
    return proc


def write_pids(petrovich: subprocess.Popen | None, lemana: subprocess.Popen | None) -> None:
    payload = {
        "petrovich": petrovich.pid if running(petrovich) else None,
        "lemana": lemana.pid if running(lemana) else None,
    }
    PID_PATH.write_text(json.dumps(payload), encoding="utf-8")


def log_is_stale(started_at: float) -> bool:
    if not LEMANA_LOG.exists():
        return time.time() - started_at > STALL_SEC
    updated = LEMANA_LOG.stat().st_mtime
    return time.time() - max(updated, started_at) > STALL_SEC


def main() -> None:
    log = setup_log()
    if not single_instance(log):
        return
    prevent_sleep()
    log.info("Сон, гибернация и гашение экрана отключены")
    lemana: subprocess.Popen | None = None
    lemana_done = False
    lemana_paused = False
    lemana_cooldown_until = 0.0
    lemana_started_at = 0.0

    while not lemana_done or running(lemana):
        hold_awake()
        _total, free = memory_gb()
        if running(lemana):
            state = "идёт"
        elif lemana_done:
            state = "готово"
        elif time.time() < lemana_cooldown_until:
            left = int((lemana_cooldown_until - time.time()) / 60) + 1
            state = f"пауза {left} мин, затем снова с последней страницы"
        else:
            state = "стоп"
        log.info("RAM свободно %.2f ГБ | лемана=%s", free, state)

        if running(lemana) and free < LOW_RAM_GB:
            log.warning("Мало памяти (%.2f ГБ), ставлю Лемана на паузу", free)
            stop_proc(lemana)
            lemana = None
            lemana_paused = True

        if running(lemana) and log_is_stale(lemana_started_at):
            log.warning("Лемана молчит дольше %s мин, перезапускаю с последней страницы", STALL_SEC // 60)
            stop_proc(lemana)
            lemana = None
            lemana_paused = True
            lemana_cooldown_until = time.time() + 20

        if lemana is not None and lemana.poll() is not None:
            code = lemana.returncode
            log.info("Лемана завершилась с кодом %s", code)
            lemana = None
            if code == 0:
                lemana_done = True
                lemana_paused = False
            elif code == 75:
                lemana_paused = True
                lemana_cooldown_until = time.time() + 20 * 60
                log.warning("Сайт закрыл доступ. Следующий запуск Лемана через 20 минут, пустые карточки не пишутся.")
            else:
                lemana_paused = True
                lemana_cooldown_until = time.time() + 30
                log.warning("Лемана упала. Повтор через 30 секунд с последней страницы, причина в output/lemana_run.log")

        if not lemana_done and not running(lemana) and not lemana_paused:
            log.info("Старт Лемана ПРО, уже сохранённые карточки пропускаются")
            lemana = start_lemana()
            lemana_started_at = time.time()

        if lemana_paused and time.time() < lemana_cooldown_until:
            pass
        elif lemana_paused and not lemana_done and not running(lemana) and free >= RESUME_RAM_GB:
            log.info("Снова запускаю Лемана с последней страницы, RAM свободно %.2f ГБ", free)
            lemana_paused = False
            lemana = start_lemana()
            lemana_started_at = time.time()

        write_pids(None, lemana)
        time.sleep(20)

    hold_awake()
    log.info("Очередь разделов собрана. Сон по-прежнему запрещён, пока этот процесс запущен.")
    while True:
        hold_awake()
        _total, free = memory_gb()
        log.info("Сбор закончен, RAM свободно %.2f ГБ, сон запрещён", free)
        time.sleep(60)


if __name__ == "__main__":
    main()
