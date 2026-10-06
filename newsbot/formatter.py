from datetime import datetime
from html import escape

from .storage import NewsItem

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def format_item(item: NewsItem) -> str:
    parts = [f"{item.emoji} <b>{escape(item.title)}</b>"]
    if item.lead:
        parts.append(escape(item.lead))
    parts.append(f'<a href="{escape(item.url, quote=True)}">{escape(item.source)}</a>')
    return "\n\n".join(parts)


def format_header(now: datetime, total: int) -> str:
    return f"🗞 <b>Дайджест за {now.day} {MONTHS[now.month - 1]}</b>\n\nНовостей: {total}"
