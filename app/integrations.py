"""Read-only external adapters. Every adapter is optional except ROR.

The caller is responsible for keeping Wikimedia requests serial and batched.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import hashlib
import re
from typing import Any
from urllib.parse import unquote

import httpx
from . import db


USER_AGENT = os.getenv(
    "CAMPUS_TRACE_USER_AGENT",
    "CampusTraceHackathon/0.3 (educational prototype; operator must set contact)",
)
_nominatim_lock = asyncio.Lock()
_nominatim_last = 0.0
_commons_lock = asyncio.Lock()
_commons_last = 0.0
_commons_retry_at = 0.0


class SourceError(Exception):
    def __init__(self, provider: str, detail: str):
        super().__init__(detail)
        self.provider = provider
        self.detail = detail


class Sources:
    def __init__(self) -> None:
        timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "8"))
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
        )
        self.events: list[dict[str, Any]] = []
        self.rate_limited: set[str] = set()

    async def close(self) -> None:
        await self.client.aclose()

    async def json(
        self, provider: str, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        start = time.monotonic()
        cache_key = None
        if provider in {"commons", "wikidata", "ror", "wdqs"}:
            cache_key = provider + ':' + hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
            cached = db.get_cached(cache_key)
            if cached is not None:
                self.events.append({"provider":provider,"outcome":"cache","elapsed_ms":0})
                return cached
        if provider in self.rate_limited:
            raise SourceError(provider, "HTTP 429: источник временно ограничил запросы")
        try:
            # Wikimedia asks API clients to identify themselves and avoid request bursts.
            # Sources instances are created per profile, so the limiter must be shared.
            if provider == "commons":
                global _commons_last, _commons_retry_at
                if time.monotonic() < _commons_retry_at:
                    raise SourceError(provider, "HTTP 429: источник восстанавливает лимит, повторите через минуту")
                async with _commons_lock:
                    await asyncio.sleep(max(0.0, 1.05 - (time.monotonic() - _commons_last)))
                    _commons_last = time.monotonic()
            response = await self.client.get(url, params=params, headers=headers)
            if response.status_code == 429:
                wait = response.headers.get("Retry-After", "1")
                try:
                    delay = float(wait)
                except ValueError:
                    delay = 1.0
                if 0 < delay <= 2:
                    await asyncio.sleep(delay)
                    response = await self.client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, (dict, list)):
                raise ValueError("Expected JSON object or array")
            if isinstance(data, dict) and data.get('error'):
                raise ValueError("Provider returned an API error")
            if cache_key: db.set_cached(cache_key, data, 86400 if provider != 'ror' else 3600)
            self.events.append({"provider": provider, "outcome": "ok", "elapsed_ms": int((time.monotonic()-start)*1000)})
            return data
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429:
                self.rate_limited.add(provider)
                if provider == 'commons': _commons_retry_at = time.monotonic() + 60
            detail = f"HTTP {code}" + (": источник временно ограничил запросы" if code == 429 else "")
            self.events.append({"provider": provider, "outcome": "error", "detail": detail, "elapsed_ms": int((time.monotonic()-start)*1000)})
            raise SourceError(provider, detail) from exc
        except (httpx.HTTPError, ValueError) as exc:
            detail = type(exc).__name__
            self.events.append({"provider": provider, "outcome": "error", "detail": detail, "elapsed_ms": int((time.monotonic()-start)*1000)})
            raise SourceError(provider, detail) from exc

    async def ror_search_page(self, query: str, page: int = 1) -> dict[str, Any]:
        escaped = re.sub(r'([+\-=!(){}\[\]^"~*?:\\/|&<>])', r'\\\1', query.strip())
        term = f'"{escaped}"' if ' ' in escaped else escaped
        return await self.json("ror", "https://api.ror.org/v2/organizations", params={"query": term, "filter": "types:education", "page": page})

    async def ror_search(self, query: str) -> list[dict[str, Any]]:
        data = await self.ror_search_page(query)
        return data.get("items", [])

    async def wikidata_details(self, qid: str) -> dict[str, Any]:
        data = await self.json("wikidata", "https://www.wikidata.org/w/api.php", params={
            "action": "wbgetentities", "ids": qid, "props": "claims|sitelinks", "format": "json"})
        return data.get("entities", {}).get(qid, {})

    async def ror_get(self, ror_id: str) -> dict[str, Any]:
        return await self.json("ror", f"https://api.ror.org/v2/organizations/{ror_id}")

    async def wikidata_category(self, qid: str) -> str | None:
        data = await self.json(
            "wikidata", "https://www.wikidata.org/w/api.php",
            params={"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"},
        )
        claims = data.get("entities", {}).get(qid, {}).get("claims", {}).get("P373", [])
        for claim in claims:
            try:
                return claim["mainsnak"]["datavalue"]["value"]
            except (KeyError, TypeError):
                pass
        return None

    async def wikidata_student_claims(self, qid: str) -> list[dict[str, Any]]:
        data = await self.json(
            "wikidata", "https://www.wikidata.org/w/api.php",
            params={"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"},
        )
        return data.get("entities", {}).get(qid, {}).get("claims", {}).get("P2196", [])

    async def commons_category(self, name: str, limit: int = 100, pages: int = 1) -> list[dict[str, Any]]:
        """Category members, optionally following the API's own continuation.

        P1.5: a single request returns at most one page in *alphabetical* order,
        so a university with a large category was cut off at "B" and its
        dormitory and lecture-hall files were never even considered. MediaWiki
        exposes ``cmcontinue`` exactly for this; each extra page costs one more
        rate-limited request, so the caller decides how many it can afford.
        """
        members: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "action": "query", "list": "categorymembers", "cmtitle": f"Category:{name}",
            "cmtype": "file|subcat", "cmlimit": str(limit), "format": "json",
        }
        for _ in range(max(1, pages)):
            data = await self.json("commons", "https://commons.wikimedia.org/w/api.php", params=params)
            members.extend(data.get("query", {}).get("categorymembers", []))
            cursor = (data.get("continue") or {}).get("cmcontinue")
            if not cursor:
                break
            params = {**params, "cmcontinue": cursor}
        return members

    async def commons_search(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        data = await self.json(
            "commons", "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "srnamespace": "6", "srlimit": str(limit), "format": "json"},
        )
        return data.get("query", {}).get("search", [])

    async def commons_depicts(self, qids: list[str], limit: int = 40) -> list[dict[str, Any]]:
        """Files whose structured data says they *depict* one of these items.

        Commons "depicts" (P180) is a statement a person made about the image
        content, which is stronger evidence than a word in a file name.
        """
        qids = [q for q in qids if re.fullmatch(r"Q\d+", q or "")][:12]
        if not qids:
            return []
        return await self.commons_search("haswbstatement:" + "|".join(f"P180={q}" for q in qids), limit=limit)

    async def commons_geosearch(self, lat: float, lon: float, radius_m: int = 700, limit: int = 60) -> list[dict[str, Any]]:
        data = await self.json(
            "commons", "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "list": "geosearch", "gscoord": f"{lat}|{lon}",
                    "gsradius": str(min(10000, max(10, radius_m))), "gsnamespace": "6",
                    "gslimit": str(limit), "format": "json"},
        )
        return data.get("query", {}).get("geosearch", [])

    async def wikidata_buildings(self, qid: str) -> list[dict[str, Any]]:
        """Items that are part of / operated / owned by the university and have
        an image, together with their Wikidata types (library, residence hall…).

        The type is a structured claim, so it can place a photo in a category
        without guessing from words in the file name.
        """
        if not re.fullmatch(r"Q\d+", qid or ""):
            return []
        query = (
            'SELECT ?b ?bLabel ?img (GROUP_CONCAT(DISTINCT ?typeLabel; separator="|") AS ?types) WHERE {\n'
            f'  ?b wdt:P361|wdt:P137|wdt:P127 wd:{qid} .\n'
            '  ?b wdt:P18 ?img .\n'
            '  OPTIONAL { ?b wdt:P31 ?type . ?type rdfs:label ?typeLabel . FILTER(LANG(?typeLabel)="en") }\n'
            '  OPTIONAL { ?b rdfs:label ?bLabel . FILTER(LANG(?bLabel)="en") }\n'
            '} GROUP BY ?b ?bLabel ?img LIMIT 80'
        )
        data = await self.json(
            "wdqs", "https://query.wikidata.org/sparql",
            params={"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
        )
        return parse_building_rows(data)

    async def commons_imageinfo(self, titles: list[str]) -> list[dict[str, Any]]:
        if not titles:
            return []
        data = await self.json(
            "commons", "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "prop": "imageinfo|coordinates", "titles": "|".join(titles),
                    "coprimary": "all", "coprop": "type|name", "colimit": "max",
                    "iiprop": "url|mime|sha1|timestamp|size|extmetadata", "iiurlwidth": "960",
                    "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|ImageDescription|DateTimeOriginal|Credit|UsageTerms",
                    "format": "json", "formatversion": "2"},
        )
        return data.get("query", {}).get("pages", [])

    async def openalex(self, ror_id: str) -> dict[str, Any]:
        return await self.json("openalex", f"https://api.openalex.org/institutions/ror:{ror_id}")

    async def nominatim(self, name: str, city: str) -> list[dict[str, Any]]:
        if not os.getenv("CAMPUS_TRACE_USER_AGENT") or "your-email@example.com" in USER_AGENT:
            raise SourceError("nominatim", "укажите контакт оператора в CAMPUS_TRACE_USER_AGENT")
        global _nominatim_last
        async with _nominatim_lock:
            await asyncio.sleep(max(0.0, 1.1 - (time.monotonic() - _nominatim_last)))
            _nominatim_last = time.monotonic()
            data = await self.json(
                "nominatim", "https://nominatim.openstreetmap.org/search",
                params={"q": f"{name}, {city}", "format": "jsonv2", "limit": "3"},
            )
        return data if isinstance(data, list) else []

    async def weather(self, lat: float, lon: float) -> dict[str, Any]:
        return await self.json(
            "openmeteo", "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code", "timezone": "auto"},
        )

    async def youtube_uploads(self, channel_id: str) -> list[dict[str, Any]]:
        key = os.getenv("YOUTUBE_API_KEY")
        if not key:
            return []
        channel = await self.json(
            "youtube", "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "contentDetails", "id": channel_id, "key": key},
        )
        items = channel.get("items", [])
        if not items:
            return []
        playlist = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not playlist:
            return []
        data = await self.json(
            "youtube", "https://www.googleapis.com/youtube/v3/playlistItems",
            params={"part": "snippet", "playlistId": playlist, "maxResults": "30", "key": key},
        )
        return data.get("items", [])

    async def flickr_search(self, name: str) -> list[dict[str, Any]]:
        key = os.getenv("FLICKR_API_KEY")
        if not key:
            return []
        data = await self.json(
            "flickr", "https://www.flickr.com/services/rest/",
            params={"method": "flickr.photos.search", "api_key": key, "format": "json",
                    "nojsoncallback": "1", "text": name, "media": "photos", "safe_search": "1",
                    "license": "4,5,9,11,12", "extras": "license,date_taken,date_upload,owner_name,url_m,geo",
                    "per_page": "20"},
        )
        return data.get("photos", {}).get("photo", [])

    async def mapbox_geocode(self, name: str, city: str | None, country_code: str | None) -> list[dict[str, Any]]:
        """Second, independent geocoder used to cross-check the campus point.

        Nominatim alone gives one unverified candidate and no way to tell a good
        hit from a bad one. Two geocoders built from different data that agree
        within a couple of kilometres are weak corroboration; two that disagree
        are a signal to show both and claim nothing.
        """
        token = os.getenv("MAPBOX_TOKEN")
        if not token:
            return []
        params: dict[str, Any] = {
            "q": ", ".join(x for x in (name, city) if x), "limit": "3",
            "types": "poi,address,place", "access_token": token,
        }
        if country_code:
            params["country"] = country_code.lower()
        data = await self.json("mapbox", "https://api.mapbox.com/search/geocode/v6/forward", params=params)
        results = []
        for feature in (data.get("features", []) if isinstance(data, dict) else []):
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            properties = feature.get("properties") or {}
            if len(coordinates) == 2 and all(isinstance(v, (int, float)) for v in coordinates):
                results.append({
                    "lat": coordinates[1], "lon": coordinates[0],
                    "label": properties.get("full_address") or properties.get("name"),
                    "kind": properties.get("feature_type"),
                })
        return results

    async def mapbox_isochrone(self, lat: float, lon: float, profile: str, minutes: int) -> dict[str, Any]:
        token = os.getenv("MAPBOX_TOKEN")
        if not token:
            return {}
        data = await self.json(
            "mapbox", f"https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lon},{lat}",
            params={"contours_minutes": str(minutes), "polygons": "true", "access_token": token},
        )
        return data if isinstance(data, dict) else {}

    async def brave_pages(self, official_domain: str, name: str) -> list[dict[str, Any]]:
        key = os.getenv("BRAVE_API_KEY")
        if not key:
            return []
        data = await self.json(
            "brave", "https://api.search.brave.com/res/v1/web/search",
            params={"q": f"site:{official_domain} {name} campus dormitory library", "count": "10"},
            headers={"X-Subscription-Token": key},
        )
        return data.get("web", {}).get("results", [])


def parse_building_rows(data: Any) -> list[dict[str, Any]]:
    """SPARQL JSON → [{qid, label, file, types}]. Pure, unit tested."""
    rows = []
    for row in (data.get("results", {}).get("bindings", []) if isinstance(data, dict) else []):
        image = (row.get("img") or {}).get("value", "")
        item = (row.get("b") or {}).get("value", "")
        if "Special:FilePath/" not in image or "/entity/Q" not in item:
            continue
        rows.append({
            "qid": item.rsplit("/", 1)[-1],
            "label": (row.get("bLabel") or {}).get("value") or "",
            "file": "File:" + unquote(image.split("Special:FilePath/", 1)[1]).replace("_", " "),
            "types": [t for t in ((row.get("types") or {}).get("value") or "").split("|") if t],
        })
    return rows


def official_youtube_channel(ror_id: str) -> str | None:
    try:
        channels = json.loads(os.getenv("OFFICIAL_YOUTUBE_CHANNELS_JSON", "{}"))
        return channels.get(ror_id)
    except (ValueError, TypeError):
        return None
