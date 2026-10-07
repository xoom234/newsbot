from aiogram import Bot

from .collector import Collector
from .config import Config
from .digest import DigestSender
from .storage import Storage


class Services:
    """Everything one run needs: database, bot, collector and sender."""

    def __init__(self, config: Config):
        self.config = config
        self.storage = Storage(config.database_url)
        self.bot = Bot(config.bot_token)
        self.collector = Collector(config, self.storage)
        self.sender = DigestSender(self.bot, config, self.storage)

    async def __aenter__(self) -> "Services":
        await self.storage.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.bot.session.close()
        await self.storage.close()

    async def collect(self) -> int:
        return await self.collector.run()

    async def run_hourly(self) -> dict:
        added = await self.collect()
        return {"added": added, "sent": await self.publish()}

    async def publish(self) -> int:
        if not self.config.chat_id:
            return 0
        sent = await self.sender.send_due(self.config.chat_id)
        await self.storage.cleanup()
        return sent

    def start_text(self, chat_id: int) -> str:
        c = self.config
        if c.delivery == "batches":
            schedule = (
                f"Новости собираются каждый час и приходят пачками по {c.batch_size} "
                f"({c.publish_window}, {c.timezone})."
            )
        elif c.delivery == "realtime":
            schedule = f"Новости приходят через {c.send_delay_minutes} мин после публикации."
        else:
            schedule = f"Дайджест приходит каждый день в {c.digest_time} ({c.timezone})."
        return f"Твой chat_id: <code>{chat_id}</code>\n\n{schedule}\n\n/stats — сколько новостей ждёт отправки"

    async def stats_text(self) -> str:
        counts = await self.storage.pending_counts()
        if not counts:
            return "Очередь пуста."
        lines = [f"{name}: {n}" for name, n in counts.items()]
        return "Ждут отправки:\n" + "\n".join(lines) + f"\n\nВсего: {sum(counts.values())}"
