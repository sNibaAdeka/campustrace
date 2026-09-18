"""CampusTrace MVP API and single-page application."""

from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, triage, vision
from .atlas import build_atlas, campus_geocode_crosscheck, isochrone, enrich_osm
from .discovery import suggest, SEEDS
from .integrations import SourceError, Sources, official_youtube_channel
from .pipeline import VERSION as PIPELINE_VERSION, build_profile, build_preview, institution_summary, latest_student_count, norm
from .voices import student_voices


BASE = Path(__file__).resolve().parents[1]
ROR_ID = re.compile(r"^[0-9a-z]{9}$")
# P1.3: the case allows 30 seconds from submit to a useful result. The budget
# below covers the *whole* server side of that, including the ROR lookup that
# used to sit outside the measured window, and leaves headroom for transfer.
PROFILE_BUDGET_SECONDS = float(os.getenv("PROFILE_BUDGET_SECONDS", "24"))
# P1.9: two visitors asking for the same university must not run two identical
# pipelines against Wikimedia. The second one waits for the first result.
_inflight: dict[str, asyncio.Task] = {}


def load_local_env() -> None:
    """Load only missing local development settings. The file is gitignored."""
    env_file = BASE / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        if "=" not in raw_line or raw_line.lstrip().startswith("#"):
            continue
        key, value = raw_line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_local_env()
    # The adapters read the contact User-Agent at import time, before .env
    # was loaded; refresh it so Wikimedia and Nominatim see the real contact.
    from . import integrations
    integrations.USER_AGENT = os.getenv("CAMPUS_TRACE_USER_AGENT", integrations.USER_AGENT)
    db.initialize()
    yield


app = FastAPI(title="CampusTrace MVP", version=PIPELINE_VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


def valid_ror(ror_id: str) -> str:
    if not ROR_ID.fullmatch(ror_id):
        raise HTTPException(400, "Invalid ROR ID")
    return ror_id


@app.get("/")
async def index() -> FileResponse:
    # The page names versioned assets (?v=N); it must itself never be cached,
    # or a visitor keeps yesterday's scripts after a deploy.
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "database": "sqlite", "version": app.version}


@app.get("/api/integrations")
async def integrations() -> dict[str, Any]:
    return {
        "required": ["ROR", "Wikidata", "Wikimedia Commons", "SQLite"],
        "keyless_enrichment": ["OpenAlex", "Open-Meteo"],
        "optional_configured": {
            "Nominatim": bool(os.getenv("CAMPUS_TRACE_USER_AGENT")) and "your-email@example.com" not in os.getenv("CAMPUS_TRACE_USER_AGENT", ""),
            "Flickr": bool(os.getenv("FLICKR_API_KEY")),
            "Brave Search": bool(os.getenv("BRAVE_API_KEY")),
            "YouTube Data API": bool(os.getenv("YOUTUBE_API_KEY")),
            "openrouteservice": bool(os.getenv("OPENROUTESERVICE_API_KEY")),
            "Groq research": bool(os.getenv("GROQ_API_KEY")),
            "Grok vision (xAI)": bool(os.getenv("GROK_API_KEY")),
            "Vision check (active provider)": vision.provider() and f"{vision.provider()}:{vision.model_name()}",
            "AI caption triage (text)": triage.configured() and triage.model_name(),
            "Student voices search": "tavily" if os.getenv("TAVILY_API_KEY") else ("groq browser_search" if os.getenv("GROQ_API_KEY") else "reddit archive only"),
            "Openverse client": bool(os.getenv("OPENVERSE_CLIENT_ID")),
            "Mapbox": bool(os.getenv("MAPBOX_TOKEN")),
        },
        "basemap": "MapLibre GL + OpenFreeMap (no key required); Mapbox is an optional cross-check only.",
        "note": "Source discovery does not grant permission to republish a photo.",
    }


@app.get("/api/institutions")
async def search_institutions(q: str = Query(min_length=2, max_length=120)) -> dict[str, Any]:
    return await suggest(q)


@app.get("/api/search/suggest")
async def search_suggestions(q: str = Query(min_length=2, max_length=120), page: int = Query(default=1, ge=1, le=500)) -> dict[str, Any]:
    return await suggest(q, page)


@app.get("/api/atlas/{ror_id}")
async def atlas(ror_id: str) -> dict[str, Any]:
    ror_id = valid_ror(ror_id)
    cached = db.get_profile(ror_id)
    if not cached:
        seed = next((item for item in SEEDS if item['ror_id']==ror_id), None)
        if seed:
            return build_atlas(seed, [])
        sources = Sources()
        try:
            record = await sources.ror_get(ror_id)
            return build_atlas(institution_summary(record), [])
        except SourceError as exc:
            raise HTTPException(502, "Geography source unavailable") from exc
        finally:
            await sources.close()
    return await enrich_osm(build_atlas(cached["institution"], cached["assets"]), cached['institution'])


@app.get("/api/atlas/{ror_id}/isochrone")
async def campus_isochrone(ror_id: str, mode: str = "walking", minutes: int = 15) -> dict[str, Any]:
    ror_id = valid_ror(ror_id)
    item = await atlas(ror_id)
    if not item["campuses"] or item["campuses"][0]["precision"] == "city_centroid":
        return {"available": False, "reason": "Точная точка кампуса не подтверждена; расчёт от центра города вводил бы в заблуждение."}
    campus = item["campuses"][0]
    return await isochrone(campus["lat"], campus["lon"], mode, minutes)


@app.get("/api/profiles/{ror_id}/preview")
async def profile_preview(ror_id: str) -> dict[str, Any]:
    """First licensed photographs in a few seconds, while the full build runs."""
    ror_id = valid_ror(ror_id)
    cached = db.get_profile(ror_id)
    if cached and cached.get("pipeline_version") == PIPELINE_VERSION:
        return {"institution": cached["institution"], "assets": cached["assets"][:12],
                "elapsed_ms": 0, "preview": True, "from_cache": True}
    started = time.monotonic()
    sources = Sources(priority=True)
    try:
        record = await sources.ror_get(ror_id)
        return await asyncio.wait_for(build_preview(sources, record, started=started), timeout=12)
    except (SourceError, TimeoutError) as exc:
        raise HTTPException(502, "Preview unavailable; the full profile is still being built") from exc
    finally:
        await sources.close()


@app.get("/api/profiles/{ror_id}")
async def profile(ror_id: str, refresh: bool = False) -> dict[str, Any]:
    ror_id = valid_ror(ror_id)
    cached = db.get_profile(ror_id)
    ttl = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
    if cached and not refresh and cached.get("pipeline_version") == PIPELINE_VERSION and cached["cache_age_seconds"] < ttl:
        cached["from_cache"] = True
        return cached

    existing = _inflight.get(ror_id)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)
    task = asyncio.ensure_future(_build(ror_id, cached))
    _inflight[ror_id] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _inflight.get(ror_id) is task and task.done():
            _inflight.pop(ror_id, None)


async def _build(ror_id: str, cached: dict[str, Any] | None) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + PROFILE_BUDGET_SECONDS
    sources = Sources()
    try:
        # The identity lookup is part of the user's wait, so it is inside both
        # the measured elapsed time and the 30-second budget.
        record = await asyncio.wait_for(sources.ror_get(ror_id), timeout=max(1.0, deadline - time.monotonic()))
        # The builder watches ``deadline`` itself and returns a partial profile;
        # this outer limit is only a backstop against a hung connection.
        result = await asyncio.wait_for(
            build_profile(sources, record, started=started, deadline=deadline),
            timeout=max(1.0, deadline + 3 - time.monotonic()),
        )
        if not result["assets"] and result["warnings"]:
            if cached and cached["assets"]:
                cached["from_cache"] = True
                cached.setdefault("warnings", []).append("Новые источники временно недоступны; показана сохранённая версия")
                return cached
            result["warnings"].append("Профиль не сохранён в кэш: внешние источники временно недоступны")
            result["from_cache"] = False
            return result
        if (cached and cached.get("pipeline_version") == PIPELINE_VERSION and result["profile_status"] == "partial"
                and cached.get("profile_status") == "complete" and len(cached["assets"]) >= len(result["assets"])):
            # A refresh that lost a source (rate limit, timeout) must not replace
            # a complete profile with a poorer one.
            cached["from_cache"] = True
            cached.setdefault("warnings", []).append(
                "Обновление получилось неполным (" + ", ".join(result["incomplete_sources"]) + "); сохранена предыдущая полная версия")
            return cached
        db.save_profile(result, record)
        result["from_cache"] = False
        return result
    except TimeoutError as exc:
        if cached:
            cached["from_cache"] = True
            cached.setdefault("warnings", []).append("Обновление превысило 30 секунд; показана сохранённая версия")
            return cached
        raise HTTPException(504, "Profile search exceeded 30 seconds") from exc
    except SourceError as exc:
        if cached:
            cached["from_cache"] = True
            cached.setdefault("warnings", []).append(f"Источник {exc.provider} недоступен; показана сохранённая версия")
            return cached
        raise HTTPException(502, f"{exc.provider} unavailable: {exc.detail}") from exc
    finally:
        await sources.close()


@app.get("/api/assets/{asset_id}")
async def asset(asset_id: str, ror_id: str | None = None) -> dict[str, Any]:
    item = db.get_asset(asset_id, valid_ror(ror_id) if ror_id else None)
    if not item:
        raise HTTPException(404, "Asset not found")
    return item


@app.get("/api/profiles/{ror_id}/extras")
async def extras(ror_id: str) -> dict[str, Any]:
    ror_id = valid_ror(ror_id)
    profile_data = db.get_profile(ror_id)
    if not profile_data:
        raise HTTPException(404, "Build the profile first")
    institution = profile_data["institution"]
    # P1.6: /extras used to re-hit OpenAlex, Wikidata, Open-Meteo and Nominatim
    # on every profile open. During judging that is a reliable way to earn a 429
    # from a public service that asks us to be polite.
    extras_key = f"extras:v2:{ror_id}"
    remembered = db.get_cached(extras_key)
    if remembered is not None:
        return {**remembered, "from_cache": True}
    sources = Sources()
    result: dict[str, Any] = {"research": None, "students": None, "weather": None, "campus_candidate": None,
                              "videos": [], "official_page_leads": [], "warnings": []}

    async def students() -> None:
        qid = institution.get("wikidata_id")
        if not qid:
            return
        try:
            result["students"] = latest_student_count(await sources.wikidata_student_claims(qid), qid)
        except SourceError as exc:
            result["warnings"].append(f"Wikidata students: {exc.detail}")

    async def research() -> None:
        try:
            data = await sources.openalex(ror_id)
            years = sorted((x for x in data.get("counts_by_year", [])
                            if isinstance(x.get("year"), int) and 2016 <= x["year"] <= 2025),
                           key=lambda x: x["year"])
            result["research"] = {
                "label": "Публикации, связанные с университетом", "source": data.get("id"),
                "works_count": data.get("works_count"), "years": years,
                "warning": "Публикации не равны числу студентов или качеству обучения.",
            }
        except SourceError as exc:
            result["warnings"].append(f"OpenAlex: {exc.detail}")

    async def climate() -> None:
        coord = institution.get("city_coordinates")
        if not coord:
            return
        try:
            data = await sources.weather(coord["lat"], coord["lon"])
            result["weather"] = {"city": institution.get("city"), "current": data.get("current"),
                                 "source": "https://open-meteo.com/", "scope": "city"}
        except SourceError as exc:
            result["warnings"].append(f"Open-Meteo: {exc.detail}")

    async def campus_candidate() -> None:
        # One geocoder gives an unverified guess with no way to judge it. We ask
        # every provider we have (Wikidata, Nominatim, optionally Mapbox) and
        # report whether they agree — corroboration of a point on a map, never
        # evidence about where a photograph was taken.
        crosscheck = await campus_geocode_crosscheck(sources, institution)
        result["geocode_crosscheck"] = crosscheck
        result["warnings"].extend(crosscheck.pop("warnings", []))
        if not crosscheck["points"]:
            return
        best = crosscheck["points"][0]
        result["campus_candidate"] = {
            "display_name": best.get("label") or institution["name"],
            "lat": best["lat"], "lon": best["lon"],
            "provider": best["provider"], "source_url": best.get("source_url"),
            "status": "cross_checked" if crosscheck["agreement"] == "confirmed" else "unverified_map_candidate",
            "agreement": crosscheck["agreement"],
            "warning": crosscheck["note"],
        }

    async def videos() -> None:
        channel_id = official_youtube_channel(ror_id) or institution.get('youtube_channel')
        if not channel_id:
            return
        try:
            items = await sources.youtube_uploads(channel_id)
            terms = ("campus", "tour", "dorm", "library", "лаборатор", "общежит", "кампус")
            for item in items:
                snippet = item.get("snippet", {})
                title = snippet.get("title", "")
                video_id = snippet.get("resourceId", {}).get("videoId")
                if video_id and any(word in title.casefold() for word in terms):
                    result["videos"].append({"title": title, "video_id": video_id,
                                             "published_at": snippet.get("publishedAt"),
                                             "source_url": f"https://www.youtube.com/watch?v={video_id}"})
        except SourceError as exc:
            result["warnings"].append(f"YouTube: {exc.detail}")

    async def page_leads() -> None:
        domain = institution.get("official_domain")
        if not domain:
            return
        try:
            pages = await sources.brave_pages(domain, institution["name"])
            for page in pages:
                url = page.get("url", "")
                host = urlparse(url).hostname or ""
                if host == domain or host.endswith("." + domain):
                    result["official_page_leads"].append({"title": page.get("title"), "url": url,
                                                           "note": "Страница найдена; права на её фото не установлены"})
        except SourceError as exc:
            result["warnings"].append(f"Brave: {exc.detail}")

    await asyncio.gather(research(), students(), climate(), campus_candidate(), videos(), page_leads())
    result["source_events"] = sources.events
    await sources.close()
    # Weather is the only short-lived field here; an hour keeps it honest while
    # still shielding the slower registries from repeated identical questions.
    db.set_cached(extras_key, result, 3600)
    return {**result, "from_cache": False}


@app.get("/api/profiles/{ror_id}/student-voices")
async def voices(ror_id: str) -> dict[str, Any]:
    ror_id = valid_ror(ror_id)
    profile_data = db.get_profile(ror_id)
    if not profile_data:
        raise HTTPException(404, "Build the profile first")
    try:
        return await asyncio.wait_for(student_voices(profile_data["institution"]), timeout=55)
    except TimeoutError:
        return {'available':False,'reason':'Поиск обсуждений занял больше 55 секунд. Повторите через минуту.'}


@app.get("/api/compare")
async def compare(left: str, right: str) -> dict[str, Any]:
    profiles = []
    for value in (left, right):
        item = db.get_profile(valid_ror(value))
        if not item:
            raise HTTPException(404, f"Build profile {value} first")
        profiles.append(item)
    # P1.8: a comparison that hides three of the eight categories, or puts a
    # "0" produced by a dead source next to a genuine "0", is not a comparison.
    comparable = all(p.get("profile_status", "complete") == "complete" for p in profiles)
    return {
        "categories": ["campus", "dormitory", "classroom", "library", "sports",
                       "laboratories", "student_life", "city"],
        "comparable": comparable,
        "profiles": [{
            "institution": p["institution"], "coverage": p["coverage"],
            "category_status": p.get("category_status", {}),
            "unclassified_count": p.get("unclassified_count", 0),
            "profile_status": p.get("profile_status", "complete"),
            "pipeline_version": p.get("pipeline_version"),
            "asset_count": len(p["assets"]), "generated_at": p["generated_at"],
            "cache_age_seconds": p.get("cache_age_seconds"),
            "visual_check": (p.get("vision") or {}).get("available", False),
            "caveat": "Количество фото отражает покрытие источников, а не качество университета.",
        } for p in profiles],
        "caveat": ("Оба профиля собраны полностью, методика одинаковая." if comparable else
                   "Минимум один профиль неполный: ноль в разделе может означать недоступность "
                   "источника, а не отсутствие материалов. Сравнивайте с осторожностью."),
    }
