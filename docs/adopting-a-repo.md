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

### Dotfiles: chezmoi (the standard)

Workspaces standardize on [chezmoi](https://chezmoi.io)-managed dotfiles. Bake the chezmoi
binary into the dev-container image (billet's own Dockerfile pins it into `/usr/local/bin`),
and pull the dotfiles at container start with `chezmoi init --apply rinman24` (first time) or
`chezmoi update --apply` (thereafter). billet's global `personal_bootstrap_cmd` does exactly
this on every `billet start`; billet's own `devcontainer.json` `postCreateCommand` repeats it
so a direct devcontainer open gets dotfiles too. Both paths converge on the same
[`rinman24/dotfiles`](https://github.com/rinman24/dotfiles) repo, which owns the tmux
config — so no tmux config is baked into any image.

### Rendering billet's Workspace identity (the consuming half)

billet never writes `status-style`, `status-left`, or any other presentation option — the
theme owns those ([ADR-0008](adr/adr-0008-workspace-identity-publication.md)). Instead
`connect` publishes three tmux **user options** into the session, and your own tmux config
decides whether and how to render them:

| Option | Value |
| --- | --- |
| `#{@billet_workspace}` | the Workspace key |
| `#{@billet_host}` | the Host key |
| `#{@billet_color}` | the Workspace's `status_color` — hex, **with** its leading `#`; unset when the block omits it |

Consuming them takes two primitives (verified on tmux 3.7b):

```text
#{?#{@billet_workspace},…present…,…absent…}   # ternary guard: unset options expand to ""
#[bg=#{@billet_color}]                        # correct — expands to #[bg=#C05CE0]
#[bg=##{@billet_color}]                       # WRONG — ## is tmux's literal-# escape, so
                                              #   this yields the uninterpolated text
                                              #   #[bg=#{@billet_color}]
```

The color already carries its `#`; interpolate it directly and never double it. Guard every
segment with the ternary so the same config still works in a plain shell tmux, where the
options do not exist.

Rendering is optional. `tmux_session` defaults to the Workspace key, so stock tmux's default
`status-left` of `[#{session_name}] ` already tells you which Workspace you are in, and
`tmux show -g @billet_workspace` answers it exactly.

#### Worked example: a one-line status segment

Nothing below depends on a theme or a plugin manager — the options are plain tmux user
options, so any config can read them. The most portable form is a single append to
`status-right`:

```text
set -ag status-right '#{?#{@billet_workspace},#[#{?#{@billet_color},bg=#{@billet_color}#,fg=#11111b,default}] #{@billet_workspace}#{?#{@billet_host}, @ #{@billet_host},} #[default],}'
```

The outer ternary drops the segment whole when billet published nothing. The inner one styles
the label with the brand color when `status_color` is set and falls back to `default` when it
is not, so the label still renders on an uncolored Workspace — `#,` is the escape for a
literal comma inside a ternary branch, and `#11111b` is a near-black picked to stay legible
on the brand hues. Expansions (`tmux display-message -p '#{E:status-right}'`, tmux 3.7b):

| published | segment expands to |
| --- | --- |
| workspace + host + color | `#[bg=#C05CE0,fg=#11111b] billet @ devbox #[default]` |
| workspace + host, no color | `#[default] billet @ devbox #[default]` |
| workspace only | `#[default] billet #[default]` |
| nothing | *(empty)* |

#### Live state is yours, not billet's

Those three options are the whole set, and the set is closed
([ADR-0009](adr/adr-0009-scope-of-identity-publication.md)). A fourth is admitted only if it is
in hand on the connect path with no new I/O, stable across connects, not derivable more cheaply
in-session, and identity rather than telemetry. Host power state, public IP and container
running state each fail at least one of those, and `billet ls` already reports host IP and
running state — a status bar is not where they are needed.

Branch and dirty state is the thing you will most want on the bar, and it is the clearest
non-candidate. The prelude leads `new-session -A`, so it re-publishes on every `billet connect`
— but nothing updates the options between connects: `connect` `execvp`s into `ssh` and leaves
no billet process behind. A published branch name would be honest until your next checkout and
stale after it. It is also the cheapest thing to compute in-session, so put it in your own
config as a `#()` segment:

```text
set -ag status-right '#(git -C "#{pane_current_path}" rev-parse --abbrev-ref HEAD 2>/dev/null | sed "s|.*| &|")'
```

`git -C` against the pane's own directory, `2>/dev/null` so a pane outside a repo renders
nothing, and the `sed` supplies the leading space only when there is a branch — so the segment
disappears whole rather than leaving a gap.

The cost is one fork per attached client per `status-interval`. Measured on tmux 3.7b: 6
invocations in ~6 s at `status-interval 1`, and 0 in ~8 s at `status-interval 15`. The cadence
is your dial — a one-second interval with a `#()` segment is a fork per second per attached
client, paid inside the container, for as long as the client stays attached.

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
  clobbers a hand-edited `.env` (`-n`). Re-running `start` fetches and, when it is safe to
  do so, fast-forwards the Host checkout to upstream (ADR-0007) — this untracked `.env` is
  not treated as a dirty tree, so it always survives the advance.

Then:

```bash
billet add my-repo              # validate the block (port uniqueness, host exists, …)
billet start my-repo --verify   # clone (or fetch + safe fast-forward), compose up --build, postCreate, verify_cmd
billet ssh-config               # re-render ~/.ssh/config.d/billet.conf with the new aliases
billet connect my-repo          # ProxyJump in, land in the tmux session
```

`connect` runs `tmux new-session -A`, so the session is created on first attach — the
repo does not need to pre-create it. `tmux_session` is omitted above on purpose: it defaults
to the Workspace key (`my-repo`), which is what makes the session name identify the Workspace
in `#S` and in stock tmux's default `status-left`. Set it explicitly only to attach to a
session some other tool already owns.

## Port ledger

`billet add` enforces per-host port uniqueness, but there is no central reservation —
keep the assigned ports discoverable by keeping every Workspace (even other operators')
in `config.toml`. Current convention on the shared devbox: gswa-backend = 2222,
billet = 2224.
