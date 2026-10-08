#!/usr/bin/env bash
# scripts/release-classify.sh — validate a pushed release tag and route the channel.
#
# Usage (TAG comes from the environment; GITHUB_OUTPUT is optional, Actions sets it):
#   TAG=v0.1.0rc1 scripts/release-classify.sh
#
# Prints (and, when GITHUB_OUTPUT is set, writes) "channel=testpypi|pypi" and
# "version=<X.Y.Z[rcN]>". Exit codes: 2 = the tag is not a vX.Y.Z or vX.Y.ZrcN
# tag; 1 = a precondition failed (pyproject.toml version mismatch, missing
# CHANGELOG entry, the exact version already on its target index, or a final
# tag whose commit carries no rc tag — D-18, D-19).
#
# The tag is attacker-influenced text that reaches this script, so it is only
# ever matched against an anchored regex and never echoed back (T-11-21).
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

TAG_VALUE=${TAG:-}
TAG_RE='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(rc[1-9][0-9]*)?$'
if ! [[ "$TAG_VALUE" =~ $TAG_RE ]]; then
  echo "release-classify: TAG is not a vX.Y.Z or vX.Y.ZrcN tag" >&2
  exit 2
fi

VERSION=${TAG_VALUE#v}
BASE=${VERSION%rc*}

# The committed version must already be the tag's base version (the CHANGELOG
# entry and pyproject.toml are reviewed on main before any tag is pushed).
BASE_ESCAPED=${BASE//./\\.}
PROJECT_VERSION=$(cd "$ROOT" && python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
if [ "$BASE" != "$PROJECT_VERSION" ]; then
  echo "release-classify: pyproject.toml version does not match the tag's base version" >&2
  exit 1
fi
if ! grep -Eq "^## \\[$BASE_ESCAPED\\] - " "$ROOT/CHANGELOG.md"; then
  echo "release-classify: CHANGELOG.md has no entry for the tag's base version" >&2
  exit 1
fi

if [[ "$VERSION" == *"rc"* ]]; then
  CHANNEL=testpypi
  PROBE_URL="https://test.pypi.org/pypi/docql/$VERSION/json"
else
  CHANNEL=pypi
  PROBE_URL="https://pypi.org/pypi/docql/$VERSION/json"
  # A final tag may only release a commit that was already dry-run as an rc (D-18).
  RC_TAG_RE="^v${BASE_ESCAPED}rc[1-9][0-9]*$"
  if ! git -C "$ROOT" tag --points-at HEAD | grep -Eq "$RC_TAG_RE"; then
    echo "release-classify: a final tag needs an rc tag on the same commit first" >&2
    exit 1
  fi
fi

# Each uploaded version number is burned forever on its index, so the exact
# version must not exist there yet (D-19). A non-404 answer (200 exists, or the
# index is unreachable) refuses the release.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$PROBE_URL")
if [ "$CODE" != 404 ]; then
  echo "release-classify: the version already exists on its target index, or the index probe failed (http_code=$CODE)" >&2
  exit 1
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    printf 'channel=%s\n' "$CHANNEL"
    printf 'version=%s\n' "$VERSION"
  } >> "$GITHUB_OUTPUT"
fi
printf 'channel=%s\n' "$CHANNEL"
printf 'version=%s\n' "$VERSION"
