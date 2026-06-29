#!/usr/bin/env bash
# up.sh — bring the cloud devbox up. Idempotent: state-detects Fresh provision vs
# Resume and does the right thing.
#
#   not-exist   -> Fresh provision (cold): create RG+VM, pin NSG, install the pinned
#                  supply chain (Docker), clone, build the compose stack, bootstrap.
#   deallocated -> Resume (hot): re-pin NSG to current IP, az vm start, wait for SSH,
#                  docker compose up -d.
#   running     -> ensure the compose stack is up.
#
# Usage: scripts/devbox/up.sh [--dry-run] [--verify] [--yes]
#   --dry-run  print the az/ssh/compose commands without executing them
#   --verify   on the cold path, run `make test` (full suite) after bootstrap
#   --yes      skip confirmation prompts (incl. the billable-create gate)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/devbox/lib.sh
source "${SCRIPT_DIR}/lib.sh"

export DEVBOX_DRY_RUN=0
export DEVBOX_ASSUME_YES=0
DEVBOX_VERIFY=0

usage() {
  cat <<'EOF'
up.sh — bring the cloud devbox up (Fresh provision OR Resume, auto-detected).

Usage: scripts/devbox/up.sh [--dry-run] [--verify] [--yes]
  --dry-run  print the az/ssh/compose commands without executing them
  --verify   on the cold path, run `make test` (full suite) after bootstrap
  --yes      skip confirmation prompts (incl. the billable-create gate)
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DEVBOX_DRY_RUN=1 ;;
    --verify) DEVBOX_VERIFY=1 ;;
    --yes | -y) DEVBOX_ASSUME_YES=1 ;;
    -h | --help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# --- remote execution helpers ------------------------------------------------------

# Run a bash snippet on the VM (or print it under dry-run). The snippet should be
# self-contained; pass host-side config in as VM-side variables via a %q-quoted
# prelude (see remote_prelude) so VM-side expansions stay on the VM.
ssh_or_echo() {
  local ip="$1" script="$2"
  if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
    log "(dry-run) would run on ${ip} over ssh:"
    printf '%s\n' "${script}" | sed 's/^/    | /'
  else
    ssh_devbox "${ip}" 'bash -se' <<<"${script}"
  fi
}

# Same, but with an interactive TTY and agent forwarding — for the agent-forwarded
# clone (the Mac key flows in over -A; nothing is parked on the VM).
ssh_or_echo_tty() {
  local ip="$1" script="$2"
  if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
    log "(dry-run) would run on ${ip} over ssh -tA (interactive):"
    printf '%s\n' "${script}" | sed 's/^/    | /'
  else
    ssh -tA -o StrictHostKeyChecking=accept-new "${DEVBOX_ADMIN_USER}@${ip}" "${script}"
  fi
}

# Emit the shared `set -euo pipefail` header plus a %q-quoted `name=value` line for
# each named host config var, so VM-side expansions stay on the VM. Each remote_*
# snippet passes only the vars it uses. Names are host-side (DEVBOX_*); the VM-side
# name drops the DEVBOX_ prefix (e.g. DEVBOX_REPO_URL -> REPO_URL). All DEVBOX_* vars
# are guaranteed set by config.sh, so the indirect expansion is safe under set -u.
remote_prelude() {
  printf 'set -euo pipefail\n'
  local host_name vm_name
  for host_name in "$@"; do
    vm_name="${host_name#DEVBOX_}"
    printf '%s=%q\n' "${vm_name}" "${!host_name}"
  done
}

wait_for_ssh() {
  local ip="$1" tries="${2:-30}" i
  if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
    log "(dry-run) would wait for SSH on ${ip}"
    return 0
  fi
  log "Waiting for SSH on ${ip} ..."
  for ((i = 1; i <= tries; i++)); do
    if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes \
      "${DEVBOX_ADMIN_USER}@${ip}" true 2>/dev/null; then
      return 0
    fi
    sleep 5
  done
  die "SSH did not come up on ${ip} after $((tries * 5))s."
}

# --- Azure-side steps --------------------------------------------------------------
# pin_nsg_to_operator lives in lib.sh (shared with pin-ip.sh).

create_vm() {
  run_or_echo az group create \
    --name "${DEVBOX_RESOURCE_GROUP}" --location "${DEVBOX_LOCATION}" \
    --output none
  run_or_echo az vm create \
    --resource-group "${DEVBOX_RESOURCE_GROUP}" \
    --name "${DEVBOX_VM_NAME}" \
    --image "${DEVBOX_VM_IMAGE}" \
    --size "${DEVBOX_VM_SIZE}" \
    --os-disk-size-gb "${DEVBOX_OS_DISK_GB}" \
    --storage-sku "${DEVBOX_STORAGE_SKU}" \
    --public-ip-sku "${DEVBOX_PUBLIC_IP_SKU}" \
    --admin-username "${DEVBOX_ADMIN_USER}" \
    --generate-ssh-keys \
    --tags managed-by=billet \
    --output none
}

# --- remote bootstrap phases (cold path) -------------------------------------------

remote_install_docker() {
  # Level B provenance: Docker's signed GPG key + pinned apt repo (signed-by), TLS
  # verified (no curl -k). NOT get.docker.com|sh.
  local body
  body='
echo "[devbox/vm] installing Docker from the signed apt repo ..."
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL "$DOCKER_GPG_URL" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
arch="$(dpkg --print-architecture)"
echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] $DOCKER_APT_URL ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
'
  ssh_or_echo "$1" "$(remote_prelude DEVBOX_DOCKER_GPG_URL DEVBOX_DOCKER_APT_URL)${body}"
}

remote_clone() {
  # Agent-forwarded clone — the developer key stays on the Mac (-A), never the VM.
  local body
  body='
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[devbox/vm] cloning $REPO_URL ..."
  git clone "$REPO_URL" "$REPO_DIR"
else
  echo "[devbox/vm] repo already present; fetching ..."
  git -C "$REPO_DIR" fetch --prune
fi
'
  ssh_or_echo_tty "$1" "$(remote_prelude DEVBOX_REPO_URL DEVBOX_REPO_DIR)${body}"
}

remote_build_and_bootstrap() {
  # Run the repo's host-side bootstrap hook (e.g. generate .env), write the optional
  # Claude agent-teams flag, build + start the compose stack, then run the repo's
  # in-container bootstrap hook. Both hooks default to a no-op (`:`) — repo-specific
  # bootstrap lives in config.local.sh, not in this generic tool. Uses `sg docker` (not
  # newgrp) so the just-added docker group is active without a re-login.
  local body
  body='
cd "$REPO_DIR"
echo "[devbox/vm] running host bootstrap hook ..."
eval "$HOST_BOOTSTRAP_CMD"
mkdir -p .claude
if [ -n "$AGENT_TEAMS_FLAG" ] && [ ! -f .claude/settings.local.json ]; then
  cat > .claude/settings.local.json <<JSON
{
  "env": {
    "${AGENT_TEAMS_FLAG}": "1"
  }
}
JSON
fi
echo "[devbox/vm] building the compose stack ..."
sg docker -c "docker compose -f \"$COMPOSE_FILE\" up -d --build"
echo "[devbox/vm] running container bootstrap hook ..."
sg docker -c "docker compose -f \"$COMPOSE_FILE\" exec -T \"$BACKEND_SERVICE\" bash -lc \"$CONTAINER_BOOTSTRAP_CMD\""
'
  ssh_or_echo "$1" "$(remote_prelude DEVBOX_REPO_DIR DEVBOX_COMPOSE_FILE DEVBOX_BACKEND_SERVICE DEVBOX_AGENT_TEAMS_FLAG DEVBOX_HOST_BOOTSTRAP_CMD DEVBOX_CONTAINER_BOOTSTRAP_CMD)${body}"
}

remote_verify() {
  local body
  body='
cd "$REPO_DIR"
echo "[devbox/vm] running the verify command ($VERIFY_CMD) ..."
sg docker -c "docker compose -f \"$COMPOSE_FILE\" exec -T \"$BACKEND_SERVICE\" bash -lc \"$VERIFY_CMD\""
'
  ssh_or_echo "$1" "$(remote_prelude DEVBOX_REPO_DIR DEVBOX_COMPOSE_FILE DEVBOX_BACKEND_SERVICE DEVBOX_VERIFY_CMD)${body}"
}

remote_compose_up() {
  # Resume/running: bring the stack up (group already active on a provisioned box).
  local body
  body='
cd "$REPO_DIR"
docker compose -f "$COMPOSE_FILE" up -d
'
  ssh_or_echo "$1" "$(remote_prelude DEVBOX_REPO_DIR DEVBOX_COMPOSE_FILE)${body}"
}

# --- dispatch paths ----------------------------------------------------------------

cold_provision() {
  log "Fresh provision: VM ${DEVBOX_VM_NAME} does not exist."
  confirm "Create resource group + VM ${DEVBOX_VM_NAME} (${DEVBOX_VM_SIZE}, billable)?" ||
    die "Aborted before creating any Azure resources."

  create_vm
  pin_nsg_to_operator

  local ip
  if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
    ip="<vm-public-ip>"
  else
    ip="$(vm_ip)"
    [[ -n "${ip}" ]] || die "VM created but no public IP found."
  fi
  log "VM public IP: ${ip}"

  ensure_ssh_key_loaded
  wait_for_ssh "${ip}"

  remote_install_docker "${ip}"
  remote_clone "${ip}"
  remote_build_and_bootstrap "${ip}"
  [[ "${DEVBOX_VERIFY}" == "1" ]] && remote_verify "${ip}"

  if [[ "${DEVBOX_DRY_RUN}" != "1" ]]; then
    audit_log "up vm=${DEVBOX_VM_NAME} path=fresh-provision ip=${ip} verify=${DEVBOX_VERIFY}"
  fi
  log "Fresh provision complete. Run scripts/devbox/install-ssh-config.sh, then connect"
  log "with scripts/devbox/connect.sh (or 'ssh ${DEVBOX_CONTAINER_ALIAS}' from an IDE)."
}

resume() {
  log "Resume: VM ${DEVBOX_VM_NAME} is deallocated."
  pin_nsg_to_operator
  run_or_echo az vm start \
    --resource-group "${DEVBOX_RESOURCE_GROUP}" --name "${DEVBOX_VM_NAME}" \
    --output none

  local ip
  if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
    ip="<vm-public-ip>"
  else
    ip="$(vm_ip)"
    [[ -n "${ip}" ]] || die "VM started but no public IP found."
  fi
  log "VM public IP: ${ip}"

  wait_for_ssh "${ip}"
  remote_compose_up "${ip}"

  if [[ "${DEVBOX_DRY_RUN}" != "1" ]]; then
    audit_log "up vm=${DEVBOX_VM_NAME} path=resume ip=${ip}"
  fi
  log "Resume complete. Connect with scripts/devbox/connect.sh (or 'ssh ${DEVBOX_CONTAINER_ALIAS}')."
}

ensure_running() {
  log "VM ${DEVBOX_VM_NAME} is already running; ensuring the compose stack is up."
  local ip
  if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
    ip="<vm-public-ip>"
  else
    ip="$(vm_ip)"
    [[ -n "${ip}" ]] || die "VM running but no public IP found."
  fi
  wait_for_ssh "${ip}"
  remote_compose_up "${ip}"
  if [[ "${DEVBOX_DRY_RUN}" != "1" ]]; then
    audit_log "up vm=${DEVBOX_VM_NAME} path=ensure-running ip=${ip}"
  fi
}

# --- main --------------------------------------------------------------------------

require_az_login
pin_subscription

state="$(vm_state)"
case "${state}" in
  notexist) cold_provision ;;
  deallocated) resume ;;
  running) ensure_running ;;
  stopped) die "VM ${DEVBOX_VM_NAME} is 'stopped' (not deallocated). Start it manually or deallocate then re-run." ;;
  *) die "VM ${DEVBOX_VM_NAME} is in an unexpected state: ${state}." ;;
esac
