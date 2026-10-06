import asyncio
import html
import logging
import re
from typing import List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

CYRILLIC = re.compile(r"[а-яё]", re.I)
LATIN = re.compile(r"[a-z]", re.I)
MYMEMORY_MAX = 450


def is_target_language(text: str, lang: str) -> bool:
    if lang != "ru":
        return False
    cyr, lat = len(CYRILLIC.findall(text)), len(LATIN.findall(text))
    return cyr > lat


def split_chunks(text: str, limit: int) -> List[Tuple[str, bool]]:
    """Splits text into (chunk, ends_paragraph) pieces <= limit, on paragraph and sentence boundaries."""
    chunks: List[Tuple[str, bool]] = []
    for paragraph in text.split("\n\n"):
        current = ""
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph.strip()):
            while len(sentence) > limit:
                cut = sentence.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                if current:
                    chunks.append((current, False))
                    current = ""
                chunks.append((sentence[:cut], False))
                sentence = sentence[cut:].lstrip()
            if current and len(current) + 1 + len(sentence) > limit:
                chunks.append((current, False))
                current = sentence
            else:
                current = f"{current} {sentence}" if current else sentence
        if current:
            chunks.append((current, True))
    return chunks


class Translator:
    """Machine translation over HTTP. provider: none | mymemory | azure | google | deepl."""

    def __init__(
        self,
        provider: str,
        target: str = "ru",
        key: Optional[str] = None,
        region: Optional[str] = None,
        email: Optional[str] = None,
    ):
        self.provider = (provider or "none").lower()
        self.target = target
        self.key = key
        self.region = region
        self.email = email
        if self.provider in ("azure", "google", "deepl") and not key:
            raise RuntimeError(f"TRANSLATOR={self.provider} requires TRANSLATOR_KEY")
        self.semaphore: Optional[asyncio.Semaphore] = None

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    async def translate(self, client: httpx.AsyncClient, texts: List[str]) -> List[str]:
        """Returns translated texts; on any failure returns the originals."""
        if not self.enabled:
            return texts
        todo = [i for i, t in enumerate(texts) if t.strip() and not is_target_language(t, self.target)]
        if not todo:
            return texts
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(3)
        try:
            async with self.semaphore:
                translated = await getattr(self, f"_{self.provider}")(client, [texts[i] for i in todo])
        except Exception as e:
            log.warning("Translation via %s failed: %r", self.provider, e)
            return texts
        result = list(texts)
        for i, t in zip(todo, translated):
            result[i] = html.unescape(t).strip() or texts[i]
        return result

    async def _mymemory(self, client: httpx.AsyncClient, texts: List[str]) -> List[str]:
        result = []
        for text in texts:
            out = ""
            for chunk, ends_paragraph in split_chunks(text, MYMEMORY_MAX):
                params = {"q": chunk, "langpair": f"en|{self.target}"}
                if self.email:
                    params["de"] = self.email
                resp = await client.get("https://api.mymemory.translated.net/get", params=params)
                resp.raise_for_status()
                data = resp.json()
                if str(data.get("responseStatus")) != "200" or data.get("quotaFinished"):
                    raise RuntimeError(f"MyMemory: {data.get('responseDetails') or data.get('responseStatus')}")
                out += data["responseData"]["translatedText"].strip() + ("\n\n" if ends_paragraph else " ")
            result.append(out.strip())
        return result

    async def _azure(self, client: httpx.AsyncClient, texts: List[str]) -> List[str]:
        headers = {"Ocp-Apim-Subscription-Key": self.key}
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        resp = await client.post(
            "https://api.cognitive.microsofttranslator.com/translate",
            params={"api-version": "3.0", "to": self.target},
            headers=headers,
            json=[{"Text": t} for t in texts],
        )
        resp.raise_for_status()
        return [item["translations"][0]["text"] for item in resp.json()]

    async def _google(self, client: httpx.AsyncClient, texts: List[str]) -> List[str]:
        resp = await client.post(
            "https://translation.googleapis.com/language/translate/v2",
            params={"key": self.key},
            json={"q": texts, "target": self.target, "format": "text"},
        )
        resp.raise_for_status()
        return [t["translatedText"] for t in resp.json()["data"]["translations"]]

    async def _deepl(self, client: httpx.AsyncClient, texts: List[str]) -> List[str]:
        host = "api-free.deepl.com" if self.key.endswith(":fx") else "api.deepl.com"
        resp = await client.post(
            f"https://{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {self.key}"},
            json={"text": texts, "target_lang": self.target.upper()},
        )
        resp.raise_for_status()
        return [t["text"] for t in resp.json()["translations"]]
