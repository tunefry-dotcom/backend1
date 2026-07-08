# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**Tunefry** — backend for a music distribution platform (artists create releases,
upload audio masters + cover art, submit for distribution to stores/DSPs). This
repo is the **FastAPI** backend. Currently only the **authentication layer** is
built; domain features (releases, tracks, media, jobs) come later.

## Stack

- **FastAPI** (async) — API framework
- **Supabase** — Auth (GoTrue), Postgres, Storage
- **Cloudinary** — cover-art image hosting (planned)
- **Upstash** — Redis cache, rate limiting, QStash background jobs (planned)
- **Render** — deployment (Docker); live at `https://backend1-xzx5.onrender.com`
- Python **3.12**

## Run locally

```bash
.\venv\Scripts\activate          # Windows (venv already created)
pip install -r requirements.txt
cp .env.example .env             # fill in Supabase keys
uvicorn app.main:app --reload
```

- API docs: http://localhost:8000/docs
- Health: `GET /health`

## Structure

```
app/
  main.py                     # app factory, CORS, / and /health routes
  core/
    config.py                 # pydantic-settings; all env vars + derived URLs
    supabase_client.py        # anon / service-role / PKCE client factories
    security.py               # JWKS ES256 JWT verification (decode_token)
  modules/auth/
    router.py                 # all /auth/* endpoints
    dependencies.py           # get_current_user (cookie or Bearer, auto-refresh)
    cookies.py                # httpOnly session + PKCE cookie helpers
    schemas.py                # request models
templates/
  confirm.html                # email-confirmation result page
  reset_password.html         # set-new-password page
```

## Auth model (important)

- **Session transport = httpOnly cookies** (`sb-access-token`, `sb-refresh-token`)
  set by FastAPI. Bearer header accepted as a fallback for API clients.
- **Token verification = JWKS / ES256**, done locally in `core/security.py` via
  `PyJWKClient` against `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`
  (10-min key cache). No shared HS256 secret unless `SUPABASE_JWT_SECRET` is set.
- **Email confirmation & password reset** use Supabase's `token_hash` query-param
  flow (server-readable), NOT the default URL-fragment flow. The Supabase email
  templates must link to `/auth/confirm?token_hash={{ .TokenHash }}&type=email`
  and `/auth/reset-password`. Verified server-side with `auth.verify_otp(...)`.

## Endpoints

- `POST /auth/signup`, `POST /auth/login`, `POST /auth/logout`
- `GET  /auth/me` (protected — proves JWT dependency)
- `GET  /auth/confirm` (email confirmation callback)
- `POST /auth/forgot-password`, `GET|POST /auth/reset-password`
- `GET  /auth/google/login`, `GET /auth/google/callback` (PKCE OAuth)

## Gotchas / conventions

- **Starlette ≥1.x** — use `templates.TemplateResponse(request, "name.html", {...})`
  (request FIRST). The old `(name, {"request": ...})` signature is removed → 500.
- Supabase OTP `type` for signup confirmation is **`email`** (`signup` is deprecated).
- Supabase built-in email sender is capped at **2 emails/hour** — use custom SMTP
  (Resend) for anything real. Configured in the Supabase dashboard, not in code.
- `SUPABASE_SERVICE_ROLE_KEY` is server-only — never expose it to a client.
- Match existing style: type hints, `from __future__ import annotations`, small
  focused modules per bounded context (microservices-ready modular monolith).

## Env vars

See `.env.example`. Key ones: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`, `OAUTH_CALLBACK_BASE_URL`, `FRONTEND_URL`,
`COOKIE_SECURE`, `COOKIE_SAMESITE`.

## Deploy

- `Dockerfile` + `render.yaml` drive the Render web service.
- Push to `main` → Render auto-deploys.
- Set all secrets in the Render dashboard (not committed).
