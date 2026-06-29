#!/usr/bin/env bash
# pin-ip.sh — re-pin the inbound SSH NSG rule to your current egress IP. Mac-side only.
#
# The NSG `default-allow-ssh` rule is pinned to the operator's egress IP/32. up.sh
# re-pins it on every fresh provision / resume, but a mid-session change to your
# egress IP (new network, VPN toggled, ISP re-lease) locks SSH out without a VM
# state change to trigger up.sh. Run this to re-pin to your current IP/32 in place.
#
# Usage: scripts/devbox/pin-ip.sh [--dry-run]
#   --dry-run  print the az command without running it
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/devbox/lib.sh
source "${SCRIPT_DIR}/lib.sh"

export DEVBOX_DRY_RUN=0

usage() {
  cat <<'EOF'
pin-ip.sh — re-pin the inbound SSH NSG rule to your current egress IP/32.

Usage: scripts/devbox/pin-ip.sh [--dry-run]
  --dry-run  print the az command without running it
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DEVBOX_DRY_RUN=1 ;;
    -h | --help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

require_az_login
pin_subscription
pin_nsg_to_operator

if [[ "${DEVBOX_DRY_RUN}" != "1" ]]; then
  audit_log "pin-ip vm=${DEVBOX_VM_NAME}"
fi
