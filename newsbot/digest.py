import asyncio
import logging
from datetime import datetime, timedelta, timezone
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


def spread_times(
    count: int,
    now: datetime,
    tz: ZoneInfo,
    window: Tuple[Tuple[int, int], Tuple[int, int]],
    batch_min: int = 1,
    batch_max: int = 1,
) -> List[datetime]:
    """Groups count items into batches of batch_min..batch_max and spreads the batches evenly
    within today's window (or tomorrow's, if today's is over), at most one batch per hour.
    Returns one moment per item."""
    (sh, sm), (eh, em) = window
    local = now.astimezone(tz)
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if local >= end:
        start, end = start + timedelta(days=1), end + timedelta(days=1)
    start = max(start, local)

    hours = max(1, int((end - start) / timedelta(hours=1)))
    batches = -(-count // batch_max)  # ceil
    if batch_min > 1:
        batches = min(batches, max(1, count // batch_min))
    # more items than hourly slots can hold at batch_max: batches grow instead of dropping news
    batches = max(1, min(batches, hours))

    step = (end - start) / batches
    return [start + step * (i * batches // max(count, 1)) for i in range(count)]


class DigestSender:
    def __init__(self, bot: Bot, config: Config, storage: Storage):
        self.bot = bot
        self.config = config
        self.storage = storage
        self.lock = asyncio.Lock()

    async def schedule(self) -> int:
        """spread mode: assigns publication times to newly collected items."""
        ids = await self.storage.unscheduled_ids()
        if ids:
            times = spread_times(
                len(ids),
                datetime.now(timezone.utc),
                ZoneInfo(self.config.timezone),
                self.config.publish_window_hm,
                *self.config.batch_range,
            )
            await self.storage.set_schedule(ids, times)
            log.info("Scheduled %d items from %s to %s", len(ids), times[0], times[-1])
        return len(ids)

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
        """Sends items whose time has come: by schedule (spread) or by publication delay (realtime)."""
        if self.lock.locked():
            return 0
        async with self.lock:
            if self.config.delivery == "spread":
                items = await self.storage.scheduled_due()
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
