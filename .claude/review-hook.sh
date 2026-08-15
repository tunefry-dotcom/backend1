#!/bin/bash

input=$(cat)

tool=$(echo "$input" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')

if [[ "$tool" != "Edit" && "$tool" != "Write" ]]; then
  echo '{"continue": true}'
  exit 0
fi

COOLDOWN_FILE="/tmp/claude_review_cooldown"
COOLDOWN_SECONDS=30

file=$(echo "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')

# Phase 3 (CLAUDE.md sync) always fires — no cooldown gate.
# Phases 0-2 (heavy review) are gated by the cooldown to avoid spam.
IN_COOLDOWN=false
if [[ -f "$COOLDOWN_FILE" ]]; then
  last_review=$(cat "$COOLDOWN_FILE")
  now=$(date +%s)
  diff=$((now - last_review))
  if [[ $diff -lt $COOLDOWN_SECONDS ]]; then
    IN_COOLDOWN=true
  fi
fi

if [[ "$IN_COOLDOWN" == "false" ]]; then
  date +%s > "$COOLDOWN_FILE"
  cat <<EOF
{
  "continue": true,
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "POST-HOOK REVIEW REQUIRED: The file $file was just modified.\n\n## PHASE 0 — PRODUCTION SAFETY (BLOCKING)\nThis project runs on LIVE PRODUCTION with real users and real data. A failed, careless, or overly broad action can cause IRREVERSIBLE real-user data loss (there may be NO backup to restore from). Therefore:\n- Do NOT perform any update, migration, bulk write, delete, or schema change that can harm real users or real data.\n- Never run a full re-migration or mass upsert against production; prefer read-only or narrowly-targeted operations.\n- Before ANY write to production, state the exact blast radius (which tables/rows, how many) and get explicit user approval.\n- If an action is destructive or irreversible and you are not certain it is safe, STOP and ask instead of proceeding.\n\n## PHASE 1 — Standard Review\nReview for:\n1. Coding best practices\n2. Optimization opportunities\n3. Code reuse (check if functionality already exists)\n4. Architecture quality (senior SDE perspective)\nIf issues found, automatically fix them.\n\n## PHASE 2 — Creative Multi-POV Scoring\nNow think creatively and critically from multiple perspectives:\n- Security engineer: are there any attack surfaces or unsafe assumptions?\n- Performance engineer: any bottlenecks, N+1s, or unnecessary work?\n- Junior dev onboarding: is this readable and maintainable?\n- Product/user lens: does this actually solve the right problem correctly?\n- Devil's advocate: what is the most likely way this breaks in production?\n\nBased on this multi-POV analysis, assign an IMPLEMENTATION SCORE out of 100.\n\nOutput the score clearly like: SCORE: XX/100\n\nIf SCORE < 85: you MUST stop, explain what dragged the score down, completely redo the implementation to address all identified flaws, and then run this same review again on the new version until the score is 85 or above. Do not move on until the score is >= 85.\n\n## PHASE 3 — Documentation sync\nIf this change introduced or altered anything durable that a future session would need — a new/changed endpoint, env var, DB column/migration, naming convention, entitlement/plan rule, cross-file invariant, or a non-obvious gotcha — update CLAUDE.md in the SAME turn to match. Skip trivial/local edits; only record architectural or contract-level facts."
  }
}
EOF
else
  cat <<EOF
{
  "continue": true,
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "PHASE 3 — Documentation sync (review cooldown active, full review skipped):\nThe file $file was just modified. If this change introduced or altered anything durable that a future session would need — a new/changed endpoint, env var, DB column/migration, naming convention, entitlement/plan rule, cross-file invariant, or a non-obvious gotcha — update CLAUDE.md in the SAME turn to match. Skip trivial/local edits; only record architectural or contract-level facts."
  }
}
EOF
fi
