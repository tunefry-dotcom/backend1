#!/bin/bash
# Fires on PreToolUse/Edit|Write — reminds about the "frontend-design" skill's
# design-quality bar whenever a UI file (component/page/style) in the sibling
# tunefry-frontend repo is about to be created/edited. Companion to
# frontend-integration-hook.sh (that one covers API wiring; this one covers
# visual/aesthetic quality).

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

# Only UI-bearing files — skip lib/ services and config where aesthetics don't apply.
if [[ "$norm" != *.jsx && "$norm" != *.css ]]; then
  exit 0
fi

python3 -c "
import json
reason = '''FRONTEND DESIGN QUALITY CHECKLIST for this file (frontend-design skill):
1. Commit to ONE bold aesthetic direction before coding (minimal, maximalist, retro-futuristic, luxury, brutalist, editorial, etc.) - no safe/generic default.
2. Never ship AI-slop: no Inter/Roboto/Arial/system-font defaults, no purple-gradient-on-white, no cookie-cutter layouts.
3. Typography: pair a distinctive display font with a refined body font - avoid overused choices (Inter, Space Grotesk).
4. Color: cohesive palette via CSS variables - dominant colors with sharp accents, not timid evenly-distributed palettes.
5. Motion: one well-orchestrated staggered reveal beats scattered micro-interactions; this is plain CSS (no Motion/Framer lib in this repo) - keep it CSS-only.
6. Break the grid: asymmetry, overlap, diagonal flow, generous negative space OR controlled density - avoid predictable centered layouts.
7. Backgrounds need atmosphere/depth (gradient meshes, noise, textures, layered transparency, shadows) - never a flat solid-color default.
8. Match code complexity to the aesthetic: maximalist needs elaborate animation/effects; minimalist needs restraint and precise spacing/typography.
9. Vary choices across files/generations - do not converge on the same safe pattern every time.
10. Styling in this repo is component-scoped CSS under src/styles/ (no UI library) - stay consistent with that convention, do not introduce Tailwind/MUI/etc. without asking.'''
print(json.dumps({
    'continue': True,
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': reason
    }
}))
"
