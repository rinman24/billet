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
- `dev-entrypoint.sh` — two jobs. It generates the persisted host keys on first boot,
  `sshd -t` fail-fasts, starts sshd via sudo, then `exec "$@"`; and, just before sshd
  starts, it snapshots the container's own environment into `/etc/environment` so image
  `ENV` and compose `environment:` values are visible in sshd login shells.
- `authorized_keys-stub` — tracked empty fallback, mounted whenever
  `BILLET_AUTHORIZED_KEYS` is unset, so a build away from the VM never hard-fails.
- `env.example` → save as `.devcontainer/.env.example`, and add `.devcontainer/.env` to
  the repo's `.gitignore`. It sets exactly one variable,
  `BILLET_AUTHORIZED_KEYS=/home/azureuser/.ssh/authorized_keys` — the VM path whose keys
  the container's sshd should trust.

That `/etc/environment` snapshot is how **non-secret** image and compose environment reaches
`billet connect`, tmux, and the fleet runners: an sshd login shell inherits nothing from the
container's PID 1, so the entrypoint republishes the values into the file `pam_env` reads on
every session (the [ADR-0003 amendment](adr/adr-0003-workspace-port-binding-contract.md) has
the mechanism and its limits — the file is world-readable, and a value containing `"`, a
backslash, or a control character is skipped with a warning). It is not a secret channel:
credentials keep travelling through `~/.claude/settings.json`
([ADR-0006](adr/adr-0006-claude-token-injection.md)), never compose `environment:`.

Then merge the two snippets:

- `docker-compose.snippet.yml` into the repo's compose service: the
  `127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-<port>}:22` publish, the entrypoint wiring,
  `init: true`, the `${BILLET_AUTHORIZED_KEYS:-…}` bind mount of `authorized_keys`, the
  host-keys named volume, and the `<service>_gh_config` named volume on `~/.config/gh`.
  Use the Workspace's **own assigned port** as the interpolation default so a manual
  `docker compose up` on the VM cannot collide with another Workspace's port; billet
  always exports `BILLET_CONTAINER_SSH_PORT` before compose, so the default never applies
  under billet. The `authorized_keys` mount interpolates `BILLET_AUTHORIZED_KEYS` the same
  way, falling back to the tracked empty stub so a build away from the VM never hard-fails.
- `Dockerfile.snippet` into the dev-container image: `openssh-server` + `sudo`, a
  non-root `dev` user (uid/gid 1000 — matches the VM admin user so the bind mount needs
  no chown), pre-created `~/.ssh` and `~/.config/gh` (both 0700, dev-owned, so the
  runtime `authorized_keys` bind mount is StrictModes-clean and the `gh` volume lands
  writable by `dev` instead of root-owned), and the `COPY` of `sshd.conf` into
  `/etc/ssh/sshd_config.d/`.

The `gh` volume persists the *credentials*, not the tool. `~/.config/gh/hosts.yml` is
written on the container filesystem, so without the volume every `compose up --build`
discards the token and the next `gh` call demands `gh auth login` again; on the named
volume — dev-owned 0700 from the mountpoint the Dockerfile pre-creates — it survives
rebuild and recreate, the same pattern as `*_claude_home`
([ADR-0006](adr/adr-0006-claude-token-injection.md)). Nothing migrates a running
container's existing token onto the fresh volume, so adopting costs one last
`gh auth login`. Installing `gh` itself stays the repo's job — a devcontainer *feature*
will not do it (see above), so bake the binary into the image or install it from
`postCreateCommand`.

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
disappears whole rather than leaving a gap. (Do not verify this one the way you verified the
options above: `display-message -p` does not run `#()` jobs, so it expands the segment to
nothing whether or not the segment works — to prove the job fires, give the command a side
effect such as `| tee -a /tmp/probe` and tail that file instead.)

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

Before the first `start`, make sure the key that opens the Host is **loaded in your ssh
agent**. billet forwards the agent and runs every Host-side git non-interactively, so a
passphrase-protected key that is not already unlocked cannot be used and cannot prompt —
the clone fails instead of asking. `ssh-add -l` lists what the agent holds; `ssh-add
~/.ssh/<key>` (macOS: `ssh-add --apple-use-keychain ~/.ssh/<key>`) loads it.

Three keys carry the tricks:

- `repo_url` — must authenticate **non-interactively** from the Host: an ssh URL reached
  over the agent billet forwards, never an `https://` URL that would ask for a username.
  `start` is unattended, so every Host-side git runs with prompts disabled and an
  unauthenticated remote fails in seconds naming the cause, instead of hanging. If the Host
  checkout's `origin` later drifts from this value, billet warns and leaves the remote alone
  — repointing it is yours to do
  ([ADR-0007 amendment](adr/adr-0007-source-fast-forward-on-start.md#amendment-2026-09-04-non-interactive-git-on-the-host)).
- `container_ssh_port` — pick the next free loopback port on that Host;
  `billet add` rejects a duplicate. Use the same number as the compose default you put
  in the repo.
- `host_bootstrap_cmd` — runs in `repo_dir` on the Host before every `compose up`.
  `cp -n .devcontainer/.env.example .devcontainer/.env` wires the real
  `authorized_keys` path on the very first cold start with zero manual steps, and never
  clobbers a hand-edited `.env` (`-n`). Re-running `start` fetches and, when it is safe to
  do so, fast-forwards the Host checkout to upstream (ADR-0007) — this untracked `.env` is
  not treated as a dirty tree, so it always survives the advance. The corollary is that a
  change to `.env.example` never reaches a Host that already has an `.env` — re-copying the
  template does not update it and `cp -n` will not overwrite it, so a variable rename has to
  be applied to each Host's `.env` by hand (or the file deleted, letting the next `start`
  re-copy it).

Then:

```bash
billet add my-repo              # validate the block (port uniqueness, host exists, …)
billet start my-repo --verify   # clone (or fetch + safe fast-forward), compose up --build, postCreate, verify_cmd
billet ssh-config               # re-render ~/.ssh/config.d/billet.conf with the new aliases
billet connect my-repo          # ProxyJump in, land in the tmux session
```

`--verify` shows what `verify_cmd` printed — stdout and stderr in the order the command
emitted them — indented beneath the finished checklist, so a version check is readable off
the start instead of costing a second round trip. Long output is trimmed to its last 40
lines with a count of what was elided; `-v` prints it whole, `--quiet` prints none of it. A
failing `verify_cmd` still fails the start, with its output in the error.

The order matters, but it is forgiving: `ssh-config` reads `remoteUser` live from the Host, so
running it before `start` simply skips the new Workspace with a warning and still writes every
other alias — re-run it after `start` to pick the new one up
([ADR-0010](adr/adr-0010-ssh-config-partial-success.md)).

`connect` runs `tmux new-session -A`, so the session is created on first attach — the
repo does not need to pre-create it. `tmux_session` is omitted above on purpose: it defaults
to the Workspace key (`my-repo`), which is what makes the session name identify the Workspace
in `#S` and in stock tmux's default `status-left`. Set it explicitly only to attach to a
session some other tool already owns.

### Claude credentials in the container

Set `[billet].claude_token_cmd` once, globally, and every Workspace container gets an
authenticated `claude` with no interactive login and no per-repo change — billet merges the
token into the container's user-level `~/.claude/settings.json`
([ADR-0006](adr/adr-0006-claude-token-injection.md)). Generate it with `claude setup-token`,
store it, and point the command at the store.

On macOS the store step needs an account flag:

```bash
security add-generic-password -a "$USER" -s billet-claude -w '<token>'
```

`security` will happily create the item without `-a`, but the read side —
`security find-generic-password -s billet-claude -w`, which is what `claude_token_cmd`
runs — then fails to match it, and `start` aborts on empty output. If you hit that, delete
the item and re-add it with `-a`.

One consequence of injecting a `setup-token` credential: `claude` in the container may show
a shorter model list than you get locally, because the picker is filtered by what the
credential is entitled to. The model is still selectable by name —

```bash
claude --model <name>
```

— so a model missing from the picker is not a model you have lost.

## Port ledger

`billet add` enforces per-host port uniqueness, but there is no central reservation, so
`config.toml` *is* the ledger — keep every Workspace in it, including other operators', or
the next `container_ssh_port` cannot be chosen safely.

Allocation starts at 2222 and climbs; the shared devbox currently has 2222 and 2224–2228
assigned. That range is a snapshot and will age, so read the live answer out of your config
rather than trusting this line:

```bash
grep -n container_ssh_port ~/.config/billet/config.toml
```

`billet add` rejects a collision on the same Host, but only among the Workspaces your
config knows about — a port another operator assigned and never wrote down is invisible to
it, and the clash surfaces as a container that will not bind on the next `start`.
