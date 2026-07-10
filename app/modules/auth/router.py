"""Auth router — all authentication endpoints.

Phase 1:  POST /auth/signup, POST /auth/login, POST /auth/logout
Phase 2:  GET  /auth/me  (protected, proves JWKS dependency works)
Phase 4:  POST /auth/forgot-password
          GET  /auth/reset-password   (receives Supabase recovery link)
          POST /auth/reset-password   (submits new password)
Phase 3:  GET  /auth/google/login     (initiates PKCE OAuth flow)
          GET  /auth/google/callback  (exchanges code, sets session)
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.supabase_client import (
    DictStorage,
    build_pkce_client,
    get_service_client,
    get_supabase,
)
from app.modules.auth.cookies import (
    clear_pkce_cookie,
    clear_session_cookies,
    set_pkce_cookie,
    set_session_cookies,
)
from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.auth.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
    SignUpRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Phase 1 — Email + password
# ---------------------------------------------------------------------------


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(body: SignUpRequest) -> dict[str, Any]:
    """Create a new account. Email confirmation may be required before login."""
    client = get_supabase()
    try:
        result = client.auth.sign_up(
            {
                "email": body.email,
                "password": body.password,
                "options": {"data": {"full_name": body.full_name}},
            }
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    user = result.user
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Signup failed")

    return {
        "id": user.id,
        "email": user.email,
        "full_name": (user.user_metadata or {}).get("full_name"),
        "email_confirmed": user.email_confirmed_at is not None,
        "message": (
            "Account created. Check your email to confirm before logging in."
            if not user.email_confirmed_at
            else "Account created."
        ),
    }


@router.post("/login")
async def login(body: LoginRequest, response: Response) -> dict[str, Any]:
    """Authenticate with email and password; sets session cookies."""
    client = get_supabase()
    try:
        result = client.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    if not result.session or not result.user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login failed")

    set_session_cookies(response, result.session.access_token, result.session.refresh_token)
    return {"id": result.user.id, "email": result.user.email}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    access_token: str | None = Cookie(default=None, alias=settings.access_cookie_name),
) -> None:
    """Invalidate the session on Supabase and clear cookies."""
    if access_token:
        try:
            client = get_supabase()
            client.auth.admin.sign_out(access_token)
        except Exception:
            pass  # best-effort; cookies are cleared regardless
    clear_session_cookies(response)


# ---------------------------------------------------------------------------
# Dev-only — create a pre-confirmed user (bypasses email). Off by default.
# ---------------------------------------------------------------------------


@router.post("/dev/create-user", status_code=status.HTTP_201_CREATED)
async def dev_create_user(body: SignUpRequest, response: Response) -> dict[str, Any]:
    """Create an already-confirmed user via the service-role admin API.

    Enabled only when DEV_AUTH_ENABLED=true. Lets us test signup/login without
    depending on email delivery. MUST remain disabled in production.
    """
    if not settings.dev_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    service = get_service_client()
    try:
        service.auth.admin.create_user(
            {
                "email": body.email,
                "password": body.password,
                "email_confirm": True,
                "user_metadata": {"full_name": body.full_name},
            }
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # Sign in immediately so the caller gets a ready-to-use session.
    client = get_supabase()
    try:
        result = client.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if not result.session or not result.user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Created but sign-in failed")

    set_session_cookies(response, result.session.access_token, result.session.refresh_token)
    return {
        "id": result.user.id,
        "email": result.user.email,
        "full_name": (result.user.user_metadata or {}).get("full_name"),
        "access_token": result.session.access_token,
    }


# ---------------------------------------------------------------------------
# Email confirmation callback
# ---------------------------------------------------------------------------


@router.get("/confirm", response_class=HTMLResponse)
async def confirm_email(
    request: Request,
    response: Response,
    token_hash: str | None = Query(default=None),
    type: str | None = Query(default="email"),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> Any:
    """Handle the email confirmation link from Supabase.

    Supabase calls this URL (via our custom email template) with
    ?token_hash=...&type=email so the server can verify the OTP directly
    without needing client-side JavaScript to parse URL fragments.
    """
    if error:
        return templates.TemplateResponse(
            request,
            "confirm.html",
            {"state": "error", "message": error_description or error},
        )

    if not token_hash:
        return templates.TemplateResponse(
            request,
            "confirm.html",
            {"state": "invalid", "message": "No confirmation token found. The link may be malformed."},
        )

    client = get_supabase()
    try:
        result = client.auth.verify_otp({"token_hash": token_hash, "type": type or "email"})
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "confirm.html",
            {"state": "error", "message": str(exc)},
        )

    if not result.session:
        return templates.TemplateResponse(
            request,
            "confirm.html",
            {"state": "invalid", "message": "Confirmation link is invalid or has already been used."},
        )

    set_session_cookies(response, result.session.access_token, result.session.refresh_token)
    return templates.TemplateResponse(
        request,
        "confirm.html",
        {"state": "success", "email": result.user.email if result.user else ""},
    )


# ---------------------------------------------------------------------------
# Phase 2 — Protected route (proves JWKS dependency)
# ---------------------------------------------------------------------------


@router.get("/me")
async def me(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> dict[str, Any]:
    """Return the authenticated user's basic profile."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role,
    }


# ---------------------------------------------------------------------------
# Phase 4 — Forgot / reset password
# ---------------------------------------------------------------------------


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: ForgotPasswordRequest) -> dict[str, str]:
    """Trigger a password-reset email. Always returns 202 to avoid enumeration."""
    try:
        client = get_supabase()
        client.auth.reset_password_for_email(
            body.email,
            {"redirect_to": settings.reset_password_url},
        )
    except Exception:
        pass  # swallow to avoid user enumeration
    return {"message": "If that email is registered you will receive a reset link shortly."}


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(
    request: Request,
    response: Response,
    token_hash: str | None = Query(default=None),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> Any:
    """Receive the recovery redirect and render the set-new-password form.

    Supabase sends either:
    - ``token_hash`` (email OTP flow) — verify_otp approach
    - ``code`` (PKCE flow) — exchange_code_for_session approach
    """
    if error:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"error": error_description or error, "valid": False},
        )

    client = get_supabase()
    session_token: str | None = None

    try:
        if token_hash:
            result = client.auth.verify_otp({"token_hash": token_hash, "type": "recovery"})
            if result.session:
                session_token = result.session.access_token
                set_session_cookies(response, result.session.access_token, result.session.refresh_token)
        elif code:
            result = client.auth.exchange_code_for_session({"auth_code": code})
            if result.session:
                session_token = result.session.access_token
                set_session_cookies(response, result.session.access_token, result.session.refresh_token)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"error": str(exc), "valid": False},
        )

    if not session_token:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"error": "Reset link is invalid or expired.", "valid": False},
        )

    return templates.TemplateResponse(
        request,
        "reset_password.html",
        {"error": None, "valid": True},
    )


@router.post("/reset-password")
async def reset_password(
    body: ResetPasswordRequest,
    response: Response,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    access_token: str | None = Cookie(default=None, alias=settings.access_cookie_name),
) -> dict[str, str]:
    """Submit the new password within the temporary recovery session."""
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Recovery session expired")

    client = get_supabase()
    # Restore the recovery session so update_user operates on the right user.
    try:
        client.auth.set_session(access_token, "")
        client.auth.update_user({"password": body.password})
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    clear_session_cookies(response)
    return {"message": "Password updated. Please log in with your new password."}


# ---------------------------------------------------------------------------
# Phase 3 — Continue with Google (PKCE OAuth)
# ---------------------------------------------------------------------------


@router.get("/google/login")
async def google_login(response: Response) -> RedirectResponse:
    """Redirect the browser to Google's consent screen via Supabase PKCE."""
    if not settings.google_oauth_enabled:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Google OAuth is disabled")

    storage = DictStorage()
    pkce_client = build_pkce_client(storage)

    result = pkce_client.auth.sign_in_with_oauth(
        {
            "provider": "google",
            "options": {"redirect_to": settings.google_callback_url, "skip_browser_redirect": True},
        }
    )

    if not result.url:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to initiate OAuth")

    # Persist the PKCE storage so the callback can reconstruct it.
    set_pkce_cookie(response, json.dumps(storage.dump()))
    return RedirectResponse(url=result.url, status_code=status.HTTP_302_FOUND)


@router.get("/google/callback")
async def google_callback(
    response: Response,
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    pkce_cookie: str | None = Cookie(default=None, alias=settings.pkce_cookie_name),
) -> RedirectResponse:
    """Exchange the authorization code for a session and redirect to the app."""
    clear_pkce_cookie(response)

    if error:
        return RedirectResponse(
            url=f"{settings.frontend_url}?auth_error={error_description or error}",
            status_code=status.HTTP_302_FOUND,
        )

    if not code:
        return RedirectResponse(
            url=f"{settings.frontend_url}?auth_error=missing_code",
            status_code=status.HTTP_302_FOUND,
        )

    storage_data: dict[str, str] = {}
    if pkce_cookie:
        try:
            storage_data = json.loads(pkce_cookie)
        except Exception:
            pass

    storage = DictStorage(storage_data)
    pkce_client = build_pkce_client(storage)

    try:
        result = pkce_client.auth.exchange_code_for_session({"auth_code": code})
    except Exception as exc:
        return RedirectResponse(
            url=f"{settings.frontend_url}?auth_error={str(exc)}",
            status_code=status.HTTP_302_FOUND,
        )

    if not result.session:
        return RedirectResponse(
            url=f"{settings.frontend_url}?auth_error=session_exchange_failed",
            status_code=status.HTTP_302_FOUND,
        )

    set_session_cookies(response, result.session.access_token, result.session.refresh_token)
    return RedirectResponse(url=settings.frontend_url, status_code=status.HTTP_302_FOUND)
