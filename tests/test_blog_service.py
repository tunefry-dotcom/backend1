"""Unit tests for app.modules.blog.service — slug generation, row<->response
mapping, and the query/write helpers backing the Tunefry Daily blog feature.
Runs fully offline against tests.fakes; no real Supabase/R2 calls."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.modules.blog import service as blog_service
from tests.fakes import FakeClient, FakeQuery


class SlugifyTests(unittest.TestCase):
    def test_lowercases_and_hyphenates(self):
        slug = blog_service.slugify("My First Song Release!!")
        self.assertTrue(slug.startswith("my-first-song-release-"))

    def test_truncates_long_titles_to_60_chars_before_the_suffix(self):
        slug = blog_service.slugify("x" * 200)
        base, suffix = slug.rsplit("-", 1)
        self.assertLessEqual(len(base), 60)
        self.assertEqual(len(suffix), 6)

    def test_empty_or_symbol_only_title_falls_back_to_article(self):
        slug = blog_service.slugify("!!!")
        self.assertTrue(slug.startswith("article-"))

    def test_two_calls_produce_different_slugs(self):
        self.assertNotEqual(blog_service.slugify("Same Title"), blog_service.slugify("Same Title"))


class ResponseMappingTests(unittest.TestCase):
    def test_to_summary_prefers_final_title_over_original(self):
        row = {
            "id": "p1", "slug": "s1", "category": "informative", "author_type": "artist",
            "final_title": "Edited Title", "original_title": "Raw Title",
            "cover_image_keys": ["blog/a.jpg", "blog/b.jpg"],
        }
        summary = blog_service.to_summary(row)
        self.assertEqual(summary["title"], "Edited Title")
        self.assertEqual(summary["cover_image_key"], "blog/a.jpg")

    def test_to_summary_falls_back_to_original_title_when_unreviewed(self):
        row = {"id": "p1", "slug": "s1", "category": "informative", "author_type": "artist",
               "original_title": "Raw Title", "cover_image_keys": []}
        summary = blog_service.to_summary(row)
        self.assertEqual(summary["title"], "Raw Title")
        self.assertIsNone(summary["cover_image_key"])

    def test_to_detail_prefers_final_body(self):
        row = {"id": "p1", "slug": "s1", "category": "informative", "author_type": "artist",
               "final_body": "Edited body", "original_body": "Raw body", "cover_image_keys": []}
        self.assertEqual(blog_service.to_detail(row)["body"], "Edited body")

    def test_to_admin_exposes_both_original_and_final_copy(self):
        row = {
            "id": "p1", "slug": "s1", "author_type": "artist", "author_email": "a@example.com",
            "category": "informative", "status": "pending",
            "original_title": "Raw", "original_body": "Raw body",
            "cover_image_keys": [],
        }
        admin = blog_service.to_admin(row)
        self.assertEqual(admin["original_title"], "Raw")
        self.assertIsNone(admin["final_title"])
        self.assertEqual(admin["status"], "pending")


class CreatePendingPostTests(unittest.TestCase):
    def test_inserts_artist_authored_pending_row(self):
        # FakeQuery.insert() doesn't mutate _data (matches every other test's
        # treatment of insert as a write-only, non-echoing call) — so the
        # DB-assigned row (with its "id") must be pre-seeded to model what a
        # real insert().execute() would hand back.
        db_row = {"id": "post-1", "slug": "placeholder", "category": "song_release",
                  "author_type": "artist", "original_title": "My Draft",
                  "cover_image_keys": ["blog/cover.jpg"]}
        client = FakeClient({"blog_posts": FakeQuery(data=[db_row])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.create_pending_post(
                author_email="artist@example.com",
                author_name="Artist Name",
                title="My Draft",
                body="Draft body",
                category="song_release",
                cover_image_key="blog/cover.jpg",
            )
        self.assertEqual(result["title"], "My Draft")
        inserted = client.table("blog_posts").last_insert
        self.assertEqual(inserted["author_type"], "artist")
        self.assertEqual(inserted["status"], "pending")
        self.assertEqual(inserted["cover_image_keys"], ["blog/cover.jpg"])
        self.assertTrue(inserted["slug"].startswith("my-draft-"))


class CreateTunefryPostTests(unittest.TestCase):
    def test_inserts_pre_approved_published_row_with_up_to_two_images(self):
        db_row = {"id": "post-2", "slug": "placeholder", "author_type": "tunefry",
                  "category": "informative", "status": "approved",
                  "cover_image_keys": ["blog/one.jpg", "blog/two.jpg"],
                  "published_at": "2026-10-01T00:00:00+00:00"}
        client = FakeClient({"blog_posts": FakeQuery(data=[db_row])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.create_tunefry_post(
                final_title="Tunefry Post",
                final_body="Body text",
                category="informative",
                cover_image_keys=["blog/one.jpg", "blog/two.jpg"],
            )
        self.assertEqual(result["status"], "approved")
        self.assertIsNotNone(result["published_at"])
        inserted = client.table("blog_posts").last_insert
        self.assertEqual(inserted["author_type"], "tunefry")
        self.assertEqual(inserted["cover_image_keys"], ["blog/one.jpg", "blog/two.jpg"])


class ReviewPostTests(unittest.TestCase):
    def _client_with_row(self, row: dict) -> FakeClient:
        return FakeClient({"blog_posts": FakeQuery(data=[row])})

    def test_unknown_post_id_raises_not_found(self):
        client = FakeClient({"blog_posts": FakeQuery(data=[])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            with self.assertRaises(blog_service.ReviewError) as ctx:
                blog_service.review_post("missing", status_value="approved", final_title="T", final_body="B")
        self.assertEqual(str(ctx.exception), "not_found")

    def test_approve_without_final_copy_raises(self):
        client = self._client_with_row({"id": "p1", "status": "pending"})
        with patch.object(blog_service, "get_service_client", return_value=client):
            with self.assertRaises(blog_service.ReviewError) as ctx:
                blog_service.review_post("p1", status_value="approved")
        self.assertEqual(str(ctx.exception), "final_copy_required")

    def test_decline_without_note_raises(self):
        client = self._client_with_row({"id": "p1", "status": "pending"})
        with patch.object(blog_service, "get_service_client", return_value=client):
            with self.assertRaises(blog_service.ReviewError) as ctx:
                blog_service.review_post("p1", status_value="declined", admin_note="")
        self.assertEqual(str(ctx.exception), "admin_note_required")

    def test_approve_success_sets_final_copy_and_published_at(self):
        client = self._client_with_row({"id": "p1", "status": "pending", "author_type": "artist",
                                         "category": "informative", "slug": "s1"})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.review_post(
                "p1", status_value="approved", final_title="Edited", final_body="Edited body"
            )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["final_title"], "Edited")
        self.assertIsNotNone(result["published_at"])

    def test_decline_success_records_note(self):
        client = self._client_with_row({"id": "p1", "status": "pending", "author_type": "artist",
                                         "category": "informative", "slug": "s1"})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.review_post("p1", status_value="declined", admin_note="Not on-brand")
        self.assertEqual(result["status"], "declined")
        self.assertEqual(result["admin_note"], "Not on-brand")


class SetFlagsTests(unittest.TestCase):
    def test_unknown_post_raises_not_found(self):
        client = FakeClient({"blog_posts": FakeQuery(data=[])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            with self.assertRaises(blog_service.ReviewError) as ctx:
                blog_service.set_flags("missing", is_featured=True, is_popular=None)
        self.assertEqual(str(ctx.exception), "not_found")

    def test_unapproved_post_raises_not_approved(self):
        client = FakeClient({"blog_posts": FakeQuery(data=[{"id": "p1", "status": "pending"}])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            with self.assertRaises(blog_service.ReviewError) as ctx:
                blog_service.set_flags("p1", is_featured=True, is_popular=None)
        self.assertEqual(str(ctx.exception), "not_approved")

    def test_approved_post_flags_are_updated(self):
        row = {"id": "p1", "status": "approved", "author_type": "tunefry", "category": "informative",
               "slug": "s1", "cover_image_keys": []}
        client = FakeClient({"blog_posts": FakeQuery(data=[row])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.set_flags("p1", is_featured=True, is_popular=False)
        self.assertTrue(result["is_featured"])
        self.assertFalse(result["is_popular"])


class ListPublicPostsTests(unittest.TestCase):
    def test_pagination_math_over_a_flat_result_set(self):
        rows = [{"id": str(i), "slug": f"s{i}", "category": "informative", "author_type": "tunefry",
                 "cover_image_keys": []} for i in range(25)]
        client = FakeClient({"blog_posts": FakeQuery(data=rows)})
        with patch.object(blog_service, "get_service_client", return_value=client):
            page1 = blog_service.list_public_posts(category=None, page=1, per_page=10)
            page3 = blog_service.list_public_posts(category=None, page=3, per_page=10)
        self.assertEqual(page1["total"], 25)
        self.assertEqual(page1["total_pages"], 3)
        self.assertEqual(len(page1["posts"]), 10)
        self.assertEqual(len(page3["posts"]), 5)

    def test_per_page_is_capped_at_15(self):
        rows = [{"id": str(i), "slug": f"s{i}", "category": "informative", "author_type": "tunefry",
                 "cover_image_keys": []} for i in range(20)]
        client = FakeClient({"blog_posts": FakeQuery(data=rows)})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.list_public_posts(category=None, page=1, per_page=999)
        self.assertEqual(result["per_page"], 15)
        self.assertEqual(len(result["posts"]), 15)


class GetPostBySlugTests(unittest.TestCase):
    def test_returns_detail_when_found(self):
        row = {"id": "p1", "slug": "my-post", "category": "informative", "author_type": "tunefry",
               "final_body": "Body", "cover_image_keys": ["blog/a.jpg"]}
        client = FakeClient({"blog_posts": FakeQuery(data=[row])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.get_post_by_slug("my-post")
        self.assertEqual(result["body"], "Body")

    def test_returns_none_when_not_found(self):
        client = FakeClient({"blog_posts": FakeQuery(data=[])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            self.assertIsNone(blog_service.get_post_by_slug("missing"))


class ListRelatedPostsTests(unittest.TestCase):
    def test_excludes_current_post_and_respects_limit(self):
        # list_related_posts queries the same table twice; FakeClient caches
        # one FakeQuery per table name, so both calls see this same _data.
        # The current post must be first so current_rows[0] resolves to it.
        rows = [
            {"id": "current", "slug": "current-slug", "category": "informative", "author_type": "tunefry",
             "cover_image_keys": []},
            {"id": "r1", "slug": "r1", "category": "informative", "author_type": "tunefry", "cover_image_keys": []},
            {"id": "r2", "slug": "r2", "category": "informative", "author_type": "tunefry", "cover_image_keys": []},
            {"id": "r3", "slug": "r3", "category": "informative", "author_type": "tunefry", "cover_image_keys": []},
        ]
        client = FakeClient({"blog_posts": FakeQuery(data=rows)})
        with patch.object(blog_service, "get_service_client", return_value=client):
            related = blog_service.list_related_posts("current-slug", limit=2)
        self.assertEqual(len(related), 2)
        self.assertNotIn("current", [r["id"] for r in related])

    def test_unknown_slug_returns_empty_list(self):
        client = FakeClient({"blog_posts": FakeQuery(data=[])})
        with patch.object(blog_service, "get_service_client", return_value=client):
            self.assertEqual(blog_service.list_related_posts("missing"), [])


class ListMyPostsTests(unittest.TestCase):
    def test_maps_rows_to_summaries(self):
        rows = [{"id": "p1", "slug": "s1", "category": "informative", "author_type": "artist",
                 "cover_image_keys": []}]
        client = FakeClient({"blog_posts": FakeQuery(data=rows)})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.list_my_posts("artist@example.com", status_filter="approved")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "p1")


class ListAdminPostsTests(unittest.TestCase):
    def test_maps_rows_to_admin_shape(self):
        rows = [{"id": "p1", "slug": "s1", "author_type": "artist", "category": "informative",
                 "status": "pending", "cover_image_keys": []}]
        client = FakeClient({"blog_posts": FakeQuery(data=rows)})
        with patch.object(blog_service, "get_service_client", return_value=client):
            result = blog_service.list_admin_posts("artist", status_filter="pending")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
