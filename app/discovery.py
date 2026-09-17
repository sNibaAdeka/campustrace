"""Search ranked by local aliases, with ROR as a live fallback."""
from __future__ import annotations

import asyncio
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any
from .integrations import Sources, SourceError
from .pipeline import institution_summary

SEEDS = [
    {"ror_id":"052bx8q98","name":"Nazarbayev University","aliases":["Nazarbayev University","Назарбаев Университет","Назарбаев Университеті","NU","НУ","Назарбаев","naz university"],"city":"Astana","country":"Kazakhstan","official_website":"https://nu.edu.kz/"},
    {"ror_id":"05jnvbc31","name":"Astana IT University","aliases":["Astana IT University","AITU","АИТУ","Астана АйТи Университет","Астана ИТ Университет","астана ит"],"city":"Astana","country":"Kazakhstan","official_website":"https://astanait.edu.kz/"},
    {"ror_id":"00b30xv10","name":"University of Pennsylvania","aliases":["University of Pennsylvania","UPenn","Penn","Пенсильванский университет","Пеннсильванский университет"],"city":"Philadelphia","country":"United States","official_website":"https://www.upenn.edu/"},
]
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_lock = asyncio.Lock()
_TRANSLIT = str.maketrans({"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"i","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"})

def normalized(value: str) -> str:
    value = unicodedata.normalize('NFKD', value.casefold().strip())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    value = ' '.join(''.join(c if c.isalnum() else ' ' for c in value).split())
    return value.translate(_TRANSLIT)

def score(query: str, item: dict[str, Any]) -> float:
    q = normalized(query)
    aliases = item.get('aliases') or [item['name']]
    values = [normalized(str(a)) for a in aliases]
    best = 0.0
    for name in values:
        if q == name: best = max(best, 100)
        elif name.startswith(q): best = max(best, 75 + min(len(q) / max(len(name),1), 1) * 12)
        elif q in name: best = max(best, 58 + min(len(q) / max(len(name),1),1) * 10)
        else:
            ratio = SequenceMatcher(None, q, name).ratio()
            if ratio >= .76: best = max(best, ratio * 62)
    return best

async def suggest(query: str, page: int = 1) -> dict[str, Any]:
    q = normalized(query)
    if len(q) < 2: return {"query":query,"results":[],"ambiguous":False,"source":"local"}
    now = time.monotonic()
    local = [dict(item) for item in SEEDS]
    exact = [item for item in local if score(query, item) == 100]
    if exact and page == 1:
        return {"query":query,"results":[{**item,"match":"точное совпадение"} for item in exact],
                "ambiguous":len(exact)>1,"source":"локальные синонимы ROR","has_more":False}
    # ROR is not used for every keystroke: shared 10-minute query cache.
    live: list[dict[str, Any]] = []
    cache_key = f"{q}:{page}"
    stale = _cache.get(cache_key)
    total = 0
    warning = None
    if stale and now - stale[0] < 600:
        live, total = stale[1]
    elif len(q) >= 3:
        async with _lock:
            stale = _cache.get(cache_key)
            if stale and now - stale[0] < 600: live, total = stale[1]
            else:
                source = Sources()
                try:
                    data = await source.ror_search_page(query, page)
                    live = [institution_summary(x) for x in data.get('items', [])]
                    total = data.get('number_of_results', len(live))
                except SourceError as exc:
                    warning = f"ROR временно недоступен ({exc.detail}). Повторите поиск."
                finally: await source.close()
                if not warning: _cache[cache_key] = (time.monotonic(), (live, total))
    merged = {x['ror_id']: x for x in live}
    for item in local if page == 1 else []:
        if score(query, item) < 35: continue
        old = merged.get(item['ror_id'], {})
        item['aliases'] = list(dict.fromkeys(item['aliases'] + old.get('aliases', [])))
        merged[item['ror_id']] = {**old, **item}
    ranked = [(score(query, x), x) for x in merged.values()]
    # Keep ROR matches, including translated names and abbreviations. A local
    # substring threshold used to discard valid results returned by the registry.
    def rank(pair):
        relevance, item = pair
        primary = normalized(item['name'])
        priority = relevance + (25 if q in primary else 0)
        if 'university' in primary or 'университет' in item['name'].lower(): priority += 6
        if any(word in primary for word in ('center ', 'centre ', 'hospital ', 'institute of')): priority -= 12
        return (-priority, len(primary), primary)
    ranked.sort(key=rank)
    results = []
    for relevance, item in ranked:
        results.append({**item, "match": "точное совпадение" if relevance >= 100 else "похожее название" if relevance < 70 else "совпадение названия"})
    return {"query":query,"results":results,"ambiguous":len(results)>1,"source":"ROR + локальные синонимы", "page":page, "total":total, "has_more":page * 20 < min(total, 10000), "warning":warning}
