import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import yaml
from dotenv import load_dotenv


@dataclass
class Source:
    name: str
    url: str
    # auto: try RSS, then look for an RSS link on the page, then fall back to link_selector
    type: str = "auto"
    emoji: str = "📰"
    link_selector: Optional[str] = None
    # false: take the text from the RSS description instead of downloading the article
    fetch_article: bool = True
    # true: only items matching topic_keywords are delivered
    filter: bool = False


@dataclass
class Config:
    bot_token: str
    chat_id: Optional[int]
    database_url: str
    timezone: str = "Europe/Moscow"
    # spread: collected once a day at collect_time, published evenly within publish_window
    # realtime: every item is sent send_delay_minutes after publication
    # digest: once a day at digest_time
    delivery: str = "spread"
    collect_time: str = "08:00"
    publish_window: str = "09:00-22:00"
    batch_size: str = "5-10"
    send_delay_minutes: int = 5
    digest_time: str = "09:00"
    collect_interval_minutes: int = 2
    max_age_hours: int = 24
    lead_max_chars: int = 600
    link_preview: bool = False
    translate_to: str = "ru"
    translator: str = "none"
    translator_key: Optional[str] = None
    translator_region: Optional[str] = None
    translator_email: Optional[str] = None
    topic_keywords: List[str] = field(default_factory=list)
    topic_min_text_hits: int = 2
    sources: List[Source] = field(default_factory=list)

    @property
    def digest_hm(self) -> Tuple[int, int]:
        return parse_hm(self.digest_time)

    @property
    def collect_hm(self) -> Tuple[int, int]:
        return parse_hm(self.collect_time)

    @property
    def batch_range(self) -> Tuple[int, int]:
        low, _, high = str(self.batch_size).partition("-")
        return int(low), int(high or low)

    @property
    def publish_window_hm(self) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        start, end = self.publish_window.split("-")
        return parse_hm(start), parse_hm(end)


def parse_hm(value: str) -> Tuple[int, int]:
    hour, minute = value.strip().split(":")
    return int(hour), int(minute)


def load_config(path: str = "config.yaml") -> Config:
    load_dotenv()
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set (see .env.example)")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    chat_id = os.getenv("CHAT_ID")

    sources = [Source(**s) for s in raw.pop("sources", None) or []]
    return Config(
        bot_token=token,
        chat_id=int(chat_id) if chat_id else None,
        database_url=database_url,
        translator=os.getenv("TRANSLATOR", "none"),
        translator_key=os.getenv("TRANSLATOR_KEY") or None,
        translator_region=os.getenv("TRANSLATOR_REGION") or None,
        translator_email=os.getenv("TRANSLATOR_EMAIL") or None,
        sources=sources,
        **raw,
    )
