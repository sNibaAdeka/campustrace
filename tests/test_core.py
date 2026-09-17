import os
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

from app import db, vision
from app.pipeline import (
    classify, commons_asset, deduplicate, describe_campus, institution_summary,
    known_name_in_text, latest_student_count, thumbnail_hashes, _hashable_host,
)
from app.integrations import Sources
from app.main import profile as get_profile_endpoint
from app.atlas import build_atlas, isochrone
from app.discovery import suggest
from app.voices import _relevant, _groq_web_search
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
        self.assertIn("Не проверено из-за недоступности источника: общежития", result["text"])
        self.assertNotIn("общежития.", result["text"].split("Проверено и не найдено")[-1].split(".")[0] + ".")
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
