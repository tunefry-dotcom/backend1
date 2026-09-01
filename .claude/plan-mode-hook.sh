#!/bin/bash
# Fires on PreToolUse/EnterPlanMode — injects senior SDE + architect review
# protocol. Claude must produce a PLAN SCORE: XX/100 before calling ExitPlanMode.
# No path filtering needed: always fires in plan mode.

cat > /dev/null  # drain stdin — EnterPlanMode carries no relevant payload

python3 -c "
import json
reason = '''PLAN MODE ACTIVATED — SENIOR SDE + ARCHITECT REVIEW PROTOCOL

Before finalising this plan, work through ALL sections below and output a PLAN SCORE: XX/100 before calling ExitPlanMode.

## 1. Architecture deep-think
- Single Responsibility: every module/component has one clear reason to change
- Dependency direction: no higher-level module depends on low-level details
- State & side-effects: minimised, isolated, observable
- Contracts: do not break existing API shapes, type signatures, or naming conventions

## 2. Failure-mode analysis (min 3 realistic prod scenarios)
For each: identify the failure, the blast radius, and the mitigation baked into the plan.
This project runs on LIVE PRODUCTION (real artists, real payments, real royalty money) —
weigh Supabase writes, migration scripts, and billing/webhook paths accordingly.

## 3. Security pass
- Input validation at every system boundary (user input, external APIs)
- No secrets/tokens in frontend code or logs
- Authn (who are you?) vs authz (what can you do?) correctly separated
- OWASP top-10 surface considered for any new endpoints or data flows

## 4. Performance pass
- N+1 queries identified and solved up-front
- Caching considered (only where TTL is safe)
- Bundle-size impact of any new dependency assessed
- React render/re-render budget estimated for new frontend components

## 5. Maintainability pass
- Names are intention-revealing; no abbreviations that force mental decoding
- No premature abstractions — wait for 3+ actual usages before generalising
- No backwards-compat shims, dead-code accumulation, or half-finished stubs
- Each file/module stays within its stated responsibility

---

## MANDATORY BRUTAL SCORING

You are a brutally honest senior SDE and architect. Score like you are rejecting a PR in production — not encouraging a junior. Be merciless. Deduct points for every flaw, assumption, vague handwave, or missing mitigation. A plan that is merely 'fine' scores 60. A plan without failure-mode analysis scores 50. Mediocre naming, skipped edge cases, or any 'we can handle that later' thinking costs points immediately.

Score on these weighted dimensions — start each at ZERO and justify every point awarded:

  Architecture     25 pts  — SOLID adherence, clear module boundaries, no coupling debt
  Code quality     20 pts  — naming, no over-engineering, idiomatic patterns
  Security         20 pts  — boundaries validated, no auth gaps, no leaked secrets
  Performance      15 pts  — no obvious bottlenecks, reasonable render budget
  Maintainability  20 pts  — future devs can navigate and extend without a guide

For each dimension state: awarded / max — one-line brutal justification.

Output exactly: PLAN SCORE: XX/100

If PLAN SCORE < 85: DO NOT call ExitPlanMode. Tear apart every dimension that lost points, rewrite those sections of the plan, and re-score from scratch. Repeat until 85+.'''
print(json.dumps({
    'continue': True,
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': reason
    }
}))
"
