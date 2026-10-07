import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Tuple
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from .config import Config
from .formatter import format_header, format_item
from .storage import NewsItem, Storage

log = logging.getLogger(__name__)

# Telegram allows roughly one message per second to a single chat
SEND_DELAY = 1.1


def batch_size(
    pending: int,
    now: datetime,
    window: Tuple[Tuple[int, int], Tuple[int, int]],
    batch_min: int,
    batch_max: int,
) -> int:
    """How many items an hourly run sends; 0 means keep accumulating until batch_min is reached.
    The last run of the day flushes everything; a backlog larger than the remaining runs can
    hold at batch_max makes batches bigger instead of carrying news over to tomorrow."""
    (sh, sm), (eh, em) = window
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    # Vercel Hobby cron fires anywhere within its hour, so the last run may come after the window end
    if pending == 0 or not (start <= now < end + timedelta(hours=1)):
        return 0
    runs_left = -(-(end - now) // timedelta(hours=1))  # ceil, this run included
    if runs_left <= 1:
        return pending
    if pending < batch_min:
        return 0
    return max(batch_max, -(-pending // runs_left))


class DigestSender:
    def __init__(self, bot: Bot, config: Config, storage: Storage):
        self.bot = bot
        self.config = config
        self.storage = storage
        self.lock = asyncio.Lock()

    async def _send(self, chat_id: int, text: str) -> None:
        preview = LinkPreviewOptions(is_disabled=not self.config.link_preview)
        while True:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML", link_preview_options=preview)
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)

    async def send(self, chat_id: int) -> int:
        """Sends everything pending at once as a digest."""
        async with self.lock:
            items = await self.storage.pending()
            if not items:
                await self._send(chat_id, "За это время новых новостей нет.")
                return 0

            now = datetime.now(ZoneInfo(self.config.timezone))
            await self._send(chat_id, format_header(now, len(items)))
            await asyncio.sleep(SEND_DELAY)
            await self._send_items(chat_id, items)
            await self.storage.cleanup()
            log.info("Digest sent: %d items", len(items))
            return len(items)

    async def send_due(self, chat_id: int) -> int:
        """Sends items whose time has come: a batch (batches) or by publication delay (realtime)."""
        if self.lock.locked():
            return 0
        async with self.lock:
            if self.config.delivery == "batches":
                now = datetime.now(ZoneInfo(self.config.timezone))
                size = batch_size(
                    await self.storage.pending_total(), now, self.config.publish_window_hm, *self.config.batch_range
                )
                items = await self.storage.oldest_pending(size) if size else []
            else:
                items = await self.storage.due(self.config.send_delay_minutes)
            await self._send_items(chat_id, items)
            if items:
                log.info("Sent %d items", len(items))
            return len(items)

    async def _send_items(self, chat_id: int, items: List[NewsItem]) -> None:
        for i, item in enumerate(items):
            if i:
                await asyncio.sleep(SEND_DELAY)
            try:
                await self._send(chat_id, format_item(item))
            except Exception as e:
                log.warning("Failed to send %s: %r", item.url, e)
                continue
            await self.storage.mark_sent(item.id)
