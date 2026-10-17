#!/usr/bin/env python3
"""
Pull-API для основной платформы.

  POST /api/v1/checks          — поставить в очередь
  GET  /api/v1/checks/{inn}    — забрать результат

  Authorization: Bearer <PARSER_API_KEY>

Запуск:
  python -m counterparty.api
"""

from __future__ import annotations

import argparse
import logging
import os

from aiohttp import web
from dotenv import load_dotenv

from common import setup_logging
from counterparty.service import get_service
from counterparty.version import PARSER_VERSION

logger = logging.getLogger(__name__)


@web.middleware
async def bearer_auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/v1/health"):
        return await handler(request)
    expected = os.getenv("PARSER_API_KEY", "").strip()
    if not expected:
        logger.error("PARSER_API_KEY is not set")
        return web.json_response({"status": "error", "error": "server misconfigured"}, status=503)
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {expected}":
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    return await handler(request)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "parser_version": PARSER_VERSION})


async def post_checks(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "invalid json"}, status=400)

    inn = str(body.get("inn", "")).strip()
    if not inn:
        return web.json_response({"status": "error", "error": "inn is required"}, status=400)

    svc = get_service()
    try:
        result = svc.enqueue_check(
            inn,
            company_id=str(body["company_id"]) if body.get("company_id") is not None else None,
            name=str(body["name"]) if body.get("name") else None,
            force=bool(body.get("force", False)),
        )
    except ValueError:
        return web.json_response({"status": "error", "error": "invalid inn"}, status=400)

    return web.json_response(result)


async def get_checks_by_inn(request: web.Request) -> web.Response:
    inn = request.match_info["inn"].strip()
    svc = get_service()
    return web.json_response(svc.get_result(inn))


async def on_startup(_app: web.Application) -> None:
    get_service().start_worker()
    logger.info("Counterparty API ready, worker running")


async def on_cleanup(_app: web.Application) -> None:
    await get_service().stop_worker()


def create_app() -> web.Application:
    app = web.Application(middlewares=[bearer_auth_middleware])
    app.router.add_get("/api/v1/health", health)
    app.router.add_post("/api/v1/checks", post_checks)
    app.router.add_get("/api/v1/checks/{inn}", get_checks_by_inn)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    load_dotenv()
    setup_logging()
    parser = argparse.ArgumentParser(description="Counterparty check API")
    parser.add_argument("--host", default=os.getenv("COUNTERPARTY_API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("COUNTERPARTY_API_PORT", "8765")))
    args = parser.parse_args()

    if not os.getenv("PARSER_API_KEY"):
        raise SystemExit("Set PARSER_API_KEY in .env before starting API")

    logger.info("Starting API on %s:%s", args.host, args.port)
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
