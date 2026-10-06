import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup

from .config import Config, Source
from .storage import NewsItem, Storage
from .topic import TopicFilter
from .translator import Translator

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.8",
}
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
TRACKING_PARAMS = re.compile(r"^(utm_|yclid|gclid|fbclid|from$|ref$)")
BOILERPLATE = re.compile(
    r"\b(inbox|newsletter|subscribe|sign up|cookies?|all rights reserved|"
    r"подпис\w+|рассылк\w+|все права защищены)\b",
    re.I,
)
READ_MORE = re.compile(r"\s*(Читать далее|Читать дальше|Подробнее|Read more|Continue reading)\W*$", re.I)

# (url, title, published_at, rss_summary)
RawEntry = Tuple[str, str, Optional[datetime], str]


def normalize_url(url: str) -> str:
    # fragment is kept: some feeds (e.g. release notes) point every entry to one page with different anchors
    parts = urlsplit(url.strip().rstrip("\\"))
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)])
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, query, parts.fragment))


def html_to_text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ")).strip()


def truncate(text: str, limit: int) -> str:
    text = READ_MORE.sub("", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "…"


def _entry_date(entry) -> Optional[datetime]:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def parse_feed(content: bytes) -> List[RawEntry]:
    feed = feedparser.parse(content)
    return [
        (e.link, html_to_text(e.get("title", "")), _entry_date(e), html_to_text(e.get("summary", "")))
        for e in feed.entries
        if e.get("link")
    ]


def find_feed_url(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")
    for link in soup.find_all("link", rel="alternate"):
        if link.get("type") in FEED_TYPES and link.get("href"):
            return urljoin(base_url, link["href"])
    return None


def parse_listing(html: str, base_url: str, selector: str) -> List[RawEntry]:
    soup = BeautifulSoup(html, "lxml")
    entries, seen = [], set()
    for el in soup.select(selector):
        a = el if el.name == "a" else el.find("a")
        if not a or not a.get("href"):
            continue
        url = urljoin(base_url, a["href"])
        if url in seen:
            continue
        seen.add(url)
        entries.append((url, re.sub(r"\s+", " ", el.get_text(" ")).strip(), None, ""))
    return entries


def extract_article(html: str, title: str, limit: int) -> Tuple[str, str]:
    """Returns (title, lead) taken from the article page."""
    meta = trafilatura.extract_metadata(html)
    if meta and meta.title:
        title = meta.title.strip()

    text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""
    paragraphs = [
        p.strip()
        for p in text.split("\n")
        if len(p.strip()) >= 40 and p.strip() != title and not (len(p) < 200 and BOILERPLATE.search(p))
    ]
    lead = ""
    for p in paragraphs:
        lead = f"{lead}\n\n{p}" if lead else p
        if len(lead) >= limit // 2:
            break
    return title, truncate(lead, limit)


class Collector:
    def __init__(self, config: Config, storage: Storage):
        self.config = config
        self.storage = storage
        self.translator = Translator(
            config.translator,
            target=config.translate_to,
            key=config.translator_key,
            region=config.translator_region,
            email=config.translator_email,
        )
        self.topic = TopicFilter(config.topic_keywords, config.topic_min_text_hits)

    async def run(self) -> int:
        self.semaphore = asyncio.Semaphore(5)
        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            results = await asyncio.gather(
                *(self._collect_source(client, s) for s in self.config.sources), return_exceptions=True
            )
        added = 0
        for source, result in zip(self.config.sources, results):
            if isinstance(result, Exception):
                log.warning("Source %s failed: %r", source.name, result)
            else:
                added += result
        log.info("Collected %d new items", added)
        return added

    async def _get(self, client: httpx.AsyncClient, url: str, attempts: int = 3) -> httpx.Response:
        for attempt in range(1, attempts + 1):
            try:
                async with self.semaphore:
                    resp = await client.get(url)
                if resp.status_code < 500 or attempt == attempts:
                    resp.raise_for_status()
                    return resp
            except httpx.TransportError:
                if attempt == attempts:
                    raise
            await asyncio.sleep(2 * attempt)
        raise RuntimeError("unreachable")

    async def _list_entries(self, client: httpx.AsyncClient, source: Source) -> Tuple[List[RawEntry], bool]:
        """Returns (entries, from_feed)."""
        resp = await self._get(client, source.url)

        if source.type in ("auto", "rss"):
            entries = parse_feed(resp.content)
            if entries or source.type == "rss":
                return entries, True

        if source.type == "auto" and not source.link_selector:
            feed_url = find_feed_url(resp.text, str(resp.url))
            if feed_url:
                log.info("%s: discovered feed %s", source.name, feed_url)
                source.url, source.type = feed_url, "rss"
                return parse_feed((await self._get(client, feed_url)).content), True

        if source.link_selector:
            return parse_listing(resp.text, str(resp.url), source.link_selector), False

        raise RuntimeError("no RSS found and link_selector is not set")

    async def _collect_source(self, client: httpx.AsyncClient, source: Source) -> int:
        entries, from_feed = await self._list_entries(client, source)
        border = datetime.now(timezone.utc) - timedelta(hours=self.config.max_age_hours)

        entries = [(normalize_url(url), *rest) for url, *rest in entries]
        known = await self.storage.existing(url for url, *_ in entries)
        # on the first visit only remember what is already published, otherwise the whole archive is delivered
        first_visit = not await self.storage.has_source(source.name)

        new_entries = []
        for url, title, published, summary in entries:
            if url in known:
                continue
            known.add(url)
            item = NewsItem(
                url=url, source=source.name, emoji=source.emoji, title=title or url, published_at=published
            )
            if first_visit or (published and published < border):
                await self.storage.add(item, skip=True)
                continue
            new_entries.append((item, summary))

        if first_visit:
            log.info("%s: first visit, remembered %d existing items", source.name, len(known))

        stored = await asyncio.gather(
            *(self._enrich_and_store(client, source, item, summary, from_feed) for item, summary in new_entries)
        )
        rejected = stored.count(False)
        if rejected:
            log.info("%s: %d items filtered out as off-topic", source.name, rejected)
        return stored.count(True)

    async def _enrich_and_store(
        self, client: httpx.AsyncClient, source: Source, item: NewsItem, summary: str, from_feed: bool
    ) -> bool:
        """Returns True if the item was stored for delivery, False if it was filtered out."""
        limit = self.config.lead_max_chars
        if not source.fetch_article:
            item.lead = truncate(summary, limit)
        else:
            try:
                resp = await self._get(client, item.url)
                title, lead = await asyncio.to_thread(extract_article, resp.text, item.title, limit)
                if not (from_feed and item.title):
                    item.title = title
                item.lead = lead or truncate(summary, limit)
            except Exception as e:
                log.debug("Article %s not fetched: %r", item.url, e)
                item.lead = truncate(summary, limit)
        if not item.title:
            return False
        if source.filter and self.topic.enabled and not self.topic.matches(item.title, item.lead):
            await self.storage.add(item, skip=True)
            return False
        item.title, item.lead = await self.translator.translate(client, [item.title, item.lead])
        await self.storage.add(item)
        return True
