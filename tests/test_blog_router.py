"""Endpoint-contract tests for the Tunefry Daily blog routes — both the public
/blog/* router (app/modules/blog/router.py) and the admin /admin/blog/* routes
(app/modules/admin/router.py) — via FastAPI's TestClient. Auth and the
Supabase client are faked; app.modules.blog.ai.rewrite_article is patched to a
deterministic stub so no real OpenAI call is ever made."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.modules.admin import router as admin_router
from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.blog import ai as blog_ai
from app.modules.blog import credits as blog_credits
from app.modules.blog import service as blog_service
from tests.fakes import FakeClient, FakeQuery


class PublicBlogEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            id="user-1", email="artist@example.com", role="authenticated",
            artist_name="Artist Name",
        )

    def tearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def test_categories_returns_the_fixed_four(self):
        resp = self.client.get("/blog/categories")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 4)

    def test_submit_without_credits_returns_402(self):
        fake = FakeClient({
            "blog_publish_credits": FakeQuery(
                data=[{"user_email": "artist@example.com", "free_article_used": True, "credits_remaining": 0}]
            ),
        })
        with patch.object(blog_credits, "get_service_client", return_value=fake):
            resp = self.client.post("/blog/submit", json={
                "title": "My Draft", "body": "Draft body", "category": "informative",
                "cover_image_key": "blog/cover.jpg",
            })
        self.assertEqual(resp.status_code, 402)

    def test_submit_with_free_article_available_succeeds(self):
        credits_client = FakeClient({"blog_publish_credits": FakeQuery(data=[])})
        db_row = {
            "id": "post-1", "slug": "my-draft-abc123", "category": "informative",
            "author_type": "artist", "author_name": "Artist Name",
            "original_title": "My Draft", "original_body": "Draft body",
            "cover_image_keys": ["blog/cover.jpg"], "is_featured": False, "is_popular": False,
        }
        posts_client = FakeClient({"blog_posts": FakeQuery(data=[db_row])})
        with patch.object(blog_credits, "get_service_client", return_value=credits_client), \
             patch.object(blog_service, "get_service_client", return_value=posts_client):
            resp = self.client.post("/blog/submit", json={
                "title": "My Draft", "body": "Draft body", "category": "informative",
                "cover_image_key": "blog/cover.jpg",
            })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["title"], "My Draft")
        self.assertEqual(body["author_type"], "artist")

    def test_get_post_by_slug_404s_when_not_found_or_not_approved(self):
        fake = FakeClient({"blog_posts": FakeQuery(data=[])})
        with patch.object(blog_service, "get_service_client", return_value=fake):
            resp = self.client.get("/blog/posts/some-pending-slug")
        self.assertEqual(resp.status_code, 404)

    def test_requires_auth_for_submit(self):
        app.dependency_overrides.pop(get_current_user, None)
        resp = self.client.post("/blog/submit", json={
            "title": "T", "body": "B", "category": "informative", "cover_image_key": "blog/c.jpg",
        })
        self.assertEqual(resp.status_code, 401)


class AdminBlogEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._prev_secret = settings.admin_secret
        settings.admin_secret = "test-admin-secret"
        self.headers = {"X-Admin-Secret": "test-admin-secret"}

    def tearDown(self):
        settings.admin_secret = self._prev_secret

    def test_wrong_secret_is_forbidden(self):
        resp = self.client.patch(
            "/admin/blog/p1",
            json={"status": "declined", "admin_note": "No"},
            headers={"X-Admin-Secret": "wrong"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_decline_without_note_returns_400(self):
        row = {"id": "p1", "status": "pending", "author_email": "artist@example.com"}
        fake = FakeClient({"blog_posts": FakeQuery(data=[row])})
        with patch.object(blog_service, "get_service_client", return_value=fake):
            resp = self.client.patch(
                "/admin/blog/p1", json={"status": "declined", "admin_note": ""}, headers=self.headers,
            )
        self.assertEqual(resp.status_code, 400)

    def test_approve_without_final_copy_returns_400(self):
        row = {"id": "p1", "status": "pending", "author_email": "artist@example.com"}
        fake = FakeClient({"blog_posts": FakeQuery(data=[row])})
        with patch.object(blog_service, "get_service_client", return_value=fake):
            resp = self.client.patch(
                "/admin/blog/p1", json={"status": "approved", "admin_note": ""}, headers=self.headers,
            )
        self.assertEqual(resp.status_code, 400)

    def test_approve_success_sets_published_at_and_notifies_artist(self):
        row = {
            "id": "p1", "status": "pending", "author_type": "artist", "author_email": "artist@example.com",
            "category": "informative", "slug": "s1", "cover_image_keys": [], "original_title": "Raw",
            "original_body": "Raw body", "admin_note": "",
        }
        blog_client = FakeClient({"blog_posts": FakeQuery(data=[row])})
        notif_client = FakeClient({"notifications": FakeQuery(data=[])})
        with patch.object(blog_service, "get_service_client", return_value=blog_client), \
             patch.object(admin_router, "get_service_client", return_value=notif_client):
            resp = self.client.patch(
                "/admin/blog/p1",
                json={"status": "approved", "final_title": "Edited", "final_body": "Edited body"},
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "approved")
        self.assertIsNotNone(body["published_at"])
        notified = notif_client.table("notifications").last_insert
        self.assertEqual(notified["recipient_email"], "artist@example.com")

    def test_rewrite_uses_patched_ai_stub_never_calls_real_openai(self):
        async def _stub_rewrite(title, body):
            return f"Rewritten: {title}", f"Rewritten: {body}"

        with patch.object(blog_ai, "rewrite_article", side_effect=_stub_rewrite):
            resp = self.client.post(
                "/admin/blog/rewrite",
                json={"title": "Raw Title", "body": "Raw body"},
                headers=self.headers,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["title"], "Rewritten: Raw Title")
        self.assertEqual(body["body"], "Rewritten: Raw body")


if __name__ == "__main__":
    unittest.main()
