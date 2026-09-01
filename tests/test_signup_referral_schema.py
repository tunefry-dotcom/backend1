"""SignUpRequest must accept an optional referral_code without breaking
existing callers that omit it entirely."""

from __future__ import annotations

import unittest

from app.modules.auth.schemas import SignUpRequest

_BASE = dict(
    full_name="Test Artist", artist_name="Testy", phone="9876543210",
    email="test@example.com", password="password123",
)


class SignUpRequestReferralFieldTests(unittest.TestCase):
    def test_referral_code_defaults_to_none(self):
        req = SignUpRequest(**_BASE)
        self.assertIsNone(req.referral_code)

    def test_referral_code_accepted_when_provided(self):
        req = SignUpRequest(**_BASE, referral_code="TF12345678")
        self.assertEqual(req.referral_code, "TF12345678")


if __name__ == "__main__":
    unittest.main()
