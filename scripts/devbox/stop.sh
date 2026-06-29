#!/usr/bin/env bash
# stop.sh — deallocate the cloud devbox (stops compute billing; OS disk persists).
#
# Usage: scripts/devbox/stop.sh [--dry-run] [--yes]
#   --dry-run  print the az command without running it
#   --yes      skip the confirmation prompt
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/devbox/lib.sh
source "${SCRIPT_DIR}/lib.sh"

export DEVBOX_DRY_RUN=0
export DEVBOX_ASSUME_YES=0

usage() {
  cat <<'EOF'
stop.sh — deallocate the cloud devbox (stops compute billing; OS disk persists).

Usage: scripts/devbox/stop.sh [--dry-run] [--yes]
  --dry-run  print the az command without running it
  --yes      skip the confirmation prompt
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DEVBOX_DRY_RUN=1 ;;
    --yes | -y) DEVBOX_ASSUME_YES=1 ;;
    -h | --help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

require_az_login
pin_subscription

state="$(vm_state)"
case "${state}" in
  notexist) die "VM ${DEVBOX_VM_NAME} does not exist — nothing to stop." ;;
  deallocated)
    log "VM ${DEVBOX_VM_NAME} is already deallocated; nothing to do."
    exit 0
    ;;
  *) : ;; # running / stopped / other → proceed to deallocate
esac

log "VM ${DEVBOX_VM_NAME} is '${state}'."
confirm "Deallocate ${DEVBOX_VM_NAME} (stops compute billing)?" ||
  die "Aborted; VM left '${state}'."

run_or_echo az vm deallocate \
  --resource-group "${DEVBOX_RESOURCE_GROUP}" \
  --name "${DEVBOX_VM_NAME}" \
  --output none

if [[ "${DEVBOX_DRY_RUN}" != "1" ]]; then
  audit_log "stop vm=${DEVBOX_VM_NAME} from-state=${state} -> deallocated"
  log "Deallocated ${DEVBOX_VM_NAME}. Resume with: scripts/devbox/up.sh"
fi
