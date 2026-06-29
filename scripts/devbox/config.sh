# shellcheck shell=bash
# config.sh — tracked, non-secret, generic defaults for the cloud devbox scripts.
#
# This file is sourced by lib.sh; it is NOT executable on its own. Every value is
# `${VAR:-default}` so any constant can be overridden from the environment (or from
# config.local.sh, which is sourced after this file) without editing tracked code.
#
# Operator/project-specific values (which subscription, resource group, VM, and which
# repo to run on the box) live in the gitignored config.local.sh — NOT here. The
# defaults below are deliberately generic placeholders; copy config.example.sh to
# config.local.sh and fill in your own.
#
# ----------------------------------------------------------------------------------
# Provenance stamp — the pinned supply chain up.sh installs on a fresh VM (level B):
#
#   Docker Engine + compose plugin
#     Installed from Docker's official apt repository, authenticated with Docker's
#     signed GPG key pinned at ${DEVBOX_DOCKER_GPG_URL} into a keyring and referenced
#     with `signed-by=` in the apt source. NOT the `get.docker.com | sh` convenience
#     script. TLS is verified (no `curl -k`).
#
# Docker is installed once, on the cold (Fresh provision) path only; Resume never
# re-installs. See docs/getting-started/cloud-devcontainer.md.
# ----------------------------------------------------------------------------------

# --- Azure placement (override in config.local.sh) ---------------------------------
DEVBOX_RESOURCE_GROUP="${DEVBOX_RESOURCE_GROUP:-devbox-rg}"
DEVBOX_VM_NAME="${DEVBOX_VM_NAME:-devbox}"
DEVBOX_LOCATION="${DEVBOX_LOCATION:-westus3}"
DEVBOX_ADMIN_USER="${DEVBOX_ADMIN_USER:-azureuser}"

# --- VM shape (matches the validated runbook) --------------------------------------
DEVBOX_VM_IMAGE="${DEVBOX_VM_IMAGE:-Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest}"
DEVBOX_VM_SIZE="${DEVBOX_VM_SIZE:-Standard_D4s_v4}"
DEVBOX_PUBLIC_IP_SKU="${DEVBOX_PUBLIC_IP_SKU:-Standard}"
DEVBOX_OS_DISK_GB="${DEVBOX_OS_DISK_GB:-64}"
DEVBOX_STORAGE_SKU="${DEVBOX_STORAGE_SKU:-Premium_LRS}"
# az vm create derives the NSG name from the VM name as "<vm>NSG".
DEVBOX_NSG_NAME="${DEVBOX_NSG_NAME:-${DEVBOX_VM_NAME}NSG}"
DEVBOX_SSH_RULE_NAME="${DEVBOX_SSH_RULE_NAME:-default-allow-ssh}"

# --- Repo + compose stack (override in config.local.sh) ----------------------------
# Which repo to clone onto the box and which compose service/container to drive. These
# are project-specific; the defaults below are placeholders.
DEVBOX_REPO_URL="${DEVBOX_REPO_URL:-git@github.com:your-org/your-repo.git}"
DEVBOX_REPO_DIR="${DEVBOX_REPO_DIR:-repo}"
DEVBOX_COMPOSE_FILE="${DEVBOX_COMPOSE_FILE:-.devcontainer/docker-compose.yml}"
DEVBOX_BACKEND_SERVICE="${DEVBOX_BACKEND_SERVICE:-app}"
DEVBOX_CONTAINER_NAME="${DEVBOX_CONTAINER_NAME:-devcontainer-app-1}"

# --- Supply-chain pins (see provenance stamp above) --------------------------------
DEVBOX_DOCKER_GPG_URL="${DEVBOX_DOCKER_GPG_URL:-https://download.docker.com/linux/ubuntu/gpg}"
DEVBOX_DOCKER_APT_URL="${DEVBOX_DOCKER_APT_URL:-https://download.docker.com/linux/ubuntu}"

# --- Connectivity / SSH (override in config.local.sh) ------------------------------
# The ssh-config host aliases install-ssh-config.sh writes (and connect*.sh use): one
# for the VM host, one for the container reached via ProxyJump. The container is reached
# as DEVBOX_CONTAINER_USER on DEVBOX_CONTAINER_SSH_PORT (the in-VM sshd port). connect.sh
# lands you in DEVBOX_WORKSPACE_FOLDER inside a tmux session named DEVBOX_TMUX_SESSION.
DEVBOX_HOST_ALIAS="${DEVBOX_HOST_ALIAS:-devbox}"
DEVBOX_CONTAINER_ALIAS="${DEVBOX_CONTAINER_ALIAS:-devbox-container}"
DEVBOX_CONTAINER_USER="${DEVBOX_CONTAINER_USER:-dev}"
DEVBOX_CONTAINER_SSH_PORT="${DEVBOX_CONTAINER_SSH_PORT:-2222}"
DEVBOX_WORKSPACE_FOLDER="${DEVBOX_WORKSPACE_FOLDER:-/workspace}"
DEVBOX_TMUX_SESSION="${DEVBOX_TMUX_SESSION:-main}"
# Label for the managed `~/.ssh/config` block (markers: `# >>> <label> >>>`).
DEVBOX_SSH_BLOCK_LABEL="${DEVBOX_SSH_BLOCK_LABEL:-billet devbox}"

# --- Connect-path defaults ---------------------------------------------------------
# Agent-team flag written into the VM's .claude/settings.local.json on provision, so
# agent teams is on whenever you start Claude inside the container's tmux session.
# Set empty to skip writing the settings file.
DEVBOX_AGENT_TEAMS_FLAG="${DEVBOX_AGENT_TEAMS_FLAG:-CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS}"

# --- Repo bootstrap hooks (override in config.local.sh) ----------------------------
# Repo-specific bootstrap is kept OUT of this generic tool; both hooks default to a
# no-op (`:`). On the cold (Fresh provision) path up.sh runs:
#   DEVBOX_HOST_BOOTSTRAP_CMD       on the VM, in the repo dir, before `compose build`
#                                   (e.g. generate .env/.env.test).
#   DEVBOX_CONTAINER_BOOTSTRAP_CMD  inside the backend container after `compose up`
#                                   (e.g. install the package + run DB migrations).
# Each must be a self-contained shell command (no unescaped double quotes); it is
# %q-quoted onto the VM and run with `eval` / `bash -lc`.
DEVBOX_HOST_BOOTSTRAP_CMD="${DEVBOX_HOST_BOOTSTRAP_CMD:-:}"
DEVBOX_CONTAINER_BOOTSTRAP_CMD="${DEVBOX_CONTAINER_BOOTSTRAP_CMD:-:}"
# Command run inside the container by `up.sh --verify` after a cold bootstrap.
DEVBOX_VERIFY_CMD="${DEVBOX_VERIFY_CMD:-make test}"

# --- Audit -------------------------------------------------------------------------
# Gitignored (covered by the repo *.log rule); the local breadcrumb trail. The
# authoritative run record is `az monitor activity-log`.
DEVBOX_AUDIT_LOG="${DEVBOX_AUDIT_LOG:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.devbox.log}"
