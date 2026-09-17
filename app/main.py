"""CampusTrace MVP API and single-page application."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .atlas import build_atlas, isochrone, enrich_osm
from .discovery import suggest, SEEDS
from .integrations import SourceError, Sources, official_youtube_channel
from .pipeline import VERSION as PIPELINE_VERSION, build_profile, institution_summary, latest_student_count, norm
from .voices import student_voices


BASE = Path(__file__).resolve().parents[1]
ROR_ID = re.compile(r"^[0-9a-z]{9}$")


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
    return FileResponse(BASE / "static" / "index.html")


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
        },
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


@app.get("/api/profiles/{ror_id}")
async def profile(ror_id: str, refresh: bool = False) -> dict[str, Any]:
    ror_id = valid_ror(ror_id)
    cached = db.get_profile(ror_id)
    ttl = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
    if cached and not refresh and cached.get("pipeline_version") == PIPELINE_VERSION and cached["cache_age_seconds"] < ttl:
        cached["from_cache"] = True
        return cached

    sources = Sources()
    try:
        record = await sources.ror_get(ror_id)
        result = await asyncio.wait_for(build_profile(sources, record), timeout=28)
        if not result["assets"] and result["warnings"]:
            if cached and cached["assets"]:
                cached["from_cache"] = True
                cached.setdefault("warnings", []).append("Новые источники временно недоступны; показана сохранённая версия")
                return cached
            result["warnings"].append("Профиль не сохранён в кэш: внешние источники временно недоступны")
            result["from_cache"] = False
            return result
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
        if institution.get('campus_coordinates'):
            point = institution['campus_coordinates']
            result['campus_candidate'] = {**point, 'display_name':institution['name'], 'status':'institution_point', 'warning':'Координата университета из Wikidata; границы корпусов уточняются отдельно.'}
            return
        if not institution.get("city"):
            return
        try:
            data = await sources.nominatim(institution["name"], institution["city"])
            if data:
                item = data[0]
                result["campus_candidate"] = {
                    "display_name": item.get("display_name"), "lat": item.get("lat"),
                    "lon": item.get("lon"), "osm_type": item.get("osm_type"),
                    "osm_id": item.get("osm_id"), "status": "unverified_map_candidate",
                    "warning": "Точка карты требует подтверждения; это не геодоказательство для фотографий.",
                }
        except SourceError as exc:
            result["warnings"].append(f"Nominatim: {exc.detail}")

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
    return result


@app.get("/api/profiles/{ror_id}/student-voices")
async def voices(ror_id: str) -> dict[str, Any]:
    ror_id = valid_ror(ror_id)
    profile_data = db.get_profile(ror_id)
    if not profile_data:
        raise HTTPException(404, "Build the profile first")
    try:
        return await asyncio.wait_for(student_voices(profile_data["institution"]), timeout=30)
    except TimeoutError:
        return {'available':False,'reason':'Поиск обсуждений занял больше 30 секунд. Повторите через минуту.'}


@app.get("/api/compare")
async def compare(left: str, right: str) -> dict[str, Any]:
    profiles = []
    for value in (left, right):
        item = db.get_profile(valid_ror(value))
        if not item:
            raise HTTPException(404, f"Build profile {value} first")
        profiles.append(item)
    return {
        "profiles": [{"institution": p["institution"], "coverage": p["coverage"],
                      "asset_count": len(p["assets"]), "generated_at": p["generated_at"],
                      "caveat": "Количество фото отражает покрытие источников, а не качество университета."}
                     for p in profiles]
    }
