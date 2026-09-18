"""Recent news articles that name the university (GDELT DOC 2.0, no key).

GDELT asks for at most one request every five seconds per client, so calls
share one lock with a pause, and every answer is cached for six hours. Only
headlines that name the university are kept; images are not taken.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import db
from .pipeline import names_institution_exactly

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_lock = asyncio.Lock()
_last = 0.0


def keep(articles: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    """Headlines that name the university, de-duplicated. Pure; unit tested."""
    out, seen = [], set()
    for row in articles:
        url, title = str(row.get("url") or ""), str(row.get("title") or "").strip()
        if not url.startswith(("https://", "http://")) or not title:
            continue
        key = title.casefold()[:80]
        if key in seen or not names_institution_exactly(title, names):
            continue
        seen.add(key)
        date = str(row.get("seendate") or "")
        out.append({"title": title[:220], "url": url, "domain": row.get("domain") or urlparse(url).hostname,
                    "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) >= 8 else None,
                    "language": row.get("language")})
    return out[:10]


async def university_news(institution: dict[str, Any]) -> dict[str, Any]:
    global _last
    key = f"news:v1:{institution['ror_id']}"
    cached = db.get_cached(key)
    if cached is not None:
        return {**cached, "from_cache": True}
    names = [n for n in [institution["name"], *(institution.get("aliases") or [])] if len(n) >= 5][:4]
    query = "(" + " OR ".join(f'"{n}"' for n in names) + ")" if len(names) > 1 else f'"{names[0]}"'
    try:
        async with _lock:
            await asyncio.sleep(max(0.0, 5.2 - (time.monotonic() - _last)))
            _last = time.monotonic()
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "CampusTraceResearch/1.0"}) as client:
                response = await client.get(GDELT_URL, params={"query": query, "mode": "artlist", "format": "json",
                                                               "maxrecords": "40", "sort": "datedesc", "timespan": "12m"})
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return {"available": False, "articles": [], "reason": "Новостной индекс GDELT сейчас не ответил. Повторите позже."}
    articles = keep(data.get("articles", []) if isinstance(data, dict) else [], names)
    result = {"available": True, "articles": articles, "source": "GDELT DOC 2.0", "window": "12 месяцев"}
    db.set_cached(key, result, 6 * 3600)
    return result
