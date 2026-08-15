#!/bin/bash
# PreToolUse production-safety reminder.
# - Always fires before Bash commands that look like they write to the production
#   DB or run a migration (the exact class of action that caused prior data loss).
# - Fires before Edit/Write with a short cooldown so it stays a reminder, not spam.

input=$(cat)

tool=$(echo "$input" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)".*/\1/')

emit() {
  cat <<'EOF'
{"continue": true, "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "PRE-TOOL PRODUCTION SAFETY CHECK: This project runs on LIVE PRODUCTION with real users and real data, and there may be NO backup to restore from. Before this action, confirm it cannot harm real users or data: do NOT run any update, migration, bulk write, delete, or schema change that could damage production; never run a full re-migration or mass upsert; prefer read-only or narrowly-targeted operations. State the exact blast radius (which tables/rows, how many) and get EXPLICIT user approval before any production write. If the action is destructive or irreversible and you are not certain it is safe, STOP and ask instead of proceeding."}}
EOF
}

if [[ "$tool" == "Bash" ]]; then
  # git push to main = production deploy on Render — always warn.
  if echo "$input" | grep -Eiq 'git\s+push'; then
    emit
    exit 0
  fi
  # Critical: command that can write to Supabase or run a migration script.
  if echo "$input" | grep -Eiq '\.(update|insert|upsert|delete)\(|migrate_users|migrate_releases|service\.table|get_service_client|admin\.(create_user|update_user|delete_user)|update_user_by_id'; then
    emit
    exit 0
  fi
  echo '{"continue": true}'
  exit 0
fi

if [[ "$tool" == "Edit" || "$tool" == "Write" ]]; then
  CD=/tmp/claude_presafety_cooldown
  NOW=$(date +%s)
  if [[ -f "$CD" ]]; then
    last=$(cat "$CD")
    if [[ $((NOW - last)) -lt 90 ]]; then
      echo '{"continue": true}'
      exit 0
    fi
  fi
  echo "$NOW" > "$CD"
  emit
  exit 0
fi

echo '{"continue": true}'
