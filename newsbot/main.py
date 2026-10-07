"""Local run: long polling + in-process scheduler. On Vercel the same work is done by app.py."""
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import load_config
from .services import Services

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.ERROR)
log = logging.getLogger("newsbot")


async def main() -> None:
    config = load_config()
    async with Services(config) as app:
        dp = Dispatcher()
        owner = F.chat.id == config.chat_id

        @dp.message(Command("start"))
        async def start(message: Message) -> None:
            await message.answer(app.start_text(message.chat.id), parse_mode="HTML")

        @dp.message(Command("stats"), owner)
        async def stats(message: Message) -> None:
            await message.answer(await app.stats_text())

        @dp.message(Command("digest"), owner)
        async def digest_now(message: Message) -> None:
            await app.sender.send(message.chat.id)

        @dp.message(Command("collect"), owner)
        async def collect_now(message: Message) -> None:
            await message.answer("Собираю…")
            await message.answer(f"Новых новостей: {await app.collect()}")

        scheduler = AsyncIOScheduler(timezone=config.timezone)
        if config.delivery == "batches":
            scheduler.add_job(
                app.run_hourly, "cron", minute=0, max_instances=1, coalesce=True, misfire_grace_time=3600
            )
        else:
            scheduler.add_job(
                app.collect,
                "interval",
                minutes=config.collect_interval_minutes,
                next_run_time=datetime.now(timezone.utc),
                max_instances=1,
                coalesce=True,
            )

        if not config.chat_id:
            log.warning("CHAT_ID is not set: send /start to the bot, put the id into .env and restart")
        elif config.delivery == "digest":
            hour, minute = config.digest_hm
            scheduler.add_job(
                app.sender.send, "cron", hour=hour, minute=minute, args=[config.chat_id], misfire_grace_time=3600
            )
        elif config.delivery == "realtime":
            scheduler.add_job(app.publish, "interval", minutes=1, max_instances=1, coalesce=True)
        scheduler.start()

        log.info("Bot started with %d sources, delivery=%s", len(config.sources), config.delivery)
        try:
            await app.bot.delete_webhook()
            await dp.start_polling(app.bot)
        finally:
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
