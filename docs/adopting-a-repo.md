# Adopting a repo as a Workspace

How to take a repository that has (or needs) a devcontainer and run it as a billet
**Workspace** on a shared Host. Onboarding is two halves that change hands cleanly:

1. **Repo-side** — one PR to the repository, giving its devcontainer an in-container
   sshd published to the VM loopback. Template files live in
   [`templates/workspace/`](https://github.com/rinman24/billet/tree/main/templates/workspace).
2. **Operator-side** — one `[workspaces.<key>]` block in `~/.config/billet/config.toml`,
   then `billet add` / `start` / `ssh-config` / `connect`.

billet's own `.devcontainer/` is the reference implementation: billet runs itself as a
Workspace beside gswa-backend on one Host, and the templates are extracted from it.

## What billet reads from the repo (the contract)

billet never duplicates container facts into its config. It reads five fields live from
the repo's `.devcontainer/devcontainer.json` on the Host
([ADR-0002](adr/adr-0002-workspace-subsystem.md)):

| Field | Used for |
| --- | --- |
| `service` | which compose service is the Workspace container |
| `dockerComposeFile` | the compose file(s), resolved relative to `.devcontainer/` |
| `workspaceFolder` | where `postCreateCommand` / `verify_cmd` run |
| `remoteUser` | the in-container user `connect` lands as |
| `postCreateCommand` | the bootstrap run once after a cold `billet start` |

Everything else the repo's compose stack must provide itself — most importantly a way
in: `billet connect` reaches the container by SSH via ProxyJump through the Host, so the
container runs its own hardened sshd published to the VM loopback at the port billet
assigns ([ADR-0003](adr/adr-0003-workspace-port-binding-contract.md)).

**devcontainer *features* are not applied.** billet drives the stack with raw
`docker compose`, not the devcontainer CLI — the `features` block in
`devcontainer.json` is VS Code tooling and does not run under `billet start`. Any tool
a feature would install (e.g. `gh`) must be baked into the image or added to
`postCreateCommand`.

## Repo-side: the PR to the repository

Copy these from [`templates/workspace/`](https://github.com/rinman24/billet/tree/main/templates/workspace)
verbatim into `.devcontainer/`:

- `sshd.conf` — key-only / non-root / `dev`-only sshd drop-in, host keys on a named
  volume.
- `dev-entrypoint.sh` — generates the persisted host keys on first boot, `sshd -t`
  fail-fast, starts sshd via sudo, then `exec "$@"`.
- `authorized_keys-stub` — tracked empty fallback so a build away from the VM never
  hard-fails.
- `env.example` → save as `.devcontainer/.env.example`, and add `.devcontainer/.env` to
  the repo's `.gitignore`.

Then merge the two snippets:

- `docker-compose.snippet.yml` into the repo's compose service: the
  `127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-<port>}:22` publish, the entrypoint wiring,
  `init: true`, the `authorized_keys` bind mount, and the host-keys named volume. Use
  the Workspace's **own assigned port** as the interpolation default so a manual
  `docker compose up` on the VM cannot collide with another Workspace's port; billet
  always exports `BILLET_CONTAINER_SSH_PORT` before compose, so the default never
  applies under billet.
- `Dockerfile.snippet` into the dev-container image: `openssh-server` + `sudo`, a
  non-root `dev` user (uid/gid 1000 — matches the VM admin user so the bind mount needs
  no chown), a pre-created `~/.ssh` (0700, dev-owned, so the runtime `authorized_keys`
  bind mount is StrictModes-clean), and the `COPY` of `sshd.conf` into
  `/etc/ssh/sshd_config.d/`.

Sanity checks before merging the PR:

- `devcontainer.json` declares `service`, `dockerComposeFile`, `workspaceFolder`, and
  `remoteUser: dev`, and its `postCreateCommand` fully bootstraps a cold container.
- Nothing the repo needs day-to-day hides in a `features` block (see the warning above).
- The compose service's default command keeps the container alive (`sleep infinity`).

## Operator-side: config + first start

Add the Workspace to `~/.config/billet/config.toml` — the annotated example block in
[`config.example.toml`](https://github.com/rinman24/billet/blob/main/config.example.toml)
documents every key:

```toml
[workspaces.my-repo]
host               = "devbox"
repo_url           = "git@github.com:my-org/my-repo.git"
repo_dir           = "my-repo"
container_ssh_port = 2225                    # distinct per Host; `billet add` validates
tmux_session       = "main"
host_alias         = "gswa-devbox"           # same alias as the shared Host
container_alias    = "my-repo-container"     # distinct per Workspace
host_bootstrap_cmd = "cp -n .devcontainer/.env.example .devcontainer/.env"
verify_cmd         = "make test"
```

Two keys carry the tricks:

- `container_ssh_port` — pick the next free loopback port on that Host;
  `billet add` rejects a duplicate. Use the same number as the compose default you put
  in the repo.
- `host_bootstrap_cmd` — runs in `repo_dir` on the Host before every `compose up`.
  `cp -n .devcontainer/.env.example .devcontainer/.env` wires the real
  `authorized_keys` path on the very first cold start with zero manual steps, and never
  clobbers a hand-edited `.env` (`-n`).

Then:

```bash
billet add my-repo              # validate the block (port uniqueness, host exists, …)
billet start my-repo --verify   # clone on the Host, compose up --build, postCreate, verify_cmd
billet ssh-config               # re-render ~/.ssh/config.d/billet.conf with the new aliases
billet connect my-repo          # ProxyJump in, land in the tmux session
```

`connect` runs `tmux new-session -A`, so the session is created on first attach — the
repo does not need to pre-create it.

## Port ledger

`billet add` enforces per-host port uniqueness, but there is no central reservation —
keep the assigned ports discoverable by keeping every Workspace (even other operators')
in `config.toml`. Current convention on the shared devbox: gswa-backend = 2222,
billet = 2224.
