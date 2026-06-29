#!/usr/bin/env bash
# connect.sh — SSH into the backend container and land in a tmux session. Mac-side only.
#
# Thin wrapper over the container host alias (DEVBOX_CONTAINER_ALIAS) in ~/.ssh/config
# (installed by install-ssh-config.sh): plain SSH via ProxyJump through the VM host, with
# the Mac ssh-agent forwarded in (ForwardAgent on that host entry) so in-container git
# uses the Mac key. No Dev Tunnels, no `ssh -R` socket dance, no `docker exec`.
#
# It attaches to (or, on the first connect, creates) a single tmux session named
# DEVBOX_TMUX_SESSION (NOT -CC control mode) running a login bash at
# DEVBOX_WORKSPACE_FOLDER: create windows/panes and start Claude or other agents yourself
# from inside tmux. Detach with `Ctrl+B d` and rerun to reattach — `-A -s <session>` lands
# you back in the same session with your work still running. tmux lives here, not in ssh
# config, because IDE Remote-SSH needs a plain shell on the shared container entry.
#
# Prerequisite: scripts/devbox/install-ssh-config.sh has written the container host block
# to ~/.ssh/config (and config.local.sh exists).
#
# Usage: scripts/devbox/connect.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/devbox/lib.sh
source "${SCRIPT_DIR}/lib.sh"

exec ssh -t "${DEVBOX_CONTAINER_ALIAS}" \
  "cd ${DEVBOX_WORKSPACE_FOLDER} && exec tmux new-session -A -s ${DEVBOX_TMUX_SESSION} bash -l"
