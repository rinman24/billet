#!/usr/bin/env bash
# install-ssh-config.sh — write the managed devbox block into ~/.ssh/config.
# Mac-side only.
#
# IDEs (Cursor / Windsurf / VS Code Remote-SSH) read ~/.ssh/config natively, so the
# ssh config — not a wrapper script — is the source of truth for how to reach the VM
# host and the backend container. This installs two host entries idempotently:
#
#   ${DEVBOX_HOST_ALIAS}       -> the VM host (DEVBOX_ADMIN_USER @ the static public IP,
#                                 resolved via az)
#   ${DEVBOX_CONTAINER_ALIAS}  -> the backend container's sshd, via ProxyJump through the
#                                 host alias (127.0.0.1:${DEVBOX_CONTAINER_SSH_PORT} on the
#                                 VM), as DEVBOX_CONTAINER_USER with ForwardAgent on
#
# The block is delimited by `# >>> ${DEVBOX_SSH_BLOCK_LABEL} >>>` / `# <<< … <<<`;
# rerunning replaces it in place rather than appending a duplicate. The container entry
# carries NO RemoteCommand/tmux — IDE Remote-SSH needs a plain shell; tmux lives in
# connect.sh.
#
# Usage: scripts/devbox/install-ssh-config.sh [--dry-run]
#   --dry-run  print the resolved block and the target file without writing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/devbox/lib.sh
source "${SCRIPT_DIR}/lib.sh"

export DEVBOX_DRY_RUN=0

usage() {
  cat <<EOF
install-ssh-config.sh — write the managed devbox block into ~/.ssh/config.

Installs two host entries (${DEVBOX_HOST_ALIAS}, ${DEVBOX_CONTAINER_ALIAS}) idempotently,
resolving the VM's static public IP via az. Rerunning replaces the managed block in place.

Usage: scripts/devbox/install-ssh-config.sh [--dry-run]
  --dry-run  print the resolved block and the target file without writing
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

SSH_CONFIG="${HOME}/.ssh/config"
BEGIN_MARKER="# >>> ${DEVBOX_SSH_BLOCK_LABEL} >>>"
END_MARKER="# <<< ${DEVBOX_SSH_BLOCK_LABEL} <<<"

require_az_login
pin_subscription

# Resolve the VM's static public IP (same query up.sh uses on resume).
if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
  ip="<static-public-ip>"
else
  ip="$(vm_ip)"
  [[ -n "${ip}" ]] || die "Could not resolve the VM's public IP.
  Is ${DEVBOX_VM_NAME} created and not deallocated? Bring it up: scripts/devbox/up.sh"
fi
log "Resolved ${DEVBOX_VM_NAME} public IP: ${ip}"

# The managed block. The container entry has no RemoteCommand on purpose — IDE Remote-SSH
# needs a plain login shell; tmux is connect.sh's job.
block="$(cat <<EOF
${BEGIN_MARKER}
Host ${DEVBOX_HOST_ALIAS}
    HostName ${ip}
    User ${DEVBOX_ADMIN_USER}

Host ${DEVBOX_CONTAINER_ALIAS}
    ProxyJump ${DEVBOX_HOST_ALIAS}
    HostName 127.0.0.1
    Port ${DEVBOX_CONTAINER_SSH_PORT}
    User ${DEVBOX_CONTAINER_USER}
    ForwardAgent yes
${END_MARKER}
EOF
)"

if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
  log "(dry-run) would write the following block to ${SSH_CONFIG}:"
  printf '%s\n' "${block}" | sed 's/^/    | /'
  exit 0
fi

# Ensure ~/.ssh exists (0700) and ~/.ssh/config exists (0600).
mkdir -p "${HOME}/.ssh"
chmod 700 "${HOME}/.ssh"
if [[ ! -f "${SSH_CONFIG}" ]]; then
  : >"${SSH_CONFIG}"
  chmod 600 "${SSH_CONFIG}"
fi

# Rewrite atomically: strip any existing managed block, append the fresh one. awk
# drops the lines between the markers (inclusive); the new block is re-appended after.
tmp="$(mktemp -t billet-ssh-config.XXXXXX)"
awk -v begin="${BEGIN_MARKER}" -v end="${END_MARKER}" '
  $0 == begin { skip = 1; next }
  $0 == end   { skip = 0; next }
  !skip       { print }
' "${SSH_CONFIG}" >"${tmp}"

# Trim a trailing blank-line run so the block is separated by exactly one blank line.
printf '%s\n\n%s\n' "$(cat "${tmp}")" "${block}" >"${SSH_CONFIG}"
chmod 600 "${SSH_CONFIG}"
rm -f "${tmp}"

log "Wrote the managed devbox block to ${SSH_CONFIG}."
log "Connect: 'ssh ${DEVBOX_CONTAINER_ALIAS}' (or scripts/devbox/connect.sh for tmux);"
log "or pick '${DEVBOX_CONTAINER_ALIAS}' from an IDE's Remote-SSH host list and open ${DEVBOX_WORKSPACE_FOLDER}."
audit_log "install-ssh-config vm=${DEVBOX_VM_NAME} ip=${ip}"
