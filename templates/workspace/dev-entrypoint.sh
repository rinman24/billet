#!/usr/bin/env bash
# Workspace dev-container entrypoint: bring up the in-container sshd, then hand off to
# the CMD (`sleep infinity`). Copy into the repo as .devcontainer/dev-entrypoint.sh and
# wire it as the compose service's entrypoint (see docker-compose.snippet.yml). The venv
# bootstrap belongs in devcontainer.json's postCreateCommand, which billet runs after a
# cold `billet start` (and VS Code runs on attach) — not here.
#
# sshd must run as root (privilege separation + per-session setuid to `dev`), but the
# container's default user is the non-root `dev` (uid 1000) so login sessions and
# `docker exec` land as dev. So we keep `dev` as default and launch the system sshd via
# the passwordless sudo the image grants.
set -euo pipefail

HOST_KEY_DIR=/etc/ssh/host_keys

# Privilege-separation directory sshd requires at runtime (not persisted).
sudo install -d -m 0755 /run/sshd

# Persisted host keys: generated once into the named volume mounted here, then reused
# forever so the container's SSH identity is stable across rebuild/recreate.
sudo install -d -m 0755 "${HOST_KEY_DIR}"
if [ ! -f "${HOST_KEY_DIR}/ssh_host_ed25519_key" ]; then
    echo "dev-entrypoint: generating persisted ed25519 host key"
    sudo ssh-keygen -q -t ed25519 -f "${HOST_KEY_DIR}/ssh_host_ed25519_key" -N ''
fi
if [ ! -f "${HOST_KEY_DIR}/ssh_host_rsa_key" ]; then
    echo "dev-entrypoint: generating persisted rsa host key"
    sudo ssh-keygen -q -t rsa -b 4096 -f "${HOST_KEY_DIR}/ssh_host_rsa_key" -N ''
fi

# Fail fast with a readable error if the config is bad, rather than a silent no-sshd
# container that looks healthy until you try to connect.
sudo /usr/sbin/sshd -t

# Start sshd as a backgrounded daemon (root via sudo), then exec the CMD. Its
# per-connection children re-parent to PID 1; `sleep infinity` never wait()s, so reaping
# is delegated to docker-init (tini), wired via compose `init: true`.
echo "dev-entrypoint: starting sshd (container :22, published to the VM loopback)"
sudo /usr/sbin/sshd

exec "$@"
