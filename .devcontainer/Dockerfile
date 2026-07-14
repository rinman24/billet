# billet dev-container image.
#
# Much simpler than gswa-backend's multi-stage Dockerfile: billet is a pure-Python
# CLI/library (typer + dev tooling) with no database, no Redis, and no web service —
# so there is a single `development` stage and no ci/wheel/release targets here.
# GitHub Actions builds, lints, and tests billet on hosted runners
# (see .github/workflows/ci.yml); this image only exists to develop billet locally.
#
# What this stage provides:
#   - Python 3.11 (billet's pinned version: .python-version / requires-python>=3.11)
#   - uv (pinned, copied from Astral's distroless image — no curl-pipe-bash)
#   - the toolchain `make lint`/`test`/`imports` need: ruff + pyright (a Node binary),
#     import-linter, pytest, shellcheck, make, git
#   - Claude Code, tmux, and the Azure CLI (billet manages Azure Hosts via `az`)
#   - a non-root `dev` user (uid/gid 1000) so `claude --dangerously-skip-permissions`
#     runs and bind-mount ownership aligns with the VM host (azureuser, uid 1000)
#   - an in-container sshd (openssh-server + sshd.conf) so billet reaches this repo as a
#     Workspace via ProxyJump, exactly like gswa-backend. VS Code attach still works too.
#
# The project venv is deliberately NOT baked: billet uses the default in-repo `.venv`
# (pyright venvPath="."), which the /workspace bind mount would shadow at runtime.
# devcontainer.json's postCreateCommand runs `uv sync --frozen` to populate it on
# first container start instead.

FROM python:3.11-bookworm AS development

ENV PYTHONUNBUFFERED=TRUE \
    PYTHONDONTWRITEBYTECODE=TRUE \
    PIP_DEFAULT_TIMEOUT=100 \
    # uv uses the base image's interpreter and never downloads its own.
    UV_PYTHON_DOWNLOADS=never \
    AZURE_CORE_COLLECT_TELEMETRY=0

# uv from its pinned, auditable distroless image (matches gswa-backend; a Renovate
# dockerfile manager would bump this tag).
COPY --from=ghcr.io/astral-sh/uv:0.11.21@sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946 /uv /uvx /bin/

WORKDIR /workspace

# System toolchain. ca-certificates/gnupg/curl/build-essential cover repo setup, `make`,
# and any sdist build; node 20 lets pyright run without downloading its own Node;
# shellcheck backs shell linting; git comes from bookworm-backports (stock ~2.39 nags on
# push); openssh-client + openssh-server (the sshd billet connects through) + tmux + jq +
# sudo round out the dev workflow; azure-cli is here because billet's job is managing
# Azure Hosts via `az`. apt sources are GPG-pinned; versions float from the pinned repos,
# reproducibility comes from the image tag.
RUN set -eux; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list; \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ bookworm main" > /etc/apt/sources.list.d/azure-cli.list; \
    echo "deb http://deb.debian.org/debian bookworm-backports main" > /etc/apt/sources.list.d/backports.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        gnupg \
        build-essential \
        openssh-client \
        openssh-server \
        tmux \
        jq \
        sudo \
        shellcheck \
        nodejs \
        azure-cli; \
    apt-get install -y --no-install-recommends -t bookworm-backports git; \
    rm -rf /var/lib/apt/lists/*

# Code intelligence: pyright-langserver for Claude Code's pyright-lsp plugin (enabled in
# ~/.claude via the chezmoi dotfiles bootstrap). The plugin has no path setting, so the
# binary must be on PATH; Node is already installed above, so a root `npm install -g` puts
# pyright-langserver in /usr/bin — on PATH for the runtime `dev` user. Unpinned by
# design — same "versions float from the pinned repos, image tag = reproducibility" stance as
# node/git/az above. It's an editor assist; `make lint` (uv.lock's pyright) stays the
# authoritative gate, so exact parity with the lockfile isn't needed and would only add an
# unenforced version to keep in sync.
RUN npm install -g pyright \
    && pyright-langserver --help >/dev/null 2>&1 || true

# Dev ergonomics, baked system-wide: a plain `git push` on a new branch auto-creates the
# same-named upstream instead of erroring.
RUN git config --system push.autoSetupRemote true

# Non-root `dev` user (uid/gid 1000). Required, not cosmetic: Claude Code refuses
# `--dangerously-skip-permissions` as root, and uid 1000 matches the VM host owner
# (azureuser) so the /workspace bind mount needs no chown. Passwordless sudo is the
# accepted dev-convenience trade-off (also lets the entrypoint start the system sshd).
# Pre-create dev-owned mountpoints so the runtime named volumes / bind mounts land
# writable by dev rather than created root:root by the daemon; .ssh is 0700 so the
# bind-mounted authorized_keys is StrictModes-clean.
RUN groupadd -g 1000 dev \
    && useradd -u 1000 -g 1000 -m -s /bin/bash dev \
    && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev \
    && install -d -o dev -g dev -m 0700 /home/dev/.claude \
    && install -d -o dev -g dev -m 0700 /home/dev/.azure \
    && install -d -o dev -g dev -m 0700 /home/dev/.ssh

# Outbound SSH for `dev`: billet's origin is git@github.com:rinman24/billet.git, so
# keyless push relies on a forwarded SSH agent. accept-new records GitHub's host key on
# first contact instead of an interactive prompt that would hang a scripted push.
RUN printf 'Host github.com\n    StrictHostKeyChecking accept-new\n' > /home/dev/.ssh/config \
    && chown dev:dev /home/dev/.ssh/config \
    && chmod 0644 /home/dev/.ssh/config

# Per-developer git defaults in dev's home (survive rebuilds): trust the bind-mounted
# /workspace, and disable commit gpg-signing (no signing key in-container).
RUN su dev -c 'git config --global safe.directory /workspace' \
    && su dev -c 'git config --global commit.gpgsign false'

# In-container sshd hardening (key-only, non-root, dev-only, agent-forwarding). Debian's
# stock sshd_config Includes /etc/ssh/sshd_config.d/*.conf; runtime host keys come from a
# named volume (see .devcontainer/dev-entrypoint.sh + docker-compose.yml). Copied as root
# (before USER dev) so it lands under /etc.
COPY .devcontainer/sshd.conf /etc/ssh/sshd_config.d/billet.conf

# Claude Code via Anthropic's native installer as `dev`. Lands in /home/dev/.local/bin,
# which the PATH below puts first on every entry path (VS Code attach, docker exec, ssh).
ENV PATH=/home/dev/.local/bin:${PATH}
RUN su dev -c 'curl -fsSL https://claude.ai/install.sh | bash'

# Drop to the non-root user as the FINAL directive so it is the default everywhere.
USER dev
