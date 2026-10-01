"""Endpoint-contract tests for signup duplicate-account detection, password
complexity validation, and the forgot-password existence check."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from supabase_auth.errors import AuthApiError

from app.main import app
from app.modules.auth import router as auth_router
from app.modules.auth.schemas import ResetPasswordRequest, SignUpRequest

_SIGNUP_BODY = dict(
    full_name="Test Artist",
    artist_name="Testy",
    phone="9876543210",
    email="test@example.com",
    password="Password123",
)


def _auth_error(code: str | None, message: str = "boom") -> AuthApiError:
    return AuthApiError(message, 400, code)


class SignupDuplicateDetectionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_duplicate_email_codes_return_friendly_400(self):
        for code in ("email_exists", "user_already_exists", "identity_already_exists"):
            service = MagicMock()
            service.auth.admin.create_user.side_effect = _auth_error(code)
            with patch.object(auth_router, "get_service_client", return_value=service):
                resp = self.client.post("/auth/signup", json=_SIGNUP_BODY)
            self.assertEqual(resp.status_code, 400, code)
            self.assertIn("already exists", resp.json()["detail"])

    def test_duplicate_email_substring_fallback_when_no_code(self):
        service = MagicMock()
        service.auth.admin.create_user.side_effect = Exception(
            "This email address is already registered"
        )
        with patch.object(auth_router, "get_service_client", return_value=service):
            resp = self.client.post("/auth/signup", json=_SIGNUP_BODY)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already exists", resp.json()["detail"])

    def test_non_duplicate_auth_error_passes_through_raw_message(self):
        service = MagicMock()
        service.auth.admin.create_user.side_effect = _auth_error(
            "email_address_invalid", message="Invalid email address"
        )
        with patch.object(auth_router, "get_service_client", return_value=service):
            resp = self.client.post("/auth/signup", json=_SIGNUP_BODY)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "Invalid email address")


class PasswordComplexitySchemaTests(unittest.TestCase):
    def _signup_kwargs(self, password: str) -> dict:
        return {**_SIGNUP_BODY, "password": password}

    def test_rejects_too_short(self):
        with self.assertRaises(Exception):
            SignUpRequest(**self._signup_kwargs("Ab1"))

    def test_rejects_missing_uppercase(self):
        with self.assertRaises(Exception):
            SignUpRequest(**self._signup_kwargs("lowercase1"))

    def test_rejects_missing_lowercase(self):
        with self.assertRaises(Exception):
            SignUpRequest(**self._signup_kwargs("UPPERCASE1"))

    def test_accepts_valid_password(self):
        req = SignUpRequest(**self._signup_kwargs("Password123"))
        self.assertEqual(req.password, "Password123")

    def test_reset_password_same_rule(self):
        with self.assertRaises(Exception):
            ResetPasswordRequest(password="alllower")
        req = ResetPasswordRequest(password="Password123")
        self.assertEqual(req.password, "Password123")


class ForgotPasswordExistenceTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _service_with_generate_link(self, side_effect=None, return_value=None):
        service = MagicMock()
        if side_effect is not None:
            service.auth.admin.generate_link.side_effect = side_effect
        else:
            service.auth.admin.generate_link.return_value = return_value
        return service

    def test_unknown_account_returns_404(self):
        service = self._service_with_generate_link(side_effect=_auth_error("user_not_found"))
        with patch.object(auth_router, "get_service_client", return_value=service):
            resp = self.client.post("/auth/forgot-password", json={"email": "nope@example.com"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("No account found", resp.json()["detail"])

    def test_known_account_without_resend_returns_202(self):
        link = MagicMock()
        link.properties.hashed_token = "tok"
        service = self._service_with_generate_link(return_value=link)
        with patch.object(auth_router, "get_service_client", return_value=service), patch.object(
            auth_router.settings, "resend_api_key", ""
        ):
            resp = self.client.post("/auth/forgot-password", json={"email": "real@example.com"})
        self.assertEqual(resp.status_code, 202)

    def test_other_auth_errors_are_swallowed_as_202(self):
        service = self._service_with_generate_link(
            side_effect=_auth_error("over_email_send_rate_limit")
        )
        with patch.object(auth_router, "get_service_client", return_value=service):
            resp = self.client.post("/auth/forgot-password", json={"email": "real@example.com"})
        self.assertEqual(resp.status_code, 202)

    def test_resend_send_failure_is_swallowed_as_202(self):
        link = MagicMock()
        link.properties.hashed_token = "tok"
        service = self._service_with_generate_link(return_value=link)
        with patch.object(auth_router, "get_service_client", return_value=service), patch.object(
            auth_router.settings, "resend_api_key", "fake-key"
        ), patch.object(auth_router, "send_email", new=AsyncMock(side_effect=Exception("resend down"))):
            resp = self.client.post("/auth/forgot-password", json={"email": "real@example.com"})
        self.assertEqual(resp.status_code, 202)


if __name__ == "__main__":
    unittest.main()
