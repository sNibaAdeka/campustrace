"""Source-grounded visual profile builder.

Conservative by design: a Commons category is evidence of association, not
proof that a photo depicts a particular building. Search-only hits are weaker.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import os
import re
import time
from math import radians, sin, cos, asin, sqrt
from datetime import datetime, timezone
from collections import defaultdict
from typing import Any
from urllib.parse import quote, urlparse

from PIL import Image, UnidentifiedImageError

from .integrations import SourceError, Sources


VERSION = "0.5.1"
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
EXCLUDED = (
    "logo", "logotype", "emblem", "coat of arms", "seal", "flag", "badge", "icon",
    "map of", "poster", "brochure", "certificate", "diploma", "portrait", "coin",
    "логотип", "герб", "эмблем", "диплом", "плакат", "афиша", "портрет",
    "prime minister", "visitors' book", "delegation", "ambassador", "president visit",
    "minister visit", "award ceremony", "conference panel",
    "manuscript", "medal", "plaque", "stamp", "certificate", "fliers", "bottle of",
    "rules for", "reeglid", "graffiti", "exhumation", "trophy", "реклам", "почтовая марка",
    "presidential library", "exhibit", "oxford road", "alumni", "alumnus",
)
CATEGORY_TERMS = {
    "dormitory": ("dorm", "residence hall", "student house", "student housing", "hostel", "общежит", "жатақхан"),
    "library": ("library", "библиотек", "библиоотек", "кітапхан"),
    "classroom": ("classroom", "lecture", "auditorium", "auditorium", "class room", "аудитор", "лекцион"),
    "campus": ("campus", "building", "university", "университет", "университеті", "корпус", "главное здание"),
}
TAG_TERMS = {
    "sports": ("sport", "stadium", "gym", "athletic", "pool", "frisbee", "spordihoone", "спорт", "стадион"),
    "laboratories": ("laborator", "research lab", "nanofab", "лаборатор"),
    "student_life": ("student", "festival", "club", "graduation", "ras at", "resident assistant", "студент", "выпуск", "клуб"),
}
SUBCATEGORY_TERMS = ("building", "campus", "library", "dorm", "residence", "sport", "interior", "laborator", "college", "общежит", "здания")
SCENE_TERMS = (
    "campus", "building", "library", "библиотек", "библиоотек", "кітапхан",
    "dorm", "residence hall", "общежит", "жатақхан", "classroom", "lecture hall",
    "laborator", "gym", "stadium", "sport hall", "спортив", "стадион",
    "main entrance", "interior", "auditorium", "school of", "faculty", "lecture",
    "hoone", "maja", "chemicum",
    "physicum", "golden autumn", "aula", "корпус", "здани", "университеті",
)
FLICKR_LICENSES = {"4": "CC BY 2.0", "5": "CC BY-SA 2.0", "9": "CC0", "11": "CC BY 4.0", "12": "CC BY-SA 4.0"}


def latest_student_count(claims: list[dict[str, Any]], qid: str) -> dict[str, Any] | None:
    current_year = datetime.now(timezone.utc).year
    observations = []
    for claim in claims:
        if claim.get("rank") == "deprecated" or claim.get("qualifiers", {}).get("P518"):
            continue  # Partial count (e.g. undergraduate only) must not be labelled total.
        try:
            amount = float(claim["mainsnak"]["datavalue"]["value"]["amount"])
            point = claim["qualifiers"]["P585"][0]["datavalue"]["value"]["time"]
            year = int(point[1:5])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if not amount.is_integer() or not 0 < amount <= 10_000_000 or not 1900 <= year <= current_year:
            continue
        references = []
        for ref in claim.get("references", []):
            for snak in ref.get("snaks", {}).get("P854", []):
                url = snak.get("datavalue", {}).get("value")
                if isinstance(url, str) and urlparse(url).scheme in ("http", "https"):
                    references.append(url)
        observations.append({
            "count": int(amount), "year": year, "source": f"https://www.wikidata.org/wiki/{qid}#P2196",
            "reference_url": references[0] if references else None,
            "rank": claim.get("rank", "normal"),
            "warning": "Число студентов из Wikidata; проверьте год и первичный источник перед сравнением.",
        })
    if not observations:
        return None
    observations.sort(key=lambda x: (x["year"], x["rank"] == "preferred"), reverse=True)
    return observations[0]


def clean(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value", "")
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


def norm(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


def display_name(record: dict[str, Any]) -> str:
    names = record.get("names", [])
    for item in names:
        if "ror_display" in item.get("types", []):
            return item["value"]
    return names[0]["value"] if names else record.get("id", "Unknown institution")


def institution_summary(record: dict[str, Any]) -> dict[str, Any]:
    geo = (record.get("locations") or [{}])[0].get("geonames_details") or {}
    domains = record.get("domains") or []
    website = next((x.get("value") for x in record.get("links", []) if x.get("type") == "website"), None)
    return {
        "ror_id": record["id"].rsplit("/", 1)[-1],
        "name": display_name(record),
        "aliases": list(dict.fromkeys(x.get("value", "") for x in record.get("names", []) if x.get("value"))),
        "city": geo.get("name"), "country": geo.get("country_name"),
        "country_code": geo.get("country_code"),
        "city_coordinates": {"lat": geo.get("lat"), "lon": geo.get("lng")} if geo.get("lat") is not None else None,
        "official_domain": domains[0] if domains else None,
        "official_website": website,
        "wikidata_id": next((v for x in record.get("external_ids", []) if x.get("type") == "wikidata" for v in x.get("all", [])), None),
    }


def classify(text: str, city_only: bool = False) -> tuple[str, list[str]]:
    if city_only:
        return "city", []
    words = norm(text)
    category = "campus"
    for key in ("dormitory", "library", "classroom"):
        if any(term in words for term in CATEGORY_TERMS[key]):
            category = key
            break
    tags = [key for key, terms in TAG_TERMS.items() if any(term in words for term in terms)]
    if category == "campus" and tags:
        for key in ("laboratories", "sports", "student_life"):
            if key in tags:
                category = key
                break
    return category, tags


def valid_title(title: str) -> bool:
    low = title.casefold()
    return low.endswith(PHOTO_EXTENSIONS) and not any(word in low for word in EXCLUDED)


def known_name_in_text(text: str, names: list[str]) -> bool:
    haystack = norm(text)
    for name in names:
        candidate = norm(name)
        if len(candidate) >= 5 and candidate in haystack:
            return True
    return False


def dhash(image_bytes: bytes) -> str | None:
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            im.draft("L", (9, 8))
            im = im.convert("L").resize((9, 8))
            pixels = list(im.getdata())
            value = 0
            for y in range(8):
                for x in range(8):
                    value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
            return f"{value:016x}"
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _hashable_host(host: str) -> bool:
    # P0.1 fix: Commons thumbnails are served from several *.wikimedia.org
    # edges (observed: thumb.wikimedia.org, not just upload.wikimedia.org).
    # A too-narrow allowlist silently skipped hashing for ~100% of Commons
    # candidates, which meant visual near-duplicates were never detected.
    return host.endswith("wikimedia.org") or host.endswith(".staticflickr.com")


async def thumbnail_hashes(sources: Sources, assets: list[dict[str, Any]]) -> dict[str, int]:
    # Hash as many candidates as the time budget allows, not just the first
    # handful — dedup must run before the final selection, per the case's
    # "удаление одинаковых и визуально похожих" requirement.
    semaphore = asyncio.Semaphore(8)
    stats = {"hash_attempted": 0, "hash_succeeded": 0}

    async def one(asset: dict[str, Any]) -> None:
        url = asset.get("image_url") or ""
        host = urlparse(url).hostname or ""
        if not _hashable_host(host):
            return
        stats["hash_attempted"] += 1
        try:
            async with semaphore:
                response = await sources.client.get(url, timeout=3)
                response.raise_for_status()
            if len(response.content) <= 3_000_000:
                asset["dhash"] = dhash(response.content)
                if asset["dhash"]:
                    stats["hash_succeeded"] += 1
        except (Exception):  # Thumbnail failure must not discard source metadata.
            asset["reasons"].append("Миниатюра недоступна: визуальный дубль не проверен")

    await asyncio.gather(*(one(a) for a in assets[:40]))
    return stats


def deduplicate(assets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    seen_sha: set[str] = set()
    duplicates = 0
    for asset in assets:
        sha = asset.get("sha1")
        if sha and sha in seen_sha:
            duplicates += 1
            continue
        if asset.get("dhash") and any(
            other.get("dhash") and hamming(asset["dhash"], other["dhash"]) <= 5
            for other in kept
        ):
            duplicates += 1
            continue
        if sha:
            seen_sha.add(sha)
        kept.append(asset)
    return kept, duplicates


def commons_asset(page: dict[str, Any], institution: dict[str, Any], scope: str) -> dict[str, Any] | None:
    title = page.get("title", "")
    info = (page.get("imageinfo") or [{}])[0]
    if not valid_title(title) or not info.get("thumburl") or not info.get("descriptionurl"):
        return None
    meta = info.get("extmetadata") or {}
    license_name = clean(meta.get("LicenseShortName"))
    author = clean(meta.get("Artist")) or clean(meta.get("Credit"))
    description = clean(meta.get("ImageDescription"))
    content = f"{title} {description}"
    city_only = scope == "city"
    name_match = known_name_in_text(content, institution["aliases"])
    title_name_match = known_name_in_text(title, institution["aliases"])
    title_scene = any(term in norm(title) for term in SCENE_TERMS) or (
        scope == "category" and len(title) <= 58 and
        any(term in norm(title) for term in ("university", "universit", "университет", "ülikool"))
    )
    if not city_only and scope == "search" and not title_name_match:
        return None
    broad_subcategory = scope.startswith("category_sub:") and "images from" in scope.casefold()
    if not city_only and (scope == "category" or broad_subcategory) and not title_scene:
        return None
    if city_only and not known_name_in_text(title, [institution.get("city") or ""]):
        return None
    if city_only and any(term in title.casefold() for term in ("district", "team", "cycling", "map", "administrative", "район", "карта", "équipe", "man shows", "person")):
        return None
    category, tags = classify(title, city_only)
    if scope.startswith("category_sub:") and category == "campus":
        subcategory = scope.split(":", 1)[1]
        if "library" in subcategory.casefold():
            category = "library"
        elif any(term in subcategory.casefold() for term in ("dorm", "residence", "общежит")):
            category = "dormitory"
        elif any(term in subcategory.casefold() for term in ("sport", "stadium")):
            category = "sports"
        elif "laborator" in subcategory.casefold():
            category = "laboratories"
    reasons = []
    if scope.startswith("category"):
        reasons.append("Файл находится в тематической категории Wikimedia Commons")
    if scope.startswith("category_sub:"):
        reasons.append(f"Подкатегория: {scope.split(':', 1)[1]}")
    if name_match:
        reasons.append("Название или описание файла содержит название университета")
    if city_only:
        reasons.append("Файл относится к городу; связь с кампусом не утверждается")
    if not license_name:
        return None
    reasons.append(f"Лицензия указана: {license_name}")
    # A Commons category and caption can have the same contributor; do not call this verified.
    status = "city_context" if city_only else "probable"
    raw_id = title.encode("utf-8")
    coordinates = next((c for c in page.get("coordinates", [])
                        if isinstance(c.get("lat"), (int, float)) and isinstance(c.get("lon"), (int, float))
                        and -90 <= c["lat"] <= 90 and -180 <= c["lon"] <= 180), None)
    reference = institution.get('campus_coordinates') or institution.get('city_coordinates')
    if coordinates and reference:
        lat1,lon1,lat2,lon2 = map(radians,(coordinates['lat'],coordinates['lon'],reference['lat'],reference['lon']))
        distance = 6371 * 2 * asin(min(1,sqrt(sin((lat1-lat2)/2)**2+cos(lat1)*cos(lat2)*sin((lon1-lon2)/2)**2)))
        if distance > 40: return None
    return {
        "id": "commons-" + hashlib.sha256(raw_id).hexdigest()[:20],
        "provider": "Wikimedia Commons", "title": title.removeprefix("File:"),
        "category": category, "tags": tags, "status": status,
        "source_url": info["descriptionurl"], "image_url": info["thumburl"],
        "author": author or "Не указан", "license": license_name,
        "license_url": clean(meta.get("LicenseUrl")),
        "published_at": info.get("timestamp"),
        "captured_at": clean(meta.get("DateTimeOriginal")) or None,
        "sha1": info.get("sha1"), "dhash": None, "reasons": reasons,
        "scope": scope,
        "coordinates": {"lat": coordinates["lat"], "lon": coordinates["lon"],
                        "type": coordinates.get("type", "unknown")} if coordinates else None,
    }


def flickr_asset(item: dict[str, Any], institution: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title") or ""
    license_name = FLICKR_LICENSES.get(str(item.get("license")))
    if not license_name or not item.get("url_m") or not known_name_in_text(title, institution["aliases"]):
        return None
    category, tags = classify(title)
    return {
        "id": "flickr-" + str(item["id"]), "provider": "Flickr", "title": title,
        "category": category, "tags": tags, "status": "probable",
        "source_url": f"https://www.flickr.com/photos/{quote(str(item['owner']))}/{item['id']}/",
        "image_url": item["url_m"], "author": item.get("ownername") or item["owner"],
        "license": license_name, "license_url": None,
        "published_at": item.get("dateupload"), "captured_at": item.get("datetaken"),
        "sha1": None, "dhash": None,
        "reasons": ["Название файла содержит название университета", f"Лицензия Flickr: {license_name}", "Принадлежность конкретному объекту пока не подтверждена"],
        "scope": "flickr_search",
    }


async def build_profile(sources: Sources, record: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    institution = institution_summary(record)
    warnings: list[str] = []
    candidates: dict[str, str] = {}
    category_name = None
    details = {}
    if institution["wikidata_id"]:
        try:
            details = await sources.wikidata_details(institution["wikidata_id"])
            claims = details.get('claims', {})
            def values(prop):
                return [c.get('mainsnak',{}).get('datavalue',{}).get('value') for c in claims.get(prop,[]) if c.get('rank') != 'deprecated']
            category_name = next((v for v in values('P373') if isinstance(v,str)), None)
            if not category_name:
                title = details.get('sitelinks',{}).get('commonswiki',{}).get('title','')
                if title.startswith('Category:'): category_name = title.removeprefix('Category:')
            for value in values('P625'):
                if isinstance(value,dict) and 'latitude' in value:
                    institution['campus_coordinates'] = {'lat':value['latitude'], 'lon':value['longitude'], 'source':f"https://www.wikidata.org/wiki/{institution['wikidata_id']}#P625", 'precision':'institution_point'}
                    break
            institution['youtube_channel'] = next((v for v in values('P2397') if isinstance(v,str) and re.fullmatch(r'UC[\w-]{22}',v)), None)
            for value in values('P18')[:3]:
                if isinstance(value,str): candidates['File:'+value] = 'category'
        except SourceError as exc:
            warnings.append(f"Wikidata: {exc.detail}")

    if not category_name:
        category_name = institution['name']
    if category_name:
        try:
            members = await sources.commons_category(category_name, limit=500)
            for item in members:
                if item.get("ns") == 6:
                    candidates[item["title"]] = "category"
            subcats = [x["title"].removeprefix("Category:") for x in members
                       if x.get("ns") == 14 and any(t in x["title"].casefold() for t in SUBCATEGORY_TERMS)]
            for subcat in subcats[:4]:
                try:
                    members_of_subcat = await sources.commons_category(subcat, limit=200)
                    for item in members_of_subcat:
                        if item.get("ns") == 6:
                            candidates.setdefault(item["title"], f"category_sub:{subcat}")
                    if any(t in subcat.casefold() for t in ('library','dorm','building','college')):
                        nested = [x["title"].removeprefix("Category:") for x in members_of_subcat
                                  if x.get("ns") == 14 and not any(t in x['title'].casefold() for t in ('people','alumni','history','art','coat of','demolished'))]
                        for child in nested[:2]:
                            for item in await sources.commons_category(child, limit=60):
                                if item.get("ns") == 6:
                                    candidates.setdefault(item["title"], f"category_sub:{child}")
                except SourceError as exc:
                    warnings.append(f"Commons {subcat}: {exc.detail}")
        except SourceError as exc:
            warnings.append(f"Commons category: {exc.detail}")

    # One category is rarely enough. Search several visual intents while retaining
    # the same conservative title/license filter below.
    if len(candidates) < 90:
        # Three focused searches preserve useful variety while keeping below
        # Wikimedia's public API burst limits for a fresh university profile.
        visual_queries = [
            f'"{institution["name"]}"',
            f'"{institution["name"]}" (library OR dormitory OR interior)',
            f'"{institution["name"]}" (campus OR library OR students)',
        ]
        for query in visual_queries:
            try:
                hits = await sources.commons_search(query, limit=30)
                for hit in hits:
                    candidates.setdefault(hit["title"], "search")
            except SourceError as exc:
                warnings.append(f"Commons search: {exc.detail}")

    # Use the institution's linked encyclopedia article when Commons search has
    # sparse coverage. Only files that also have Commons licence metadata survive.
    if len(candidates) < 25:
        for language in ('en', 'ru'):
            title = details.get('sitelinks',{}).get(language+'wiki',{}).get('title')
            if not title: continue
            try:
                data = await sources.json('wikidata',f'https://{language}.wikipedia.org/w/api.php',params={'action':'query','prop':'images','titles':title,'imlimit':30,'format':'json'})
                for page in data.get('query',{}).get('pages',{}).values():
                    for image in page.get('images',[]):
                        candidates.setdefault(image['title'],'category_sub:'+institution['name'])
            except SourceError as exc:
                warnings.append(f'Wikipedia: {exc.detail}')
            break

    # City search is explicitly a different claim from a campus photograph.
    if institution.get("city"):
        try:
            hits = await sources.commons_search(f'{institution["city"]} skyline', limit=12)
            for hit in hits:
                candidates.setdefault(hit["title"], "city")
        except SourceError as exc:
            warnings.append(f"Commons city: {exc.detail}")

    groups = {key: [(title, scope) for title, scope in candidates.items()
                    if (scope.startswith("category_sub:") if key == "category_sub" else scope == key)
                    and valid_title(title)]
              for key in ("category", "category_sub", "search", "city")}
    selected = (groups["category_sub"][:28] + groups["category"][:32] +
                groups["search"][:32] + groups["city"][:8])
    pages: list[dict[str, Any]] = []
    for start in range(0, len(selected), 50):
        try:
            pages.extend(await sources.commons_imageinfo([t for t, _ in selected[start:start+50]]))
        except SourceError as exc:
            warnings.append(f"Commons metadata: {exc.detail}")
            break
    scope_by_title = dict(selected)
    assets = [a for p in pages if (a := commons_asset(p, institution, scope_by_title.get(p.get("title", ""), "search")))]

    flickr_candidates = 0
    if os.getenv("FLICKR_API_KEY"):
        try:
            flickr_items = await sources.flickr_search(institution["name"])
            flickr_candidates = len(flickr_items)
            for item in flickr_items:
                if asset := flickr_asset(item, institution):
                    assets.append(asset)
        except SourceError as exc:
            warnings.append(f"Flickr: {exc.detail}")

    # Prefer campus content, clearer names, and a spread of categories.
    assets.sort(key=lambda a: (
        a["category"] == "city", a["scope"] == "search", a["scope"] == "flickr_search",
        not known_name_in_text(a["title"], institution["aliases"]),
    ))
    hash_stats = await thumbnail_hashes(sources, assets)
    license_eligible_count = len(assets)
    assets, duplicate_count = deduplicate(assets)
    unique_count = len(assets)
    warnings.append(
        f"Визуальная проверка дублей: {hash_stats['hash_succeeded']}/{hash_stats['hash_attempted']} "
        "кандидатов хешировано (perceptual hash)."
    )
    # Gallery breadth is a feature, provided the source and licence remain visible.
    assets = [a for a in assets if a["category"] != "city"][:60] + [a for a in assets if a["category"] == "city"][:10]

    counts = {category: 0 for category in ("campus", "dormitory", "classroom", "library", "city", "sports", "laboratories", "student_life")}
    for asset in assets:
        counts[asset["category"]] += 1
    name = institution["name"]
    place = ", ".join(x for x in (institution.get("city"), institution.get("country")) if x)
    summary = f"{name} — университет в {place}. " if place else f"{name}. "
    summary += f"Найдено {len(assets)} материалов с указанными источниками и лицензиями. "
    summary += "Связь каждого кадра с конкретным корпусом требует отдельного подтверждения; статусы и основания указаны в карточках."

    return {
        "pipeline_version": VERSION, "generated_at": int(time.time()),
        "elapsed_ms": int((time.monotonic()-started)*1000),
        "institution": institution, "summary": summary, "assets": assets,
        "coverage": counts, "candidate_count": len(candidates) + flickr_candidates,
        "license_eligible_count": license_eligible_count,
        "unique_count": unique_count,
        "duplicate_count": duplicate_count, "warnings": warnings,
        "source_events": sources.events.copy(),
        "methodology": "Автоматический поиск в Wikimedia Commons и подключённых источниках; карточки без подтверждённой лицензии не публикуются. Статус 'вероятно' не означает доказанное местоположение.",
    }
