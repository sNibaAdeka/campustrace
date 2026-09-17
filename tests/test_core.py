import os
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

from app import db
from app.pipeline import commons_asset, deduplicate, institution_summary, latest_student_count, thumbnail_hashes, _hashable_host
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


if __name__ == "__main__":
    unittest.main()
