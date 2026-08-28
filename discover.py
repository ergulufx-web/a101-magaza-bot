"""
A101 endpoint keşfi.

Telefondan DevTools açmak zor olduğu için bu modül işi sunucuda yapar:
A101 sayfasını indirir, sayfadaki JS paketlerini tarar ve içinde geçen
API yollarını çıkarır. Sonucu botta /kesfet komutuyla görürsün.

Bulduğun adresi a101_client.py içindeki ENDPOINT CONFIG bloğuna yazarsın.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass

import httpx

from a101_client import BASE_URL, DEFAULT_HEADERS

log = logging.getLogger(__name__)

# Aday yolları ayıklamak için kalıp: "/api/..." ya da "/magazada-bul" gibi
PATH_PATTERN = re.compile(r"""["'`](/[a-zA-Z0-9\-_/{}.$]{4,120})["'`]""")
ABSOLUTE_PATTERN = re.compile(r"""["'`](https?://[a-zA-Z0-9\-._]+/[a-zA-Z0-9\-_/{}.$]{2,120})["'`]""")
SCRIPT_PATTERN = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)

# Mağaza/stok ile ilgili olma ihtimalini artıran kelimeler
STRONG_HINTS = ["magazadabul", "storestock", "storeavailability", "stockbystore",
                "magaza", "store", "stok", "stock", "inventory", "branch", "sube"]
WEAK_HINTS = ["api", "product", "urun", "city", "sehir", "il", "district", "ilce"]

NOISE = ["/static/", "/assets/img", ".png", ".jpg", ".svg", ".css", ".woff",
         "/_next/static/chunks/", "node_modules"]

MAX_SCRIPTS = 25
MAX_SCRIPT_BYTES = 4_000_000


@dataclass
class Candidate:
    path: str
    score: int
    seen_in: str = ""

    def __str__(self) -> str:
        return f"[{self.score:>2}] {self.path}"


def score_path(path: str) -> int:
    """Yolun mağaza-stok endpoint'i olma ihtimalini kabaca puanlar."""
    low = path.lower()
    if any(n in low for n in NOISE):
        return 0
    compact = re.sub(r"[^a-z0-9]", "", low)
    score = 0
    for hint in STRONG_HINTS:
        if hint in compact:
            score += 5
    for hint in WEAK_HINTS:
        if hint in compact:
            score += 1
    if score == 0:
        return 0
    if "/api" in low or "/v1" in low or "/v2" in low:
        score += 3
    if "{" in path or "$" in path:  # şablon değişkeni içeriyor
        score += 2
    return score


def extract_paths(text: str) -> Counter:
    found: Counter = Counter()
    for match in PATH_PATTERN.findall(text):
        found[match] += 1
    for match in ABSOLUTE_PATTERN.findall(text):
        found[match] += 1
    return found


async def discover(product_slug_url: str | None = None, timeout: float = 25.0) -> list[Candidate]:
    """A101 sayfalarını tarayıp aday API yollarını puanlı şekilde döndürür."""
    pages = [BASE_URL + "/"]
    if product_slug_url:
        pages.append(product_slug_url if product_slug_url.startswith("http")
                     else BASE_URL + "/" + product_slug_url.lstrip("/"))

    totals: Counter = Counter()
    origin: dict[str, str] = {}

    async with httpx.AsyncClient(
        headers={**DEFAULT_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"},
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        script_urls: list[str] = []

        for page in pages:
            try:
                response = await client.get(page)
            except httpx.HTTPError as exc:
                log.warning("Sayfa alınamadı %s: %s", page, exc)
                continue
            html_text = response.text
            for path, count in extract_paths(html_text).items():
                totals[path] += count
                origin.setdefault(path, "sayfa HTML")
            for src in SCRIPT_PATTERN.findall(html_text):
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = BASE_URL + src
                elif not src.startswith("http"):
                    continue
                if src not in script_urls:
                    script_urls.append(src)

        async def fetch_script(url: str) -> None:
            try:
                response = await client.get(url)
            except httpx.HTTPError:
                return
            if response.status_code >= 400:
                return
            body = response.text[:MAX_SCRIPT_BYTES]
            name = url.rsplit("/", 1)[-1][:40]
            for path, count in extract_paths(body).items():
                totals[path] += count
                origin.setdefault(path, name)

        semaphore = asyncio.Semaphore(5)

        async def guarded(url: str) -> None:
            async with semaphore:
                await fetch_script(url)

        await asyncio.gather(*(guarded(u) for u in script_urls[:MAX_SCRIPTS]))

    candidates = []
    for path in totals:
        score = score_path(path)
        if score >= 5:
            candidates.append(Candidate(path=path, score=score, seen_in=origin.get(path, "")))

    candidates.sort(key=lambda c: (-c.score, c.path))
    return candidates[:30]


async def probe(paths: list[str], timeout: float = 15.0) -> list[tuple[str, str]]:
    """Aday yolları GET ile yoklar, dönen içerik tipini raporlar."""
    results: list[tuple[str, str]] = []
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True
    ) as client:
        for path in paths:
            if "{" in path or "$" in path:
                results.append((path, "şablon — değişken gerekiyor, elle dene"))
                continue
            try:
                response = await client.get(path)
            except httpx.HTTPError as exc:
                results.append((path, f"hata: {type(exc).__name__}"))
                continue
            kind = response.headers.get("content-type", "?").split(";")[0]
            note = f"HTTP {response.status_code} · {kind}"
            if "json" in kind:
                note += " ✅ JSON"
            results.append((path, note))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main() -> None:
        found = await discover()
        if not found:
            print("Aday bulunamadı — site JS'i korumalı olabilir.")
            return
        for candidate in found:
            print(candidate, "  ←", candidate.seen_in)
        print("\nYoklanıyor...\n")
        for path, note in await probe([c.path for c in found[:10]]):
            print(f"{note:<28} {path}")

    asyncio.run(main())
