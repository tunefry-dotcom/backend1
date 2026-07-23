# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**Tunefry** — music distribution platform for indie artists. Artists create
releases, upload audio masters + cover art, submit for distribution to
DSPs/stores, manage subscriptions, and get analytics. This repo is the
**FastAPI backend**; the frontend is a separate React SPA at
`C:\Users\ViditVaibhav\Desktop\tunefry frontend`.

## Stack

| Layer | Technology |
|-------|-----------|
| API framework | FastAPI 0.115+ (async), Python 3.12 |
| Auth + DB | Supabase (GoTrue auth, PostgreSQL, JWKS) |
| File storage | Cloudflare R2 (S3-compatible via boto3) |
| Email | Resend (custom HTTP, replaces Supabase SMTP) |
| Payments | Razorpay (INR) |
| Frontend | React 18 + Vite SPA deployed on Vercel |
| Backend deploy | Render (Docker); live at `https://backend1-xzx5.onrender.com` |

Cloudinary and Upstash are **not used** — R2 replaced Cloudinary; Upstash
was planned but never wired in.

## Run locally

```bash
.\venv\Scripts\activate          # Windows venv already created
pip install -r requirements.txt
cp .env.example .env             # fill in all secrets
uvicorn app.main:app --reload
```

- Swagger docs: `http://localhost:8000/docs`
- Health: `GET /health`

## Backend structure

```
app/
  main.py                     # FastAPI factory, CORS, / and /health
  core/
    config.py                 # pydantic-settings; all env vars; computed bool flags
    supabase_client.py        # anon / service-role / PKCE client factories
    security.py               # JWKS ES256 JWT verification (decode_token)
    email.py                  # Resend HTTP API; confirmation + reset HTML templates
    r2_client.py              # boto3 R2 client; presign_put/presign_get; key builder
  modules/
    auth/
      router.py               # /auth/* — signup, login, logout, confirm, OAuth, reset
      dependencies.py         # get_current_user (cookie or Bearer, auto-refresh)
      cookies.py              # httpOnly session + PKCE cookie helpers
      schemas.py              # SignUpRequest, LoginRequest, etc.
    billing/
      plans.py                # Plan/Feature enums, PLAN_SPECS dict, entitlement matrix
      service.py              # subscriptions table R/W (service-role); effective_plan
      dependencies.py         # require_feature(...) FastAPI route guard
      payment.py              # Razorpay order creation + HMAC-SHA256 verify
      router.py               # /billing/* endpoints
      schemas.py              # PlanSummary, MyPlanResponse, order/verify DTOs
    profile/
      service.py              # get_profile / upsert_profile via service-role client
      router.py               # GET|PUT /profile/me
      schemas.py              # ProfileResponse (is_complete, missing_fields)
    home/
      service.py              # get/upsert home_content table (CMS, id=1 singleton)
      router.py               # GET /home/content (public); GET /home/assets/{key}
      schemas.py              # HomeContent, ArtistCard, YTTestimonial
    media/
      router.py               # POST /media/presign → R2 presigned PUT URL
    submissions/
      router.py               # POST /submissions/{type}; GET /submissions/my
    admin/
      router.py               # /admin/* (X-Admin-Secret header required)
templates/
  confirm.html                # email-confirmation result page (Jinja2)
  reset_password.html         # set-new-password form (Jinja2)
supabase/
  migrations/                 # SQL run once manually in Supabase SQL editor
```

## Frontend structure (separate repo)

Path: `C:\Users\ViditVaibhav\Desktop\tunefry frontend`

```
src/
  context/AuthContext.jsx     # global user state; fetches /auth/me + /billing/me in parallel
  lib/
    auth.js                   # login, signup, logout, getCurrentUser
    billing.js                # FEATURES enum, canAccess(), fetchPlans(), changePlan()
    payment.js                # Razorpay order + verify flow; ProfileIncompleteError
    profile.js                # getProfile(), updateProfile()
    r2upload.js               # validates file type/dimensions; calls /api/upload/r2
  components/
    ProtectedRoute.jsx        # spinner while loading; redirects unauthenticated
    PlanGate.jsx              # blocks feature if not confirmed or wrong plan
    AppLayout.jsx             # sidebar + topbar + optional right panel
    PublicLayout.jsx          # nav + footer for marketing pages
  pages/                      # one file per route (98 .jsx files total)
  data/plans.jsx              # hardcoded plan catalogue (pricing page)
  styles/                     # component-scoped CSS (custom, no UI library)
```

**Frontend → backend:** all calls use `credentials: 'include'` (cookies);
base URL hardcoded to `https://backend1-xzx5.onrender.com`.

**Auth state machine:**
- `user === undefined` → loading (shows splash)
- `user === null` → logged out
- `user === { id, email, plan, planName, entitlements, isFree, planConfirmed, ... }` → logged in

**LocalStorage keys (per user-id):**
- `tf_plan_chosen_{uid}` — user has selected a plan (hides "choose first" gate)
- `tf_pitched_{uid}` — JSON array of submission IDs already dismissed from pitch UI
- `tf_notif_ts_{uid}` — timestamp of last approval notification check
- `tunefry_admin_secret` — sessionStorage key for admin panel secret

## Auth model

- **Session transport = httpOnly cookies** (`sb-access-token`, `sb-refresh-token`)
  set by FastAPI; Bearer header accepted as fallback for API clients.
- **Token verification = JWKS / ES256** done locally in `core/security.py` via
  `PyJWKClient` against `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`
  (10-min JWKS cache; auto-refreshes on unknown `kid`). HS256 fallback only if
  `SUPABASE_JWT_SECRET` is set (legacy projects).
- **Signup does NOT use `auth.sign_up`.** Supabase's built-in SMTP hangs 30s+
  on this project. Instead: (1) `admin.create_user` (no email) → (2)
  `admin.generate_link(type="signup")` → (3) send via **Resend HTTP API**
  (`core/email.py`, async httpx). Duplicate emails detected from the
  `admin.create_user` error message (`_is_duplicate_email_error`). Rolls back
  user if email send fails.
- **Email confirmation** — `token_hash` query-param flow. Link points to
  `{OAUTH_CALLBACK_BASE_URL}/auth/confirm?token_hash=…&type=email`; server
  verifies with `auth.verify_otp(...)`. OTP type must be `"email"` (not
  `"signup"` — that's deprecated).
- **Password reset** — same Resend flow; token minted via
  `admin.generate_link(type="recovery")`. Failures swallowed (always 202) to
  avoid user enumeration.
- Supabase GoTrue client timeout raised to `SUPABASE_HTTP_TIMEOUT` (30s) in
  `_apply_timeout`; sync SDK calls run via `run_in_threadpool`.
- **Google OAuth** uses PKCE with stateless code-verifier storage: serialized
  to/from a short-lived (10 min) cookie, no server session needed.

## All endpoints

### Auth
| Method | Path | Notes |
|--------|------|-------|
| POST | `/auth/signup` | Creates unconfirmed user + sends Resend email |
| POST | `/auth/login` | Sets httpOnly session cookies |
| POST | `/auth/logout` | Clears cookies + invalidates Supabase session |
| GET | `/auth/me` | Protected — returns CurrentUser |
| GET | `/auth/confirm` | Email confirmation callback; sets session |
| POST | `/auth/forgot-password` | Sends reset email via Resend (always 202) |
| GET | `/auth/reset-password` | Renders reset form in a temp recovery session |
| POST | `/auth/reset-password` | Updates password, clears recovery cookies |
| GET | `/auth/google/login` | Redirects to Google consent (PKCE) |
| GET | `/auth/google/callback` | Exchanges code, sets session, redirects frontend |
| POST | `/auth/dev/create-user` | Pre-confirmed user; gated by `DEV_AUTH_ENABLED` |

### Billing
| Method | Path | Notes |
|--------|------|-------|
| GET | `/billing/plans` | Public plan catalogue |
| GET | `/billing/me` | Protected — plan + entitlements + lifecycle |
| POST | `/billing/select-free` | Explicitly activate Free plan + set plan_confirmed |
| POST | `/billing/orders` | Protected — creates Razorpay order (amount server-derived) |
| POST | `/billing/verify-payment` | Verifies HMAC-SHA256, grants plan, refreshes session |
| POST | `/billing/change-plan` | Dev-only; gated by `DEV_AUTH_ENABLED` |

### Profile
| Method | Path | Notes |
|--------|------|-------|
| GET | `/profile/me` | Protected — profile row + is_complete + missing_fields |
| PUT | `/profile/me` | Protected — partial upsert via service-role |

### Home (CMS)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/home/content` | Public; 5-min cache header |
| GET | `/home/assets/{key:path}` | 307 redirect to 15-min presigned R2 GET URL |

### Media
| Method | Path | Notes |
|--------|------|-------|
| POST | `/media/presign` | Protected — returns R2 presigned PUT URL + key |

### Submissions
| Method | Path | Notes |
|--------|------|-------|
| POST | `/submissions/song` | new_song / transfer_song (multipart) |
| POST | `/submissions/album` | new_album / transfer_album (multipart) |
| POST | `/submissions/profile-mismatch` | Profile dispute |
| POST | `/submissions/claim-removal` | Claim removal request |
| POST | `/submissions/insta-link` | Instagram linking request |
| GET | `/submissions/my` | Protected — user's own submissions |

### Admin (X-Admin-Secret header required)
| Method | Path | Notes |
|--------|------|-------|
| GET | `/admin/users` | Paginated users + subscriptions + profiles |
| PATCH | `/admin/users/{uid}` | Update plan / auth metadata / profile |
| DELETE | `/admin/users/{uid}` | Delete user (cascades) |
| GET | `/admin/submissions/{category}` | Pending-first submission list |
| PATCH | `/admin/submissions/{id}` | Approve / decline; inserts new-artist-queue if approved |
| GET | `/admin/new-artist-queue` | Pending queue entries |
| PATCH | `/admin/new-artist-queue/{id}` | Save Spotify + Apple Music links |
| GET | `/admin/purchases` | All paid subscriptions + revenue stats |
| GET | `/admin/home` | Fetch CMS content |
| PUT | `/admin/home` | Update CMS content |
| POST | `/admin/home/artist-image` | Upload artist image to R2 (5 MB, JPEG/PNG/WebP) |
| GET | `/admin/media/download-url` | 15-min presigned R2 GET URL for a key |

## Plans / entitlements

| Plan | Price | Royalty | Max Releases | Max Artists | Notable features |
|------|-------|---------|-------------|------------|-----------------|
| Free | ₹0 | 75% | 10 | 1 | Singles only |
| Single Song | ₹299 | 85% | 1 | 1 | Singles only |
| Starter | ₹999/yr | 90% | ∞ | 1 | + Content ID, Instagram linking |
| Single Artist | ₹1,599/yr | 100% | ∞ | 1 | + Albums, transfers, playlist pitching |
| Double Artist | ₹2,999/yr | 100% | ∞ | 2 | + Custom label name |
| Label | ₹6,999/yr | 100% | ∞ | 5 | + Custom label; ₹1,260/extra artist |

**Feature enum** (gating keys): `RELEASE_SINGLE`, `RELEASE_ALBUM`,
`TRANSFER_SINGLE`, `TRANSFER_ALBUM`, `PLAYLIST_PITCHING`, `INSTAGRAM_LINKING`,
`CONTENT_ID`, `CUSTOM_LABEL`.

- Canonical matrix in `billing/plans.py` → `PLAN_SPECS` dict. Frontend mirrors
  feature keys in `src/lib/billing.js`; full entitlement map fetched from
  `/billing/me`.
- Gate domain routes: `Depends(require_feature(Feature.X))` → 403 with
  `{error, feature, current_plan, required_plan}`.
- **Plan in JWT** stamped by Postgres access-token hook
  (`custom_access_token_hook`); gating reads JWT (zero DB calls). Display
  reads `public.subscriptions` directly.
- **DB invariant**: `handle_new_user` trigger auto-creates a Free row for every
  new user (email, Google OAuth, admin) — app layer never assigns the default.
- Expired / cancelled subscriptions degrade to Free automatically in both hook
  and `service.effective_plan()`.

## Database schema

All migrations are SQL files run once manually in Supabase SQL editor:

| File | Creates |
|------|---------|
| `0001_subscriptions_and_auth_hook.sql` | `public.subscriptions`, `handle_new_user` trigger, `custom_access_token_hook` |
| `0002_profiles.sql` | `public.profiles`, `handle_new_user_profile` trigger |
| `0003_home_content.sql` | `public.home_content` (singleton id=1) |
| `0003_submissions.sql` | `public.submissions` (type, status, data JSONB) — **note: same prefix as above; run both** |
| `0004_apple_music_and_new_artist_queue.sql` | `profiles.apple_music_url`, `public.new_artist_queue` |
| *(inline migration)* | `subscriptions.plan_confirmed` boolean (added with `IF NOT EXISTS` guard in `billing/service.py`) |

RLS summary:
- `subscriptions`: user reads own row; service-role writes only.
- `profiles`: user reads own row; service-role writes only.
- `home_content`: public read; service-role write.
- `submissions`: service-role read/write only.

## Cloudflare R2 file layout

```
{sanitized_artist}/{sanitized_release}/cover_art.{ext}
{sanitized_artist}/{sanitized_release}/audio.{ext}        # single
{sanitized_artist}/{sanitized_release}/track_01.{ext}     # album (1-based, zero-padded)
home/{filename}                                            # home CMS images
```

- `sanitize_key_part()`: lowercases, strips special chars, collapses whitespace.
- Artist name comes from **JWT** (not request body) to prevent path traversal.
- Presigned PUT URLs expire in 1 hour; GET in 15 minutes.
- `r2_enabled` is false if any R2 env var is blank → submissions store filename
  strings instead (graceful degradation).

## Submission workflow

All submissions are `multipart/form-data`. `_parse_form()` does a two-pass parse:
1. Collect text fields, buffer files.
2. Upload files to R2; inject keys back into the data dict.

Fields `cover_art` → `cover_art_key`, `audio_file` → `audio_key`,
`audio_N` (album tracks) → `songs[N-1].audio_key`. Unknown files store
filename only. Data stored as JSONB in `submissions.data`.

On admin approval with `new_artist=true`, a row is inserted into
`new_artist_queue`; admin then saves Spotify + Apple Music links via
`PATCH /admin/new-artist-queue/{id}`, which also updates `profiles`.

## Env vars

See `.env.example`. All required for production:

| Var | Purpose |
|-----|---------|
| `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | Supabase access |
| `SUPABASE_JWT_SECRET` | Optional HS256 fallback; omit if on JWKS |
| `SUPABASE_HTTP_TIMEOUT` | Default 30s; covers slow GoTrue SMTP path |
| `OAUTH_CALLBACK_BASE_URL` | Backend base for auth redirect links |
| `FRONTEND_URL` | CORS + OAuth post-login redirect |
| `EXTRA_CORS_ORIGIN` | Comma-separated additional CORS origins |
| `COOKIE_SECURE`, `COOKIE_SAMESITE` | `true`/`strict` in production |
| `SESSION_SECRET` | Starlette session signing key |
| `RESEND_API_KEY`, `RESEND_FROM_EMAIL` | Transactional email |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Payment processing (live keys) |
| `PAYMENT_AMOUNT_DIVISOR` | `1` = full price; `100` = 1/100th for QA |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | R2 file storage |
| `ADMIN_SECRET` | `X-Admin-Secret` header value for `/admin/*` — use a strong random secret (≥32 chars), not a memorable password |
| `DEV_AUTH_ENABLED` | `true` to enable dev-only endpoints (default false → 404) |

Never commit real values. Rotate Razorpay keys via the dashboard if accidentally
exposed. `SERVICE_ROLE_KEY` is server-only — never ship to client.

## Gotchas / conventions

### FastAPI / Starlette
- **Starlette ≥1.x** — `templates.TemplateResponse(request, "name.html", {...})`
  (request FIRST). Old signature removed → 500.
- **Cookies on returned responses** — FastAPI does NOT merge the injected
  `response: Response` headers into a `Response`/`TemplateResponse` you return
  yourself. Call `set_session_cookies(returned_response, ...)` directly on the
  returned object. Bit us in password reset (recovery cookie silently dropped).
- Supabase OTP `type` for signup confirmation must be `"email"`, not `"signup"`
  (deprecated).

### Supabase
- Built-in email sender is capped at 2 emails/hour → always use Resend.
- `_apply_timeout` patches the GoTrue HTTP client after construction
  (ClientOptions doesn't expose auth-timeout).
- Sync SDK calls in signup/login run via `run_in_threadpool` to avoid blocking
  the async event loop.
- `admin.create_user` error messages are parsed with `_is_duplicate_email_error`
  — brittle but no other API surface for this.

### Payments
- Amount is derived server-side from `PLAN_SPECS` — never trust the client.
- `PAYMENT_AMOUNT_DIVISOR=100` → ₹14.99 charged as ₹0.15 for QA.
- Replay protection: payment_id tracked to prevent duplicate plan grants.
- Uses httpx instead of the official Razorpay SDK (avoids setuptools issues on
  python:3.12-slim).

### Code style
- `from __future__ import annotations` at the top of every module.
- Full type hints throughout.
- Small focused modules per bounded context (modular monolith, microservices-ready).
- Graceful degradation for missing DB tables/columns (fall back to defaults,
  not 500s) — enables partial feature deployment.
- Comments only when the WHY is non-obvious (existing codebase convention).

## One-time Supabase setup (manual)

1. Run each SQL file in `supabase/migrations/` via the Supabase SQL editor.
   Order matters: 0001 → 0002 → both 0003 files (either order) → 0004.
   Note there are **two files prefixed `0003_`** — run both.
2. Enable the **custom access-token hook**:
   Auth → Hooks → JWT Claims Customization →
   `public.custom_access_token_hook`.
   Without this, all users fall back to Free plan.
3. Add backend and frontend URLs to the **Redirect Allow List** in
   Auth → URL Configuration.
4. Verify the Resend sender domain in the Resend dashboard; set
   `RESEND_FROM_EMAIL` to a verified address.

## Deploy

- **Backend**: `Dockerfile` + `render.yaml` → Render web service. Push to
  `main` → auto-deploy. All secrets set in Render dashboard.
- **Frontend**: Vercel; all routes rewrite to `index.html` (SPA). Backend base
  URL hardcoded in `src/lib/` files.
- Health check endpoint: `GET /health` (Render ping).
- `SESSION_SECRET` should be auto-generated by Render (not committed).
