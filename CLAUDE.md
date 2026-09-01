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
| Backend deploy | Render (Docker); prod API at `https://api.tunefry.com` (CNAME → `backend1-xzx5.onrender.com`, which still works directly) |

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

## Tests

`tests/` — stdlib `unittest` (no pytest dependency added). Everything runs
fully offline: the Supabase client is faked via `tests/fakes.py` (a minimal
fluent query-builder stand-in — `table().select().eq()...execute()` chains,
plus `auth.admin.get_user_by_id`), and FastAPI's `TestClient` (already
available via `fastapi`/`starlette`, no extra install) exercises endpoints
with `app.dependency_overrides` swapping in a fake `CurrentUser` for
`get_current_user`. There is **no live Supabase project or seeded test DB** —
these are logic-level unit/endpoint-contract tests, not full integration
tests against real Postgres.

```bash
.\venv\Scripts\activate
python -m unittest discover -s tests -v
```

Currently covers the Refer & Earn feature end-to-end at the logic level:
referral-code generation/resolution, `credit_referral`'s 10% commission math
and its no-op paths (no referral, free plan, missing referrer email, DB
errors always swallowed — never raised into the caller), `recompute_balance`
folding `referral_earnings` into the existing wallet (including graceful
degradation when migration 0010 hasn't been applied yet), the
`GET /referrals/me` contract, and the `SignUpRequest.referral_code` optional
field. When adding a new module, prefer extending `tests/fakes.py` over
mocking `get_service_client` ad hoc in each test file.

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
    earnings/
      router.py               # /earnings/* + /withdrawals (artist earnings + payouts)
      service.py              # song_stats/artist_balances reads; create_withdrawal (zeroes balance)
      schemas.py              # WithdrawalRequestBody
    referrals/
      service.py              # referral_code gen, resolve_referrer, credit_referral (10% commission)
      router.py               # GET /referrals/me
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
    auth.js                   # login, signup(..., referralCode), logout, getCurrentUser
    billing.js                # FEATURES enum, canAccess(), fetchPlans(), changePlan()
    payment.js                # Razorpay order + verify flow; ProfileIncompleteError
    profile.js                # getProfile(), updateProfile()
    referrals.js              # getMyReferrals() -> GET /referrals/me
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

**Frontend → backend:** all calls use `credentials: 'include'` (cookies). The
API base URL is centralized in `src/lib/config.js` as `API_BASE` — read from the
`VITE_API_BASE` env var (prod = `https://api.tunefry.com`), falling back to
`https://backend1-xzx5.onrender.com` when the env var is unset.
Do not hardcode the backend URL in new files; import `API_BASE` from
`src/lib/config.js` instead.

**Safari/incognito login — FIXED (2026-08-11) via shared parent domain.** The
frontend (`tunefry.com`/`www`, Vercel) and backend are now on the same
registrable domain: backend served at `api.tunefry.com` (Render custom domain,
CNAME → `backend1-xzx5.onrender.com`). Session cookies are scoped
`Domain=.tunefry.com` (first-party for both apps), so iOS Safari ITP / incognito
no longer block them. Production config: Render `COOKIE_DOMAIN=.tunefry.com`,
`COOKIE_SAMESITE=none`, `COOKIE_SECURE=true`, `FRONTEND_URL=https://tunefry.com`,
`EXTRA_CORS_ORIGIN=https://www.tunefry.com`,
`OAUTH_CALLBACK_BASE_URL=https://api.tunefry.com`; Vercel
`VITE_API_BASE=https://api.tunefry.com` (Vite inlines at build → redeploy needed
to change); Supabase redirect allowlist includes `tunefry.com` + `api.tunefry.com`.
**Gotcha:** `VITE_API_BASE` points at a domain that must actually resolve — if
`api.tunefry.com` DNS/cert is missing, the whole site goes down in *every*
browser (not just Safari). Full write-up in `docs/auth-crosssite-cookie-fix.md`.

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
| PUT | `/profile/me` | Protected — partial upsert via service-role. `full_name`, `artist_name`, `phone` are in `EDITABLE_FIELDS` and always accepted. On the frontend (`Profile.jsx`) these inputs are `disabled` unless the user has `CUSTOM_LABEL` feature (Double Artist / Label plan); Spotify and Apple Music URLs follow the same gate and additionally lock permanently after first save on lower plans. |

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
| GET | `/admin/users` | Paginated users + subscriptions + profiles. `full_name`/`artist_name`/`phone` sourced from `public.profiles` (authoritative) with `auth.users.user_metadata` as fallback for legacy rows. |
| POST | `/admin/users` | Create a pre-confirmed user (bypasses email verification). Body: `{email, password, full_name?, artist_name?, phone?, plan?}`. Upserts profile + assigns plan; `handle_new_user` trigger auto-creates the Free subscription row. Returns 409 on duplicate email. |
| PATCH | `/admin/users/{uid}` | Writes ALL profile fields (including `full_name`/`artist_name`/`phone`) to `public.profiles` first (blocking). Auth `user_metadata` sync is best-effort afterwards — failure logs a warning but never returns 502. Also accepts `plan` (changes subscription plan) and `expires_at` (`"YYYY-MM-DD"` to set end-of-day UTC expiry; `""` to clear to NULL = never expires; omit/null = no change). `expires_at` is applied AFTER `assign_plan()` so it wins when both arrive in the same request. Response includes `expires_at` key only when it was updated. |
| PATCH | `/admin/users/{uid}/password` | Set a new password for any user (admin override). Body: `{password}` (min 6 chars). Passwords are always bcrypt-hashed — plaintext is never stored or retrievable. |
| DELETE | `/admin/users/{uid}` | Delete user (cascades) |
| GET | `/admin/submissions/{category}` | Ordered by `created_at` DESC only — status never affects ordering, so a card's position stays stable across approve/decline and reload (was previously pending-first via an alphabetical status sort; removed 2026-09-01 because it sank just-reviewed cards to the bottom on every refetch). category ∈ `new-songs`, `transfer-songs`, `new-albums`, `transfer-albums`, `profile-mismatch`, `claim-removal`, `insta-link` (new/transfer are split — not combined `songs`/`albums`). Plan badge = user's **live** plan (joined from `subscriptions`), not the stored `user_plan` snapshot. Optional `?q=` (substring match on song/album title from `data` JSONB **or** `user_email`) and `?plan=` (filter by live plan; `all`/blank = no filter) — both applied server-side **after** the live-plan join and **before** pagination, so `total`/`total_pages` reflect the filtered set |
| PATCH | `/admin/submissions/{id}` | Approve / decline; inserts new-artist-queue if approved; fires non-blocking `asyncio.create_task(send_email(...))` to artist via Resend — failure never blocks the response |
| DELETE | `/admin/submissions` | Bulk delete; JSON body `{ids: [...]}` (single or many). Also deletes each row's R2 files (`cover_art_key`/`audio_key`/`songs[].audio_key`) — but only keys **no surviving submission still references** (R2 keys are `{artist}/{release}`-derived and not unique per row, so resubmits share objects). Returns `{deleted: N}` |
| GET | `/admin/new-artist-queue` | Pending queue entries |
| PATCH | `/admin/new-artist-queue/{id}` | Save Spotify + Apple Music links |
| GET | `/admin/purchases` | All paid subscriptions + revenue stats |
| GET | `/admin/home` | Fetch CMS content |
| PUT | `/admin/home` | Update CMS content |
| POST | `/admin/home/artist-image` | Upload artist image to R2 (5 MB, JPEG/PNG/WebP) |
| GET | `/admin/media/download-url` | 15-min presigned R2 GET URL for a key |
| POST | `/admin/notifications` | Admin-only; inserts a broadcast row into `public.notifications`; body `{title, body}` |
| GET | `/admin/withdrawals` | All withdrawal requests, **pending first** then newest; each row carries the artist `snapshot` (plan/name/city/state/age) + `payout_details`. Returns `{requests, total_pending}` |
| PATCH | `/admin/withdrawals/{id}` | Mark request **paid** (`{status:"paid", admin_note?}`); sets `processed_at`. Balance was already zeroed at request time so no balance change here |
| DELETE | `/admin/withdrawals/{id}` | Delete a request. If it was **not** paid, credits `amount` back to `artist_balances.available_balance` (so no earnings are lost); paid requests are removed without crediting |

### Notifications
| Method | Path | Notes |
|--------|------|-------|
| GET | `/notifications/announcements` | Protected (auth cookie required); returns last 20 admin broadcast rows ordered by `created_at DESC` |

### Earnings / Withdrawals
| Method | Path | Notes |
|--------|------|-------|
| GET | `/earnings/me` | Protected — `{total_streams, total_revenue, available_balance, monthly[], platforms[], songs[]}` from `public.song_stats`, scoped by `current_user.email`. `monthly` = all-songs aggregated monthly totals (sorted chronologically); `platforms` = all-songs aggregated platform totals (sorted by streams desc). `period_month` in `song_stats` is a **full name string** (`"January"` … `"December"`), not a number — chart components must use name-keyed lookups, not array indexing. `total_revenue` includes referral commissions (`referral_earnings` sum folded in — see `get_earnings_summary` in `earnings/service.py`) so it stays consistent with `available_balance`/`artist_balances.total_earned`; the per-song/per-month/per-platform breakdowns stay stream-only (referral money has no song/month association, so only the top-level aggregate changes). |
| GET | `/earnings/balance` | Protected — `{available_balance, total_earned, total_withdrawn, min_withdrawal: 1500, eligible}` from `public.artist_balances` |
| GET | `/earnings/songs/{submission_id}` | Protected — per-release platform-group breakdown (`platforms[]`, majors + `Other`) + monthly trend (`monthly[]`) |
| GET | `/withdrawals/me` | Protected — the user's own withdrawal request history |
| POST | `/withdrawals` | Protected — creates a **full-balance** payout request. Amount is server-derived from `artist_balances` (never trusts client), must be ≥ ₹1500; snapshots artist plan/name/city/state/age-from-DOB + `payout_details` (UPI or bank); on success **zeroes** `available_balance`. 400 if below the minimum. Admin later marks it paid or deletes it |

### Refer & Earn
| Method | Path | Notes |
|--------|------|-------|
| GET | `/referrals/me` | Protected — `{referral_code, referred_count, referrals: [{email, plan, joined_at}], total_referral_earned}`. Referral code is lazily generated + persisted to `profiles.referral_code` on first call (no backfill needed for pre-existing users). |

**Referral commission model**: each user has a deterministic referral code
(`"TF" + user_id.hex[:8].upper()` — pure string derivation, no randomness, no
external calls, no collision-checking needed). A signup with `referral_code`
set (`SignUpRequest.referral_code`, optional) records a `public.referrals`
row (migration 0010) linking referrer → referred user; a bad/unknown code
never blocks signup. Whenever the referred user's plan activates, the
referrer gets **10% of that plan's price**, credited to their *existing*
`artist_balances.available_balance` (via `earnings.service.recompute_balance`,
which now also sums `referral_earnings` — migration 0010's immutable audit
ledger — alongside `song_stats`). This repeats on every future purchase, not
just the first. `referrals.service.credit_referral(...)` is called explicitly
from three sites (NOT baked into `billing.service.assign_plan`, which stays a
pure persistence function):
- `billing/router.py` `verify_payment_and_upgrade` — always credits; the
  existing replay-protection check guarantees every call here is a genuinely
  new payment.
- `admin/router.py` `admin_create_user` — always credits (first plan grant).
- `admin/router.py` `update_user` (`PATCH /admin/users/{uid}`) — credits
  **only if** the requested plan differs from the subscription row's plan
  *before* the call, so an admin resaving unrelated profile fields (which
  still sends the same `plan` value) can't double-credit.
- **`POST /billing/change-plan` (dev-only QA endpoint) deliberately never
  credits** — it must never fabricate wallet balance during testing.

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
| `0005_plan_confirmed.sql` | `subscriptions.plan_confirmed` boolean (default false; backfills paid users to true) |
| `0006_notifications.sql` | `public.notifications` (id UUID PK, title TEXT, body TEXT, created_at TIMESTAMPTZ); no RLS — read/write via service-role only through API |
| `0007_backfill_profile_names.sql` | One-time backfill: copies `full_name`/`artist_name`/`phone` from `auth.users.raw_user_meta_data` → `public.profiles` for rows where the column is NULL/empty. COALESCE-safe — never overwrites existing values. Run Step 1 (SELECT) first as dry-run, then Step 2 (UPDATE). |
| `0008_earnings.sql` | `public.song_stats` (per song × platform-group × month; `revenue NUMERIC(20,10)`; UNIQUE `(user_email, song_title, platform, period_month, period_year)` → monthly upsert is idempotent), `public.artist_balances` (per-user `total_earned`/`total_withdrawn`/`available_balance`, all `NUMERIC(20,10)`), `public.withdrawal_requests` (payout queue; `amount`, `status` ∈ `pending`/`paid`, `method`, `payout_details` JSONB, `snapshot` JSONB). RLS: authenticated read-own by JWT email/uid; service-role writes only. Populated by `migration/ingest_streams.py`. |
| `0009_custom_label_name.sql` | `profiles.custom_label_name TEXT` — stores the custom label name for Double Artist / Label plan artists. Editable by admin via `PATCH /admin/users/{uid}` and by the artist via `PUT /profile/me` (gated by `CUSTOM_LABEL` entitlement in the frontend). In `EDITABLE_FIELDS` in `profile/service.py`. |
| `0010_referrals.sql` | `profiles.referral_code TEXT UNIQUE` (lazily populated, not backfilled), `public.referrals` (referrer_user_id, referred_user_id UNIQUE, referral_code_used), `public.referral_earnings` (immutable audit ledger: referrer_user_id/email, referred_user_id, plan, amount, source, payment_ref). See "Refer & Earn" under Endpoints for the crediting model. |

RLS summary:
- `subscriptions`: user reads own row; service-role writes only.
- `profiles`: user reads own row; service-role writes only.
- `song_stats` / `artist_balances` / `withdrawal_requests`: user reads own rows (by JWT email/uid); service-role writes only.
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

**Form parity:** `NewSong.jsx` and `TransferSong.jsx` (frontend) must submit the
same field set; Transfer additionally sends `upc_code` / `isrc_code`. Both share
the `tunefryCustomLabelName` localStorage key for the `CUSTOM_LABEL` entitlement,
and store `yt_beat` / `explicit` / `yt_content_id` as lowercase `yes`/`no` and
genre under the `genre` key. Submission rows are **immutable JSONB** — data is
captured at submit time and never backfilled, so historical rows keep only the
fields that existed when they were created (they won't gain later-added fields).

**Album forms (`NewAlbum.jsx` / `TransferAlbum.jsx`) mirror the single-song
forms:** release dates are **album-level** — `original_release_date` /
`go_live_date` are sent as top-level form fields (one pair per album), NOT
per-track. There is **no per-track `duration`** (removed — was meaningless).
Per-track `mood` is a **single string** (`mood` key, matching NewSong), not the
old `moods` array. When a track's YouTube-beat toggle is "yes" a required
`yt_beat_link` URL is captured on that track (same as NewSong). `yt_beat` /
`yt_content_id` / `explicit` serialize to lowercase `yes`/`no` on both album
forms. Because rows are immutable, **old album rows** still carry the legacy
shape (per-track `duration`, per-track dates, `moods` array) — the admin viewer
(`SecretPanel.jsx`) keeps fallback reads (`track.mood ?? track.moods`,
`track.original_release_date || track.originalReleaseDate`) so they still render.

**Per-track artist shape (album forms, as of 2026-08-18):**
`songs[].main_artists[]` = `{name, spotify, apple_music, instagram}` — Instagram
is **main-artist-only** by design. `songs[].featured_artists[]` =
`{name, spotify, apple_music}` — Instagram is intentionally **omitted** for
featured artists (input hidden in both album forms; payload strips it; admin
viewer skips it even if a legacy row has it). The admin album-track renderer in
`SecretPanel.jsx` (inside the `k === 'songs'` branch) shows per-artist mini-
cards with these exact keys — not just names. Historical rows submitted before
this contract may have `main_artists[].instagram === undefined` and/or
`featured_artists[].instagram` present; both are handled gracefully by the
viewer's `filter((k) => a[k])` (empty-key skip) and the hard-coded featured
key list. **TransferAlbum previously had no UI inputs for the required album
dates** — fixed 2026-08-18 by mirroring NewAlbum's date-picker JSX into the
Step-01 Album Info form-grid.

**Admin plan display:** `list_submissions` overrides each row's stored
`user_plan` with the user's live plan (email → user_id → `subscriptions` join,
same source as `/admin/users`). The stored `submissions.user_plan` is a stale
JWT snapshot — it reads free after upgrades, or for everyone if the
access-token hook isn't stamping the plan claim.

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
- **Never use `.maybe_single()` on a query that can legitimately match zero
  rows.** It sets `Accept: application/vnd.pgrst.object+json`, and PostgREST
  returns **406** (not 200-with-null) for zero matches — the installed
  `postgrest-py` raises `APIError` on that instead of returning `None`. This
  bit `recompute_balance()`'s `artist_balances` lookup for any user with no
  pre-existing balance row (e.g. a referrer who never sold a song, credited
  for the first time via a referral) — the exception was silently swallowed
  by `credit_referral`'s try/except, leaving `available_balance` stale with
  no trace. Fixed by switching to the safe pattern already used in
  `get_balance()`: plain `.select(...).eq(...).limit(1).execute()` +
  manual `(res.data or [None])[0]` check, which never raises on zero rows.
  Two more latent instances of the same bug were fixed in
  `admin/router.py`'s song-stats PATCH/DELETE-by-id lookups (were returning
  a confusing 502 instead of the intended 404 for a missing row).

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
   Order matters: 0001 → 0002 → both 0003 files (either order) → 0004 → 0005.
   Note there are **two files prefixed `0003_`** — run both.
2. Enable the **custom access-token hook**:
   Auth → Hooks → JWT Claims Customization →
   `public.custom_access_token_hook`.
   Without this, all users fall back to Free plan.
3. Add backend and frontend URLs to the **Redirect Allow List** in
   Auth → URL Configuration.
4. Verify the Resend sender domain in the Resend dashboard; set
   `RESEND_FROM_EMAIL` to a verified address.

## Legacy data migration (`migration/`)

One-time scripts to import artists + releases from the legacy SQL Server dump
(`*.sql` SSMS export, UTF-16 with BOM).

### Scripts

| Script | Purpose |
|--------|---------|
| `migrate_users.py` | Creates Supabase auth users + `profiles` + `subscriptions` rows from legacy `Users`/`PaymentDetails` tables |
| `migrate_releases.py` | Inserts `submissions` rows from legacy `ReleaseDetails` table; requires users already present |
| `fix_migrated_status.py` | One-time corrector: sets migrated rows to `declined` where `IsActive=0`; idempotent |
| `ingest_streams.py` | **Historically frozen** — one-time ingest of legacy `dbo.MusicStreams` (SQL dump). Do NOT re-run: it rewrites `total_withdrawn` from its own view (tunefry + WithdrawalHistory + paid requests) and would corrupt `withdrawn_baseline.json`'s invariants. See invariants below. |
| `ingest_royalty_report.py` | **Monthly** ingest of the DSP-consolidated Excel report (`Combined_All` sheet). USD → INR via `fx_rates.json`. Period-replace strategy: deletes existing `song_stats` for covered months (per matched user) then re-inserts — fully idempotent. Recomputes `artist_balances` from full song_stats table + `withdrawn_baseline.json` + current paid/pending. Default is **dry-run**; requires `--live` to write. Full runbook: [`MONTHLY_ROYALTY_INGESTION.md`](migration/MONTHLY_ROYALTY_INGESTION.md). |
| `compute_withdrawn_baseline.py` | **One-time** capture of each artist's legacy withdrawn (tunefry + WithdrawalHistory) into `withdrawn_baseline.json`. Uses `processed_at ≤ artist_balances.last_updated + 1 min` to identify paid requests already reflected in `total_withdrawn`; refuses to write on deficit rows without `--force`. Read-only against Supabase. |
| `platform_map.py` | Shared normalization for both ingest scripts. `raw source_platform` → `(canonical_name, platform_group)`. **Group must be one of** `Spotify`, `Apple Music`, `YouTube`, `Facebook`, `Amazon`, `JioSaavn`, `Gaana`, `TikTok`, `Other` — the frontend Stats > Platform chart groups on this exact set. |
| `legacy_artist_stats.py` | **Read-only, standalone** — no Supabase/env dependency. Given `--artist "<name>"` (+ optional `--month`/`--year` pair, `--user-id` to disambiguate, `--json`), parses `dbo.Users` / `dbo.MusicStreams` / `dbo.WithdrawalHistory` straight from the raw dump and reports combined all-platform stats, per-song breakdown, monthly breakdown, and **remaining available balance** = `Σ(Revenue − RedeemedAmount)` over the matched rows — a derivation not exposed anywhere else, since `RedeemedAmount` is a per-row withdrawal-allocation marker, not `round(Revenue, 2)`. Join key `Users`→`MusicStreams.ArtistName` is ambiguous (tries `Username`/`FullName`/`ArtistName` against actual distinct values in `MusicStreams`); on multiple `Users` matches it lists candidates and exits rather than guessing. Invoked via the `/legacy-artist-stats` skill (`.claude/skills/legacy-artist-stats/SKILL.md`). |

### Key invariants

- **Multiline robustness** — both scripts use `iter_insert_statements()` (defined in
  `migrate_releases.py`, imported by `migrate_users.py`) to accumulate physical SQL lines
  until the quote count is even and the row ends with `)`. Fixes SSMS exports where a
  free-text field (bio, error message) contains a literal newline that splits the row.
- **`migrate_users.py` is create-only by default** — existing emails are skipped entirely
  (no profile/sub upsert). Pass `--update-existing` to re-upsert; this clobbers any
  post-migration user edits and is **production-dangerous**.
- **`migrate_releases.py` is idempotent** — pre-fetches the set of
  `data->>legacy_release_id` from existing migrated rows, skips any `ReleaseID` already
  present. Safe to re-run; will only insert new releases.
- **Status derivation** — `IsActive='0'` → `declined`; else `approved`. The `error` field
  from legacy `ReleaseDetails` is preserved in `admin_note` for declined rows.
- **`plan_confirmed`** — set to `True` for any paid plan subscription row written by
  `migrate_users.py`; required for the app to surface non-free plans.

### `ingest_royalty_report.py` invariants (earnings — money-critical, active monthly script)

- **Attribution (6-tier priority)** — (1) Legacy ISRC map (`migration/legacy_map.json`); (2) **Supabase ISRC-only** — an ISRC that unambiguously belongs to a single Supabase submission owner (derived from `isrc_to_sub` with a >1-email drop); (3) Supabase (email, ISRC) → submission_id match (requires artist candidate to resolve first); (4) normalized `artist` / `sub_label` → email from `public.profiles.artist_name` + `auth.users.user_metadata` + legacy name maps (`legacy_map.json` artist_name / full_name / username); (5) Legacy `(artist‖title)` fallback from `legacy_map.json`; (6) **Supabase title-only** — the report's normalized song title unambiguously belongs to a single Supabase submission owner (derived from `title_to_sub` with a >1-email drop). Multi-artist rows (double-space, comma, "feat"/"ft"/"x"/"&"/"and") are split into candidates. Names/ISRCs/titles mapping to more than one email are **dropped** (never guess). Every run prints per-tier row counts + INR + distinct users so the operator can review new-tier attributions before `--live`. Unmatched artists → `unmatched_YYYYMMDD_HHMM.csv` next to the workbook (columns: `artist, sub_label, row_count, total_royalty_usd, total_royalty_inr, total_streams` — sorted by INR in INR-workbook mode, by USD otherwise).
- **`submission_id` linkage** — (email, ISRC) map from `submissions.data->>isrc` / `data->>isrc_code` / `data->'songs'[N]->>isrc`. Fallback: (email, normalized song_title). If neither matches, `submission_id` is NULL and the per-song modal shows empty for that song.
- **Workbook format auto-detected** — script checks for `royalty_inr` column in the first 100 rows. If present: **INR workbook** (`Royalty_Reports_*_INR.xlsx`) — uses `royalty_inr` directly, no FX applied, FX guard skipped. If absent: **USD workbook** (`Royalty_Detail_Reports_*.xlsx`) — uses `royalty` column × `fx_rates.json["YYYY-MM"]`; script **refuses to run** if any period lacks an FX entry. Plan royalty % is NOT applied in either mode.
- **Apple Music INR CSV (`--csv PATH`)** — Optional supplementary source. Columns: `item_artist`, `song_name`, `total` (streams), `royality` (net INR — no FX applied). Platform fixed to `Apple Music`. Period auto-detected from `_MM_YYYY_` filename pattern; override with `--csv-period YYYY-MM`. CSV data **fully replaces** xlsx Apple Music rows for the same period (prevents double-counting).
- **Reconciliation cap (`--expected-net AMOUNT`)** — After aggregation the script checks `total_to_write_inr ≤ expected_net`. If breached: **aborts before any live write**. In dry-run: warns. Also accepts `--expected-gross` for an informational diff.
- **`Dispute Resolution` rows are EXCLUDED** — `adjustment_type == "Dispute Resolution"` rows skipped entirely.
- **`song_stats.period_month` MUST be the full English month name** (`"February"`, not `"02"` or `"Feb"`) — required by the frontend `_MONTH_ORDER` chart map in `app/modules/earnings/service.py`.
- **`platform_group` MUST match the frontend's exact set** (see `platform_map.py`). Facebook / Meta → `Facebook` (so Overview UGC toggle works). Snap → `Other`.
- **Period-replace write strategy** — for each user with rows in the file, `DELETE` existing `song_stats` for covered (period_month, period_year) tuples, then INSERT fresh rows. Fully idempotent. Older periods never touched. If `--csv` covers a period not in xlsx, a platform-scoped delete (`platform='Apple Music'`) runs instead.
- **Balance formula** — `total_withdrawn = withdrawn_baseline.json[email] + Σ(withdrawal_requests.paid)`; `available_balance = max(0, total_earned − total_withdrawn − Σ(pending))`. `total_earned` recomputed from the FULL `song_stats` table each run.
- **`withdrawn_baseline.json` is IMMUTABLE** — one-time snapshot of each artist's legacy withdrawn. If regenerated after live runs, double-counts paid requests. If lost: restore from git, do NOT regenerate.
- **Dry-run is the default** — `--live` flag required for actual writes. Every run produces `unmatched_*.csv` regardless of mode.

### `ingest_streams.py` invariants (historically frozen — do NOT re-run)

- **Attribution** — a `MusicStreams` row maps to a user via `(ArtistName, Song)` →
  `ReleaseDetails.(Artist, Song|SongTitle)` → `ReleaseID` → the migrated `submissions`
  row (`data->>legacy_release_id`) → `user_email` + `submission_id`. Fallback:
  `ArtistName` → `Users.ArtistName` → `Email` (no `submission_id`). Unmatched rows are
  counted and skipped (never guessed).
- **`IsDeleted=1` rows are EXCLUDED** from earnings (soft-deleted); only `0`/NULL count.
- **Revenue is full-precision** — summed from the `Decimal(18,10)` `Revenue` column into
  `NUMERIC(20,10)`; `RedeemedAmount` is ignored for totals. `MusicStreams.Revenue` is the
  artist-payable net — plan royalty % is **not** re-applied.
- **`platform_group`** collapses the messy platform variants into majors (Spotify, Apple
  Music, YouTube, Facebook/Meta, Amazon, JioSaavn, Gaana, TikTok); everything else →
  `Other`. The negative-revenue rows on pseudo-platform `tunefry` are prior redemptions:
  excluded from `song_stats`, counted as withdrawn.
- **Balance** — `available_balance = total_earned − total_withdrawn − pending`, where
  `total_withdrawn` = tunefry adjustments + `WithdrawalHistory` (Completed) + Supabase
  `withdrawal_requests` (paid), and `pending` = Supabase requests still `pending`. So a
  re-run never resurrects a balance a user already requested. Never goes negative.
- **Withdrawal amount is server-derived** (= full `available_balance`, must be ≥ ₹1500);
  requesting zeroes the balance. Admin `Delete` on an unpaid request credits it back.

### Production safety

Running `migrate_users.py` (even with the create-only default) against live production
**will create new auth users** in Supabase if any new emails are in the dump. Re-running
it with `--update-existing` overwrote ~1,726 subscriptions + 1,727 profiles in one
incident (no backup on free Supabase plan). Always dry-run first:
```bash
python migration/migrate_users.py "<dump.sql>" --dry-run
python migration/migrate_releases.py "<dump.sql>" --dry-run
# ingest_streams.py is FROZEN — do NOT re-run. Historical only.
# Monthly earnings ingest — dry-run is the default:
python migration/ingest_royalty_report.py "migration/reports/YYYY-MM.xlsx" \
    --fx-rates migration/fx_rates.json \
    --csv "migration/reports/applemusic_process_MM_YYYY_detail_report.csv" \
    --expected-net 126361.52
# Add --live once dry-run is reviewed. Full runbook:
#   migration/MONTHLY_ROYALTY_INGESTION.md
```

## Deploy

- **Backend**: `Dockerfile` + `render.yaml` → Render web service. Push to
  `main` → auto-deploy. All secrets set in Render dashboard.
- **Frontend**: Vercel; all routes rewrite to `index.html` (SPA). Backend base
  URL hardcoded in `src/lib/` files.
- Health check endpoint: `GET /health` (Render ping).
- `SESSION_SECRET` should be auto-generated by Render (not committed).

## Claude Code hooks (`.claude/`)

Hooks wired in `.claude/settings.json` that fire automatically during Claude Code sessions:

| Hook | File | Fires on | What it does |
|------|------|----------|-------------|
| PreToolUse | `pre-safety-hook.sh` | Every `Bash` + every `Edit`/`Write` (90 s cooldown) | Emits production-safety reminder. **Always** fires on `git push` (push = Render deploy) and on dangerous Bash patterns (Supabase writes, migration scripts). |
| PreToolUse | `plan-mode-hook.sh` | `EnterPlanMode` | Injects a senior-SDE/architect review protocol; Claude must output a `PLAN SCORE: XX/100` (re-scoring until ≥85) before calling `ExitPlanMode`. |
| PreToolUse | `dep-guard-hook.sh` | `Bash` running `git commit`/`git push` | Blocks (via `permissionDecision: ask`) when staged dependency files (`requirements*.txt`, `pyproject.toml`, etc.) touch a critical package (fastapi, starlette, supabase, gotrue, postgrest, boto3, httpx, pyjwt/python-jose, pydantic, uvicorn) — surfaces the exact diff lines before approval. |
| PreToolUse | `push-gate-hook.sh` | `Bash` running `git push` | Always requires explicit approval before push. If `DIAL_API_KEY` env var is set, calls EPAM Dial AI to classify the push HIGH/LOW stakes and summarize it; otherwise fail-opens to a neutral LOW classification (no hardcoded secret in the script — this file is git-tracked). Any `supabase/migrations/*.sql` file in the push forces HIGH stakes (manual-run migrations, not auto-applied). |
| PreToolUse | `frontend-integration-hook.sh` | `Edit`/`Write` on a path containing `tunefry frontend` | Reminds about this repo's actual frontend conventions (service modules in `src/lib/*.js`, `API_BASE` from `src/lib/config.js`, cookie-based `credentials: include`, `AuthContext.jsx`, `PlanGate`) before editing sibling-repo frontend files. |
| PreToolUse | `frontend-design-hook.sh` | `Edit`/`Write` on `.jsx`/`.css` under a path containing `tunefry frontend` | Design-quality checklist (bold aesthetic direction, no AI-slop defaults, distinctive typography) — companion to `frontend-integration-hook.sh`. |
| PostToolUse | `review-hook.sh` | Every `Edit`/`Write` | **Phase 3 (CLAUDE.md sync) always fires.** Phases 0–2 (full review + scoring) have a 30 s cooldown to avoid spam on rapid multi-file edits — during cooldown a lightweight Phase 3-only reminder is emitted instead. |
| PostToolUse | `post-data-safety-hook.sh` | Every `Edit`/`Write` (no cooldown) | Targeted safety checklist based on file type: migration SQL → idempotency/blast-radius; dependency files → no-downgrade guard; app source → regression/contract check. |

**Key gotcha**: hooks run via `bash`, so they require Git Bash on Windows (already present). The cooldown files live in `/tmp/` — they reset on reboot or WSL restart, which is fine (just means the first edit after restart always triggers the full reminder).

**`push-gate-hook.sh` / `dep-guard-hook.sh` / `plan-mode-hook.sh` / `frontend-*-hook.sh` ported from the SCDM project (2026-09-01)** — adapted for this repo's stack (FastAPI/Supabase/R2, not Alembic/Celery) and this repo's sibling-repo frontend layout (`tunefry frontend`, not a `frontend/` subfolder). The original SCDM `push-gate-hook.sh` had a live EPAM Dial API key hardcoded; this repo's copy never hardcodes it — `push-gate-hook.sh` reads `DIAL_API_KEY` from the environment, and additionally sources `.claude/push-gate.local.sh` if present (gitignored — see `.gitignore`) to set it. The actual key lives only in that local file, never in a git-tracked one. Without any key configured, the hook still gates every push behind approval, just without the AI-generated stakes summary.
