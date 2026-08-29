#!/usr/bin/env sh
# The README states a test count and a coverage figure. Both are repository
# facts, not run facts, so `reclaim verify-docs` - which checks figures against
# a run's audit log - cannot see them, and both went stale silently once
# already. This closes that gap the same way: derive the number, diff it
# against the claim, fail loudly.
set -eu

BIN="${BIN:-.venv/bin}"
README="${1:-README.md}"

# `--collect-only -q` prints one `path: n` line per file here, so sum the
# counts rather than parsing a summary line that this config does not emit.
collected=$("$BIN/python" -m pytest --collect-only -q 2>/dev/null |
  awk -F': ' '/: [0-9]+$/ {n += $2} END {print n+0}')
claimed=$(grep -oE '^[0-9]+ tests, [0-9]+% line coverage' "$README" | awk '{print $1}')

if [ -z "$claimed" ]; then
  echo "FAIL: no '<n> tests, <n>% line coverage' claim found in $README" >&2
  exit 1
fi

if [ "$collected" != "$claimed" ]; then
  echo "FAIL: $README claims $claimed tests; pytest collects $collected" >&2
  exit 1
fi

echo "  [OK   ] README test count                       $claimed (pytest collects $collected)"
