#!/usr/bin/env bash
# Activate the Canon rules plugin inside THIS container.
#
# Idempotent + credential-free (canon is a PUBLIC repo): a no-op once installed,
# so it is safe to run on every container start or every shell start. A committed
# .claude/settings.json only *declares* the marketplace + pin (and drives CI); it
# does not install — Claude Code will not let a checked-out repo fetch and run code
# silently. This script performs that one-time, per-environment activation.
#
# Copy to <repo>/.devcontainer/setup-canon.sh (chmod +x) and call it from wherever
# reliably runs when your container comes up — see docs/adopting-in-dev-containers.md.
set -euo pipefail

# --- knobs (override via env if ever needed) ------------------------------------
MARKETPLACE="${CANON_MARKETPLACE:-rinman24/canon}"   # github owner/repo
PLUGIN="${CANON_PLUGIN:-canon-core@canon}"           # plugin@marketplace
SCOPE="${CANON_SCOPE:-user}"                         # user = whole container; project = this repo only
# -------------------------------------------------------------------------------

# Skip quietly if this image has no Claude CLI, so the line is safe to drop into a
# shared entrypoint used by containers that don't run Claude.
if ! command -v claude >/dev/null 2>&1; then
    echo "setup-canon: claude CLI not on PATH; skipping activation"
    exit 0
fi

# project scope writes to the project Claude config, so it must run at the repo root.
# This script lives at <repo>/.devcontainer/, so the repo root is one level up.
if [ "${SCOPE}" = "project" ]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cd "${CANON_PROJECT_DIR:-$(cd "${script_dir}/.." && pwd)}"
fi

echo "setup-canon: marketplace '${MARKETPLACE}', plugin '${PLUGIN}', scope '${SCOPE}'"
claude plugin marketplace add "${MARKETPLACE}" --scope "${SCOPE}"
claude plugin install "${PLUGIN}" --scope "${SCOPE}"

echo "setup-canon: done — restart Claude Code so the SessionStart hook injects (not /clear)"
