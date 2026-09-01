#!/bin/bash
# Fires on PreToolUse/Edit|Write — reminds about frontend<->backend integration
# conventions whenever a file in the sibling tunefry-frontend repo is about to
# be created/edited.

input=$(cat)

parsed=$(echo "$input" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('tool_name', ''))
print(d.get('tool_input', {}).get('file_path', ''))
" 2>/dev/null)

if [[ -n "$parsed" ]]; then
  tool=$(echo "$parsed" | sed -n '1p')
  file=$(echo "$parsed" | sed -n '2p')
else
  tool=$(echo "$input" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')
  file=$(echo "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')
fi

if [[ "$tool" != "Edit" && "$tool" != "Write" ]]; then
  exit 0
fi

norm=$(echo "$file" | tr '\\' '/' | tr '[:upper:]' '[:lower:]')

if [[ "$norm" != *"tunefry frontend"* ]] || [[ "$norm" == *"/node_modules/"* ]]; then
  exit 0
fi

python3 -c "
import json
reason = '''FRONTEND<->BACKEND INTEGRATION CHECKLIST for this file:
1. API calls go through src/lib/*.js service modules (auth.js, billing.js, payment.js, profile.js, r2upload.js) - no raw fetch/axios directly in components/pages.
2. Backend base URL only via API_BASE imported from src/lib/config.js (reads VITE_API_BASE, falls back to the Render backend URL) - never hardcode a backend URL in new files.
3. All calls must use credentials: \"include\" (cookie-based sessions) - no Authorization header juggling on the frontend.
4. Auth/session state lives in src/context/AuthContext.jsx (user === undefined => loading, null => logged out, object => logged in) - do not duplicate user state per-page.
5. Feature gating goes through canAccess()/FEATURES from src/lib/billing.js and the <PlanGate> component - do not hand-roll plan checks.
6. Handle loading / error / empty / unauthorized states for every backend-connected view; ProtectedRoute only guards auth, not plan/feature access.
7. Match the backend real response shape - check the backend/app/modules/*/schemas.py and router.py for the actual endpoint contract before wiring a new call; do not invent fields.
8. If a backend Pydantic schema, entitlement, or route changed, verify the corresponding frontend service function and consuming component were updated to match.
9. Vite inlines VITE_API_BASE at build time - changing it requires a Vercel redeploy, not just an env var edit.
10. Keep tunefry-backend/CLAUDE.md (Frontend structure section) in sync if this touches a durable contract/convention (new lib file, new context key, new localStorage key, new route).'''
print(json.dumps({
    'continue': True,
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': reason
    }
}))
"
