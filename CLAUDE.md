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
  modules/billing/            # subscription plans + feature entitlements
    plans.py                  # Plan/Feature enums, entitlement matrix, pure helpers
    service.py                # read/write plan in Supabase app_metadata (admin API)
    dependencies.py           # require_feature(...) route guard for domain endpoints
    payment.py                # Razorpay order creation + signature verification
    router.py                 # /billing/* endpoints
    schemas.py                # response models
  modules/profile/            # artist profile persistence
    service.py                # get_profile / upsert_profile via service-role client
    router.py                 # GET|PUT /profile/me
    schemas.py                # ProfileResponse (+ is_complete, missing_fields)
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
- **Signup does NOT use `auth.sign_up`.** Supabase's built-in SMTP sender hangs on
  this project (sign_up blocks 30s+ on the inline SMTP send → `httpx.ReadTimeout` →
  503). Instead `POST /auth/signup` (1) creates the user unconfirmed via the admin
  API (`admin.create_user`, no email), (2) generates the confirmation token via
  `admin.generate_link(type="signup")` (no email), and (3) sends the confirmation
  email itself through the **Resend HTTP API** (`app/core/email.py`, async httpx).
  Requires `RESEND_API_KEY` + a Resend-verified `RESEND_FROM_EMAIL`. Duplicate emails
  are detected from the `admin.create_user` error (`_is_duplicate_email_error`).
- **Email confirmation** uses Supabase's `token_hash` query-param flow
  (server-readable). Our confirmation email links to
  `{OAUTH_CALLBACK_BASE_URL}/auth/confirm?token_hash=<hashed_token>&type=email`;
  verified server-side with `auth.verify_otp(...)`.
- **Password reset** still uses `auth.reset_password_for_email` → Supabase's SMTP,
  which has the same hang problem. TODO: migrate it to the same Resend flow
  (`admin.generate_link(type="recovery")` + `send_email`).
- The Supabase auth (GoTrue) client timeout is raised from httpx's 5s default to
  `SUPABASE_HTTP_TIMEOUT` (30s) in `core/supabase_client._apply_timeout`, and the
  sync SDK calls in signup/login run via `run_in_threadpool` so they don't block the
  event loop.

## Endpoints

- `POST /auth/signup`, `POST /auth/login`, `POST /auth/logout`
- `GET  /auth/me` (protected — proves JWT dependency)
- `GET  /auth/confirm` (email confirmation callback)
- `POST /auth/forgot-password`, `GET|POST /auth/reset-password`
- `GET  /auth/google/login`, `GET /auth/google/callback` (PKCE OAuth)
- `GET  /billing/plans` (public plan catalogue + per-plan feature map)
- `GET  /billing/me` (protected — current plan + resolved entitlements)
- `POST /billing/orders` (protected — profile-gated Razorpay order creation; amount server-derived)
- `POST /billing/verify-payment` (protected — signature check → assign_plan → session refresh)
- `POST /billing/change-plan` (dev-gated placeholder, `DEV_AUTH_ENABLED` only)
- `GET  /profile/me` (protected — fetch own profile row + is_complete + missing_fields)
- `PUT  /profile/me` (protected — upsert profile fields via service-role; returns ProfileResponse)

## Plans / entitlements (billing module)

- **Source of truth = the `public.subscriptions` table** (plan, status,
  started_at, expires_at, payment_ref). RLS: a user may read only their own row;
  **only the service role writes** it → no self-escalation. See
  `supabase/migrations/0001_subscriptions_and_auth_hook.sql`.
- **Assignment is a DB invariant.** A trigger (`handle_new_user`) on `auth.users`
  auto-creates a **Free** row for every new user — email, Google OAuth *and* admin
  signups — so no code path can forget it. The app layer never assigns the default.
- **Effective plan reaches the JWT via a custom access-token hook**
  (`custom_access_token_hook`): at token-issue it reads the table and stamps
  `app_metadata.plan`, downgrading expired/cancelled subs to `free`. Gating then
  reads the JWT (`get_current_user` → `plan_from_claims`) with **zero per-request DB
  calls**, yet always reflects the table on the next refresh. Missing claim → Free
  (fail-closed). **The hook must be enabled once in the dashboard** (Auth → Hooks).
- `plans.py` holds the single source-of-truth **feature entitlement matrix**
  (from the pricing page): `release_single` (all plans), `release_album` /
  `transfer_single` / `transfer_album` / `playlist_pitching` (Single Artist+),
  `instagram_linking` + `content_id` (Starter+), `custom_label` (Double Artist+).
  `service.effective_plan()` mirrors the hook's expiry logic for `/billing/me`.
- Enforce on domain routes with `Depends(require_feature(Feature.X))` → 403 with an
  `{error, feature, current_plan, required_plan}` body when the plan lacks it.
- Frontend mirrors only the feature *keys* (`src/lib/billing.js`); the authoritative
  entitlement map + lifecycle is fetched from `/billing/me`. Route gates use
  `<PlanGate>` and a free-plan `<UpgradeBanner>`.
- `POST /billing/change-plan` is gated by `DEV_AUTH_ENABLED` (404 when off). It
  upserts the subscriptions row then refreshes the session so the hook re-stamps the
  new plan into a fresh JWT. Replace with a payment webhook later.

## Gotchas / conventions

- **Starlette ≥1.x** — use `templates.TemplateResponse(request, "name.html", {...})`
  (request FIRST). The old `(name, {"request": ...})` signature is removed → 500.
- Supabase OTP `type` for signup confirmation is **`email`** (`signup` is deprecated).
- Supabase built-in email sender is capped at **2 emails/hour** — use custom SMTP
  (Resend) for anything real. Configured in the Supabase dashboard, not in code.
- `SUPABASE_SERVICE_ROLE_KEY` is server-only — never expose it to a client.
- Match existing style: type hints, `from __future__ import annotations`, small
  focused modules per bounded context (microservices-ready modular monolith).
- `POST /auth/dev/create-user` creates a pre-confirmed user via the service-role
  admin API (bypasses email). Gated by `DEV_AUTH_ENABLED` (default false → 404).
  For testing only — must stay disabled in production.

## Env vars

See `.env.example`. Key ones: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`, `OAUTH_CALLBACK_BASE_URL`, `FRONTEND_URL`,
`COOKIE_SECURE`, `COOKIE_SAMESITE`.

Razorpay (billing): `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` (live keys — never commit;
rotate in Razorpay dashboard after any accidental exposure), `PAYMENT_AMOUNT_DIVISOR`
(100 = charge 1/100th for testing, 1 = full price in production). Add all three to Render
env vars before deploying. `razorpay_enabled` is false if either key is blank → orders
endpoint returns 503.

## One-time Supabase setup (manual — not in code)

1. Run `supabase/migrations/0002_profiles.sql` in the SQL Editor on your Supabase project
   to create `public.profiles`, its RLS policy, and the `handle_new_user_profile` trigger
   (auto-creates a blank profile row on every new signup).
2. Enable the **custom access-token hook** in the Supabase dashboard:
   Auth → Hooks → JWT Claims Customization → select `public.custom_access_token_hook`.
   This stamps `app_metadata.plan` into the JWT on every token issue/refresh.
   Without it, plan gating falls back to Free for all users.

## Deploy

- `Dockerfile` + `render.yaml` drive the Render web service.
- Push to `main` → Render auto-deploys.
- Set all secrets in the Render dashboard (not committed).
