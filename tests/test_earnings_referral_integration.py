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


if __name__ == "__main__":
    unittest.main()
