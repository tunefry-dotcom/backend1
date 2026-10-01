"""Unit tests for app.modules.blog.credits — the publish-credit ledger
(one free article per user for life, then paid packs). Mocks the Supabase
client via tests.fakes so this runs offline with no live database."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException

from app.modules.blog import credits as blog_credits
from tests.fakes import FakeClient, FakeQuery


class ConsumePublishCreditTests(unittest.TestCase):
    def test_first_ever_call_consumes_the_free_article(self):
        client = FakeClient({"blog_publish_credits": FakeQuery(data=[])})
        with patch.object(blog_credits, "get_service_client", return_value=client):
            blog_credits.consume_publish_credit("artist@example.com")

        row = client.table("blog_publish_credits")
        self.assertEqual(row.last_upsert["free_article_used"], True)
        self.assertEqual(row.last_upsert["credits_remaining"], 0)

    def test_second_call_with_no_paid_credits_raises_402(self):
        client = FakeClient({
            "blog_publish_credits": FakeQuery(
                data=[{"user_email": "artist@example.com", "free_article_used": True, "credits_remaining": 0}]
            )
        })
        with patch.object(blog_credits, "get_service_client", return_value=client):
            with self.assertRaises(HTTPException) as ctx:
                blog_credits.consume_publish_credit("artist@example.com")
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertEqual(ctx.exception.detail["error"], "no_publish_credits")

    def test_call_with_remaining_paid_credits_decrements_by_one(self):
        client = FakeClient({
            "blog_publish_credits": FakeQuery(
                data=[{"user_email": "artist@example.com", "free_article_used": True, "credits_remaining": 3}]
            )
        })
        with patch.object(blog_credits, "get_service_client", return_value=client):
            blog_credits.consume_publish_credit("artist@example.com")

        row = client.table("blog_publish_credits")
        self.assertEqual(row.last_upsert["free_article_used"], True)
        self.assertEqual(row.last_upsert["credits_remaining"], 2)

    def test_free_article_then_immediate_second_call_without_credits_is_blocked(self):
        """Sequential calls against the same underlying row: the fake's
        upsert() merges into _data, so the second read reflects the first
        write — mirrors the real read-modify-write path in production."""
        client = FakeClient({"blog_publish_credits": FakeQuery(data=[])})
        with patch.object(blog_credits, "get_service_client", return_value=client):
            blog_credits.consume_publish_credit("artist@example.com")
            with self.assertRaises(HTTPException) as ctx:
                blog_credits.consume_publish_credit("artist@example.com")
        self.assertEqual(ctx.exception.status_code, 402)


class GetCreditStatusTests(unittest.TestCase):
    def test_no_row_means_free_article_available(self):
        client = FakeClient({"blog_publish_credits": FakeQuery(data=[])})
        with patch.object(blog_credits, "get_service_client", return_value=client):
            status = blog_credits.get_credit_status("new@example.com")
        self.assertEqual(status, {"free_article_used": False, "credits_remaining": 0, "can_publish": True})

    def test_free_used_and_no_credits_cannot_publish(self):
        client = FakeClient({
            "blog_publish_credits": FakeQuery(data=[{"free_article_used": True, "credits_remaining": 0}])
        })
        with patch.object(blog_credits, "get_service_client", return_value=client):
            status = blog_credits.get_credit_status("spent@example.com")
        self.assertFalse(status["can_publish"])

    def test_free_used_but_credits_remaining_can_publish(self):
        client = FakeClient({
            "blog_publish_credits": FakeQuery(data=[{"free_article_used": True, "credits_remaining": 5}])
        })
        with patch.object(blog_credits, "get_service_client", return_value=client):
            status = blog_credits.get_credit_status("paid@example.com")
        self.assertTrue(status["can_publish"])


class GrantCreditsTests(unittest.TestCase):
    def test_grants_on_top_of_existing_balance(self):
        client = FakeClient({
            "blog_publish_credits": FakeQuery(data=[{"free_article_used": True, "credits_remaining": 2}])
        })
        with patch.object(blog_credits, "get_service_client", return_value=client):
            result = blog_credits.grant_credits("artist@example.com", 20)
        self.assertEqual(result, {"free_article_used": True, "credits_remaining": 22})

    def test_grants_from_zero_when_no_existing_row(self):
        client = FakeClient({"blog_publish_credits": FakeQuery(data=[])})
        with patch.object(blog_credits, "get_service_client", return_value=client):
            result = blog_credits.grant_credits("brandnew@example.com", 1)
        self.assertEqual(result, {"free_article_used": False, "credits_remaining": 1})


class RecordPurchaseAndGrantTests(unittest.TestCase):
    def _client(self) -> FakeClient:
        return FakeClient({
            "blog_credit_purchases": FakeQuery(data=[]),
            "blog_publish_credits": FakeQuery(data=[{"free_article_used": True, "credits_remaining": 0}]),
        })

    def test_success_records_purchase_and_grants_credits(self):
        client = self._client()
        with patch.object(blog_credits, "get_service_client", return_value=client):
            result = blog_credits.record_purchase_and_grant(
                email="artist@example.com",
                pack="single",
                razorpay_order_id="order_1",
                razorpay_payment_id="pay_1",
                amount_inr=Decimal("49"),
                credits=1,
            )
        self.assertEqual(result, {"credited": True, "free_article_used": True, "credits_remaining": 1})
        purchase = client.table("blog_credit_purchases").last_insert
        self.assertEqual(purchase["razorpay_payment_id"], "pay_1")
        self.assertEqual(purchase["credits_granted"], 1)

    def test_duplicate_payment_id_is_rejected_with_409_and_grants_nothing(self):
        class _DupeInsertQuery(FakeQuery):
            def insert(self, row, *a, **k):
                raise RuntimeError('duplicate key value violates unique constraint "blog_credit_purchases_razorpay_payment_id_key"')

        client = FakeClient({
            "blog_credit_purchases": _DupeInsertQuery(),
            "blog_publish_credits": FakeQuery(data=[{"free_article_used": True, "credits_remaining": 0}]),
        })
        with patch.object(blog_credits, "get_service_client", return_value=client):
            with self.assertRaises(HTTPException) as ctx:
                blog_credits.record_purchase_and_grant(
                    email="artist@example.com",
                    pack="single",
                    razorpay_order_id="order_1",
                    razorpay_payment_id="pay_1",
                    amount_inr=Decimal("49"),
                    credits=1,
                )
        self.assertEqual(ctx.exception.status_code, 409)
        # Credits ledger was never touched.
        self.assertFalse(hasattr(client.table("blog_publish_credits"), "last_upsert"))

    def test_grant_failure_after_purchase_recorded_returns_credited_false_not_5xx(self):
        client = self._client()
        with patch.object(blog_credits, "get_service_client", return_value=client), \
             patch.object(blog_credits, "grant_credits", side_effect=RuntimeError("db down")), \
             self.assertLogs(blog_credits._log, level="ERROR") as logs:
            result = blog_credits.record_purchase_and_grant(
                email="artist@example.com",
                pack="bundle_20",
                razorpay_order_id="order_2",
                razorpay_payment_id="pay_2",
                amount_inr=Decimal("799"),
                credits=20,
            )
        self.assertEqual(result["credited"], False)
        self.assertIn("support_note", result)
        # The purchase row was still recorded despite the grant failure.
        self.assertIsNotNone(client.table("blog_credit_purchases").last_insert)
        self.assertTrue(any("needs manual reconciliation" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
