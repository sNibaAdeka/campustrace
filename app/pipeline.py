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

from . import triage, vision
from .integrations import SourceError, Sources


VERSION = "0.8.0"
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
# P0.3: terms that describe an *event or an object*, not a place. Before this
# list existed, "campus" was the default bucket, so an award badge, a concert,
# a senate sitting and a graduation photo from the University of Tartu category
# were all published as views of the campus.
NON_SCENE_TERMS = (
    "ceremony", "ceremonial", "tseremoonia", "церемон", "рәсім",
    "concert", "kontsert", "концерт", "recital",
    "senate", "senat", "сенат", "council meeting", "nõukogu",
    "award", "auhin", "награжд", "вручен", "prize", "приз", "марапат",
    "anniversary", "juubel", "юбилей", "jubilee",
    "signing", "подписан", "memorandum", "меморандум",
    "meeting", "koosolek", "заседан", "совещан", "митинг",
    "press conference", "пресс-конференц", "interview", "интервью",
    "speech", "выступлен", "kõne", "seminar", "семинар", "workshop",
    "choir", "хор", "orchestra", "оркестр", "dance", "танц", "theatre", "театр",
    "exhibition", "выставк", "näitus", "protest", "rally",
    "funeral", "похорон", "grave", "могил", "monument to", "памятник",
    # People, not places: titles and roles in a file name mean a portrait.
    " prof ", " dr ", " mr ", " ms ", " mrs ", " pm ", "professor", "emeritus", "minister",
    "president of", "rector", "ректор", "профессор", "visit", "визит", "shaking hands",
)
SCENE_TERMS = (
    "campus", "building", "library", "библиотек", "библиоотек", "кітапхан",
    "dorm", "residence hall", "общежит", "жатақхан", "classroom", "lecture hall",
    "laborator", "gym", "stadium", "sport hall", "спортив", "стадион",
    "main entrance", "interior", "auditorium", "school of", "faculty", "lecture",
    "hoone", "maja", "chemicum",
    "physicum", "golden autumn", "aula", "корпус", "здани", "университеті",
)
FLICKR_LICENSES = {"4": "CC BY 2.0", "5": "CC BY-SA 2.0", "9": "CC0", "11": "CC BY 4.0", "12": "CC BY-SA 4.0"}
# Wikidata P31 labels of an item that belongs to the university → our taxonomy.
# Order matters: a "residence hall" is also a "building". Anything that does not
# match (a faculty, a satellite, a publishing house) is not a place and is skipped.
BUILDING_TYPE_CATEGORY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dormitory", ("residence hall", "dormitory", "student housing", "hall of residence", "student residence")),
    ("library", ("library",)),
    ("sports", ("stadium", "sports venue", "arena", "gymnasium", "sports hall", "swimming", "athletic", "sports facility")),
    ("classroom", ("lecture hall", "auditorium", "lecture theatre")),
    ("campus", ("university building", "academic building", "building", "campus", "courtyard", "quadrangle",
                "chapel", "observatory", "college of the university", "academic hall", "hall", "tower", "structure")),
)
# Open licences only. Anything else is dropped before it can be shown.
OPEN_LICENSE = re.compile(r"^(cc0|cc[ -]by(-sa)?[ -]?\d(\.\d)?.*|cc[ -]by(-sa)?|public domain|pd.*|attribution.*|gfdl.*|fal|free art license.*)$", re.I)
GEO_NEAR_METRES = 1500
# Wikipedia editions to read besides English, by country (the local article is
# usually where campus buildings, dormitories and libraries are illustrated).
WIKI_LANGS = {
    "KZ": ("ru", "kk"), "RU": ("ru",), "UA": ("uk",), "BY": ("ru", "be"), "KG": ("ru", "ky"), "UZ": ("uz", "ru"),
    "JP": ("ja",), "CN": ("zh",), "KR": ("ko",), "TW": ("zh",), "DE": ("de",), "AT": ("de",), "CH": ("de", "fr"),
    "FR": ("fr",), "BE": ("fr", "nl"), "NL": ("nl",), "IT": ("it",), "ES": ("es",), "PT": ("pt",), "BR": ("pt",),
    "AR": ("es",), "MX": ("es",), "CL": ("es",), "CO": ("es",), "PL": ("pl",), "CZ": ("cs",), "SE": ("sv",),
    "NO": ("no",), "DK": ("da",), "FI": ("fi",), "EE": ("et",), "LV": ("lv",), "LT": ("lt",), "TR": ("tr",),
    "IR": ("fa",), "IL": ("he",), "EG": ("ar",), "SA": ("ar",), "AE": ("ar",), "IN": ("hi",), "VN": ("vi",),
    "TH": ("th",), "ID": ("id",), "GR": ("el",), "HU": ("hu",), "RO": ("ro",), "BG": ("bg",), "RS": ("sr",),
}


def city_center_distance(institution: dict[str, Any]) -> dict[str, Any] | None:
    """Straight-line distance from the campus point (Wikidata P625) to the city
    centre point (GeoNames, via ROR). Both points and their origin are returned,
    because a straight line is not a commute and a P625 point is not a gate."""
    campus, centre = institution.get("campus_coordinates"), institution.get("city_coordinates")
    if not campus or not centre or centre.get("lat") is None:
        return None
    metres = distance_m(campus, centre)
    if metres > 80000:
        return None  # a regional campus or a wrong point; not a "distance to the centre"
    return {"km": round(metres / 1000, 1), "straight_line": True,
            "from": {"lat": campus["lat"], "lon": campus["lon"], "source": campus.get("source") or "Wikidata P625"},
            "to": {"lat": centre["lat"], "lon": centre["lon"], "source": "GeoNames (через ROR): центр города"}}


def building_category(types: list[str]) -> str | None:
    low = [t.casefold() for t in types]
    for category, terms in BUILDING_TYPE_CATEGORY:
        if any(term in t for t in low for term in terms):
            return category
    return None


def open_license(name: str) -> bool:
    return bool(OPEN_LICENSE.match((name or "").strip()))


def distance_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    return 6371000 * 2 * asin(min(1, sqrt(sin((lat1 - lat2) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon1 - lon2) / 2) ** 2)))


# Evidence kinds that come from *different* people or systems. The UI shows
# how many of them agree — a count of facts, never a made-up probability.
EVIDENCE_LABELS = {
    "wikidata_type": "Wikidata: объект вуза с типом",
    "wikidata_image": "Wikidata: изображение вуза (P18 и др.)",
    "wikipedia": "Иллюстрация статьи о вузе в Википедии",
    "depicts": "Commons: на фото отмечен объект",
    "category": "Категория Commons вуза",
    "name_in_text": "Название вуза в названии/описании файла",
    "geo_near": "Геотег рядом с точкой кампуса",
    "vision": "Визуальная проверка изображения",
}


def evidence_level(asset: dict[str, Any]) -> int:
    return len({e["kind"] for e in asset.get("evidence", []) if e.get("supports", True)})


def reliability(asset: dict[str, Any]) -> dict[str, Any]:
    """Case's "показатель достоверности": a labelled level, not a probability.

    The level is a deterministic function of how many *independent kinds* of
    evidence support the photo, and it is lowered — never raised — when the
    visual check disagrees with the text or the category is unknown. The basis
    is returned so the interface can show exactly why.
    """
    count = evidence_level(asset)
    disputed = any(e.get("supports") is False for e in asset.get("evidence", []))
    level = "high" if count >= 3 else "medium" if count == 2 else "low"
    if disputed or asset.get("category") in ("unknown", "city") or asset.get("status") == "unknown":
        level = {"high": "medium", "medium": "low"}.get(level, "low")
    return {"level": level, "supporting": count, "disputed": disputed}


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
        "types": [t for t in record.get("types", []) if isinstance(t, str)],
    }


def classify(text: str, city_only: bool = False) -> tuple[str, list[str], str]:
    """Return (category, tags, why).

    P0.3 fix: ``campus`` is no longer the fallback bucket. A candidate is only
    called a campus view when the text actually names a place, and an event word
    ("ceremony", "senate", "concert") vetoes the generic campus guess. Anything
    we cannot place becomes ``unknown`` — which the UI shows as "needs checking"
    rather than quietly presenting it as a photo of the university.
    """
    if city_only:
        return "city", [], "city_query"
    words = f" {norm(text)} "
    tags = [key for key, terms in TAG_TERMS.items() if any(term in words for term in terms)]
    for key in ("dormitory", "library", "classroom"):
        if any(term in words for term in CATEGORY_TERMS[key]):
            return key, tags, "specific_place_term"
    for key in ("laboratories", "sports", "student_life"):
        if key in tags:
            return key, tags, "activity_term"
    event_hit = any(term in words for term in NON_SCENE_TERMS)
    if any(term in words for term in CATEGORY_TERMS["campus"]) and not event_hit:
        return "campus", tags, "campus_term"
    return "unknown", tags, "event_word" if event_hit else "no_scene_term"


# Whole-word markers of documents and holdings rather than places: a library
# owns maps, manuscripts and scans, but a scan of a map is not the library.
DOCUMENT_WORDS = re.compile(
    r"\b(maps?|plan|plans|scan|scans|scanned|manuscripts?|folio|page|pages|book|books|letter|"
    r"engraving|lithograph|painting|drawing|illustration|diagram|chart|exlibris|ex libris|bookplate|wappen|logo|карта|план|рукопис|гравюр)\b", re.I)
# Subcategories that hold *collections* or *people*, not views of the campus.
SUBCATEGORY_EXCLUDED = ("map", "plan", "manuscript", "collection", "scan", "copy", "book", "document",
                        "people", "alumni", "faculty members", "history", "art", "painting", "portrait",
                        "coat of", "demolished", "events", "logo", "publication", "ukiyo")
PER_SUBCATEGORY_LIMIT = 10


def valid_title(title: str) -> bool:
    low = title.casefold()
    return (low.endswith(PHOTO_EXTENSIONS) and not any(word in low for word in EXCLUDED)
            and not DOCUMENT_WORDS.search(title.rsplit(".", 1)[0].replace("_", " ")))


_SUBCATEGORY_EXCLUDED_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in SUBCATEGORY_EXCLUDED) + r")s?\b", re.I)


def usable_subcategory(name: str) -> bool:
    # Whole words: "art" must not veto "Department of Chemistry".
    return not _SUBCATEGORY_EXCLUDED_RE.search(name)


def known_name_in_text(text: str, names: list[str]) -> bool:
    """Does the text contain one of the institution's known names?

    P1.4: the old rule required an alias of at least five characters, which
    silently disqualified every short official acronym — MIT, NYU, LSE, KTH,
    НУ. Those are matched as whole words instead of substrings, so "MIT" is
    found in "MIT Great Dome" but not inside "summit" or "Smith".
    """
    haystack = norm(text)
    padded = f" {haystack} "
    for name in names:
        candidate = norm(name)
        if not candidate:
            continue
        if len(candidate) >= 5:
            if candidate in haystack:
                return True
        elif len(candidate) >= 2 and f" {candidate} " in padded:
            return True
    return False


_SIBLING = re.compile(r"^\s+(of|for|at|hospital|press|medical|business|school|college)\b", re.I)


def names_institution_exactly(text: str, names: list[str]) -> bool:
    """Like known_name_in_text, but "Kyoto University of Art and Design" is not
    "Kyoto University": a name followed by "of/for/hospital/press…" is another
    organisation that merely starts with ours."""
    haystack = norm(text)
    padded = f" {haystack} "
    for name in names:
        candidate = norm(name)
        if len(candidate) < 3:
            continue
        for match in re.finditer(re.escape(candidate), padded):
            before_ok = padded[match.start() - 1] == " "
            after = padded[match.end():]
            if before_ok and (after[:1] == " ") and not _SIBLING.match(after):
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
    return host.endswith("wikimedia.org") or host.endswith(".staticflickr.com") or host == "api.openverse.org"


async def thumbnail_hashes(sources: Sources, assets: list[dict[str, Any]]) -> dict[str, int]:
    # Hash as many candidates as the time budget allows, not just the first
    # handful — dedup must run before the final selection, per the case's
    # "удаление одинаковых и визуально похожих" requirement.
    semaphore = asyncio.Semaphore(10)
    stats = {"hash_attempted": 0, "hash_succeeded": 0}

    async def one(asset: dict[str, Any]) -> None:
        url = asset.get("image_url") or ""
        # A 330 px rendition is plenty for a 9x8 hash and for the vision check,
        # and it downloads several times faster than the 960 px display thumb.
        url = url.replace("/960px-", "/330px-")
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
                asset["_thumb"] = response.content  # reused by the vision check, never serialised
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


def commons_asset(page: dict[str, Any], institution: dict[str, Any], scope: str,
                  extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    extra = extra or {}
    building = extra.get("building")
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
    name_match = names_institution_exactly(content, institution["aliases"])
    title_name_match = names_institution_exactly(title, institution["aliases"])
    title_scene = any(term in norm(title) for term in SCENE_TERMS) or (
        scope == "category" and len(title) <= 58 and
        any(term in norm(title) for term in ("university", "universit", "университет", "ülikool"))
    )
    building_names = institution.get("building_names") or []
    building_match = bool(building_names) and known_name_in_text(title, building_names)
    structured = bool(building) or scope in ("depicts", "wikidata_image", "wikipedia")
    if not city_only and scope == "search" and not title_name_match:
        return None
    # A geotag near the campus says nothing about *which* building; it only
    # counts when the file also names the university or one of its buildings.
    if scope == "geo" and not (name_match or building_match):
        return None
    broad_subcategory = scope.startswith("category_sub:") and "images from" in scope.casefold()
    if not city_only and not structured and (scope == "category" or broad_subcategory) and not title_scene:
        return None
    if city_only and not known_name_in_text(title, [institution.get("city") or ""]):
        return None
    if city_only and any(term in title.casefold() for term in ("district", "team", "cycling", "map", "administrative", "район", "карта", "équipe", "man shows", "person")):
        return None
    category, tags, text_evidence = classify(title, city_only)
    if building and building.get("category"):
        category, text_evidence = building["category"], "wikidata_type"
    if scope.startswith("category_sub:") and category in ("campus", "unknown"):
        subcategory = scope.split(":", 1)[1]
        if "library" in subcategory.casefold():
            category = "library"
        elif any(term in subcategory.casefold() for term in ("dorm", "residence", "общежит")):
            category = "dormitory"
        elif any(term in subcategory.casefold() for term in ("sport", "stadium")):
            category = "sports"
        elif "laborator" in subcategory.casefold():
            category = "laboratories"
        if category != "unknown":
            text_evidence = "subcategory_provenance"
    reasons = []
    if scope.startswith("category"):
        reasons.append("Файл находится в тематической категории Wikimedia Commons")
    if scope.startswith("category_sub:"):
        reasons.append(f"Подкатегория: {scope.split(':', 1)[1]}")
    if name_match:
        reasons.append("Название или описание файла содержит название университета")
    if city_only:
        reasons.append("Файл относится к городу; связь с кампусом не утверждается")
    if not license_name or not open_license(license_name):
        return None
    reasons.append(f"Лицензия указана: {license_name}")
    if building:
        reasons.append(f"Wikidata: «{building.get('label') or building['qid']}» — {', '.join(building.get('types') or [])}")
    if scope == "depicts":
        reasons.append("Структурированные данные Commons: на снимке отмечен объект университета (depicts)")
    if category == "unknown":
        reasons.append(
            "Сцена не распознана по тексту: в названии есть слово о событии, а не о месте"
            if text_evidence == "event_word" else
            "Сцена не распознана по тексту: название не содержит указания на тип объекта"
        )
    # A Commons category and caption can have the same contributor; do not call this verified.
    status = "city_context" if city_only else "unknown" if category == "unknown" else "probable"
    raw_id = title.encode("utf-8")
    coordinates = next((c for c in page.get("coordinates", [])
                        if isinstance(c.get("lat"), (int, float)) and isinstance(c.get("lon"), (int, float))
                        and -90 <= c["lat"] <= 90 and -180 <= c["lon"] <= 180), None)
    reference = institution.get('campus_coordinates') or institution.get('city_coordinates')
    distance = None
    if coordinates and reference:
        distance = distance_m(coordinates, reference)
        if distance > 40000: return None
    evidence: list[dict[str, Any]] = []
    if building:
        evidence.append({"kind": "wikidata_type", "detail": f"{building.get('label') or building['qid']}: {', '.join(building.get('types') or [])}",
                         "url": f"https://www.wikidata.org/wiki/{building['qid']}"})
    if scope == "depicts" or extra.get("depicts"):
        evidence.append({"kind": "depicts", "detail": "отмечено в структурированных данных файла", "url": info["descriptionurl"]})
    if scope == "wikipedia" or extra.get("wikipedia"):
        lang = extra.get("wikipedia") or "?"
        evidence.append({"kind": "wikipedia", "detail": f"иллюстрация статьи о вузе ({lang}.wikipedia)",
                         "url": f"https://{lang}.wikipedia.org/wiki/Special:Search?search={quote(institution['name'])}"})
        reasons.append(f"Изображение выбрано редакторами статьи о вузе в Википедии ({lang})")
    if scope == "wikidata_image":
        evidence.append({"kind": "wikidata_image", "detail": "указано в элементе вуза",
                         "url": f"https://www.wikidata.org/wiki/{institution.get('wikidata_id')}"})
        reasons.append("Wikidata: файл указан как изображение самого университета")
    if scope.startswith("category") and not building:
        evidence.append({"kind": "category", "detail": scope.split(":", 1)[1] if ":" in scope else "основная категория"})
    if name_match or building_match:
        evidence.append({"kind": "name_in_text", "detail": "название здания вуза" if building_match and not name_match else "название вуза"})
    if distance is not None and distance <= GEO_NEAR_METRES and not city_only and reference is institution.get("campus_coordinates"):
        basis = {"institution_point": "Wikidata P625", "headquarters_point": "Wikidata P159",
                 "buildings_point": "здания вуза в Wikidata"}.get(reference.get("precision"), "Wikidata")
        evidence.append({"kind": "geo_near", "detail": f"{int(round(distance, -1))} м от точки кампуса ({basis})"})
    return {
        "id": "commons-" + hashlib.sha256(raw_id).hexdigest()[:20],
        "provider": "Wikimedia Commons", "title": title.removeprefix("File:"),
        "category": category, "tags": tags, "status": status,
        "source_url": info["descriptionurl"], "image_url": info["thumburl"],
        "author": author or "Не указан", "license": license_name, "description": description[:200],
        "license_url": clean(meta.get("LicenseUrl")),
        "published_at": info.get("timestamp"),
        "captured_at": clean(meta.get("DateTimeOriginal")) or None,
        "sha1": info.get("sha1"), "dhash": None, "reasons": reasons,
        "scope": scope, "text_evidence": text_evidence, "evidence": evidence,
        "distance_m": None if distance is None else int(distance),
        "coordinates": {"lat": coordinates["lat"], "lon": coordinates["lon"],
                        "type": coordinates.get("type", "unknown")} if coordinates else None,
    }


def flickr_asset(item: dict[str, Any], institution: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title") or ""
    license_name = FLICKR_LICENSES.get(str(item.get("license")))
    if not license_name or not item.get("url_m") or not known_name_in_text(title, institution["aliases"]):
        return None
    category, tags, text_evidence = classify(title)
    return {
        "text_evidence": text_evidence,
        "id": "flickr-" + str(item["id"]), "provider": "Flickr", "title": title,
        "category": category, "tags": tags,
        "status": "unknown" if category == "unknown" else "probable",
        "source_url": f"https://www.flickr.com/photos/{quote(str(item['owner']))}/{item['id']}/",
        "image_url": item["url_m"], "author": item.get("ownername") or item["owner"],
        "license": license_name, "license_url": None,
        "published_at": item.get("dateupload"), "captured_at": item.get("datetaken"),
        "sha1": None, "dhash": None,
        "reasons": ["Название файла содержит название университета", f"Лицензия Flickr: {license_name}", "Принадлежность конкретному объекту пока не подтверждена"],
        "scope": "flickr_search",
    }


OPENVERSE_LICENSES = {"by": "CC BY", "by-sa": "CC BY-SA", "cc0": "CC0", "pdm": "Public Domain Mark"}


def openverse_asset(item: dict[str, Any], institution: dict[str, Any]) -> dict[str, Any] | None:
    """One Openverse record -> asset, or None. Pure, unit tested.

    The record must name the university (or one of its typed buildings) in its
    title or tags: a search hit alone is not evidence. Its only evidence is that
    text match, so its reliability stays low until the visual check agrees.
    """
    license_code = str(item.get("license") or "").lower()
    if license_code not in OPENVERSE_LICENSES:
        return None
    title = clean(item.get("title"))
    landing, thumb = item.get("foreign_landing_url"), item.get("thumbnail")
    if not title or not landing or not thumb or urlparse(str(landing)).scheme not in ("http", "https"):
        return None
    if any(word in title.casefold() for word in EXCLUDED) or DOCUMENT_WORDS.search(title):
        return None
    tags = " ".join(clean(t.get("name")) for t in (item.get("tags") or []) if isinstance(t, dict))
    # Two-letter acronyms (NU, KU) collide with everything; they never identify.
    aliases = [a for a in institution["aliases"] if len(norm(a)) >= 3]
    names = aliases + (institution.get("building_names") or [])
    if not (names_institution_exactly(title, names) or names_institution_exactly(f"{title} {tags}", aliases)):
        return None
    # The category comes from the title only: a tag like "library" on a photo of
    # a tightrope walker must not file it under libraries.
    category, tags_found, text_evidence = classify(title)
    version = clean(item.get("license_version"))
    license_name = f"{OPENVERSE_LICENSES[license_code]}{' ' + version if version and license_code in ('by', 'by-sa') else ''}"
    source = str(item.get("source") or item.get("provider") or "openverse").capitalize()
    return {
        "id": "openverse-" + hashlib.sha256(str(item.get("id") or landing).encode()).hexdigest()[:20],
        "provider": f"Openverse / {source}", "title": title, "category": category, "tags": tags_found,
        "status": "unknown" if category == "unknown" else "probable",
        "source_url": str(landing), "image_url": str(thumb),
        "author": clean(item.get("creator")) or "Не указан", "license": license_name,
        "license_url": clean(item.get("license_url")) or None,
        "published_at": None, "captured_at": None, "sha1": None, "dhash": None,
        "reasons": [f"Открытая лицензия: {license_name} (Openverse)", "Название вуза есть в названии или тегах снимка",
                    "Принадлежность конкретному объекту не подтверждена — только текст автора"],
        "scope": "openverse", "text_evidence": text_evidence,
        "evidence": [{"kind": "name_in_text", "detail": "название вуза в названии/тегах, указанных автором"}],
        "distance_m": None, "coordinates": None,
    }


CATEGORY_LABELS_RU = {
    "campus": "кампус и корпуса", "dormitory": "общежития", "classroom": "аудитории",
    "library": "библиотеки", "sports": "спорт", "laboratories": "лаборатории",
    "student_life": "студенческая жизнь", "city": "город",
}


def _year(value: Any) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def describe_campus(institution: dict[str, Any], assets: list[dict[str, Any]],
                    counts: dict[str, int], category_status: dict[str, str]) -> dict[str, Any]:
    """Case requirement 7: a short description built only from what we found.

    Every sentence is derived from data already on the page — the objects that
    have at least one licensed photograph, the years those photographs were
    taken, and the gaps. Nothing here is generated prose about how good the
    university is, because we have no source for that and the case forbids
    dressing up a guess as a finding.
    """
    name = institution["name"]
    place = ", ".join(x for x in (institution.get("city"), institution.get("country")) if x)
    sentences: list[str] = []
    facts: list[dict[str, Any]] = []

    opening = f"{name} — университет в {place}." if place else f"{name}."
    if institution.get("official_website"):
        opening += f" Официальный сайт: {institution['official_website']}."
    sentences.append(opening)

    published = [a for a in assets if a["category"] not in ("city", "unknown")]
    present = [(key, counts[key]) for key in CATEGORY_LABELS_RU if key != "city" and counts.get(key)]
    if present:
        listed = ", ".join(f"{CATEGORY_LABELS_RU[key]} — {count}" for key, count in present)
        sentences.append(
            f"Подтверждено лицензией и источником {len(published)} снимков по разделам: {listed}."
        )
        for key, count in present:
            examples = [a for a in assets if a["category"] == key][:3]
            facts.append({
                "category": key, "label": CATEGORY_LABELS_RU[key], "count": count,
                "examples": [{"title": a["title"], "source_url": a["source_url"],
                              "license": a.get("license"), "author": a.get("author")}
                             for a in examples],
            })
    else:
        sentences.append(
            "Ни одного снимка кампуса с подтверждённой открытой лицензией найти не удалось, "
            "поэтому описание объектов не строится."
        )

    years = sorted(y for y in (_year(a.get("captured_at") or a.get("published_at")) for a in published) if y)
    if len(years) >= 2 and years[0] != years[-1]:
        sentences.append(f"Даты съёмки или загрузки материалов охватывают {years[0]}–{years[-1]} годы.")
    elif years:
        sentences.append(f"Все найденные материалы относятся к {years[0]} году.")

    geotagged = [a for a in published if a.get("coordinates")]
    if geotagged:
        sentences.append(
            f"У {len(geotagged)} кадров есть собственные геотеги Commons; остальные привязаны к вузу "
            "только по источнику, а не по координате."
        )

    failed = [CATEGORY_LABELS_RU[key] for key, state in category_status.items()
              if state == "source_failed" and key in CATEGORY_LABELS_RU]
    empty = [CATEGORY_LABELS_RU[key] for key, state in category_status.items()
             if state == "empty_confirmed" and key in CATEGORY_LABELS_RU]
    if empty:
        sentences.append(
            "Проверено и не найдено открытых материалов: " + ", ".join(empty) + "."
        )
    if failed:
        sentences.append(
            "Не проверено из-за недоступности источника: " + ", ".join(failed) +
            " — это не значит, что таких материалов нет."
        )
    return {"text": " ".join(sentences), "facts": facts}


async def build_profile(
    sources: Sources, record: dict[str, Any], *,
    started: float | None = None, deadline: float | None = None,
) -> dict[str, Any]:
    # P1.3: the clock can be started by the caller *before* the ROR lookup, so
    # elapsed_ms measures the user's wait and not just the part after identity
    # resolution. ``deadline`` is an absolute monotonic instant: every expensive
    # optional stage checks it instead of assuming it has the full budget.
    started = time.monotonic() if started is None else started
    stage_times: dict[str, int] = {}

    def stage(name: str, since: float) -> None:
        stage_times[name] = int((time.monotonic() - since) * 1000)

    def budget_left() -> float:
        return 1e9 if deadline is None else deadline - time.monotonic()

    discovery_started = time.monotonic()
    institution = institution_summary(record)
    warnings: list[str] = []
    # P0.4: track which candidate-discovery sources failed/were cut short so
    # an empty category can be reported honestly as "not confirmed empty"
    # rather than looking identical to "genuinely searched, nothing found".
    incomplete_sources: list[str] = []
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
            if 'campus_coordinates' not in institution:
                # Many universities carry the point only as a qualifier of their
                # headquarters location (P159 -> P625), not as their own P625.
                for claim in claims.get('P159', []):
                    for q in claim.get('qualifiers', {}).get('P625', []):
                        value = q.get('datavalue', {}).get('value')
                        if isinstance(value, dict) and 'latitude' in value and 'campus_coordinates' not in institution:
                            institution['campus_coordinates'] = {'lat': value['latitude'], 'lon': value['longitude'],
                                'source': f"https://www.wikidata.org/wiki/{institution['wikidata_id']}#P159", 'precision': 'headquarters_point'}
            institution['subreddit'] = next((v for v in values('P3984') if isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_]{2,21}', v)), None)
            # Official accounts recorded in Wikidata. They are shown only through
            # each platform's own embed widget, never copied as campus photos.
            social_props = {"instagram": ("P2003", r"[A-Za-z0-9_.]{1,30}"), "tiktok": ("P7085", r"[A-Za-z0-9_.]{2,24}"),
                            "facebook": ("P2013", r"[A-Za-z0-9_.\-]{2,60}"), "vk": ("P3185", r"[A-Za-z0-9_.]{2,60}"),
                            "telegram": ("P3789", r"[A-Za-z0-9_]{4,32}"), "x": ("P2002", r"[A-Za-z0-9_]{1,15}"),
                            "linkedin": ("P4264", r"[A-Za-z0-9_\-.]{2,120}")}
            institution['social'] = {name: v for name, (prop, pattern) in social_props.items()
                                     for v in [next((x for x in values(prop) if isinstance(x, str) and re.fullmatch(pattern, x)), None)] if v}
            institution['youtube_channel'] = next((v for v in values('P2397') if isinstance(v,str) and re.fullmatch(r'UC[\w-]{22}',v)), None)
            for prop in ('P18', 'P8517', 'P3451', 'P5775'):
                for value in values(prop)[:3]:
                    if isinstance(value,str): candidates['File:'+value] = 'wikidata_image'
        except SourceError as exc:
            warnings.append(f"Wikidata: {exc.detail}"); incomplete_sources.append("wikidata")

    # Illustrations of the university's own Wikipedia articles, in English and
    # in the country's languages: an editor chose them for this article, and the
    # local-language article is where dormitories and libraries usually appear.
    wikipedia_lang: dict[str, str] = {}

    async def wikipedia_images() -> list[tuple[str, str]]:
        wanted = ["en"] + [x for x in WIKI_LANGS.get(institution.get("country_code") or "", ()) if x != "en"]
        links = details.get("sitelinks", {}) if isinstance(details, dict) else {}
        jobs = [(lang, links[lang + "wiki"]["title"]) for lang in wanted[:3] if links.get(lang + "wiki", {}).get("title")]

        async def one(lang: str, article: str) -> list[tuple[str, str]]:
            try:
                data = await sources.json("wikidata", f"https://{lang}.wikipedia.org/w/api.php", params={
                    "action": "query", "prop": "images", "titles": article, "imlimit": "40", "format": "json"})
            except SourceError:
                return []
            # Local editions name the file namespace in their own language
            # ("Файл:", "Datei:", "ファイル:"); Commons only knows "File:".
            return [("File:" + image["title"].split(":", 1)[1], lang)
                    for page in data.get("query", {}).get("pages", {}).values() for image in page.get("images", [])
                    if image.get("ns") == 6 and ":" in image.get("title", "")]
        batches = await asyncio.gather(*(one(lang, article) for lang, article in jobs))
        return [pair for batch in batches for pair in batch]
    wikipedia_task = asyncio.ensure_future(wikipedia_images()) if details else None

    async def wikipedia_about() -> dict[str, Any] | None:
        # A short, attributed description from the encyclopedia (CC BY-SA),
        # Russian first because the interface is Russian.
        links = details.get("sitelinks", {}) if isinstance(details, dict) else {}
        for lang in ("ru", "en", *WIKI_LANGS.get(institution.get("country_code") or "", ())):
            title = links.get(lang + "wiki", {}).get("title")
            if not title:
                continue
            try:
                data = await sources.json("wikidata", f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}")
            except SourceError:
                continue
            text = clean(data.get("extract"))
            if text:
                return {"text": text[:900], "lang": lang, "title": title,
                        "url": (data.get("content_urls", {}).get("desktop", {}) or {}).get("page") or f"https://{lang}.wikipedia.org/wiki/{quote(title)}",
                        "license": "CC BY-SA 4.0"}
        return None
    about_task = asyncio.ensure_future(wikipedia_about()) if details else None

    # Structured sources first. SPARQL runs on a different host than the
    # rate-limited Commons API, so it overlaps with the Commons chain below.
    buildings_task = (asyncio.ensure_future(sources.wikidata_buildings(institution["wikidata_id"]))
                      if institution["wikidata_id"] else None)
    building_by_file: dict[str, dict[str, Any]] = {}
    depicts_titles: set[str] = set()
    # Openverse is another host with its own limits: it overlaps with the serial
    # Commons chain instead of adding to it.
    # A three-letter-plus acronym ("MIT") is how Flickr users tag; the full name
    # is the fallback when there is none.
    short = next((a for a in institution["aliases"] if 3 <= len(a) <= 6 and a.isupper() and " " not in a), None)
    ov_name = short or institution["name"]

    async def openverse_all() -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        # The bare name finds the most; the two extra words target the
        # categories that stay empty on Commons for most universities.
        for query, size in ((ov_name, 30), (f"{ov_name} dormitory", 15), (f"{ov_name} library", 15)):
            found.extend(await sources.openverse_images(query, limit=size))
        return found
    openverse_task = asyncio.ensure_future(openverse_all()) if os.getenv("OPENVERSE", "1") != "0" else None

    if not category_name:
        category_name = institution['name']
    if category_name:
        try:
            # Two pages of the main category whenever the budget allows: the
            # first page alone is alphabetical and routinely stops before the
            # dormitory/library files (P1.5).
            # One page (up to 500 members): Commons requests are serial by
            # Wikimedia etiquette, so request count is what the user waits for.
            members = await sources.commons_category(category_name, limit=500, pages=2 if budget_left() > 20.5 else 1)
            for item in members:
                if item.get("ns") == 6:
                    candidates[item["title"]] = "category"
            subcats = [x["title"].removeprefix("Category:") for x in members
                       if x.get("ns") == 14 and any(t in x["title"].casefold() for t in SUBCATEGORY_TERMS)
                       and usable_subcategory(x["title"])]
            for subcat in subcats[:4]:
                if budget_left() < 14:
                    incomplete_sources.append("commons_subcategories")
                    warnings.append("Часть подкатегорий Commons пропущена: не хватило времени в бюджете")
                    break
                try:
                    members_of_subcat = await sources.commons_category(subcat, limit=200)
                    for item in members_of_subcat:
                        if item.get("ns") == 6:
                            candidates.setdefault(item["title"], f"category_sub:{subcat}")
                    if any(t in subcat.casefold() for t in ('library','dorm','building','college')):
                        nested = [x["title"].removeprefix("Category:") for x in members_of_subcat
                                  if x.get("ns") == 14 and usable_subcategory(x["title"])]
                        for child in nested[:1]:
                            if budget_left() < 15:
                                break
                            for item in await sources.commons_category(child, limit=60):
                                if item.get("ns") == 6:
                                    candidates.setdefault(item["title"], f"category_sub:{child}")
                except SourceError as exc:
                    warnings.append(f"Commons {subcat}: {exc.detail}"); incomplete_sources.append(f"commons_subcat:{subcat}")
        except SourceError as exc:
            warnings.append(f"Commons category: {exc.detail}"); incomplete_sources.append("commons_category")

    buildings: list[dict[str, Any]] = []
    if buildings_task is not None:
        try:
            buildings = await asyncio.wait_for(buildings_task, timeout=max(0.5, min(8.0, budget_left() - 8)))
        except (SourceError, TimeoutError) as exc:
            buildings_task.cancel()
            warnings.append(f"Wikidata SPARQL: {getattr(exc, 'detail', 'таймаут')}"); incomplete_sources.append("wikidata_buildings")
    typed_buildings = []
    for row in buildings:
        category = building_category(row["types"])
        if not category:
            continue  # a faculty, a lab-as-organisation, a satellite: not a place
        row = {**row, "category": category}
        typed_buildings.append(row)
        building_by_file.setdefault(row["file"], row)
        candidates[row["file"]] = "wikidata_building"
    institution["building_names"] = [b["label"] for b in typed_buildings if len(b["label"]) >= 5][:40]
    if not institution.get("campus_coordinates"):
        # No point for the university itself: use its main building, else the
        # median of its buildings, and say so in the source field.
        points = [b for b in typed_buildings if b.get("coord")]
        main = next((b for b in points if re.search(r"main|главн|peahoone|hauptgebäude", b["label"], re.I)), None)
        if main or points:
            if main:
                point, how = main["coord"], f"главное здание: {main['label']}"
            else:
                lats, lons = sorted(b["coord"]["lat"] for b in points), sorted(b["coord"]["lon"] for b in points)
                point, how = {"lat": lats[len(lats) // 2], "lon": lons[len(lons) // 2]}, f"медиана {len(points)} зданий вуза"
            institution["campus_coordinates"] = {**point, "source": f"Wikidata SPARQL ({how})", "precision": "buildings_point"}
    institution["buildings"] = list({b["qid"]: {"qid": b["qid"], "label": b["label"], "category": b["category"]}
                                     for b in typed_buildings}.values())[:40]

    # Buildings that Wikidata types as a dormitory / library / sports venue /
    # lecture hall usually have their own Commons category with dozens of views.
    # These are exactly the categories that stay empty otherwise.
    building_categories: list[tuple[str, dict[str, Any]]] = []
    seen_cat: set[str] = set()
    for wanted in ("dormitory", "library", "classroom", "sports"):
        for row in typed_buildings:
            if row["category"] == wanted and row.get("commons_category") and row["commons_category"] not in seen_cat:
                building_categories.append((row["commons_category"], row)); seen_cat.add(row["commons_category"]); break
    for cat, row in building_categories[:3]:
        if budget_left() < 13:
            incomplete_sources.append("commons_building_categories"); break
        try:
            for member in await sources.commons_category(cat, limit=40):
                if member.get("ns") == 6 and member["title"] not in candidates:
                    candidates[member["title"]] = "wikidata_building"
                    building_by_file.setdefault(member["title"], row)
        except SourceError as exc:
            warnings.append(f"Commons {cat}: {exc.detail}"); incomplete_sources.append("commons_building_categories"); break

    depicts_qids = [institution["wikidata_id"]] + [b["qid"] for b in typed_buildings] if institution["wikidata_id"] else []
    if depicts_qids and budget_left() > 8:
        try:
            for hit in await sources.commons_depicts(depicts_qids, limit=40):
                depicts_titles.add(hit["title"])
                candidates.setdefault(hit["title"], "depicts")
        except SourceError as exc:
            warnings.append(f"Commons depicts: {exc.detail}"); incomplete_sources.append("commons_depicts")

    campus_point = institution.get("campus_coordinates")
    if campus_point and budget_left() > 8:
        try:
            for hit in await sources.commons_geosearch(campus_point["lat"], campus_point["lon"], radius_m=800, limit=60):
                candidates.setdefault(hit["title"], "geo")
        except SourceError as exc:
            warnings.append(f"Commons geosearch: {exc.detail}"); incomplete_sources.append("commons_geosearch")

    # One category is rarely enough. Search several visual intents while retaining
    # the same conservative title/license filter below.
    if len(candidates) < 90:
        # Three focused searches preserve useful variety while keeping below
        # Wikimedia's public API burst limits for a fresh university profile.
        visual_queries = [
            f'"{institution["name"]}"',
            f'"{institution["name"]}" (library OR dormitory OR interior)',
            f'"{institution["name"]}" (campus OR library OR students)',
        ][:1 if len(candidates) > 20 else 3]
        # Local-language names ("Tartu Ülikool", "京都大学") find files that
        # English queries never reach; one OR-query keeps it to one request.
        local_names = [a for a in institution.get("aliases", [])
                       if a != institution["name"] and len(a) >= 4 and not a.isupper() and '"' not in a][:3]
        if local_names:
            visual_queries.append(" OR ".join(f'"{a}"' for a in local_names))
        for query in visual_queries:
            if budget_left() < 6:
                incomplete_sources.append("commons_search")
                warnings.append("Часть поисковых запросов Commons пропущена: не хватило времени в бюджете 30 секунд")
                break
            try:
                hits = await sources.commons_search(query, limit=30)
                for hit in hits:
                    candidates.setdefault(hit["title"], "search")
            except SourceError as exc:
                warnings.append(f"Commons search: {exc.detail}"); incomplete_sources.append("commons_search")

    # Use the institution's linked encyclopedia article when Commons search has
    # sparse coverage. Only files that also have Commons licence metadata survive.
    if wikipedia_task is not None:
        try:
            for title, language in await asyncio.wait_for(wikipedia_task, timeout=max(0.5, min(4.0, budget_left() - 10))):
                if title not in candidates:
                    candidates[title] = "wikipedia"
                    wikipedia_lang.setdefault(title, language)
        except TimeoutError:
            wikipedia_task.cancel(); incomplete_sources.append("wikipedia")

    # City search is explicitly a different claim from a campus photograph.
    if institution.get("city") and budget_left() > 9:
        try:
            hits = await sources.commons_search(f'{institution["city"]} skyline', limit=12)
            for hit in hits:
                candidates.setdefault(hit["title"], "city")
        except SourceError as exc:
            warnings.append(f"Commons city: {exc.detail}"); incomplete_sources.append("commons_city")

    groups = {key: [(title, scope) for title, scope in candidates.items()
                    if (scope.startswith("category_sub:") if key == "category_sub" else scope == key)
                    and valid_title(title)]
              for key in ("wikidata_image", "wikidata_building", "depicts", "wikipedia", "category", "category_sub", "geo", "search", "city")}
    per_sub: dict[str, int] = defaultdict(int)
    capped = []
    for title, scope in groups["category_sub"]:
        per_sub[scope] += 1
        if per_sub[scope] <= PER_SUBCATEGORY_LIMIT:
            capped.append((title, scope))
    groups["category_sub"] = capped
    selected = (groups["wikidata_image"][:8] + groups["wikidata_building"][:24] + groups["wikipedia"][:16] + groups["depicts"][:30] + groups["category_sub"][:24] +
                groups["category"][:40] + groups["geo"][:24] + groups["search"][:24] + groups["city"][:8])[:150]
    pages: list[dict[str, Any]] = []
    for start in range(0, len(selected), 50):
        if start and budget_left() < 7:
            incomplete_sources.append("commons_metadata")
            warnings.append("Метаданные части кандидатов не запрошены: не хватило времени в бюджете")
            break
        try:
            pages.extend(await sources.commons_imageinfo([t for t, _ in selected[start:start+50]]))
        except SourceError as exc:
            warnings.append(f"Commons metadata: {exc.detail}"); incomplete_sources.append("commons_metadata")
            break
    scope_by_title = dict(selected)
    assets = []
    rejected_license = 0
    for page in pages:
        page_title = page.get("title", "")
        extra = {"building": building_by_file.get(page_title), "depicts": page_title in depicts_titles,
                 "wikipedia": wikipedia_lang.get(page_title)}
        asset = commons_asset(page, institution, scope_by_title.get(page_title, "search"), extra)
        if asset:
            assets.append(asset)
        elif page.get("imageinfo") and not open_license(clean(((page["imageinfo"][0].get("extmetadata") or {}).get("LicenseShortName")))):
            rejected_license += 1
    first_asset_ms = int((time.monotonic() - started) * 1000) if assets else None

    openverse_candidates = 0
    if openverse_task is not None:
        try:
            items = await asyncio.wait_for(openverse_task, timeout=max(0.5, min(6.0, budget_left() - 6)))
            openverse_candidates = len(items)
            seen_landing: set[str] = set()
            for item in items:
                if item.get("foreign_landing_url") in seen_landing:
                    continue
                seen_landing.add(item.get("foreign_landing_url"))
                if asset := openverse_asset(item, institution):
                    asset["reliability"] = None
                    assets.append(asset)
        except (SourceError, TimeoutError) as exc:
            openverse_task.cancel()
            detail = getattr(exc, 'detail', 'таймаут')
            if detail in ("HTTP 401", "HTTP 403"):
                # Openverse refuses anonymous datacenter traffic; this is a
                # configuration limit (no client id), not a transient outage, so
                # it must not label every profile "partial".
                warnings.append("Openverse не подключён на этом хосте (нужен бесплатный OPENVERSE_CLIENT_ID/SECRET): фото оттуда не искались")
            else:
                warnings.append(f"Openverse: {detail}"); incomplete_sources.append("openverse")

    flickr_candidates = 0
    if os.getenv("FLICKR_API_KEY"):
        try:
            flickr_items = await sources.flickr_search(institution["name"])
            flickr_candidates = len(flickr_items)
            for item in flickr_items:
                if asset := flickr_asset(item, institution):
                    assets.append(asset)
        except SourceError as exc:
            warnings.append(f"Flickr: {exc.detail}"); incomplete_sources.append("flickr")

    stage("discovery", discovery_started)

    # Prefer campus content, clearer names, and a spread of categories.
    assets.sort(key=lambda a: (
        a["category"] == "city", a["category"] == "unknown",
        -evidence_level(a),
        a["scope"] == "search", a["scope"] == "flickr_search",
        not known_name_in_text(a["title"], institution["aliases"]),
    ))
    hash_started = time.monotonic()
    try:
        hash_stats = await asyncio.wait_for(thumbnail_hashes(sources, assets), timeout=max(1.0, min(8.0, budget_left() - 4)))
    except TimeoutError:
        # Hashing is cut, not the profile: unhashed candidates keep a reason
        # saying their visual duplicate check did not run.
        hash_stats = {"hash_attempted": sum(1 for a in assets[:40] if a.get("image_url")),
                      "hash_succeeded": sum(1 for a in assets if a.get("dhash"))}
        warnings.append("Проверка визуальных дублей прервана по бюджету времени")
    stage("visual_hash", hash_started)
    license_eligible_count = len(assets)
    assets, duplicate_count = deduplicate(assets)
    unique_count = len(assets)
    warnings.append(
        f"Визуальная проверка дублей: {hash_stats['hash_succeeded']}/{hash_stats['hash_attempted']} "
        "кандидатов хешировано (perceptual hash)."
    )
    # Multilingual reading of the words that accompany each photo (one batched
    # Groq call). It fills "unknown" categories and demotes captions that are
    # about another organisation or a person; it adds no independent evidence.
    triage_started = time.monotonic()
    triage_stats = await triage.annotate(
        institution, assets, deadline=None if deadline is None else min(deadline - 3, time.monotonic() + 8))
    stage("ai_text_triage", triage_started)
    if triage_stats["available"] and triage_stats["checked"]:
        warnings.append(
            f"ИИ-разбор подписей ({triage_stats['model']}): прочитано {triage_stats['checked']}, "
            f"уточнена категория у {triage_stats['categorised']}, понижено {triage_stats['demoted']}.")
    # Gallery breadth is a feature, provided the source and licence remain visible.
    assets = ([a for a in assets if a["category"] not in ("city", "unknown")][:90] +
              [a for a in assets if a["category"] == "city"][:10] +
              [a for a in assets if a["category"] == "unknown"][:14])

    # Second, independent opinion on what the picture actually shows (P0.3).
    # It runs after licence filtering and dedup so no quota is spent on images
    # we could not publish anyway, and it never runs past the time budget.
    vision_started = time.monotonic()
    vision_stats = await vision.annotate(
        assets, deadline=None if deadline is None else min(deadline, time.monotonic() + max(0.0, budget_left() - 2)))
    stage("visual_classifier", vision_started)
    rejected_by_vision = [a for a in assets if a.get("drop")]
    assets = [a for a in assets if not a.pop("drop", False)]
    for asset in assets:
        asset.pop("_thumb", None)
        verdict = asset.get("vision") or {}
        if verdict.get("available"):
            agreement = str(verdict.get("agreement", ""))
            asset.setdefault("evidence", []).append({
                "kind": "vision", "supports": agreement.startswith(("confirmed", "vision_only")) and "low_confidence" not in agreement,
                "detail": f"{verdict.get('scene_label')} ({verdict.get('model')})"})
        asset["evidence_level"] = evidence_level(asset)
        asset["reliability"] = reliability(asset)
    # Within a category, the best-corroborated photographs come first.
    assets.sort(key=lambda a: (a["category"] == "city", a["category"] == "unknown", -a["evidence_level"],
                               -(_year(a.get("captured_at") or a.get("published_at")) or 0)))
    if vision_stats["available"]:
        warnings.append(
            f"Независимая визуальная классификация ({vision_stats['model']}): проверено "
            f"{vision_stats['checked']} из {len(assets) + len(rejected_by_vision)}, снято с публикации {vision_stats['rejected']}, "
            f"расхождений с текстом {vision_stats['conflict']}."
            + (" Лимит запросов провайдера исчерпан — остальные кадры не проверены в этот раз." if vision_stats.get("rate_limited") else "")
        )
    else:
        warnings.append(
            "Независимая визуальная классификация не выполнена: не задан ключ vision-модели (GROK_API_KEY или GROQ_API_KEY). "
            "Категории основаны только на тексте Commons."
        )

    counts = {category: 0 for category in ("campus", "dormitory", "classroom", "library", "city", "sports", "laboratories", "student_life")}
    unclassified_count = 0
    for asset in assets:
        if asset["category"] in counts:
            counts[asset["category"]] += 1
        else:
            unclassified_count += 1
    profile_status = "partial" if incomplete_sources else "complete"
    # A zero-count category is only "confirmed empty" if nothing that feeds it
    # failed mid-run; otherwise we honestly say we couldn't finish checking.
    category_status = {
        category: ("has_results" if count > 0 else ("source_failed" if incomplete_sources else "empty_confirmed"))
        for category, count in counts.items()
    }
    institution["city_center_distance"] = city_center_distance(institution)
    if about_task is not None:
        try:
            institution["about"] = await asyncio.wait_for(about_task, timeout=max(0.2, min(2.0, budget_left() - 1)))
        except (TimeoutError, SourceError):
            about_task.cancel()
    inception = next((c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time") for c in
                      (details.get("claims", {}) if isinstance(details, dict) else {}).get("P571", []) if c.get("rank") != "deprecated"), None)
    if isinstance(inception, str) and re.match(r"[+-]\d{4}", inception):
        institution["founded"] = {"year": int(inception[1:5]), "source": f"https://www.wikidata.org/wiki/{institution['wikidata_id']}#P571"}
    description = describe_campus(institution, assets, counts, category_status)
    stage_times["total"] = int((time.monotonic() - started) * 1000)

    return {
        "pipeline_version": VERSION, "generated_at": int(time.time()),
        "elapsed_ms": stage_times["total"],
        "timings": stage_times,
        "time_to_first_asset_ms": first_asset_ms,
        "institution": institution, "summary": description["text"],
        "campus_facts": description["facts"], "assets": assets,
        "coverage": counts, "unclassified_count": unclassified_count,
        "candidate_count": len(candidates) + flickr_candidates + openverse_candidates,
        "license_eligible_count": license_eligible_count,
        "unique_count": unique_count,
        "duplicate_count": duplicate_count, "warnings": warnings,
        "rejected_license_count": rejected_license,
        "evidence_labels": EVIDENCE_LABELS,
        "profile_status": profile_status,
        "category_status": category_status,
        "incomplete_sources": incomplete_sources,
        "vision": vision_stats, "ai_text": triage_stats,
        "rejected_by_vision": [{"title": a["title"], "source_url": a["source_url"],
                                "scene": (a.get("vision") or {}).get("scene_label")}
                               for a in rejected_by_vision],
        "source_events": sources.events.copy(),
        "methodology": (
            "Автоматический поиск в Wikimedia Commons и подключённых источниках; карточки без "
            "подтверждённой лицензии не публикуются. Категория проверяется двумя независимыми "
            "слоями — текстом источника и визуальным классификатором; при расхождении уверенность "
            "понижается. Статус «вероятно» не означает доказанное местоположение."
        ),
    }


async def build_preview(sources: Sources, record: dict[str, Any], *, started: float | None = None) -> dict[str, Any]:
    """First photographs within a few seconds, from structured sources only.

    Wikidata image properties of the university and of its typed buildings,
    one Commons metadata request, the same licence and title filters as the
    full build. No hashing and no vision here — the full profile replaces this.
    """
    started = time.monotonic() if started is None else started
    institution = institution_summary(record)
    qid = institution["wikidata_id"]
    if not qid:
        return {"institution": institution, "assets": [], "elapsed_ms": int((time.monotonic() - started) * 1000),
                "note": "У организации нет Wikidata ID — быстрый предпросмотр невозможен"}
    details_task = asyncio.ensure_future(sources.wikidata_details(qid))
    buildings_task = asyncio.ensure_future(sources.wikidata_buildings(qid))
    titles: dict[str, dict[str, Any] | None] = {}
    try:
        details = await details_task
        claims = details.get("claims", {})
        for prop in ("P18", "P8517", "P3451", "P5775"):
            for claim in claims.get(prop, [])[:3]:
                value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
                if isinstance(value, str) and claim.get("rank") != "deprecated":
                    titles.setdefault("File:" + value, None)
    except SourceError:
        pass
    try:
        # The preview must be fast; a slow SPARQL endpoint only costs us the
        # building photos here — the full build waits for it longer.
        for row in await asyncio.wait_for(buildings_task, timeout=1.5):
            category = building_category(row["types"])
            if category:
                titles.setdefault(row["file"], {**row, "category": category})
    except (SourceError, TimeoutError):
        buildings_task.cancel()
    selected = [t for t in titles if valid_title(t)][:50]
    assets: list[dict[str, Any]] = []
    if selected:
        try:
            for page in await sources.commons_imageinfo(selected):
                building = titles.get(page.get("title", ""))
                scope = "wikidata_building" if building else "wikidata_image"
                if asset := commons_asset(page, institution, scope, {"building": building}):
                    asset["evidence_level"] = evidence_level(asset)
                    asset["reliability"] = reliability(asset)
                    assets.append(asset)
        except SourceError:
            pass
    assets.sort(key=lambda a: (a["category"] == "unknown", -a["evidence_level"]))
    return {"institution": institution, "assets": assets[:12],
            "elapsed_ms": int((time.monotonic() - started) * 1000), "preview": True}
