# shellcheck shell=bash
# lib.sh — shared helpers for the cloud devbox scripts.
#
# Sourced (not executed) by up.sh / stop.sh / connect-*.sh. Sourcing this file also
# loads config.sh (tracked defaults) and config.local.sh (per-developer, gitignored),
# failing fast if config.local.sh is absent.
#
# Conventions the helpers honour, set by the calling script from its flags:
#   DEVBOX_DRY_RUN=1    run_or_echo prints commands instead of running them
#   DEVBOX_ASSUME_YES=1 confirm() returns success without prompting

# --- logging -----------------------------------------------------------------------
log() { printf '%s\n' "[devbox] $*" >&2; }
die() {
  printf '%s\n' "[devbox] error: $*" >&2
  exit 1
}

# --- config loading ----------------------------------------------------------------
_DEVBOX_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/devbox/config.sh
source "${_DEVBOX_LIB_DIR}/config.sh"

if [[ ! -f "${_DEVBOX_LIB_DIR}/config.local.sh" ]]; then
  die "scripts/devbox/config.local.sh not found — copy the template first:
  cp scripts/devbox/config.example.sh scripts/devbox/config.local.sh"
fi
# shellcheck source=/dev/null
source "${_DEVBOX_LIB_DIR}/config.local.sh"

# --- audit -------------------------------------------------------------------------
# Append a timestamped breadcrumb to the gitignored audit log. The authoritative
# record of state-changing Azure operations is `az monitor activity-log`.
audit_log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"${DEVBOX_AUDIT_LOG}"
}

# --- dry-run / confirm gates -------------------------------------------------------
# Run argv as a command, or print it (prefixed with +) when DEVBOX_DRY_RUN=1.
run_or_echo() {
  if [[ "${DEVBOX_DRY_RUN:-0}" == "1" ]]; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}

# y/N prompt; returns success on yes. Bypassed (success) when DEVBOX_ASSUME_YES=1, or
# under DEVBOX_DRY_RUN where nothing is executed so there is nothing to gate.
confirm() {
  if [[ "${DEVBOX_DRY_RUN:-0}" == "1" ]]; then
    log "(dry-run) would prompt: ${1:-Proceed?} [assuming yes]"
    return 0
  fi
  if [[ "${DEVBOX_ASSUME_YES:-0}" == "1" ]]; then
    return 0
  fi
  local reply
  read -r -p "[devbox] ${1:-Proceed?} [y/N] " reply
  [[ "${reply}" =~ ^[Yy]$ ]]
}

# --- Azure auth (gate, never auto-driven) ------------------------------------------
# Preflight the control-plane token. On failure, instruct the operator to log in and
# exit non-zero — we never drive an interactive device-code/login flow ourselves.
require_az_login() {
  if ! az account get-access-token \
    --scope https://management.azure.com/.default \
    --query expiresOn -o tsv >/dev/null 2>&1; then
    die "Azure CLI is not logged in (control-plane token unavailable).
  Run: az login --scope https://management.azure.com/.default
  then re-run this script."
  fi
}

# Pin the subscription deterministically, independent of ambient CLI state, and
# verify it took. Runs even under --dry-run: `az account set` only mutates local CLI
# config (not a billable Azure resource), and the read-only state checks downstream
# (vm_state/vm_ip) must query the correct subscription to print an accurate plan.
pin_subscription() {
  local sub="${DEVBOX_SUBSCRIPTION_ID:?DEVBOX_SUBSCRIPTION_ID not set — see scripts/devbox/config.local.sh}"
  az account set --subscription "${sub}"
  local active
  active="$(az account show --query id -o tsv)"
  [[ "${active}" == "${sub}" ]] ||
    die "Failed to pin subscription to ${sub} (active is ${active})."
}

# --- VM state / address ------------------------------------------------------------
# Echo one of: notexist | running | deallocated | stopped | other:<raw>
vm_state() {
  local power
  if ! power="$(az vm show -d \
    -g "${DEVBOX_RESOURCE_GROUP}" -n "${DEVBOX_VM_NAME}" \
    --query powerState -o tsv 2>/dev/null)"; then
    printf 'notexist\n'
    return 0
  fi
  case "${power}" in
    "VM running") printf 'running\n' ;;
    "VM deallocated") printf 'deallocated\n' ;;
    "VM stopped") printf 'stopped\n' ;;
    *) printf 'other:%s\n' "${power}" ;;
  esac
}

# Echo the VM's public IP (empty if none / VM absent).
vm_ip() {
  az vm show -d \
    -g "${DEVBOX_RESOURCE_GROUP}" -n "${DEVBOX_VM_NAME}" \
    --query publicIps -o tsv 2>/dev/null
}

# The operator's current IPv4 egress address, for pinning the inbound NSG rule.
operator_ip() {
  curl -4 -s ifconfig.me
}

# Re-pin the inbound SSH NSG rule to the operator's current egress IP/32. Shared by
# up.sh (on every fresh provision / resume) and pin-ip.sh (the standalone
# IP-changed-mid-session re-pin). Honours DEVBOX_DRY_RUN via run_or_echo.
pin_nsg_to_operator() {
  local myip
  if [[ "${DEVBOX_DRY_RUN:-0}" == "1" ]]; then
    myip="<operator-ipv4>"
  else
    myip="$(operator_ip)"
    [[ -n "${myip}" ]] ||
      die "Could not determine operator egress IPv4 (curl -4 ifconfig.me was empty)."
  fi
  log "Pinning inbound SSH NSG rule to ${myip}/32 (primary direct-SSH path)."
  run_or_echo az network nsg rule update \
    -g "${DEVBOX_RESOURCE_GROUP}" --nsg-name "${DEVBOX_NSG_NAME}" \
    --name "${DEVBOX_SSH_RULE_NAME}" --source-address-prefixes "${myip}/32" \
    --output none
}

# --- SSH ---------------------------------------------------------------------------
# ssh into the devbox with TOFU host-key acceptance (accept-new — trust on first use,
# but still fail on a *changed* key). Usage: ssh_devbox <ip> [remote command...]
ssh_devbox() {
  local ip="$1"
  shift
  ssh -o StrictHostKeyChecking=accept-new "${DEVBOX_ADMIN_USER}@${ip}" "$@"
}

# Fail fast (with the fix) if no key is loaded in the Mac's ssh-agent — needed for the
# agent-forwarded (`ssh -A`) clone on the cold path.
ensure_ssh_key_loaded() {
  if ! ssh-add -l >/dev/null 2>&1; then
    die "No SSH key loaded in the agent.
  Run: ssh-add ~/.ssh/id_rsa   (the devbox key is passphrase-protected)"
  fi
}
