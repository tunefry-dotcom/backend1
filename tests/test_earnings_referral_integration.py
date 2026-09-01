"""Verifies recompute_balance() folds referral commissions into the same
wallet as song_stats earnings, and degrades gracefully if the referral
tables don't exist yet (pre-migration deployments)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.modules.earnings import service as earnings_service
from tests.fakes import FakeClient, FakeQuery


class RecomputeBalanceReferralTests(unittest.TestCase):
    def test_total_earned_includes_referral_earnings(self):
        client = FakeClient({
            "song_stats": FakeQuery(data=[{"revenue": "100.00"}, {"revenue": "50.00"}]),
            "artist_balances": FakeQuery(data=[{"total_withdrawn": "0"}]),
            "withdrawal_requests": FakeQuery(data=[]),
            "referral_earnings": FakeQuery(data=[{"amount": "159.90"}, {"amount": "40.00"}]),
        })
        with patch.object(earnings_service, "get_service_client", return_value=client):
            result = earnings_service.recompute_balance("artist@example.com")

        # 100 + 50 (song_stats) + 159.90 + 40 (referral_earnings) = 349.90
        self.assertEqual(result["total_earned"], 349.90)
        self.assertEqual(result["available_balance"], 349.90)

    def test_missing_referral_earnings_table_degrades_to_zero(self):
        client = FakeClient({
            "song_stats": FakeQuery(data=[{"revenue": "100.00"}]),
            "artist_balances": FakeQuery(data=[{"total_withdrawn": "0"}]),
            "withdrawal_requests": FakeQuery(data=[]),
        })

        def table(name):
            if name == "referral_earnings":
                raise RuntimeError("relation \"referral_earnings\" does not exist")
            return client._tables.setdefault(name, FakeQuery())

        client.table = table  # simulate migration 0010 not yet applied

        with patch.object(earnings_service, "get_service_client", return_value=client):
            result = earnings_service.recompute_balance("artist@example.com")

        self.assertEqual(result["total_earned"], 100.0)

    def test_brand_new_referrer_with_no_prior_balance_row_does_not_raise(self):
        """Regression test: a referrer with zero prior artist_balances rows
        (e.g. someone who only ever referred, never sold a song) used to
        crash recompute_balance — .maybe_single() raises on PostgREST's 406
        for zero matching rows rather than returning None. This is exactly
        the state a first-time referral commission recipient is in.
        """
        client = FakeClient({
            "song_stats": FakeQuery(data=[]),
            "artist_balances": FakeQuery(data=[]),  # no pre-existing row at all
            "withdrawal_requests": FakeQuery(data=[]),
            "referral_earnings": FakeQuery(data=[{"amount": "159.90"}]),
        })
        with patch.object(earnings_service, "get_service_client", return_value=client):
            result = earnings_service.recompute_balance("new-referrer@example.com")

        self.assertEqual(result["total_earned"], 159.90)
        self.assertEqual(result["available_balance"], 159.90)
        # Confirms the upsert was actually reached (previously it never was).
        self.assertEqual(
            client.table("artist_balances").last_upsert["available_balance"], "159.90"
        )


class EarningsSummaryReferralTests(unittest.TestCase):
    def test_total_revenue_includes_referral_earnings(self):
        client = FakeClient({
            "song_stats": FakeQuery(data=[
                {"submission_id": "s1", "song_title": "Song A", "artist_name": "Artist",
                 "platform": "Spotify", "platform_group": "Spotify",
                 "period_month": "January", "period_year": 2026,
                 "streams": 1000, "revenue": "100.00"},
            ]),
            "artist_balances": FakeQuery(data=[
                {"total_earned": "259.90", "total_withdrawn": "0", "available_balance": "259.90"},
            ]),
            "referral_earnings": FakeQuery(data=[{"amount": "159.90"}]),
        })
        with patch.object(earnings_service, "get_service_client", return_value=client):
            result = earnings_service.get_earnings_summary("artist@example.com")

        # 100 (song_stats) + 159.90 (referral_earnings) = 259.90
        self.assertEqual(result["total_revenue"], 259.90)
        # available_balance already came from artist_balances and stays consistent
        self.assertEqual(result["available_balance"], 259.90)

    def test_total_revenue_unaffected_when_no_referrals(self):
        client = FakeClient({
            "song_stats": FakeQuery(data=[
                {"submission_id": "s1", "song_title": "Song A", "artist_name": "Artist",
                 "platform": "Spotify", "platform_group": "Spotify",
                 "period_month": "January", "period_year": 2026,
                 "streams": 1000, "revenue": "100.00"},
            ]),
            "artist_balances": FakeQuery(data=[
                {"total_earned": "100.00", "total_withdrawn": "0", "available_balance": "100.00"},
            ]),
            "referral_earnings": FakeQuery(data=[]),
        })
        with patch.object(earnings_service, "get_service_client", return_value=client):
            result = earnings_service.get_earnings_summary("artist@example.com")

        self.assertEqual(result["total_revenue"], 100.0)


if __name__ == "__main__":
    unittest.main()
