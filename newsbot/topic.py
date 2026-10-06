import re
from typing import List, Optional, Pattern


def _compile(keyword: str) -> Pattern:
    """`*` at the end matches any word ending; ALL-CAPS keywords (AI, ML, MCP) are case-sensitive."""
    wildcard = keyword.endswith("*")
    word = keyword.rstrip("*")
    body = re.escape(word).replace(r"\ ", r"[\s-]+") + (r"\w*" if wildcard else "")
    flags = 0 if word.isupper() else re.IGNORECASE
    return re.compile(rf"(?<!\w){body}(?!\w)", flags)


class TopicFilter:
    """Passes an item when its title mentions a keyword, or its text mentions keywords at least min_text_hits times."""

    def __init__(self, keywords: List[str], min_text_hits: int = 2):
        self.patterns = [_compile(k) for k in keywords]
        self.min_text_hits = min_text_hits

    @property
    def enabled(self) -> bool:
        return bool(self.patterns)

    def title_matches(self, title: str) -> bool:
        return any(p.search(title) for p in self.patterns)

    def matches(self, title: str, text: Optional[str] = None) -> bool:
        if self.title_matches(title):
            return True
        if not text:
            return False
        return sum(len(p.findall(text)) for p in self.patterns) >= self.min_text_hits
