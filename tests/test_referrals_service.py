"""Unit tests for app.modules.referrals.service — the referral-code and
commission-crediting logic. These mock the Supabase client (via tests.fakes)
so they run offline with no live database.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from app.modules.billing.plans import Plan
from app.modules.referrals import service as referrals_service
from tests.fakes import FakeClient, FakeQuery


class GenerateReferralCodeTests(unittest.TestCase):
    def test_pure_deterministic_no_dashes(self):
        user_id = "12345678-aaaa-bbbb-cccc-dddddddddddd"
        code = referrals_service.generate_referral_code(user_id)
        self.assertEqual(code, "TF12345678")

    def test_same_id_always_yields_same_code(self):
        user_id = "abcdef01-2345-6789-abcd-ef0123456789"
        self.assertEqual(
            referrals_service.generate_referral_code(user_id),
            referrals_service.generate_referral_code(user_id),
        )

    def test_different_ids_yield_different_codes(self):
        a = referrals_service.generate_referral_code("11111111-0000-0000-0000-000000000000")
        b = referrals_service.generate_referral_code("22222222-0000-0000-0000-000000000000")
        self.assertNotEqual(a, b)


class ResolveReferrerTests(unittest.TestCase):
    def test_unknown_code_returns_none(self):
        client = FakeClient({"profiles": FakeQuery(data=[])})
        with patch.object(referrals_service, "get_service_client", return_value=client):
            self.assertIsNone(referrals_service.resolve_referrer("NOPE"))

    def test_blank_code_returns_none_without_querying(self):
        with patch.object(referrals_service, "get_service_client") as mock_get:
            self.assertIsNone(referrals_service.resolve_referrer(""))
            mock_get.assert_not_called()

    def test_known_code_resolves_to_referrer_id(self):
        client = FakeClient({"profiles": FakeQuery(data=[{"user_id": "referrer-1"}])})
        with patch.object(referrals_service, "get_service_client", return_value=client):
            self.assertEqual(referrals_service.resolve_referrer("tf12345678"), "referrer-1")


class CreditReferralTests(unittest.TestCase):
    def _client_with_referral(self, referrer_id: str, referrer_email: str) -> FakeClient:
        client = FakeClient({
            "referrals": FakeQuery(data=[{"referrer_user_id": referrer_id}]),
            "referral_earnings": FakeQuery(),
        })
        client.auth.admin.register_email(referrer_id, referrer_email)
        return client

    def test_no_referral_row_is_a_noop(self):
        client = FakeClient({"referrals": FakeQuery(data=[])})
        with patch.object(referrals_service, "get_service_client", return_value=client), \
             patch.object(referrals_service, "recompute_balance") as mock_recompute:
            referrals_service.credit_referral(referred_user_id="u1", plan=Plan.SINGLE_ARTIST, source="payment")
            mock_recompute.assert_not_called()

    def test_free_plan_credits_nothing(self):
        client = self._client_with_referral("referrer-1", "referrer@example.com")
        with patch.object(referrals_service, "get_service_client", return_value=client), \
             patch.object(referrals_service, "recompute_balance") as mock_recompute:
            referrals_service.credit_referral(referred_user_id="u1", plan=Plan.FREE, source="payment")
            mock_recompute.assert_not_called()
            self.assertFalse(hasattr(client.table("referral_earnings"), "last_insert"))

    def test_paid_plan_credits_ten_percent_and_recomputes_referrer_balance(self):
        client = self._client_with_referral("referrer-1", "referrer@example.com")
        with patch.object(referrals_service, "get_service_client", return_value=client), \
             patch.object(referrals_service, "recompute_balance") as mock_recompute:
            referrals_service.credit_referral(
                referred_user_id="u1", plan=Plan.SINGLE_ARTIST, source="payment", payment_ref="pay_123",
            )

        # Single Artist plan is ₹1599 -> 10% = ₹159.90
        inserted = client.table("referral_earnings").last_insert
        self.assertEqual(Decimal(inserted["amount"]), Decimal("159.90"))
        self.assertEqual(inserted["referrer_email"], "referrer@example.com")
        self.assertEqual(inserted["referred_user_id"], "u1")
        self.assertEqual(inserted["source"], "payment")
        self.assertEqual(inserted["payment_ref"], "pay_123")
        mock_recompute.assert_called_once_with("referrer@example.com")

    def test_missing_referrer_email_skips_credit(self):
        client = FakeClient({
            "referrals": FakeQuery(data=[{"referrer_user_id": "referrer-1"}]),
            "referral_earnings": FakeQuery(),
        })
        # No email registered for referrer-1 -> get_user_by_id returns user=None
        with patch.object(referrals_service, "get_service_client", return_value=client), \
             patch.object(referrals_service, "recompute_balance") as mock_recompute:
            referrals_service.credit_referral(referred_user_id="u1", plan=Plan.STARTER, source="admin")
            mock_recompute.assert_not_called()

    def test_exceptions_are_swallowed_never_raised(self):
        with patch.object(referrals_service, "get_service_client", side_effect=RuntimeError("db down")):
            try:
                referrals_service.credit_referral(referred_user_id="u1", plan=Plan.LABEL, source="admin")
            except Exception as exc:  # pragma: no cover - failure path
                self.fail(f"credit_referral must never raise, got {exc!r}")


class RecordReferralTests(unittest.TestCase):
    def test_insert_failure_is_swallowed(self):
        with patch.object(referrals_service, "get_service_client", side_effect=RuntimeError("db down")):
            try:
                referrals_service.record_referral("referrer-1", "referred-1", "TF12345678")
            except Exception as exc:  # pragma: no cover
                self.fail(f"record_referral must never raise, got {exc!r}")


if __name__ == "__main__":
    unittest.main()
