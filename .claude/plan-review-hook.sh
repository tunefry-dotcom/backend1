#!/bin/bash
# Fires on PostToolUse/Write for plan files (.claude/plans/).
# Injects the 5-dimension PLAN SCORE protocol — works whether plan mode
# was entered via the EnterPlanMode tool OR pre-activated from outside.

input=$(cat)

# Fast pre-filter: skip python3 startup entirely for the vast majority of
# source-file edits that can't possibly be plan files.
if ! echo "$input" | grep -q '\.claude'; then
  echo '{"continue": true}'
  exit 0
fi

# python3 -c reads source from the argument, leaving stdin free for JSON data
result=$(echo "$input" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    fp = data.get('tool_input', {}).get('file_path', '')
    normalized = fp.replace('\\\\', '/')
    print('review' if '.claude/plans/' in normalized else 'skip')
except Exception:
    print('skip')
")

if [[ "$result" != "review" ]]; then
  echo '{"continue": true}'
  exit 0
fi

python3 -c "
import json
reason = '''PLAN FILE WRITTEN — SENIOR SDE + ARCHITECT REVIEW PROTOCOL

Before calling ExitPlanMode, work through ALL sections below and output PLAN SCORE: XX/100.

## 1. Architecture deep-think
- Single Responsibility: every module/component has one clear reason to change
- Dependency direction: no higher-level module depends on low-level details
- State & side-effects: minimised, isolated, observable
- Contracts: do not break existing API shapes, type signatures, or naming conventions

## 2. Failure-mode analysis (min 3 realistic prod scenarios)
For each: identify the failure, the blast radius, and the mitigation baked into the plan.

## 3. Security pass
- Input validation at every system boundary (user input, external APIs)
- No secrets/tokens in frontend code or logs
- Authn vs authz correctly separated
- OWASP top-10 surface considered for any new endpoints or data flows

## 4. Performance pass
- N+1 queries identified and solved up-front
- Caching considered (only where TTL is safe)
- Bundle-size impact of any new dependency assessed
- Render/re-render budget estimated for new components

## 5. Maintainability pass
- Names are intention-revealing; no abbreviations that force mental decoding
- No premature abstractions — wait for 3+ actual usages before generalising
- No backwards-compat shims, dead-code accumulation, or half-finished stubs
- Each file/module stays within its stated responsibility

---

## MANDATORY BRUTAL SCORING

You are a brutally honest senior SDE and architect. Score like you are rejecting a PR in production. Be merciless. A plan that is merely fine scores 60. A plan without failure-mode analysis scores 50.

Score on these weighted dimensions — start each at ZERO and justify every point awarded:

  Architecture     25 pts  — SOLID adherence, clear module boundaries, no coupling debt
  Code quality     20 pts  — naming, no over-engineering, idiomatic patterns
  Security         20 pts  — boundaries validated, no auth gaps, no leaked secrets
  Performance      15 pts  — no obvious bottlenecks, reasonable render/query budget
  Maintainability  20 pts  — future devs can navigate and extend without a guide

For each dimension state: awarded / max — one-line brutal justification.

Output exactly: PLAN SCORE: XX/100

If PLAN SCORE < 85: DO NOT call ExitPlanMode. Tear apart every dimension that lost points, rewrite those sections of the plan, and re-score from scratch. Repeat until 85+.'''
print(json.dumps({
    'continue': True,
    'hookSpecificOutput': {
        'hookEventName': 'PostToolUse',
        'additionalContext': reason
    }
}))
"
