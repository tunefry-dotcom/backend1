"""JWT verification against Supabase's JWKS endpoint (ES256 / RS256).

PyJWKClient caches the key set and refreshes automatically when it encounters
an unknown kid, which covers key rotation. We cap the cache lifespan at 600 s
(10 minutes) to stay within Supabase's edge-cache window.

Optional legacy path: if SUPABASE_JWT_SECRET is set the module also accepts
HS256 tokens, letting projects that haven't migrated to asymmetric keys still
work. Remove this once the project is migrated.
"""

from __future__ import annotations

from typing import Any

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError, PyJWKClient

from app.core.config import settings

_jwks_client: PyJWKClient | None = None

_ALGORITHMS_ASYMMETRIC = ["ES256", "RS256"]
_ALGORITHMS_LEGACY = ["HS256"]


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(settings.jwks_url, lifespan=600, cache_jwk_set=True)
    return _jwks_client


def decode_token(token: str) -> dict[str, Any]:
    """Verify a Supabase JWT and return its claims.

    Tries asymmetric verification first; falls back to HS256 only if
    SUPABASE_JWT_SECRET is configured (legacy projects).

    Raises jwt.InvalidTokenError (or subclasses) on failure.
    """
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALGORITHMS_ASYMMETRIC,
            audience="authenticated",
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "aud"]},
        )
    except InvalidTokenError:
        # Re-raise immediately unless we have a legacy secret to try.
        if not settings.supabase_jwt_secret:
            raise
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=_ALGORITHMS_LEGACY,
            audience="authenticated",
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "aud"]},
        )


__all__ = ["decode_token", "ExpiredSignatureError", "InvalidTokenError"]
