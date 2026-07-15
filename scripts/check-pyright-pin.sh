#!/usr/bin/env bash
#
# check-pyright-pin.sh — enforce the two-place pyright version invariant.
#
# The dev-container Dockerfile pins pyright via `npm install -g pyright@X.Y.Z`
# so the editor's language server matches the version resolved in uv.lock
# (the authoritative `uv run pyright` gate). This script fails when those two
# versions drift apart.
#
# Usage:
#   check-pyright-pin.sh --dockerfile <path> [--stage <name>] [--uv-lock <path>]
#
# Exit codes: 0 = match, 1 = drift, 2 = could not extract a version.
set -euo pipefail

DOCKERFILE=""
UVLOCK="uv.lock"
STAGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dockerfile) DOCKERFILE="$2"; shift 2 ;;
    --uv-lock)    UVLOCK="$2";     shift 2 ;;
    --stage)      STAGE="$2";      shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$DOCKERFILE" ]]; then echo "ERROR: --dockerfile is required" >&2; exit 2; fi
if [[ ! -f "$DOCKERFILE" ]]; then echo "ERROR: Dockerfile not found: $DOCKERFILE" >&2; exit 2; fi
if [[ ! -f "$UVLOCK" ]]; then echo "ERROR: uv.lock not found: $UVLOCK" >&2; exit 2; fi

# pyright version resolved in uv.lock: the [[package]] block named "pyright".
uv_ver="$(awk '
  /^\[\[package\]\]/    { inpkg=1; name=""; ver=""; next }
  inpkg && /^name =/    { line=$0; gsub(/[",]/, "", line); split(line, a, "="); gsub(/[[:space:]]/, "", a[2]); name=a[2] }
  inpkg && /^version =/ { line=$0; gsub(/[",]/, "", line); split(line, a, "="); gsub(/[[:space:]]/, "", a[2]); ver=a[2] }
  inpkg && name=="pyright" && ver!="" { print ver; exit }
' "$UVLOCK")"

# pyright version pinned in the Dockerfile; optionally scoped to a build stage.
if [[ -n "$STAGE" ]]; then
  dockerfile_scope="$(awk -v stage="$STAGE" '
    toupper($1) == "FROM" {
      instage=0
      for (i=1; i<=NF; i++) if (toupper($i) == "AS" && $(i+1) == stage) instage=1
      next
    }
    instage { print }
  ' "$DOCKERFILE")"
else
  dockerfile_scope="$(cat "$DOCKERFILE")"
fi

pin_ver="$(printf '%s\n' "$dockerfile_scope" \
  | grep -oE 'pyright@[0-9]+\.[0-9]+\.[0-9]+' | head -n1 | cut -d'@' -f2 || true)"

scope_desc="$DOCKERFILE"
[[ -n "$STAGE" ]] && scope_desc="$DOCKERFILE (stage '$STAGE')"

if [[ -z "$uv_ver" ]]; then echo "ERROR: could not find a pyright version in $UVLOCK" >&2; exit 2; fi
if [[ -z "$pin_ver" ]]; then echo "ERROR: could not find 'npm install -g pyright@X.Y.Z' in $scope_desc" >&2; exit 2; fi

if [[ "$uv_ver" != "$pin_ver" ]]; then
  {
    echo "ERROR: pyright version drift detected."
    echo "  Dockerfile pin : $pin_ver   ($scope_desc)"
    echo "  uv.lock version: $uv_ver   ($UVLOCK)"
    echo "These must match. Update the Dockerfile 'npm install -g pyright@...' pin"
    echo "and uv.lock together so the dev-container language server matches the CI gate."
  } >&2
  exit 1
fi

echo "OK: pyright pinned to $pin_ver in both $scope_desc and $UVLOCK"
