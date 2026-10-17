#!/usr/bin/env python3
"""Probe Kontur Focus: login via auth.kontur.ru, then lookup company by INN."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

from common import CloakSession, setup_logging

# ПАО Сбербанк — тестовый ИНН для прогона.
DEFAULT_INN = "7707083893"
PROFILE_DIR = str(Path(__file__).resolve().parent / "output" / "kontur_profile")
AUTH_HOST = "auth.kontur.ru"
FOCUS_HOME = "https://focus.kontur.ru/"


def _is_auth_url(url: str) -> bool:
    return AUTH_HOST in url


async def _click_first(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            try:
                await loc.wait_for(state="visible", timeout=5000)
                await loc.click()
                return sel
            except Exception:
                continue
    return None


async def _open_auth_page(page) -> tuple[object, dict]:
    """Open Kontur login form. Never touch the Focus search input."""
    step: dict = {"step": "open_auth"}

    if _is_auth_url(page.url):
        step["mode"] = "already_on_auth"
        return page, step

    login_btn = page.locator('button:has-text("Войти")').first
    if await login_btn.count() == 0:
        step["mode"] = "no_login_button"
        return page, step

    try:
        async with page.context.expect_page(timeout=12_000) as popup_info:
            await login_btn.click()
        auth_page = await popup_info.value
        await auth_page.wait_for_load_state("domcontentloaded")
        step.update({"mode": "popup", "url": auth_page.url})
        return auth_page, step
    except Exception:
        pass

    await login_btn.click()
    await asyncio.sleep(4)
    if _is_auth_url(page.url):
        step.update({"mode": "same_tab_redirect", "url": page.url})
        return page, step

    redirect = (
        "https://auth.kontur.ru/?back=https%3A%2F%2Ffocus.kontur.ru%2F"
        "&clientId=focus"
    )
    await page.goto(redirect, wait_until="domcontentloaded", timeout=120_000)
    step.update({"mode": "direct_auth_url", "url": page.url})
    return page, step


async def _fill_auth_form(auth_page, email: str, password: str) -> list[dict]:
    """Fill credentials ONLY on auth.kontur.ru — not on Focus search."""
    steps: list[dict] = []
    if not _is_auth_url(auth_page.url):
        steps.append({"step": "skip_fill", "reason": "not_auth_page", "url": auth_page.url})
        return steps

    await asyncio.sleep(2)

    email_input = auth_page.locator(
        'input[type="email"], input[name="email"], input[name="login"], '
        'input[name="Login"], input[autocomplete="username"]'
    ).first
    if await email_input.count() > 0:
        await email_input.wait_for(state="visible", timeout=10_000)
        await email_input.fill(email)
        steps.append({"step": "fill_email"})
        cont = await _click_first(
            auth_page,
            ['button:has-text("Продолжить")', 'button:has-text("Далее")', 'button[type="submit"]'],
        )
        if cont:
            steps.append({"step": "continue_after_email", "selector": cont})
            await asyncio.sleep(3)

    pwd_input = auth_page.locator(
        'input[type="password"], input[name="password"], input[autocomplete="current-password"]'
    ).first
    if await pwd_input.count() > 0:
        await pwd_input.wait_for(state="visible", timeout=10_000)
        await pwd_input.fill(password)
        steps.append({"step": "fill_password"})

    submit = await _click_first(
        auth_page,
        ['button:has-text("Войти")', 'button[type="submit"]', 'input[type="submit"]'],
    )
    if submit:
        steps.append({"step": "submit", "selector": submit})

    return steps


async def try_login(page, email: str, password: str) -> dict:
    result: dict = {"logged_in": False, "steps": []}

    await page.goto(FOCUS_HOME, wait_until="domcontentloaded", timeout=120_000)
    await asyncio.sleep(4)
    result["steps"].append({"step": "home", "url": page.url})

    html = (await page.content()).lower()
    if await page.locator('button:has-text("Войти")').count() == 0 and (
        "выйти" in html or "logout" in html
    ):
        result["logged_in"] = True
        result["steps"].append({"step": "already_logged_in"})
        return result

    auth_page, open_step = await _open_auth_page(page)
    result["steps"].append(open_step)
    result["steps"].append({
        "step": "auth_page",
        "url": auth_page.url,
        "title": await auth_page.title(),
    })

    result["steps"].extend(await _fill_auth_form(auth_page, email, password))

    try:
        await page.wait_for_url(re.compile(r"focus\.kontur\.ru"), timeout=45_000)
    except Exception:
        pass
    await asyncio.sleep(8)

    if auth_page is not page and not auth_page.is_closed():
        await auth_page.close()

    result["steps"].append({"step": "after_login", "url": page.url, "title": await page.title()})

    on_focus = "focus.kontur.ru" in page.url
    html = (await page.content()).lower()
    login_btn_count = await page.locator('button:has-text("Войти")').count()
    if any(x in html for x in ("неверный пароль", "неверный логин", "wrong password")):
        result["error_hint"] = "login_rejected"
    elif on_focus and (login_btn_count == 0 or "выйти" in html):
        result["logged_in"] = True
    elif _is_auth_url(page.url):
        result["error_hint"] = "stuck_on_auth_page"
    else:
        result["error_hint"] = "login_incomplete"

    return result


async def probe_company(page, inn: str) -> dict:
    """Lookup company by INN — separate from login form."""
    url = f"https://focus.kontur.ru/entity?query={quote(inn)}"
    await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    await asyncio.sleep(6)

    html = await page.content()
    low = html.lower()
    hints = []
    if inn in html:
        hints.append("inn_in_html")
    if re.search(r"выручк", low):
        hints.append("revenue")
    if re.search(r"прибыл", low):
        hints.append("profit")
    if re.search(r"исполнительн", low):
        hints.append("enforcement")
    if re.search(r"арбитраж", low):
        hints.append("arbitration")

    return {
        "inn": inn,
        "url": page.url,
        "title": await page.title(),
        "html_len": len(html),
        "has_inn": inn in html,
        "hints": hints,
        "captcha": "captcha" in low or "smartcaptcha" in low,
    }


async def run_probe(inn: str, *, headless: bool, email: str, password: str) -> dict:
    out: dict = {"inn": inn, "profile_dir": PROFILE_DIR}
    async with CloakSession(headless=headless, humanize=True, profile_dir=PROFILE_DIR) as session:
        page = await session.new_page()
        try:
            out["login"] = await try_login(page, email, password)
            out["company"] = await probe_company(page, inn)
        finally:
            await page.close()
    return out


def main() -> None:
    setup_logging()
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--inn", default=DEFAULT_INN, help="Company INN for lookup after login")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    email = os.getenv("KONTUR_EMAIL", "").strip()
    password = os.getenv("KONTUR_PASSWORD", "").strip()
    if not email or not password:
        raise SystemExit("Set KONTUR_EMAIL and KONTUR_PASSWORD in .env")

    from cloakbrowser.download import ensure_binary

    print(f"CloakBrowser: {ensure_binary()}")
    print(f"1) Login on auth.kontur.ru  2) Lookup INN {args.inn}")

    result = asyncio.run(run_probe(args.inn, headless=args.headless, email=email, password=password))
    print(json.dumps(result, ensure_ascii=False, indent=2))

    logged = result.get("login", {}).get("logged_in")
    company = result.get("company", {})
    print("\n=== SUMMARY ===")
    print(f"Login: {'OK' if logged else 'FAIL'}")
    print(f"INN {args.inn}: {company.get('title', '?')}")
    print(f"Data hints: {', '.join(company.get('hints', [])) or '-'}")


if __name__ == "__main__":
    main()
