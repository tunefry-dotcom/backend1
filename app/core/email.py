"""Transactional email via Resend's HTTP API.

We send our own confirmation emails instead of relying on Supabase's built-in SMTP
sender: on the current project that sender hangs (``sign_up`` blocks 30s+ on the SMTP
connection). Using the Resend REST API with an async httpx client keeps the request
off the SMTP path and never blocks the event loop.
"""

from __future__ import annotations

import html as _html

import httpx

from app.core.config import settings

_RESEND_ENDPOINT = "https://api.resend.com/emails"


async def send_email(*, to: str, subject: str, html_body: str) -> None:
    """Send one email through Resend. Raises on misconfiguration or API error."""
    if not settings.resend_enabled:
        raise RuntimeError("RESEND_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.resend_from,
                "to": [to],
                "subject": subject,
                "html": html_body,
            },
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Resend API error {resp.status_code}: {resp.text[:300]}")


def confirmation_email_html(full_name: str | None, confirm_url: str) -> str:
    """Branded HTML for the signup confirmation email.

    ``confirm_url`` is trusted (built server-side); ``full_name`` is user-supplied
    and therefore HTML-escaped before interpolation.
    """
    greeting = f"Hi {_html.escape(full_name)}," if full_name else "Hi,"
    return f"""<!DOCTYPE html>
<html lang="en">
<body style="margin:0;padding:0;background:#0a0a0a;font-family:system-ui,-apple-system,sans-serif;">
  <div style="max-width:480px;margin:0 auto;padding:40px 24px;">
    <div style="background:#161616;border:1px solid #2a2a2a;border-radius:16px;padding:40px 32px;text-align:center;">
      <div style="font-size:40px;margin-bottom:12px;">🎵</div>
      <h1 style="color:#f0f0f0;font-size:22px;margin:0 0 8px;">Confirm your email</h1>
      <p style="color:#aaa;font-size:15px;line-height:1.6;margin:0 0 28px;">
        {greeting}<br>Welcome to Tunefry. Confirm your email address to activate your account.
      </p>
      <a href="{confirm_url}"
         style="display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#FF8A50,#F26522);
                color:#fff;border-radius:8px;text-decoration:none;font-size:15px;font-weight:600;">
        Confirm Email
      </a>
      <p style="color:#666;font-size:12px;line-height:1.6;margin:28px 0 0;">
        If the button doesn't work, paste this link into your browser:<br>
        <a href="{confirm_url}" style="color:#F26522;word-break:break-all;">{confirm_url}</a>
      </p>
      <p style="color:#555;font-size:12px;margin:20px 0 0;">
        Didn't sign up? You can safely ignore this email.
      </p>
    </div>
  </div>
</body>
</html>"""
