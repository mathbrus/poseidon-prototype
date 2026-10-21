#!/usr/bin/env bash
# Make a release: the one act after a human said yes to a proposal
# (docs/developing/releasing.md). Stamps the version into its mirrors,
# turns the changelog's Unreleased section into the release's section,
# commits that directly on master — the one commit that lands without a
# pull request: the review happened in the pull requests it releases —
# tags it `v<version>` with the section as the message, pushes both, and
# creates the GitHub release with the same notes.
#
#   scripts/release.sh 0.1.0+2026.10.21      (a leading v is accepted)
#
# Refuses off master, with a dirty or unpushed tree, with a version that
# is malformed, not dated today or not above the last release, with an
# empty Unreleased, and until CI is green on the commit being released.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=${1:?usage: release.sh MAJOR.MINOR.PATCH+YYYY.MM.DD}
VERSION=${VERSION#v}
TAG="v$VERSION"
TODAY=$(date +%Y.%m.%d)
DATE=$(date +%Y-%m-%d)

fail() { echo "release: $*" >&2; exit 1; }

case "$VERSION" in
  *+"$TODAY") ;;
  *) fail "$VERSION is not dated today: the version ends with +$TODAY, the day it is tagged" ;;
esac
uv run python scripts/version.py validate "$VERSION"
[ "$(git rev-parse --abbrev-ref HEAD)" = master ] || fail "not on master"
[ -z "$(git status --porcelain)" ] || fail "the tree is not clean"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/master)" ] || fail "master differs from origin/master"
! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || fail "$TAG exists"

LAST=$(git describe --tags --match 'v*' --abbrev=0 2>/dev/null || true)
if [ -n "$LAST" ]; then
  last_core=${LAST#v}; last_core=${last_core%%+*}
  new_core=${VERSION%%+*}
  [ "$last_core" != "$new_core" ] || fail "$new_core is the last release's number ($LAST); bump it"
  [ "$(printf '%s\n%s\n' "$last_core" "$new_core" | sort -V | tail -1)" = "$new_core" ] \
    || fail "$new_core is below the last release ($LAST)"
fi

# The Unreleased section, without the blank lines that frame it: the
# release's notes, the tag's message and the commit's body.
NOTES=$(awk '/^## \[Unreleased\]/{on=1; next} /^## \[/{on=0} on' CHANGELOG.md \
  | sed -e '1{/^$/d}' -e '${/^$/d}')
[ -n "$(printf '%s' "$NOTES" | tr -d '[:space:]')" ] || fail "CHANGELOG.md has nothing under Unreleased"

ci=$(gh run list --workflow ci --commit "$(git rev-parse HEAD)" --json status,conclusion --limit 1 \
  --jq '.[0] | "\(.status) \(.conclusion)"')
[ "$ci" = "completed success" ] || fail "CI on $(git rev-parse --short HEAD) is '${ci:-absent}', not green"

uv run python scripts/version.py set "$VERSION"
uv run python - "$VERSION" "$DATE" <<'EOF'
import pathlib, sys
version, date = sys.argv[1:]
path = pathlib.Path("CHANGELOG.md")
text = path.read_text()
head = "## [Unreleased]\n"
assert text.count(head) == 1
path.write_text(text.replace(head, f"{head}\n## [{version}] - {date}\n", 1))
EOF
notes=$(mktemp)
printf '%s\n\n%s\n' "$TAG" "$NOTES" >"$notes"

git add -A
git commit -q -F - <<EOF
release: $TAG

$NOTES
EOF
git tag -a "$TAG" -F "$notes"
git push -q origin master "$TAG"
gh release create "$TAG" --title "$TAG" --notes-file "$notes" >/dev/null
rm -f "$notes"
echo "released $TAG: $(gh release view "$TAG" --json url --jq .url)"
