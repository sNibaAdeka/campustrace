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
_locks: dict[str, asyncio.Lock] = {}
# Organisations that share a university's name but are not where students live
# and study. They stay in the list (the user may want them) but sink to the end.
NOT_A_CAMPUS = ("press", "hospital", "foundation", "health", "clinic", "medical center", "medical centre",
                "museum", "library system", "alumni", "bank", "city of", "school district", "издательств", "больниц")
_CYRILLIC = __import__("re").compile(r"[а-яёәғқңөұүһі]", __import__("re").I)
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
    exact = [item for item in local if score(query, item) == 100 and len(q) >= 3 or normalized(query) in [normalized(a) for a in item["aliases"]]]
    if exact and page == 1:
        return {"query":query,"results":[{**item,"match":"точное совпадение"} for item in exact],
                "ambiguous":len(exact)>1,"source":"локальные синонимы ROR","has_more":False}
    # ROR is not used for every keystroke: shared 10-minute query cache.
    live: list[dict[str, Any]] = []
    wikidata_order: list[str] = []
    cache_key = f"{q}:{page}"
    stale = _cache.get(cache_key)
    total = 0
    warning = None
    if stale and now - stale[0] < 600:
        live, total, wikidata_order = stale[1]
    elif len(q) >= 3:
        async with _locks.setdefault(cache_key, asyncio.Lock()):
            stale = _cache.get(cache_key)
            if stale and now - stale[0] < 600: live, total, wikidata_order = stale[1]
            else:
                source = Sources()
                try:
                    # ROR is the registry of record; Wikidata search is run in
                    # parallel only to *rank*: it knows that "MIT" usually means
                    # the one in Cambridge and that "МГУ" is Lomonosov MSU.
                    wiki_task = asyncio.ensure_future(source.wikidata_ror_candidates(query)) if page == 1 else None
                    try:
                        data = await source.ror_search_page(query, page)
                        live = [institution_summary(x) for x in data.get('items', [])]
                        total = data.get('number_of_results', len(live))
                    except SourceError as exc:
                        warning = f"ROR временно недоступен ({exc.detail}). Повторите поиск."
                    if wiki_task is not None:
                        try:
                            wikidata_order = (await asyncio.wait_for(wiki_task, timeout=4))[:3]
                        except (SourceError, TimeoutError):
                            wiki_task.cancel(); wikidata_order = []
                    # Cyrillic spelling of a Latin-registered name ("Сатпаев"):
                    # retry the registry with a transliteration, and ask Wikidata
                    # for the university rather than the person it is named after.
                    if page == 1 and not live and _CYRILLIC.search(query):
                        try:
                            data = await source.ror_search_page(normalized(query), 1)
                            live = [institution_summary(x) for x in data.get('items', [])]
                            total = data.get('number_of_results', len(live))
                            if live: warning = None
                        except SourceError:
                            pass
                        if not wikidata_order:
                            try:
                                wikidata_order = (await asyncio.wait_for(source.wikidata_ror_candidates(f"{query} университет"), timeout=4))[:3]
                            except (SourceError, TimeoutError):
                                pass
                    if page == 1 and not live and not wikidata_order:
                        try:
                            wikidata_order = (await asyncio.wait_for(source.wikidata_fulltext_ror(query), timeout=4))[:3]
                        except (SourceError, TimeoutError, AttributeError):
                            pass
                    known = {x['ror_id'] for x in live}
                    missing = [rid for rid in wikidata_order if rid not in known]
                    if missing:
                        fetched = await asyncio.gather(*(source.ror_get(rid) for rid in missing), return_exceptions=True)
                        # Wikidata may point at a city, a press or a hospital with
                        # a ROR ID; only education organisations are injected.
                        added = [institution_summary(r) for r in fetched if isinstance(r, dict) and r.get('id')
                                 and 'education' in (r.get('types') or [])]
                        wikidata_order = [rid for rid in wikidata_order if rid in known or rid in {x['ror_id'] for x in added}]
                        live = added + live
                        if warning and live: warning = None
                finally: await source.close()
                if not warning: _cache[cache_key] = (time.monotonic(), (live, total, wikidata_order))
    merged = {x['ror_id']: x for x in live}
    for item in local if page == 1 else []:
        if score(query, item) < 60: continue
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
        if item['ror_id'] in wikidata_order:
            priority += 1000 - 100 * wikidata_order.index(item["ror_id"])
        if 'university' in primary or 'университет' in item['name'].lower(): priority += 6
        if any(word in primary for word in ('center ', 'centre ', 'hospital ', 'institute of')): priority -= 12
        if any(word in item['name'].lower() for word in NOT_A_CAMPUS) and not any(word in q for word in NOT_A_CAMPUS):
            priority -= 60
        return (-priority, len(primary), primary)
    ranked.sort(key=rank)
    results = []
    for relevance, item in ranked:
        match = "точное совпадение" if relevance >= 100 else "похожее название" if relevance < 70 else "совпадение названия"
        if item['ror_id'] in wikidata_order[:1]: match = "лучшее совпадение (Wikidata)"
        results.append({**item, "match": match})
    return {"query":query,"results":results,"ambiguous":len(results)>1,"source":"ROR + Wikidata (ранжирование) + локальные синонимы", "page":page, "total":total, "has_more":page * 20 < min(total, 10000), "warning":warning}
