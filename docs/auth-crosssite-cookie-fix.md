# Auth: Cross-site cookie login failure (incognito / iOS Safari) — fix guide

> **Status: IMPLEMENTED (2026-08-11).** The shared-parent-domain fix below is live:
> backend served at `api.tunefry.com` (Render custom domain, CNAME →
> `backend1-xzx5.onrender.com`), cookies scoped `Domain=.tunefry.com`, frontend
> `VITE_API_BASE=https://api.tunefry.com`. Verified: `api.tunefry.com/health` = 200,
> CORS from `www.tunefry.com` allows credentials, cookie is first-party.
>
> The backend code (`cookie_domain` in `config.py` + `cookies.py`) and the frontend
> `API_BASE` centralization were already in place before this write-up; the final
> steps that shipped were the Render custom domain, the GoDaddy `api` CNAME, the
> Render/Vercel env vars, the Supabase redirect allowlist, and removing the last
> hardcoded backend URL in `src/pages/YourPlan.jsx`.
>
> **Operational gotcha learned in prod:** `VITE_API_BASE` must point at a host that
> actually resolves with a valid TLS cert. Setting it to `api.tunefry.com` *before*
> creating the DNS record / Render custom domain took the whole site down in **every**
> browser (not just Safari) — the frontend called a non-existent host. Create the
> DNS + cert first, verify `/health`, then flip `VITE_API_BASE`.

## Symptom
Email+password login works in normal Chrome, but in **Chrome/Safari incognito** and
on **iOS Safari** the user enters credentials and is bounced back to the home page —
never reaching the dashboard.

## Root cause
The session is carried in httpOnly cookies (`sb-access-token`, `sb-refresh-token`)
set by the backend. Frontend and backend are on **different registrable domains**:
- Frontend: `*.vercel.app`
- Backend: `backend1-xzx5.onrender.com`

So the cookie is a **third-party cookie** relative to the frontend. Normal Chrome
still allows third-party cookies (with `SameSite=None; Secure`, which prod already
uses — otherwise even normal Chrome would fail on cross-site `fetch`). But **iOS
Safari (ITP) and incognito mode block all third-party cookies**, so the cookie is
dropped immediately after login → `/auth/me` returns 401 → `ProtectedRoute` redirects
to `/home`. `SameSite=None` does not help; the block is at the third-party level.

Relevant code (current state):
- `app/modules/auth/cookies.py` `_set()` — sets cookies with `httponly`, `secure`,
  `samesite`, `path="/"`, **no `domain`** attribute.
- `app/core/config.py` — `cookie_secure`, `cookie_samesite` settings; **no
  `cookie_domain`**.
- `app/modules/auth/dependencies.py` `get_current_user` — already accepts a Bearer
  token as fallback (`token = access_token or _extract_bearer(authorization)`).
- Frontend hardcodes `const BASE = 'https://backend1-xzx5.onrender.com'` in ~17 files
  (all `src/lib/*.js` + several `src/pages/*`), all using `credentials: 'include'`.
- `vercel.json` has only the SPA fallback rewrite — no `/api` proxy.

## Chosen fix: shared parent domain (custom `api` subdomain)
Serve both apps under one registrable domain so the cookie is **first-party**:
- Frontend → `app.tunefry.com` (or apex `tunefry.com` + `www`)
- Backend  → `api.tunefry.com`
- Cookie `Domain=.tunefry.com` → same-site for both → not blocked by Safari/incognito.

Chosen over the Vercel `/api` reverse-proxy (large audio-master uploads would risk
Vercel proxy body-size limits) and over Bearer-token/localStorage (weaker XSS posture
+ larger refactor). Uploads keep going straight to the backend.

### Implementation steps

**1. DNS + hosting**
- Add `app.tunefry.com` (or apex) as a custom domain on **Vercel**; follow Vercel's
  DNS records.
- Add `api.tunefry.com` as a custom domain on **Render** (CNAME to the Render host);
  wait for the TLS cert to issue.

**2. Backend — add a cookie domain setting**
- `app/core/config.py`: add `cookie_domain: str | None = None` (read from
  `COOKIE_DOMAIN`).
- `app/modules/auth/cookies.py` `_set()`: pass
  `domain=settings.cookie_domain or None` to `response.set_cookie(...)`.
  (`clear_session_cookies` / `delete_cookie` must pass the same `domain` so deletion
  matches.)
- Render env vars:
  - `COOKIE_DOMAIN=.tunefry.com`
  - `COOKIE_SECURE=true`
  - `COOKIE_SAMESITE=none`  (safe; `lax` also works once same-site)
  - `FRONTEND_URL=https://app.tunefry.com`
  - `EXTRA_CORS_ORIGIN=https://tunefry.com,https://www.tunefry.com` (if apex/www used)
  - `OAUTH_CALLBACK_BASE_URL=https://api.tunefry.com` (for when Google OAuth is turned
    on — currently a "coming soon" placeholder, so not urgent)
- CORS in `app/main.py` already has `allow_credentials=True`; it will pick up the new
  `FRONTEND_URL` / `EXTRA_CORS_ORIGIN`.

**3. Supabase dashboard**
- Add `https://app.tunefry.com` and `https://api.tunefry.com` to Auth → URL
  Configuration → Redirect Allow List.

**4. Frontend — point at the new API host (and centralize it)**
- Replace the ~17 hardcoded `const BASE = 'https://backend1-xzx5.onrender.com'` with a
  single source: create `src/lib/config.js` exporting
  `export const API_BASE = import.meta.env.VITE_API_BASE` and import it everywhere;
  set `VITE_API_BASE=https://api.tunefry.com` in Vercel env. (Representative files:
  `src/lib/auth.js`, `src/lib/billing.js`, `src/lib/payment.js`, `src/lib/profile.js`,
  `src/lib/home.js`, and pages `Overview.jsx`, `Releases.jsx`, `YourPlan.jsx`,
  `PitchSong.jsx`, `InstaLink.jsx`, `ClaimRemoval.jsx`, `ProfileMismatch.jsx`,
  `admin/SecretPanel.jsx`, `upload/NewSong.jsx`, `upload/NewAlbum.jsx`,
  `upload/TransferSong.jsx`, `upload/TransferAlbum.jsx`.) Keep `credentials: 'include'`
  on every call.
- Also fix the placeholder `src/lib/r2upload.js` `/api/upload/r2` if that path is ever
  wired (currently unused placeholder).

**5. Secondary UX bug (do alongside) — `src/pages/auth/Login.jsx:15-28`**
Login navigates to `/` even when the session didn't stick, causing the silent bounce.
Guard it:
```js
await apiLogin(form.email, form.password)
const u = await refreshUser()
if (!u) { setError('Could not establish a session. Please try again.'); return }
navigate('/', { replace: true })
```
This surfaces a real error instead of a confusing redirect if cookies ever fail again.

### Verification
1. Deploy backend (Render) + frontend (Vercel) on the new domains.
2. **iOS Safari** and **Chrome incognito**: log in with email+password → lands on the
   dashboard (not `/home`).
3. DevTools → Application → Cookies: `sb-access-token` present under `.tunefry.com`,
   `Secure`, `HttpOnly`, and sent on the `/auth/me` request (200, not 401).
4. Upload an audio master → succeeds (uploads go directly to `api.tunefry.com`, no
   proxy size limit).
5. Regression: normal Chrome still logs in and stays logged in across refresh.

### Rollback
Revert `VITE_API_BASE` to the onrender URL and unset `COOKIE_DOMAIN`; the old
cross-site behavior returns (works in normal browsers).
