#!/usr/bin/env bash
# rebuild.sh — rebuild the cloud devbox compose stack over direct SSH in one verb.
#
# A single memorable verb for the daily "I changed the Dockerfile / dependencies and
# want the running stack rebuilt" loop, instead of memorising `docker compose -f
# .devcontainer/docker-compose.yml up -d --build` on the VM host. It reaches the VM over
# direct SSH (the primary path — same hop as up.sh: ssh_devbox to the VM's static public
# IP; no `az login`, no NSG re-pin, since a rebuild changes no Azure state), runs the
# rebuild remotely, and prints the resulting `compose ps`.
#
# Only services with a `build:` section are rebuilt; the pulled images are left
# untouched, and named volumes + anything baked into the image survive a recreate — so no
# re-bootstrap is needed on these paths. The destructive clean-slate (`down -v` +
# re-bootstrap, which wipes data volumes) is deliberately NOT offered here;
# use the manual sequence in docs/getting-started/cloud-devcontainer.md for that.
#
# A connect failure usually means the VM is deallocated — run scripts/devbox/up.sh first.
#
# Usage: scripts/devbox/rebuild.sh [--no-cache] [--force-recreate] [--yes] [--dry-run]
#   --no-cache        rebuild the image from scratch (`build --no-cache` then `up -d`)
#   --force-recreate  recreate the containers even if the image is unchanged
#   --yes / -y        skip the confirmation prompt
#   --dry-run         print the ssh/compose plan without contacting the VM
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/devbox/lib.sh
source "${SCRIPT_DIR}/lib.sh"

export DEVBOX_DRY_RUN=0
export DEVBOX_ASSUME_YES=0
no_cache=0
recreate_flag=""

usage() {
  cat <<'EOF'
rebuild.sh — rebuild the cloud devbox compose stack over direct SSH in one verb.

Rebuilds the services with a `build:` section; the pulled images and named volumes are
left intact. The destructive clean-slate (`down -v` + re-bootstrap) is NOT offered — see
the manual sequence in docs/getting-started/cloud-devcontainer.md. A connect failure
usually means the VM is deallocated — run scripts/devbox/up.sh first.

Usage: scripts/devbox/rebuild.sh [--no-cache] [--force-recreate] [--yes] [--dry-run]
  --no-cache        rebuild the image from scratch (`build --no-cache` then `up -d`)
  --force-recreate  recreate the containers even if the image is unchanged
  --yes / -y        skip the confirmation prompt
  --dry-run         print the ssh/compose plan without contacting the VM
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-cache) no_cache=1 ;;
    --force-recreate) recreate_flag="--force-recreate" ;;
    --yes | -y) DEVBOX_ASSUME_YES=1 ;;
    --dry-run) DEVBOX_DRY_RUN=1 ;;
    -h | --help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# Human-readable label for the active variant, for the log line + audit breadcrumb.
variant="default"
if [[ "${no_cache}" == "1" && -n "${recreate_flag}" ]]; then
  variant="no-cache+force-recreate"
elif [[ "${no_cache}" == "1" ]]; then
  variant="no-cache"
elif [[ -n "${recreate_flag}" ]]; then
  variant="force-recreate"
fi

# Build the remote body locally. The two trusted config values (DEVBOX_REPO_DIR,
# DEVBOX_COMPOSE_FILE) and the variant selectors are %q-quoted into a `set -euo
# pipefail` prelude so all VM-side expansion stays on the VM; the body itself is a
# quoted heredoc (no host-side expansion). `--no-cache` is a build-time flag, so it
# splits into a `build` then a plain `up -d` (`up --build` does not accept it);
# RECREATE_FLAG is `--force-recreate` or empty.
remote_body="$(
  printf 'set -euo pipefail\n'
  printf 'REPO_DIR=%q\n' "${DEVBOX_REPO_DIR}"
  printf 'COMPOSE_FILE=%q\n' "${DEVBOX_COMPOSE_FILE}"
  printf 'NO_CACHE=%q\n' "${no_cache}"
  printf 'RECREATE_FLAG=%q\n' "${recreate_flag}"
  cat <<'REMOTE'
cd "$REPO_DIR"
if [ "$NO_CACHE" = "1" ]; then
  docker compose -f "$COMPOSE_FILE" build --no-cache
  docker compose -f "$COMPOSE_FILE" up -d $RECREATE_FLAG
else
  docker compose -f "$COMPOSE_FILE" up -d --build $RECREATE_FLAG
fi
docker compose -f "$COMPOSE_FILE" ps
REMOTE
)"

if [[ "${DEVBOX_DRY_RUN}" == "1" ]]; then
  log "(dry-run) variant: ${variant}"
  log "(dry-run) would resolve the VM public IP via vm_ip, then run over ssh:"
  printf '+ ssh -o StrictHostKeyChecking=accept-new %s@<vm-public-ip> bash -se <<remote-body\n' \
    "${DEVBOX_ADMIN_USER}"
  printf '%s\n' "${remote_body}" | sed 's/^/    | /'
  exit 0
fi

log "Rebuilding the compose stack on ${DEVBOX_VM_NAME} (variant: ${variant})."
confirm "Rebuild + recreate the compose stack on ${DEVBOX_VM_NAME}? This drops any attached
  VS Code / tmux session (the repo bind mount, named volumes, and any auth/data volumes
  all persist)." ||
  die "Aborted; nothing rebuilt."

# Resolve the VM's static public IP (needs an az control-plane token + the right
# subscription pinned). An empty result means the VM is absent/deallocated — run
# scripts/devbox/up.sh first.
require_az_login
pin_subscription
ip="$(vm_ip)"
[[ -n "${ip}" ]] || die "no public IP for ${DEVBOX_VM_NAME} — is it running? Run scripts/devbox/up.sh first."

log "Running the rebuild on the VM host over SSH ..."
audit_log "rebuild vm=${DEVBOX_VM_NAME} ip=${ip} variant=${variant}"

# Direct SSH to the VM host (ssh_devbox does TOFU host-key acceptance). `bash -se` reads
# the body from stdin under `set -e`.
ssh_devbox "${ip}" 'bash -se' <<<"${remote_body}"
