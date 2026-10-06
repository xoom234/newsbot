from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id BIGSERIAL PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    emoji TEXT NOT NULL,
    title TEXT NOT NULL,
    lead TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMPTZ,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);
ALTER TABLE news ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_news_pending ON news (source, collected_at) WHERE sent_at IS NULL;
"""


@dataclass
class NewsItem:
    url: str
    source: str
    emoji: str
    title: str
    lead: str = ""
    published_at: Optional[datetime] = None
    id: Optional[int] = None


class Storage:
    def __init__(self, dsn: str):
        # asyncpg rejects libpq-only options that hosted providers (e.g. Neon) put into the URL
        parts = urlsplit(dsn)
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"])
        self.dsn = urlunsplit(parts._replace(query=query))
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        # statement cache off: hosted poolers (PgBouncer in transaction mode) do not keep prepared statements
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5, statement_cache_size=0)
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def existing(self, urls: Iterable[str]) -> Set[str]:
        rows = await self.pool.fetch("SELECT url FROM news WHERE url = ANY($1::text[])", list(urls))
        return {r["url"] for r in rows}

    async def add(self, item: NewsItem, skip: bool = False) -> None:
        """skip=True stores the URL as already sent, so stale items are remembered but never delivered."""
        await self.pool.execute(
            "INSERT INTO news (url, source, emoji, title, lead, published_at, sent_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, CASE WHEN $7 THEN now() END) "
            "ON CONFLICT (url) DO NOTHING",
            item.url,
            item.source,
            item.emoji,
            item.title,
            item.lead,
            item.published_at,
            skip,
        )

    async def has_source(self, source: str) -> bool:
        return await self.pool.fetchval("SELECT EXISTS (SELECT 1 FROM news WHERE source = $1)", source)

    async def pending(self) -> List[NewsItem]:
        rows = await self.pool.fetch(
            "SELECT id, url, source, emoji, title, lead, published_at FROM news WHERE sent_at IS NULL "
            "ORDER BY source, COALESCE(published_at, collected_at)"
        )
        return [NewsItem(**dict(r)) for r in rows]

    async def due(self, delay_minutes: int) -> List[NewsItem]:
        """Unsent items published (or, without a date, collected) at least delay_minutes ago."""
        rows = await self.pool.fetch(
            "SELECT id, url, source, emoji, title, lead, published_at FROM news "
            "WHERE sent_at IS NULL "
            "AND COALESCE(published_at, collected_at) <= now() - make_interval(mins => $1) "
            "ORDER BY COALESCE(published_at, collected_at)",
            delay_minutes,
        )
        return [NewsItem(**dict(r)) for r in rows]

    async def scheduled_due(self) -> List[NewsItem]:
        rows = await self.pool.fetch(
            "SELECT id, url, source, emoji, title, lead, published_at FROM news "
            "WHERE sent_at IS NULL AND scheduled_at <= now() ORDER BY scheduled_at"
        )
        return [NewsItem(**dict(r)) for r in rows]

    async def unscheduled_ids(self) -> List[int]:
        rows = await self.pool.fetch(
            "SELECT id FROM news WHERE sent_at IS NULL AND scheduled_at IS NULL "
            "ORDER BY COALESCE(published_at, collected_at)"
        )
        return [r["id"] for r in rows]

    async def set_schedule(self, ids: List[int], times: List[datetime]) -> None:
        await self.pool.executemany(
            "UPDATE news SET scheduled_at = $2 WHERE id = $1", list(zip(ids, times))
        )

    async def next_scheduled(self) -> Optional[datetime]:
        return await self.pool.fetchval(
            "SELECT min(scheduled_at) FROM news WHERE sent_at IS NULL AND scheduled_at > now()"
        )

    async def pending_counts(self) -> Dict[str, int]:
        rows = await self.pool.fetch(
            "SELECT source, COUNT(*) AS n FROM news WHERE sent_at IS NULL GROUP BY source ORDER BY source"
        )
        return {r["source"]: r["n"] for r in rows}

    async def mark_sent(self, item_id: int) -> None:
        await self.pool.execute("UPDATE news SET sent_at = now() WHERE id = $1", item_id)

    async def cleanup(self, days: int = 30) -> None:
        await self.pool.execute(
            "DELETE FROM news WHERE sent_at < now() - make_interval(days => $1)", days
        )
