#!/bin/bash
# Fires on PreToolUse/Bash — blocks git commit/push when critical dependencies change.
# Critical packages for this stack: fastapi/starlette (breaking API changes),
# supabase/gotrue/postgrest (auth+DB client), boto3 (R2 storage), httpx (Resend/Razorpay
# HTTP calls), pyjwt/python-jose (JWKS token verification), pydantic (schema validation).

input=$(cat)

# Extract command (use python3 for reliable JSON parsing, fallback to grep)
cmd=$(echo "$input" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null || \
      echo "$input" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')

# Only intercept git commit or git push
if [[ ! "$cmd" =~ ^git[[:space:]]+(commit|push) ]]; then
  exit 0
fi

# Extract working directory where the git command would run
cwd=$(echo "$input" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('cwd',''))" 2>/dev/null || echo "")

if [[ -n "$cwd" ]] && [[ -d "$cwd" ]]; then
  cd "$cwd" || exit 0
fi

# Find staged dependency files
DEP_FILES=$(git diff --cached --name-only 2>/dev/null | \
  grep -iE '(requirements[^/]*\.txt|pyproject\.toml|setup\.(py|cfg)|Pipfile(\.lock)?|poetry\.lock)' | \
  tr '\n' ' ')

if [[ -z "$DEP_FILES" ]]; then
  exit 0
fi

# Critical packages to guard
CRITICAL="fastapi|starlette|supabase|gotrue|postgrest|boto3|botocore|httpx|pyjwt|python-jose|jose|pydantic|uvicorn"

# Get changed lines touching critical packages (+ additions, - removals)
CHANGES=$(git diff --cached -- $DEP_FILES 2>/dev/null | \
  grep -E "^[+-].*(${CRITICAL})" | \
  grep -v "^[+-]{3}")

if [[ -z "$CHANGES" ]]; then
  exit 0
fi

# Export for Python (avoids all bash/JSON quoting hazards)
export GUARD_FILES="$DEP_FILES"
export GUARD_CHANGES="$CHANGES"

python3 -c "
import json, os
files = os.environ.get('GUARD_FILES', '').strip()
changes = os.environ.get('GUARD_CHANGES', '')

reason = (
    '⚠️  CRITICAL DEPENDENCY CHANGES DETECTED\n\n'
    'Dependency files in this commit:\n  ' + '\n  '.join(files.split()) + '\n\n'
    'Lines touching critical packages:\n' + changes + '\n\n'
    '🔴 FASTAPI / STARLETTE\n'
    '   Major version bumps have broken TemplateResponse signatures and cookie-merging\n'
    '   behavior before in this repo (see CLAUDE.md Gotchas section).\n\n'
    '⚠️  SUPABASE / GOTRUE / POSTGREST / PYJWT\n'
    '   Version mismatches can break JWKS token verification, admin.create_user,\n'
    '   or the custom access-token hook contract — silently, with no obvious error\n'
    '   at deploy time (auth breaks for all users).\n\n'
    '⚠️  BOTO3 / HTTPX\n'
    '   Breaking changes here can silently break R2 presigned URLs or Resend/Razorpay\n'
    '   HTTP calls (emails stop sending, payments stop verifying).\n\n'
    'Before approving:\n'
    '  1. Is this version change intentional (not an accidental downgrade)?\n'
    '  2. Have you tested login/signup + a presigned upload + a payment flow locally?\n'
    '  3. Is the change reflected in the deployed Render environment (same requirements.txt)?\n\n'
    'Approve only if 100% certain. Deny to abort the commit.'
)

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'ask',
        'permissionDecisionReason': reason
    }
}))
"
