"""Helpers for storing the Supabase session in httpOnly cookies."""

from __future__ import annotations

from fastapi import Response

from app.core.config import settings

# Access tokens are short-lived (~1h); refresh tokens live longer.
_ACCESS_MAX_AGE = 60 * 60           # 1 hour
_REFRESH_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_PKCE_MAX_AGE = 60 * 10             # 10 minutes (OAuth round-trip window)


def _set(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


def set_session_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    _set(response, settings.access_cookie_name, access_token, _ACCESS_MAX_AGE)
    _set(response, settings.refresh_cookie_name, refresh_token, _REFRESH_MAX_AGE)


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(settings.access_cookie_name, path="/")
    response.delete_cookie(settings.refresh_cookie_name, path="/")


def set_pkce_cookie(response: Response, value: str) -> None:
    _set(response, settings.pkce_cookie_name, value, _PKCE_MAX_AGE)


def clear_pkce_cookie(response: Response) -> None:
    response.delete_cookie(settings.pkce_cookie_name, path="/")
