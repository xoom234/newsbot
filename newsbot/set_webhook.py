"""Points the Telegram bot to the Vercel deployment.

Usage: python -m newsbot.set_webhook https://your-project.vercel.app
"""
import asyncio
import os
import sys

from aiogram import Bot
from dotenv import load_dotenv


async def main(base_url: str) -> None:
    load_dotenv()
    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        raise SystemExit("WEBHOOK_SECRET is not set")
    bot = Bot(os.environ["BOT_TOKEN"])
    try:
        url = base_url.rstrip("/") + "/api/telegram"
        await bot.set_webhook(url, secret_token=secret, allowed_updates=["message"])
        info = await bot.get_webhook_info()
        print(f"Webhook: {info.url}")
        if info.last_error_message:
            print(f"Last error: {info.last_error_message}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1]))
