# Tunefry Backend

FastAPI + Supabase music distribution platform backend.

## Quick start

```bash
python -m venv venv
# Windows
.\venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # fill in your values
uvicorn app.main:app --reload
```

API docs: http://localhost:8000/docs

---

## Phase 0 — Supabase project setup

1. Create a project at https://supabase.com
2. **Project Settings → API** — copy three values into `.env`:
   - `Project URL` → `SUPABASE_URL`
   - `anon / public` key → `SUPABASE_ANON_KEY`
   - `service_role` key → `SUPABASE_SERVICE_ROLE_KEY` (server-only, never in the browser)
3. **Project Settings → JWT** — verify your project uses asymmetric signing (JWKS). If it still shows a `JWT Secret`, the legacy HS256 path is active; set `SUPABASE_JWT_SECRET` in `.env` until you migrate.
4. **Authentication → Email** — disable "Confirm email" for local development; re-enable it in production.

---

## Phase 3 — Google OAuth setup

### Google Cloud Console
1. Go to https://console.cloud.google.com → APIs & Services → Credentials → Create OAuth 2.0 Client ID.
2. Application type: **Web application**.
3. Authorized redirect URIs: `https://<your-project-ref>.supabase.co/auth/v1/callback`
4. Copy the **Client ID** and **Client Secret**.

### Supabase dashboard
1. Authentication → Providers → Google → enable.
2. Paste the Client ID and Client Secret.
3. Authentication → URL Configuration → add to **Redirect URLs**:
   - `http://localhost:8000/auth/google/callback` (dev)
   - `https://<your-render-app>.onrender.com/auth/google/callback` (prod)

### Same-email collision note
If a user signs up with email/password *and* later tries Google with the same address, Supabase will **link** the accounts by default if "Link accounts with the same email" is enabled in Authentication → Settings. Decide this before going live.

---

## Phase 4 — Password reset setup

In Supabase → Authentication → URL Configuration → **Redirect URLs**, add:
- `http://localhost:8000/auth/reset-password` (dev)
- `https://<your-render-app>.onrender.com/auth/reset-password` (prod)

---

## Phase 5 — Production hardening checklist

- [ ] Replace Supabase's built-in email sender with a real SMTP provider (Resend, SendGrid, or Postmark) — the built-in one is rate-limited and for testing only. Set this in Supabase → Project Settings → Authentication → SMTP Settings.
- [ ] Add all production URLs to the Supabase redirect allow-list.
- [ ] Enable email confirmation (`Authentication → Email → Confirm email`).
- [ ] Ensure `SUPABASE_SERVICE_ROLE_KEY` is only ever set server-side; never shipped to a client.
- [ ] Set `COOKIE_SECURE=true` and `COOKIE_SAMESITE=strict` (or `none` for cross-origin) in production.
- [ ] Enable **Row Level Security (RLS)** on all user-data tables in Supabase with policies keyed to `auth.uid()` — the most commonly skipped Supabase security step.
- [ ] Rotate `SESSION_SECRET` to a cryptographically random value before deploy.
