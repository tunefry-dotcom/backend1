#!/bin/bash
# PostToolUse hook — fires after Edit/Write on sensitive file types.
# Injects targeted safety instructions based on what was just changed:
#   • Migration SQL  → data-safety + idempotency checklist
#   • requirements.txt / package.json → dependency-downgrade guard
#   • App code (*.py / *.jsx / *.js / *.ts) → existing-function regression check

input=$(cat)

tool=$(echo "$input" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')

if [[ "$tool" != "Edit" && "$tool" != "Write" ]]; then
  echo '{"continue": true}'
  exit 0
fi

file=$(echo "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')

# ── Migration SQL ────────────────────────────────────────────────────────────
if echo "$file" | grep -qi 'migrations/.*\.sql'; then
  cat <<'EOF'
{
  "continue": true,
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "MIGRATION SAFETY CHECK (auto-triggered because a migration SQL file was just modified):\n\n1. EXISTING DATA — Will this run modify or delete any row that already exists in production? Every UPDATE/DELETE must use a WHERE clause that is as narrow as possible. COALESCE or NULLIF guards must be present if backfilling NULLs so non-NULL values are never overwritten.\n2. IDEMPOTENCY — Running this migration twice must produce the same result (no duplicate rows, no double-decrement). Use IF NOT EXISTS, ON CONFLICT DO NOTHING, or a WHERE ... IS NULL guard.\n3. REVERSIBILITY — If this migration cannot be rolled back (e.g. DROP COLUMN, destructive UPDATE), state that explicitly and confirm the user approved the irreversible action.\n4. BLAST RADIUS — State exactly which table(s) and approximately how many rows are affected before the migration runs.\n5. DRY-RUN FIRST — The migration file must include (or be preceded by) a SELECT-only dry-run that previews affected rows without committing anything.\n\nIf any of these checks fail, STOP and fix the migration before proceeding."
  }
}
EOF
  exit 0
fi

# ── Dependency files ─────────────────────────────────────────────────────────
if echo "$file" | grep -qiE '(requirements\.txt|package\.json|package-lock\.json|Pipfile|pyproject\.toml)'; then
  cat <<'EOF'
{
  "continue": true,
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "DEPENDENCY SAFETY CHECK (auto-triggered because a dependency file was just modified):\n\n1. NO DOWNGRADES — Compare every changed package version against the previous value. A version change is only acceptable if it is an upgrade (higher semver) or a new package. Downgrading any existing package is BLOCKED unless the user explicitly requested it and confirmed the reason.\n2. COMPATIBILITY — If a package was upgraded by a major version, call out any known breaking-change surface (e.g. Pydantic v1 → v2, React 17 → 18). List the affected areas and confirm nothing in this codebase relies on the removed/changed API.\n3. LOCK FILE SYNC — If requirements.txt or package.json changed, the corresponding lock file (requirements.lock / package-lock.json) must also be updated in the same commit.\n4. EXISTING FEATURES — Identify which existing app features exercise the changed dependency and confirm they still work (either by reasoning through the diff or by running the relevant tests).\n\nIf a downgrade is detected and was not explicitly requested, REVERT the change immediately."
  }
}
EOF
  exit 0
fi

# ── App source code (Python / JS / TS / JSX) ─────────────────────────────────
if echo "$file" | grep -qiE '\.(py|js|jsx|ts|tsx)$'; then
  cat <<'EOF'
{
  "continue": true,
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "EXISTING-FUNCTION REGRESSION CHECK (auto-triggered because app source was just modified):\n\n1. FUNCTION CONTRACTS — Did any public function, API endpoint, or exported component have its signature, return shape, or side-effects changed? If yes, list every caller and confirm each one is compatible with the new contract.\n2. EXISTING USER DATA — Does this change read from or write to the database in a new way? Confirm it cannot corrupt, silently overwrite, or lose data for users who were created before this deploy.\n3. PLAN / ENTITLEMENT GATES — If the changed code touches billing, plan checks, or feature flags, confirm that Free / lower-plan users are not accidentally granted premium features, and that paid users are not accidentally locked out.\n4. AUTH & PERMISSIONS — If the changed code touches auth, cookies, or admin guards, confirm no privilege-escalation path was introduced.\n\nIf any regression risk is found, fix it before moving on."
  }
}
EOF
  exit 0
fi

echo '{"continue": true}'
