"""Endpoint-contract test for GET /referrals/me using FastAPI's TestClient,
with the auth dependency and Supabase client both faked out."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.referrals import service as referrals_service
from tests.fakes import FakeClient, FakeQuery


class ReferralsMeEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            id="user-1", email="me@example.com", role="authenticated",
        )

    def tearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def test_returns_own_code_and_totals(self):
        fake = FakeClient({
            "profiles": FakeQuery(data=[{"referral_code": "TF12345678"}]),
            "referrals": FakeQuery(data=[]),
            "referral_earnings": FakeQuery(data=[{"amount": "159.90"}]),
        })
        with patch.object(referrals_service, "get_service_client", return_value=fake):
            resp = self.client.get("/referrals/me")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["referral_code"], "TF12345678")
        self.assertEqual(body["referred_count"], 0)
        self.assertEqual(body["total_referral_earned"], 159.90)

    def test_requires_auth(self):
        app.dependency_overrides.pop(get_current_user, None)
        resp = self.client.get("/referrals/me")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
