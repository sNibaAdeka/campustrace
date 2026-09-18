import json
import os
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

from app import db, vision
from app.pipeline import (
    build_profile, classify, commons_asset, deduplicate, describe_campus,
    institution_summary, known_name_in_text, latest_student_count,
    thumbnail_hashes, _hashable_host, VERSION, building_category, open_license,
    evidence_level, valid_title, usable_subcategory,
)
from app.integrations import parse_building_rows
from app.integrations import Sources, SourceError
from app.main import profile as get_profile_endpoint
from app.atlas import build_atlas, crosscheck_points, isochrone
from app.discovery import suggest
from app.voices import _relevant, _groq_web_search, student_voices
import httpx


RECORD = {
    "id": "https://ror.org/052bx8q98",
    "names": [{"value": "Nazarbayev University", "types": ["ror_display"]}],
    "locations": [{"geonames_details": {"name": "Astana", "country_name": "Kazakhstan", "lat": 51.1, "lng": 71.4}}],
    "domains": ["nu.edu.kz"],
}


def page(title, license_name="CC BY-SA 4.0"):
    return {
        "title": f"File:{title}",
        "imageinfo": [{
            "thumburl": "https://upload.wikimedia.org/thumb.jpg",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "timestamp": "2022-01-01T00:00:00Z", "sha1": title,
            "extmetadata": {"LicenseShortName": {"value": license_name}, "Artist": {"value": "Author"}},
        }],
    }


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.institution = institution_summary(RECORD)

    def test_city_context_is_never_presented_as_campus(self):
        item = commons_asset(page("Astana skyline.jpg"), self.institution, "city")
        self.assertEqual(item["status"], "city_context")
        self.assertEqual(item["category"], "city")
        self.assertIn("связь с кампусом не утверждается", " ".join(item["reasons"]))

    def test_distant_photo_is_rejected_even_when_title_matches(self):
        item = page('Nazarbayev University campus.jpg')
        item['coordinates'] = [{'lat':40.0,'lon':-70.0}]
        self.assertIsNone(commons_asset(item,self.institution,'category'))

    def test_missing_license_and_off_topic_asset_are_rejected(self):
        self.assertIsNone(commons_asset(page("Nazarbayev University campus.jpg", ""), self.institution, "category"))
        self.assertIsNone(commons_asset(page("Reeglid Tartu Ülikooli.jpg"), self.institution, "category"))

    def test_search_requires_name_in_title(self):
        self.assertIsNone(commons_asset(page("Beautiful campus.jpg"), self.institution, "search"))
        self.assertIsNotNone(commons_asset(page("Nazarbayev University campus.jpg"), self.institution, "search"))

    def test_subcategory_provenance_supports_classification(self):
        item = commons_asset(page("Library reading hall.jpg"), self.institution, "category_sub:Nazarbayev University Library")
        self.assertEqual(item["category"], "library")
        self.assertTrue(any("Подкатегория" in reason for reason in item["reasons"]))

    def test_near_duplicate_is_removed(self):
        a = {"id": "a", "sha1": "one", "dhash": "0000000000000000"}
        b = {"id": "b", "sha1": "two", "dhash": "0000000000000001"}
        kept, removed = deduplicate([a, b])
        self.assertEqual(([item["id"] for item in kept], removed), (["a"], 1))

    def test_hashable_host_accepts_thumb_wikimedia_subdomain(self):
        # Regression for P0.1: real Commons thumbnails are served from
        # thumb.wikimedia.org, not only upload.wikimedia.org. A host check
        # that misses this silently skips visual dedup for ~all candidates.
        self.assertTrue(_hashable_host("thumb.wikimedia.org"))
        self.assertTrue(_hashable_host("upload.wikimedia.org"))
        self.assertTrue(_hashable_host("live.staticflickr.com"))
        self.assertFalse(_hashable_host("evil.example.com"))

    def test_thumbnail_hashes_actually_hashes_thumb_wikimedia_urls(self):
        import io
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (16, 16), color=(120, 200, 80)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        class FakeResponse:
            content = png_bytes
            def raise_for_status(self): pass

        class FakeClient:
            async def get(self, url, timeout=3):
                return FakeResponse()

        class FakeSources:
            client = FakeClient()

        asset = {"id": "x", "image_url": "https://thumb.wikimedia.org/thumb/a/ab/File.jpg/300px-File.jpg", "reasons": []}
        stats = self.loop_run(thumbnail_hashes(FakeSources(), [asset]))
        self.assertEqual(stats, {"hash_attempted": 1, "hash_succeeded": 1})
        self.assertIsNotNone(asset["dhash"])

    @staticmethod
    def loop_run(coro):
        import asyncio
        return asyncio.run(coro)

    def test_camera_geotag_preserves_type_without_promoting_trust(self):
        source = page("Nazarbayev University campus.jpg")
        source["coordinates"] = [{"lat":51.09,"lon":71.4,"type":"camera"}]
        asset = commons_asset(source, self.institution, "category")
        self.assertEqual(asset["coordinates"]["type"], "camera")
        self.assertEqual(asset["status"], "probable")

    def test_student_count_requires_date_and_excludes_partial_count(self):
        def claim(amount, year=None, partial=False):
            item = {"mainsnak": {"datavalue": {"value": {"amount": str(amount)}}},
                    "qualifiers": {}, "rank": "normal"}
            if year:
                item["qualifiers"]["P585"] = [{"datavalue": {"value": {"time": f"+{year}-01-01T00:00:00Z"}}}]
            if partial:
                item["qualifiers"]["P518"] = [{}]
            return item
        result = latest_student_count([claim(100, 2022), claim(200, 2024, True),
                                       claim(300), claim(120, 2023)], "Q123")
        self.assertEqual((result["count"], result["year"]), (120, 2023))


class DatabaseTests(unittest.TestCase):
    def test_profile_and_asset_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"DATABASE_PATH": folder + "/test.sqlite3"}):
            db.initialize()
            inst = institution_summary(RECORD)
            asset = commons_asset(page("Nazarbayev University campus.jpg"), inst, "category")
            profile = {"institution": inst, "pipeline_version": "test", "generated_at": 1,
                       "assets": [asset], "coverage": {"campus": 1},
                       "source_events": [{"provider": "commons", "outcome": "ok", "elapsed_ms": 42}]}
            db.save_profile(profile, RECORD)
            self.assertEqual(db.get_profile(inst["ror_id"])["assets"][0]["id"], asset["id"])
            self.assertEqual(db.get_asset(asset["id"])["reasons"], asset["reasons"])


class CacheFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limited_refresh_preserves_old_profile(self):
        previous = {"assets": [{"id": "old"}], "warnings": [], "cache_age_seconds": 999999,
                    "pipeline_version": "old"}
        source = type("StubSource", (), {})()
        source.ror_get = AsyncMock(return_value=RECORD)
        source.close = AsyncMock()
        failed = {"assets": [], "warnings": ["Commons: HTTP 429"]}
        with patch("app.main.db.get_profile", return_value=previous), \
             patch("app.main.db.save_profile") as save, \
             patch("app.main.Sources", return_value=source), \
             patch("app.main.build_profile", new=AsyncMock(return_value=failed)):
            result = await get_profile_endpoint("052bx8q98", refresh=True)
        self.assertTrue(result["from_cache"])
        self.assertEqual(result["assets"][0]["id"], "old")
        save.assert_not_called()


class AtlasAndSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_paginates_without_discarding_registry_aliases(self):
        records = [{**RECORD,'id':f'https://ror.org/0000000{i:02d}', 'names':[{'value':f'Campus {i}','types':['ror_display']}]} for i in range(20)]
        source = type('Stub',(),{})()
        source.ror_search_page = AsyncMock(return_value={'items':records,'number_of_results':45})
        source.close = AsyncMock()
        with patch('app.discovery.Sources',return_value=source), patch('app.discovery._cache',{}):
            result=await suggest('RegistryAlias',page=2)
        self.assertEqual(len(result['results']),20)
        self.assertTrue(result['has_more'])
        source.ror_search_page.assert_awaited_once_with('RegistryAlias',2)

    async def test_wikidata_campus_point_is_used_outside_demo(self):
        inst={**institution_summary(RECORD),'ror_id':'012345678','campus_coordinates':{'lat':10,'lon':20,'source':'https://www.wikidata.org/wiki/Q123'}}
        result=build_atlas(inst,[])
        self.assertEqual(result['campuses'][0]['precision'],'institution_point')
        self.assertEqual(result['campuses'][0]['lat'],10)

    async def test_oxford_city_post_is_not_university_review(self):
        post={'title':'Parking in Oxford','excerpt':'The campus football stadium has parking','subreddit':'rosebowlparking','url':'https://reddit.com/r/x/comments/abc'}
        self.assertEqual(_relevant([post],'University of Oxford'),[])
        post['excerpt']='University of Oxford student housing experiences'
        self.assertEqual(len(_relevant([post],'University of Oxford')),1)

    async def test_groq_search_does_not_publish_urls_from_generated_prose(self):
        payload={'choices':[{'message':{'content':'Invented https://example.com/review','executed_tools':[{'search_results':{'results':[{'title':'Actual source','url':'https://reddit.com/r/stanford/comments/abc','content':'Stanford University student housing'}]}}]}}]}
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload))
        with patch.dict(os.environ,{'GROQ_API_KEY':'test'}):
            async with httpx.AsyncClient(transport=transport) as client:
                posts=await _groq_web_search(client,'Stanford University','USA')
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0]['title'],'Actual source')
    async def test_cyrillic_acronym_finds_exact_institution(self):
        result = await suggest("НУ")
        self.assertEqual(result["results"][0]["ror_id"], "052bx8q98")
        self.assertEqual(result["results"][0]["match"], "точное совпадение")

    async def test_isochrone_never_invents_circle_without_key(self):
        with patch.dict(os.environ, {"OPENROUTESERVICE_API_KEY": ""}):
            result = await isochrone(51.09, 71.4, "walking", 15)
        self.assertFalse(result["available"])
        self.assertNotIn("geojson", result)

    async def test_nu_walk_stops_have_osm_sources_inside_mapped_boundary(self):
        atlas = build_atlas(institution_summary(RECORD), [])
        self.assertEqual(atlas["boundary"]["geometry"]["type"], "Polygon")
        self.assertEqual(len(atlas["walk_stops"]), 7)
        self.assertTrue(all(stop["source_url"].startswith("https://www.openstreetmap.org/way/") for stop in atlas["walk_stops"]))


class SceneClassificationTests(unittest.TestCase):
    """P0.3: 'campus' was the default bucket, so ceremonies became campus views."""

    def setUp(self):
        self.institution = institution_summary(RECORD)

    def test_event_words_are_not_published_as_campus_views(self):
        for title in ("Senate of the University of Tartu",
                      "Concert in the university aula",
                      "Signing of a memorandum at the university"):
            category, _tags, why = classify(title)
            self.assertEqual(category, "unknown", title)
            self.assertEqual(why, "event_word", title)

    def test_photo_without_any_scene_word_is_unknown_not_campus(self):
        self.assertEqual(classify("Tartu 2013 DSC 4471")[0], "unknown")

    def test_real_place_words_still_classify(self):
        self.assertEqual(classify("Nazarbayev University main building")[0], "campus")
        self.assertEqual(classify("University library reading room")[0], "library")
        self.assertEqual(classify("Student dormitory block C")[0], "dormitory")

    def test_unknown_asset_is_marked_unknown_not_probable(self):
        asset = commons_asset(page("Senate of Nazarbayev University.jpg"), self.institution, "category")
        self.assertEqual(asset["category"], "unknown")
        self.assertEqual(asset["status"], "unknown")
        self.assertTrue(any("не распознана" in reason for reason in asset["reasons"]))

    def test_subcategory_provenance_rescues_an_unreadable_filename(self):
        asset = commons_asset(page("DSC 00421.jpg"), self.institution,
                              "category_sub:Nazarbayev University Library")
        self.assertEqual(asset["category"], "library")

    def test_short_official_acronym_is_matched_as_a_whole_word(self):
        # P1.4: the old five-character rule discarded MIT, NYU, LSE, KTH.
        self.assertTrue(known_name_in_text("MIT Great Dome at dusk", ["MIT"]))
        self.assertFalse(known_name_in_text("Summit of rectors", ["MIT"]))
        self.assertFalse(known_name_in_text("Blacksmith workshop", ["MIT"]))


class VisualClassifierTests(unittest.TestCase):
    """The visual layer is a second opinion; it must never quietly raise trust."""

    def asset(self, category="campus", status="probable"):
        return {"id": "a", "category": category, "status": status, "reasons": [],
                "title": "x", "source_url": "https://commons.wikimedia.org/x"}

    def test_disagreement_lowers_confidence_instead_of_overwriting_text(self):
        asset = self.asset("campus")
        agreement = vision.reconcile(asset, {"scene": "library", "confidence": 0.9, "note": ""})
        self.assertEqual(agreement, "conflict")
        self.assertEqual(asset["status"], "unknown")
        self.assertEqual(asset["category"], "campus")

    def test_agreement_is_recorded_but_status_stays_probable(self):
        asset = self.asset("library")
        agreement = vision.reconcile(asset, {"scene": "library", "confidence": 0.9, "note": ""})
        self.assertEqual(agreement, "confirmed")
        self.assertEqual(asset["status"], "probable")
        self.assertNotIn("drop", asset)

    def test_irrelevant_image_is_dropped(self):
        asset = self.asset("campus")
        vision.reconcile(asset, {"scene": "not_relevant", "confidence": 0.95, "note": "medal"})
        self.assertTrue(asset["drop"])

    def test_hesitant_model_cannot_be_the_only_reason_to_delete(self):
        asset = self.asset("campus")
        agreement = vision.reconcile(asset, {"scene": "not_relevant", "confidence": 0.2, "note": ""})
        self.assertNotIn("drop", asset)
        self.assertEqual(asset["status"], "unknown")
        self.assertTrue(agreement.endswith("low_confidence"))

    def test_vision_fills_a_category_the_text_could_not_give(self):
        asset = self.asset("unknown", "unknown")
        agreement = vision.reconcile(asset, {"scene": "campus_exterior", "confidence": 0.8, "note": ""})
        self.assertEqual(agreement, "vision_only")
        self.assertEqual(asset["category"], "campus")
        self.assertTrue(any("только визуальным" in reason for reason in asset["reasons"]))

    def test_street_view_is_demoted_to_city_context(self):
        asset = self.asset("campus")
        vision.reconcile(asset, {"scene": "city_not_campus", "confidence": 0.8, "note": ""})
        self.assertEqual((asset["category"], asset["status"]), ("city", "city_context"))

    def test_model_prose_outside_the_vocabulary_is_rejected(self):
        self.assertIsNone(vision._parse("The photo shows a lovely campus."))
        self.assertIsNone(vision._parse('{"scene":"anything_goes","confidence":1}'))
        self.assertEqual(vision._parse('noise {"scene":"library","confidence":0.7} noise')["scene"], "library")

    def test_layer_is_a_no_op_without_a_key(self):
        with patch.dict(os.environ, {"GROK_API_KEY": ""}):
            stats = EvidenceTests.loop_run(vision.annotate([{"id": "a", "image_url": "https://x/y.jpg"}]))
        self.assertFalse(stats["available"])
        self.assertEqual(stats["checked"], 0)


class CommonsPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_category_follows_cmcontinue_to_a_second_page(self):
        # P1.5: one page is alphabetical, so a large category was cut off long
        # before the dormitory and lecture-hall files.
        responses = [
            {"query": {"categorymembers": [{"title": "File:A.jpg", "ns": 6}]},
             "continue": {"cmcontinue": "page-2"}},
            {"query": {"categorymembers": [{"title": "File:Z.jpg", "ns": 6}]}},
        ]
        seen = []
        sources = Sources()
        try:
            async def fake_json(provider, url, *, params=None, headers=None):
                seen.append((params or {}).get("cmcontinue"))
                return responses[len(seen) - 1]
            with patch.object(sources, "json", fake_json):
                members = await sources.commons_category("Example", limit=500, pages=2)
        finally:
            await sources.close()
        self.assertEqual([m["title"] for m in members], ["File:A.jpg", "File:Z.jpg"])
        self.assertEqual(seen, [None, "page-2"])

    async def test_single_page_request_does_not_continue(self):
        sources = Sources()
        calls = []
        try:
            async def fake_json(provider, url, *, params=None, headers=None):
                calls.append(1)
                return {"query": {"categorymembers": []}, "continue": {"cmcontinue": "more"}}
            with patch.object(sources, "json", fake_json):
                await sources.commons_category("Example", pages=1)
        finally:
            await sources.close()
        self.assertEqual(len(calls), 1)


class SocialSourcePolicyTests(unittest.IsolatedAsyncioTestCase):
    """Text and a link from a forum are fair use of a source. A photo is not.

    Instagram and Threads have no public API and no open licence for reuse, and
    the case forbids presenting someone else's photograph as a picture of a
    specific campus. This test makes that product decision enforceable rather
    than a promise in the README.
    """

    async def test_student_voices_never_returns_an_embeddable_image(self):
        posts = [{"title": "Dorm life at Nazarbayev University", "selftext": "housing is fine",
                  "subreddit": "nuredd", "permalink": "/r/nuredd/comments/abc",
                  "created_utc": 1700000000,
                  # A source that tries to hand us an image must be ignored.
                  "thumbnail": "https://preview.redd.it/a.jpg",
                  "url_overridden_by_dest": "https://i.redd.it/a.jpg"}]

        async def fake_reddit(client, query):
            return [{"title": posts[0]["title"], "excerpt": "student housing and campus",
                     "subreddit": "nuredd", "url": "https://www.reddit.com/r/nuredd/comments/abc",
                     "date": "2023-11-14", "provider": "Reddit / PullPush"}]

        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"DATABASE_PATH": folder + "/v.sqlite3",
                                     "GROQ_API_KEY": "", "BRAVE_API_KEY": ""}), \
             patch("app.voices._reddit_search", new=fake_reddit):
            db.initialize()
            result = await student_voices({"ror_id": "052bx8q98", "name": "Nazarbayev University",
                                           "city": "Astana", "country": "Kazakhstan", "aliases": []})

        serialised = json.dumps(result, ensure_ascii=False)
        for forbidden in (".jpg", ".jpeg", ".png", ".webp", "i.redd.it", "preview.redd.it",
                          "cdninstagram", "instagram.com", "threads.net"):
            self.assertNotIn(forbidden, serialised, f"leaked media reference: {forbidden}")
        for item in result["sources"]:
            self.assertEqual(set(item) & {"image", "image_url", "thumbnail", "media"}, set())
        self.assertIn("не встраиваются", result["media_policy"])


class GeocodeCrossCheckTests(unittest.TestCase):
    """Mapbox is a second opinion on the map point, not a replacement basemap."""

    def test_two_agreeing_providers_corroborate_the_point(self):
        result = crosscheck_points([
            {"provider": "Wikidata", "lat": 51.0900, "lon": 71.3994},
            {"provider": "Mapbox", "lat": 51.0913, "lon": 71.4021},
        ])
        self.assertEqual(result["agreement"], "confirmed")
        self.assertLess(result["max_distance_km"], 2)
        # Corroborating a coordinate is not corroborating a photograph.
        self.assertIn("не место съёмки", result["note"])

    def test_disagreeing_providers_do_not_pick_a_winner(self):
        result = crosscheck_points([
            {"provider": "Wikidata", "lat": 51.09, "lon": 71.40},
            {"provider": "Mapbox", "lat": 51.50, "lon": 71.40},
        ])
        self.assertEqual(result["agreement"], "conflict")
        self.assertEqual(len(result["points"]), 2)

    def test_single_provider_is_labelled_as_unchecked(self):
        result = crosscheck_points([{"provider": "Mapbox", "lat": 1.0, "lon": 2.0}])
        self.assertEqual(result["agreement"], "single_source")

    def test_no_provider_means_no_point(self):
        self.assertEqual(crosscheck_points([])["agreement"], "unavailable")
        self.assertEqual(crosscheck_points([{"provider": "x", "lat": None, "lon": None}])["points"], [])


class IsochroneProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapbox_token_alone_is_enough_to_offer_the_zone(self):
        with patch.dict(os.environ, {"OPENROUTESERVICE_API_KEY": "", "MAPBOX_TOKEN": ""}):
            unavailable = await isochrone(51.09, 71.4, "walking", 15)
        self.assertFalse(unavailable["available"])
        self.assertIn("MAPBOX_TOKEN", unavailable["reason"])

    async def test_invalid_mode_is_refused_before_any_key_check(self):
        with patch.dict(os.environ, {"MAPBOX_TOKEN": "x"}):
            result = await isochrone(51.09, 71.4, "teleport", 15)
        self.assertFalse(result["available"])
        self.assertNotIn("geojson", result)


class PipelineEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Run the whole builder against stubbed sources — no network, real code path."""

    def stub_sources(self, *, fail_metadata=False):
        pages = [page("Nazarbayev University main building.jpg"),
                 page("Nazarbayev University library reading room.jpg"),
                 page("Senate of Nazarbayev University.jpg"),
                 page("Astana skyline at night.jpg")]
        for index, item in enumerate(pages):
            item["imageinfo"][0]["sha1"] = f"sha{index}"
            item["imageinfo"][0]["thumburl"] = f"https://thumb.wikimedia.org/{index}.jpg"

        class Stub:
            events = []
            client = None
            def __init__(self):
                self.events = []
            async def wikidata_details(self, qid):
                return {"claims": {}, "sitelinks": {}}
            async def commons_category(self, name, limit=100, pages_=1, **kw):
                return [{"title": p["title"], "ns": 6} for p in pages[:3]]
            async def commons_search(self, query, limit=30):
                return [{"title": "File:Astana skyline at night.jpg"}] if "skyline" in query else []
            async def commons_imageinfo(self, titles):
                if fail_metadata:
                    raise SourceError("commons", "HTTP 429")
                return [p for p in pages if p["title"] in titles]
            async def flickr_search(self, name):
                return []
        return Stub()

    async def test_full_build_produces_an_honest_complete_profile(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"DATABASE_PATH": folder + "/t.sqlite3", "GROK_API_KEY": "", "GROQ_API_KEY": ""}), \
             patch("app.pipeline.thumbnail_hashes", new=AsyncMock(return_value={"hash_attempted": 3, "hash_succeeded": 3})):
            db.initialize()
            result = await build_profile(self.stub_sources(), RECORD)

        self.assertEqual(result["profile_status"], "complete")
        self.assertEqual(result["pipeline_version"], VERSION)
        # The senate photo must not inflate the campus count.
        categories_found = {a["title"]: a["category"] for a in result["assets"]}
        self.assertEqual(categories_found["Senate of Nazarbayev University.jpg"], "unknown")
        self.assertEqual(categories_found["Nazarbayev University library reading room.jpg"], "library")
        self.assertEqual(result["unclassified_count"], 1)
        self.assertNotIn("unknown", result["coverage"])
        # Without a key the visual layer is absent and the profile says so.
        self.assertFalse(result["vision"]["available"])
        self.assertTrue(any("GROK_API_KEY" in w for w in result["warnings"]))
        # Timings cover the whole run, not one stage.
        self.assertIn("total", result["timings"])
        self.assertIn("discovery", result["timings"])
        self.assertIsInstance(result["time_to_first_asset_ms"], int)
        # The description is grounded in what was actually found.
        self.assertIn("библиотеки — 1", result["summary"])
        self.assertTrue(result["campus_facts"])

    async def test_metadata_failure_marks_the_profile_partial(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"DATABASE_PATH": folder + "/t.sqlite3", "GROK_API_KEY": ""}), \
             patch("app.pipeline.thumbnail_hashes", new=AsyncMock(return_value={"hash_attempted": 0, "hash_succeeded": 0})):
            db.initialize()
            result = await build_profile(self.stub_sources(fail_metadata=True), RECORD)

        self.assertEqual(result["profile_status"], "partial")
        self.assertIn("commons_metadata", result["incomplete_sources"])
        # Every empty category must say "not checked", never "confirmed empty".
        self.assertTrue(all(v == "source_failed" for v in result["category_status"].values()))
        self.assertIn("Не проверено из-за недоступности источника", result["summary"])

    async def test_visual_layer_can_remove_a_candidate_end_to_end(self):
        verdicts = {
            "Nazarbayev University main building.jpg": {"scene": "campus_exterior", "confidence": 0.9, "note": ""},
            "Nazarbayev University library reading room.jpg": {"scene": "library", "confidence": 0.9, "note": ""},
            "Senate of Nazarbayev University.jpg": {"scene": "not_relevant", "confidence": 0.95, "note": "portraits"},
            "Astana skyline at night.jpg": {"scene": "city_not_campus", "confidence": 0.9, "note": ""},
        }

        async def fake_ask(client, key, image_url):
            return None

        async def fake_annotate(assets, *, deadline=None):
            stats = {"available": True, "model": "stub", "checked": 0, "from_cache": 0, "failed": 0,
                     "rejected": 0, "confirmed": 0, "conflict": 0, "vision_only": 0, "elapsed_ms": 1}
            for asset in assets:
                verdict = verdicts.get(asset["title"])
                if not verdict:
                    continue
                stats["checked"] += 1
                agreement = vision.reconcile(asset, verdict)
                for name in ("rejected", "confirmed", "conflict", "vision_only"):
                    if agreement.startswith(name):
                        stats[name] += 1
            return stats

        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"DATABASE_PATH": folder + "/t.sqlite3"}), \
             patch("app.pipeline.thumbnail_hashes", new=AsyncMock(return_value={"hash_attempted": 4, "hash_succeeded": 4})), \
             patch("app.pipeline.vision.annotate", new=fake_annotate):
            db.initialize()
            result = await build_profile(self.stub_sources(), RECORD)

        titles = [a["title"] for a in result["assets"]]
        self.assertNotIn("Senate of Nazarbayev University.jpg", titles)
        self.assertEqual(len(result["rejected_by_vision"]), 1)
        self.assertEqual(result["unclassified_count"], 0)
        # A dropped candidate must not leave the internal flag in the payload.
        self.assertTrue(all("drop" not in a for a in result["assets"]))


class DescriptionTests(unittest.TestCase):
    """Case requirement 7: describe the campus from sources, or say nothing."""

    def test_description_names_found_objects_and_separates_gap_reasons(self):
        institution = institution_summary(RECORD)
        assets = [
            {"category": "library", "title": "Main library.jpg", "captured_at": "2015-05-01",
             "published_at": None, "source_url": "https://commons.wikimedia.org/1",
             "license": "CC BY-SA 4.0", "author": "A"},
            {"category": "campus", "title": "Block 7.jpg", "captured_at": "2021-09-01",
             "published_at": None, "source_url": "https://commons.wikimedia.org/2",
             "license": "CC BY 4.0", "author": "B"},
        ]
        counts = {"campus": 1, "library": 1, "dormitory": 0, "classroom": 0,
                  "sports": 0, "laboratories": 0, "student_life": 0, "city": 0}
        status = {**{k: "has_results" if v else "empty_confirmed" for k, v in counts.items()},
                  "dormitory": "source_failed"}
        result = describe_campus(institution, assets, counts, status)
        self.assertIn("библиотеки — 1", result["text"])
        self.assertIn("2015–2021", result["text"])
        # A source that died is reported separately from a confirmed absence.
        self.assertIn("Не проверено из-за недоступности источника: общежития", result["text"])
        confirmed_empty = result["text"].split("Проверено и не найдено открытых материалов: ")[1]
        self.assertNotIn("общежития", confirmed_empty.split(".")[0])
        self.assertEqual({fact["category"] for fact in result["facts"]}, {"campus", "library"})

    def test_no_material_means_no_invented_description(self):
        institution = institution_summary(RECORD)
        counts = dict.fromkeys(
            ("campus", "dormitory", "classroom", "library", "sports", "laboratories", "student_life", "city"), 0)
        result = describe_campus(institution, [], counts, dict.fromkeys(counts, "empty_confirmed"))
        self.assertIn("не удалось", result["text"])
        self.assertEqual(result["facts"], [])


if __name__ == "__main__":
    unittest.main()


class StructuredEvidenceTests(unittest.TestCase):
    """v0.7: categories come from structured claims first, keywords last."""

    def setUp(self):
        self.institution = institution_summary(RECORD)
        self.institution["campus_coordinates"] = {"lat": 51.09, "lon": 71.40}

    def test_wikidata_type_maps_to_category_and_skips_non_places(self):
        self.assertEqual(building_category(["residence hall", "building"]), "dormitory")
        self.assertEqual(building_category(["academic library"]), "library")
        self.assertEqual(building_category(["sports venue"]), "sports")
        self.assertEqual(building_category(["university building"]), "campus")
        self.assertIsNone(building_category(["faculty", "research institute"]))
        self.assertIsNone(building_category(["space telescope"]))

    def test_only_open_licences_pass(self):
        for name in ("CC BY-SA 4.0", "CC BY 2.0", "CC0", "Public domain", "CC BY-SA 3.0 de", "GFDL", "PD-US"):
            self.assertTrue(open_license(name), name)
        for name in ("", "All rights reserved", "Fair use", "Copyrighted free use?"):
            self.assertFalse(open_license(name), name)

    def test_sparql_rows_are_parsed_into_commons_titles(self):
        rows = parse_building_rows({"results": {"bindings": [
            {"b": {"value": "http://www.wikidata.org/entity/Q1"}, "bLabel": {"value": "Main Library"},
             "img": {"value": "http://commons.wikimedia.org/wiki/Special:FilePath/Main%20Library_2020.jpg"},
             "types": {"value": "academic library|building"}},
            {"b": {"value": "http://www.wikidata.org/entity/Q2"}, "img": {"value": "not a file"}},
        ]}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file"], "File:Main Library 2020.jpg")
        self.assertEqual(rows[0]["types"], ["academic library", "building"])

    def test_wikidata_building_sets_category_even_without_keywords(self):
        building = {"qid": "Q9", "label": "Block 7", "types": ["residence hall"], "category": "dormitory"}
        item = commons_asset(page("IMG_2041.jpg"), self.institution, "wikidata_building", {"building": building})
        self.assertEqual(item["category"], "dormitory")
        self.assertIn("wikidata_type", {e["kind"] for e in item["evidence"]})

    def test_geotag_alone_does_not_make_a_campus_photo(self):
        near = page("Some street view.jpg")
        near["coordinates"] = [{"lat": 51.0901, "lon": 71.4001}]
        self.assertIsNone(commons_asset(near, self.institution, "geo"))
        named = page("Nazarbayev University building in winter.jpg")
        named["coordinates"] = [{"lat": 51.0901, "lon": 71.4001}]
        item = commons_asset(named, self.institution, "geo")
        kinds = {e["kind"] for e in item["evidence"]}
        self.assertIn("geo_near", kinds)
        self.assertIn("name_in_text", kinds)
        self.assertEqual(evidence_level(item), 2)

    def test_non_open_licence_is_rejected_for_structured_hits_too(self):
        building = {"qid": "Q9", "label": "Block 7", "types": ["residence hall"], "category": "dormitory"}
        self.assertIsNone(commons_asset(page("Block 7.jpg", "All rights reserved"), self.institution,
                                        "wikidata_building", {"building": building}))

    def test_disagreeing_vision_does_not_count_as_support(self):
        asset = {"evidence": [{"kind": "category"}, {"kind": "vision", "supports": False}]}
        self.assertEqual(evidence_level(asset), 1)


class VisionProviderTests(unittest.TestCase):
    def test_groq_is_used_when_only_groq_key_exists(self):
        with patch.dict(os.environ, {"GROK_API_KEY": "", "GROQ_API_KEY": "k", "GROQ_VISION_MODEL": ""}):
            os.environ.pop("GROQ_VISION_MODEL")
            self.assertEqual(vision.provider(), "groq")
            self.assertEqual(vision.model_name(), vision.DEFAULT_GROQ_MODEL)
        with patch.dict(os.environ, {"GROK_API_KEY": "x", "GROQ_API_KEY": "k"}):
            self.assertEqual(vision.provider(), "xai")
        with patch.dict(os.environ, {"GROK_API_KEY": "", "GROQ_API_KEY": ""}):
            self.assertFalse(vision.configured())

    def test_image_is_sent_as_bytes_not_hotlinked(self):
        ref = vision._image_ref({"_thumb": b"\xff\xd8\xff\xe0rest"})
        self.assertTrue(ref.startswith("data:image/jpeg;base64,"))
        self.assertIsNone(vision._image_ref({"image_url": "https://upload.wikimedia.org/x.jpg"}))


class CollectionLeakTests(unittest.TestCase):
    """Kyoto regression: 24 scans of a Paris map were filed as 'library'."""

    def test_scans_and_maps_are_not_campus_photos(self):
        self.assertFalse(valid_title("File:Turgot map Paris KU 07.jpg"))
        self.assertFalse(valid_title("File:Chateau de la Tournelle plan.jpg"))
        self.assertFalse(valid_title("File:Campus_map_2020.png"))
        self.assertTrue(valid_title("File:Kyoto University Library 2018 a.jpg"))
        self.assertTrue(valid_title("File:Planetarium building.jpg"))

    def test_collection_subcategories_are_not_followed(self):
        self.assertFalse(usable_subcategory("Category:Turgot map of Paris, Kyoto University Library copy"))
        self.assertFalse(usable_subcategory("Category:People of Kyoto University"))
        self.assertTrue(usable_subcategory("Category:Kyoto University Library"))
        self.assertTrue(usable_subcategory("Category:Buildings of Kyoto University"))
        self.assertTrue(usable_subcategory("Category:Department of Earth Sciences building"))


class MosaicVisionTests(unittest.TestCase):
    def test_grid_parsing_keeps_tiles_independent(self):
        raw = '{"1":{"scene":"library","confidence":0.9,"note":"x"},"2":{"scene":"made_up"},"3":"bad"}'
        verdicts = vision.parse_grid(raw, 4)
        self.assertEqual(verdicts[0]["scene"], "library")
        self.assertEqual(verdicts[1:], [None, None, None])
        self.assertEqual(vision.parse_grid("not json", 2), [None, None])

    def test_mosaic_is_a_single_jpeg(self):
        import io
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (400, 300), (200, 10, 10)).save(buf, "JPEG")
        mosaic = vision.build_mosaic([buf.getvalue()] * 3)
        with Image.open(io.BytesIO(mosaic)) as im:
            self.assertEqual(im.size, (vision.GRID_TILE * 2, vision.GRID_TILE * 2))
        self.assertIsNone(vision.build_mosaic([b"not an image"]))


class PeopleAreNotPlacesTests(unittest.TestCase):
    def test_portraits_and_visits_are_not_campus_views(self):
        for title in ("Prof. Emeritus Dr. Mikio UMEDA of Kyoto University.jpg",
                      "PM Modi raises concern during Kyoto University visit.jpg"):
            self.assertEqual(classify(title)[0], "unknown", title)
        self.assertEqual(classify("Kyoto University Clock Tower building.jpg")[0], "campus")
        self.assertEqual(classify("Hydrology building.jpg")[0], "campus")
