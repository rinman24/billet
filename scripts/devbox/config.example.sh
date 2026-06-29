# shellcheck shell=bash
# config.example.sh — template for per-operator / per-project devbox values.
#
# Copy this to config.local.sh (gitignored) and fill in your values:
#
#     cp scripts/devbox/config.example.sh scripts/devbox/config.local.sh
#
# lib.sh sources config.sh (tracked, generic defaults) and then config.local.sh. If
# config.local.sh is absent the scripts fail fast with this instruction — nothing in
# Azure is touched. This file is tracked as the template; config.local.sh never is.
#
# Nothing here is a secret (a subscription id is an identifier, not a credential —
# Azure access is gated by `az login`). These values live in config.local.sh rather
# than the tracked config.sh because they are operator/tenant/project-specific, so the
# file is also the natural home for any future per-developer secret without retracking.

# --- Azure subscription ------------------------------------------------------------
# The subscription the devbox is provisioned into. pin_subscription() runs
# `az account set --subscription` to this value so the run is deterministic regardless
# of which subscription your ambient `az` session happens to default to.
DEVBOX_SUBSCRIPTION_ID="${DEVBOX_SUBSCRIPTION_ID:-00000000-0000-0000-0000-000000000000}"

# --- Azure placement ---------------------------------------------------------------
# The resource group and VM name for your devbox. The NSG name is derived as
# "<vm>NSG" by config.sh unless you override DEVBOX_NSG_NAME.
DEVBOX_RESOURCE_GROUP="${DEVBOX_RESOURCE_GROUP:-my-devbox-rg}"
DEVBOX_VM_NAME="${DEVBOX_VM_NAME:-my-devbox}"

# --- Repo + compose stack ----------------------------------------------------------
# The repo to clone onto the box and the compose service/container to drive. The
# container name follows compose's "devcontainer-<service>-1" convention by default.
DEVBOX_REPO_URL="${DEVBOX_REPO_URL:-git@github.com:my-org/my-repo.git}"
DEVBOX_REPO_DIR="${DEVBOX_REPO_DIR:-my-repo}"
DEVBOX_BACKEND_SERVICE="${DEVBOX_BACKEND_SERVICE:-my-repo}"
DEVBOX_CONTAINER_NAME="${DEVBOX_CONTAINER_NAME:-devcontainer-my-repo-1}"

# --- Connectivity / SSH aliases ----------------------------------------------------
# The ~/.ssh/config host aliases install-ssh-config.sh writes and connect*.sh use, the
# in-container ssh user/port, and where connect.sh lands you. Defaults (config.sh) are
# generic; override to taste.
# DEVBOX_HOST_ALIAS="${DEVBOX_HOST_ALIAS:-devbox}"
# DEVBOX_CONTAINER_ALIAS="${DEVBOX_CONTAINER_ALIAS:-devbox-container}"
# DEVBOX_CONTAINER_USER="${DEVBOX_CONTAINER_USER:-dev}"
# DEVBOX_CONTAINER_SSH_PORT="${DEVBOX_CONTAINER_SSH_PORT:-2222}"
# DEVBOX_WORKSPACE_FOLDER="${DEVBOX_WORKSPACE_FOLDER:-/workspace}"

# --- Repo bootstrap hooks (cold-provision path) ------------------------------------
# Repo-specific bootstrap is kept out of the generic tool. Set these to wire your repo's
# bootstrap into a Fresh provision; both default to a no-op. Each must be a self-contained
# shell command (single-quote to defer expansion to the VM). Examples:
#
#   # Generate the repo's env files on the VM host before `compose build`:
#   DEVBOX_HOST_BOOTSTRAP_CMD='if [ ! -f .env ]; then printf "ENV=dev\n" > .env; fi'
#
#   # Install the package + run migrations inside the container after `compose up`:
#   DEVBOX_CONTAINER_BOOTSTRAP_CMD='make install-root && uv run --no-sync alembic upgrade head'
#
#   # What `up.sh --verify` runs inside the container:
#   DEVBOX_VERIFY_CMD='make test'

# --- Optional per-developer overrides ----------------------------------------------
# Any constant from config.sh can be overridden here too. Examples:
# DEVBOX_LOCATION="${DEVBOX_LOCATION:-westus3}"
# DEVBOX_VM_SIZE="${DEVBOX_VM_SIZE:-Standard_D4s_v5}"   # once your DSv5 quota lands
