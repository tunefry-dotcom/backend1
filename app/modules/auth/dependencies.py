"""FastAPI dependency: get_current_user.

Extracts the JWT from the httpOnly cookie (primary) or Authorization Bearer
header (API clients). Verifies locally via JWKS. On expiry, silently refreshes
using the refresh cookie if available; otherwise raises 401.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Response, status
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import settings
from app.core.security import decode_token
from app.core.supabase_client import get_supabase
from app.modules.auth.cookies import set_session_cookies


@dataclass
class CurrentUser:
    id: str
    email: Optional[str]
    role: str
    full_name: Optional[str] = None


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    return None


def _user_from_claims(claims: dict) -> CurrentUser:
    metadata = claims.get("user_metadata") or {}
    return CurrentUser(
        id=claims["sub"],
        email=claims.get("email"),
        role=claims.get("role", "authenticated"),
        full_name=metadata.get("full_name"),
    )


async def get_current_user(
    response: Response,
    access_token: Optional[str] = Cookie(default=None, alias=settings.access_cookie_name),
    refresh_token: Optional[str] = Cookie(default=None, alias=settings.refresh_cookie_name),
    authorization: Optional[str] = Header(default=None),
) -> CurrentUser:
    token = access_token or _extract_bearer(authorization)

    if token:
        try:
            return _user_from_claims(decode_token(token))
        except ExpiredSignatureError:
            pass  # fall through to refresh
        except InvalidTokenError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Attempt silent refresh
    if refresh_token:
        try:
            client = get_supabase()
            result = client.auth.refresh_session(refresh_token)
            if result.session:
                new_access = result.session.access_token
                new_refresh = result.session.refresh_token
                set_session_cookies(response, new_access, new_refresh)
                return _user_from_claims(decode_token(new_access))
        except Exception:
            pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
