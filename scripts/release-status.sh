#!/usr/bin/env bash
# The facts a release proposal is made from; nothing is decided here. The
# last release and its date, what master gained since (pull requests,
# commits), what the changelog's Unreleased section already says and
# which merged pull requests it does not mention, and which user-visible
# surfaces the delta touched — a touched surface is what makes a release
# due, and a minor rather than a patch (docs/developing/releasing.md).
#
#   scripts/release-status.sh [REF]      read against REF (default origin/master); no side effects
set -euo pipefail
cd "$(dirname "$0")/.."

git fetch -q origin
HEAD_REF=${1:-origin/master}
LAST=$(git describe --tags --match 'v*' --abbrev=0 "$HEAD_REF" 2>/dev/null || true)
if [ -n "$LAST" ]; then
  BASE=$LAST
  echo "last release: $LAST, $(git log -1 --format=%ad --date=short "$LAST")"
else
  BASE=poc
  echo "last release: none yet; measuring since the poc tag"
fi
echo "version in the tree: $(uv run python scripts/version.py show)"
echo "master: $(git log -1 --format='%h, %ad' --date=short "$HEAD_REF"), $(git rev-list --count "$BASE..$HEAD_REF") commits since $BASE"

echo
echo "pull requests merged since $BASE:"
# A merge commit's subject names the pull request, its body's first line
# is the pull request's title; one pass yields "number<TAB>title".
MERGED=$(git log --merges --first-parent --format='%s%x1f%b' "$BASE..$HEAD_REF" \
  | sed -n 's/^Merge pull request #\([0-9]*\) [^\x1f]*\x1f\(.*\)$/\1\t\2/p')
if [ -z "$MERGED" ]; then
  echo "  none"
fi
while IFS=$'\t' read -r number title; do
  [ -z "$number" ] && continue
  echo "  #$number  $title"
done <<<"$MERGED"

echo
echo "user-visible surfaces touched since $BASE (any → a release is due, and a minor):"
# surface | paths
SURFACES='
the blueprint and its policy|packages/schema/src packages/compiler/src crds examples
the CLI|packages/cli/src
the console|packages/console/src
the chart|deploy/helm
the prompts|packages/agent/src/agent/prompts
'
touched=0
while IFS='|' read -r surface paths; do
  [ -z "$surface" ] && continue
  # shellcheck disable=SC2086
  files=$(git diff --name-only "$BASE" "$HEAD_REF" -- $paths | wc -l)
  if [ "$files" -gt 0 ]; then
    echo "  $surface: $files file(s)"
    touched=1
  fi
done <<<"$SURFACES"
if [ "$touched" = 0 ]; then
  echo "  none: fixes, refactors, docs and tests only (a patch, if anything)"
fi

echo
echo "CHANGELOG.md, Unreleased:"
UNRELEASED=$(git show "$HEAD_REF:CHANGELOG.md" 2>/dev/null | awk '/^## \[Unreleased\]/{on=1; next} /^## \[/{on=0} on')
if [ -z "$(printf '%s' "$UNRELEASED" | tr -d '[:space:]')" ]; then
  echo "  (empty)"
else
  printf '%s\n' "$UNRELEASED" | sed 's/^/  /'
fi
missing=""
for number in $(cut -f1 <<<"$MERGED"); do
  if ! grep -q "#$number\b" <<<"$UNRELEASED"; then
    missing="$missing #$number"
  fi
done
if [ -n "$missing" ]; then
  echo
  echo "merged since $BASE but not mentioned in Unreleased:$missing"
fi
