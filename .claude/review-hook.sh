#!/bin/bash

input=$(cat)

tool=$(echo "$input" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')

if [[ "$tool" != "Edit" && "$tool" != "Write" ]]; then
  echo '{"continue": true}'
  exit 0
fi

COOLDOWN_FILE="/tmp/claude_review_cooldown"
COOLDOWN_SECONDS=30

if [[ -f "$COOLDOWN_FILE" ]]; then
  last_review=$(cat "$COOLDOWN_FILE")
  now=$(date +%s)
  diff=$((now - last_review))
  if [[ $diff -lt $COOLDOWN_SECONDS ]]; then
    echo '{"continue": true}'
    exit 0
  fi
fi

date +%s > "$COOLDOWN_FILE"

file=$(echo "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')

cat <<EOF
{
  "continue": true,
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "POST-HOOK REVIEW REQUIRED: The file $file was just modified.\n\n## PHASE 1 — Standard Review\nReview for:\n1. Coding best practices\n2. Optimization opportunities\n3. Code reuse (check if functionality already exists)\n4. Architecture quality (senior SDE perspective)\nIf issues found, automatically fix them.\n\n## PHASE 2 — Creative Multi-POV Scoring\nNow think creatively and critically from multiple perspectives:\n- Security engineer: are there any attack surfaces or unsafe assumptions?\n- Performance engineer: any bottlenecks, N+1s, or unnecessary work?\n- Junior dev onboarding: is this readable and maintainable?\n- Product/user lens: does this actually solve the right problem correctly?\n- Devil's advocate: what is the most likely way this breaks in production?\n\nBased on this multi-POV analysis, assign an IMPLEMENTATION SCORE out of 100.\n\nOutput the score clearly like: SCORE: XX/100\n\nIf SCORE < 85: you MUST stop, explain what dragged the score down, completely redo the implementation to address all identified flaws, and then run this same review again on the new version until the score is 85 or above. Do not move on until the score is >= 85."
  }
}
EOF
