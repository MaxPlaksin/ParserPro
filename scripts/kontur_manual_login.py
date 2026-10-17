#!/usr/bin/env python3
"""
Ручной вход в Контур.Фокус — сессия сохранится в профиль CloakBrowser.

Запуск (откроется окно браузера):
  .\\.venv\\Scripts\\python.exe scripts\\kontur_manual_login.py

1. Войдите в Контур вручную (email + пароль, 2FA если есть).
2. Убедитесь, что на focus.kontur.ru видна главная уже авторизованная.
3. Вернитесь в консоль и нажмите Enter — профиль сохранится.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

PROFILE_DIR = str(ROOT / "output" / "kontur_profile")
FOCUS_URL = "https://focus.kontur.ru/"


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wait-sec",
        type=int,
        default=600,
        help="Сколько секунд держать браузер открытым (по умолчанию 10 мин)",
    )
    args = parser.parse_args()

    setup_logging()
    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)

    from cloakbrowser.download import ensure_binary

    print(f"CloakBrowser: {ensure_binary()}")
    print(f"Профиль сессии: {PROFILE_DIR}")
    print(f"Открываю {FOCUS_URL} ...")
    print()
    print(">>> Войдите в Контур в открывшемся окне браузера.")
    print(f">>> Браузер закроется через {args.wait_sec} сек — успейте авторизоваться.")
    print()

    async with CloakSession(headless=False, humanize=True, profile_dir=PROFILE_DIR) as session:
        page = await session.new_page()
        await page.goto(FOCUS_URL, wait_until="domcontentloaded", timeout=120_000)
        for left in range(args.wait_sec, 0, -30):
            print(f"  ... осталось ~{left} сек", flush=True)
            await asyncio.sleep(min(30, left))
        title = await page.title()
        url = page.url
        html = (await page.content()).lower()
        logged_in = await page.locator('button:has-text("Войти")').count() == 0
        print()
        print(f"URL:   {url}")
        print(f"Title: {title}")
        print(f"Вход выполнен: {'ДА' if logged_in else 'НЕТ — запустите скрипт ещё раз'}")
        await page.close()

    print(f"Профиль сохранён: {PROFILE_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
