#!/usr/bin/env bash
# connect-host.sh — SSH into the cloud devbox VM *host* (NOT the backend container).
# Mac-side only.
#
# Use it for host-level work the container cannot do — `docker compose build`/recreate,
# inspecting named volumes, `docker logs` — since the container has no Docker socket
# (it is a sibling of the VM's Docker, not DinD). Thin wrapper over the VM host alias
# (DEVBOX_HOST_ALIAS) in ~/.ssh/config (installed by install-ssh-config.sh); for the
# container, use the container alias or scripts/devbox/connect.sh.
#
# Usage: scripts/devbox/connect-host.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/devbox/lib.sh
source "${SCRIPT_DIR}/lib.sh"

exec ssh -t "${DEVBOX_HOST_ALIAS}"
