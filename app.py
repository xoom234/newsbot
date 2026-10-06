"""Vercel entrypoint: cron endpoints and Telegram webhook."""
import hmac
import logging
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from newsbot.config import load_config
from newsbot.services import Services

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.ERROR)

config = load_config()


def _secret_matches(given: str, env_name: str) -> bool:
    expected = os.getenv(env_name)
    return bool(expected) and hmac.compare_digest(given, expected)


def _cron_authorized(request: Request) -> bool:
    # Vercel Cron sends "Authorization: Bearer $CRON_SECRET"
    return _secret_matches(request.headers.get("authorization", "").removeprefix("Bearer "), "CRON_SECRET")


async def collect(request: Request) -> Response:
    if not _cron_authorized(request):
        return Response(status_code=401)
    async with Services(config) as app:
        added = await app.collect()
        sent = await app.publish()
    return JSONResponse({"added": added, "sent": sent})


async def publish(request: Request) -> Response:
    if not _cron_authorized(request):
        return Response(status_code=401)
    async with Services(config) as app:
        sent = await app.publish()
    return JSONResponse({"sent": sent})


async def telegram(request: Request) -> Response:
    if not _secret_matches(request.headers.get("x-telegram-bot-api-secret-token", ""), "WEBHOOK_SECRET"):
        return Response(status_code=401)
    message = (await request.json()).get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    command = (message.get("text") or "").split(maxsplit=1)[0].split("@")[0] if message.get("text") else ""
    if not chat_id or not command.startswith("/"):
        return Response()

    async with Services(config) as app:
        if command == "/start":
            await app.bot.send_message(chat_id, app.start_text(chat_id), parse_mode="HTML")
        elif command == "/stats" and chat_id == config.chat_id:
            await app.bot.send_message(chat_id, await app.stats_text())
    return Response()


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "sources": len(config.sources), "delivery": config.delivery})


app = Starlette(
    routes=[
        Route("/", health),
        Route("/api/collect", collect, methods=["GET", "POST"]),
        Route("/api/publish", publish, methods=["GET", "POST"]),
        # Vercel merges cron jobs with identical paths, so every daily run gets its own path
        Route("/api/publish/{slot}", publish, methods=["GET", "POST"]),
        Route("/api/telegram", telegram, methods=["POST"]),
    ]
)
