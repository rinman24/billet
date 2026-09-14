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
`postCreateCommand` — which is why `gh` and `az` ship as
[opt-in image recipes](#optional-auth-tooling-gh-az) rather than as a `features` entry.

## Repo-side: the PR to the repository

Copy these from [`templates/workspace/`](https://github.com/rinman24/billet/tree/main/templates/workspace)
verbatim into `.devcontainer/`:

- `sshd.conf` — key-only / non-root / `dev`-only sshd drop-in, host keys on a named
  volume.
- `dev-entrypoint.sh` — three jobs. It repairs the ownership of named-volume mount
  targets under `/home/dev` that came up root-owned and empty, and ensures `~/.ssh`
  (see [Locker ownership at mount time](#locker-ownership-at-mount-time-berth-1) below);
  it generates the persisted host keys on first boot, `sshd -t` fail-fasts, starts sshd
  via sudo, then `exec "$@"`; and, just before sshd starts, it snapshots the container's
  own environment into `/etc/environment` so image `ENV` and compose `environment:`
  values are visible in sshd login shells.
- `berth.version` — the Berth version these files carry (one integer; see the
  [templates README](https://github.com/rinman24/billet/blob/main/templates/workspace/README.md#the-berth)).
  The entrypoint reads it from beside itself and logs `dev-entrypoint: berth=N` on every
  start, so copy it every time you re-copy a Berth file; a missing file logs
  `berth=unknown`.
- `authorized_keys-stub` — tracked empty fallback, mounted whenever
  `BILLET_AUTHORIZED_KEYS` is unset, so a build away from the VM never hard-fails. Under
  billet it is never unset: `start` exports the Host admin user's
  `~/.ssh/authorized_keys` before every compose call, so the same key that opens the Host
  opens the container with no file to copy.

That `/etc/environment` snapshot is how **non-secret** image and compose environment reaches
`billet connect`, tmux, and the fleet runners: an sshd login shell inherits nothing from the
container's PID 1, so the entrypoint republishes the values into the file `pam_env` reads on
every session (the [ADR-0003 amendment](adr/adr-0003-workspace-port-binding-contract.md) has
the mechanism and its limits — the file is world-readable, and a value containing `"`, a
backslash, or a control character is skipped with a warning). It is not a secret channel:
credentials keep travelling through `~/.claude/settings.json`
([ADR-0006](adr/adr-0006-claude-token-injection.md)), never compose `environment:`.

### Locker ownership at mount time (Berth 1)

A **Locker** is a named compose volume that persists one tool's state under the login
user's home — `<service>_claude_home` on `~/.claude`, `<service>_gh_config` on
`~/.config/gh`, `<service>_azure_home` on `~/.azure`. Docker initialises a fresh volume from
whatever the image has at the mountpoint; when the image has nothing there, the directory
comes up `root:root` and the tool cannot write it. From Berth 1 the entrypoint, not the
image, guarantees ownership
([ADR-0013](adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md)): at every start,
before generating host keys, it reads `/proc/self/mountinfo` and applies one policy to
every Docker named volume mounted under `/home/dev`:

| Target state | What happens | Log line |
| --- | --- | --- |
| directory, owned by uid 0, empty | `sudo -n install -d -o <uid> -g <gid> -m 0700` | `dev-entrypoint: repaired <path> (was root:root <mode>)` |
| directory, owned by uid 0, populated | left alone | `dev-entrypoint: warning: <path> is root-owned and not empty; not repaired` |
| directory, owned by another uid | left alone | `dev-entrypoint: warning: <path> owned by uid <n>; not repaired` |
| directory, owned by the login user | left alone, mode included | none |
| the repair itself fails | sshd still starts | `dev-entrypoint: warning: repair of <path> failed; continuing` |

`~/.ssh` gets the same rule applied to a known path (created dev-owned 0700 if missing),
because sshd's `authorized_keys` bind mount lives under it; it is Berth infrastructure,
not a Locker. Bind mounts are skipped by source. Nothing is recursive and nothing is
`chown -R`. The consequence for the repo is that **a Locker is one compose `volumes:` line
and nothing in the Dockerfile**: the image need not pre-create the mountpoint, and the
`Dockerfile.snippet` keeps only `~/.ssh`.

Migration for a Workspace already bitten by this — a Locker whose first use failed with
`Permission denied` — is nothing more than re-copying the Berth 1 entrypoint (with
`berth.version`) and running the next `billet start`: an already-broken **empty** volume is
repaired on that start, and no `docker volume rm` is involved. A populated root-owned volume
is only reported; decide whose files they are before re-owning them by hand.

Then merge the two snippets:

- `docker-compose.snippet.yml` into the repo's compose service: the
  `127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-<port>}:22` publish, the entrypoint wiring,
  `init: true`, the `${BILLET_AUTHORIZED_KEYS:-…}` bind mount of `authorized_keys`, the
  host-keys named volume, and the Claude Locker — `<service>_claude_home` on `~/.claude`
  with `CLAUDE_CONFIG_DIR: /home/dev/.claude` under `environment:`. The Locker is where
  the token billet injects lands ([ADR-0006](adr/adr-0006-claude-token-injection.md)); the
  variable pins `claude` to the same directory, and its value is fixed because the injector
  hardcodes `~/.claude/settings.json`. No image-side mountpoint is needed: the entrypoint
  re-owns a fresh Locker at start
  ([ADR-0013](adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md)).
  Use the Workspace's **own assigned port** as the interpolation default so a manual
  `docker compose up` on the VM cannot collide with another Workspace's port; billet
  always exports `BILLET_CONTAINER_SSH_PORT` before compose, so the default never applies
  under billet. The `authorized_keys` mount interpolates `BILLET_AUTHORIZED_KEYS` the same
  way — also exported by billet — falling back to the tracked empty stub so a build away
  from the VM never hard-fails.
- `Dockerfile.snippet` into the dev-container image: `openssh-server` + `sudo`, a
  non-root `dev` user (uid/gid 1000 — matches the VM admin user so the bind mount needs
  no chown), a pre-created `~/.ssh` (0700, dev-owned, so the runtime `authorized_keys`
  bind mount is StrictModes-clean), and the `COPY` of `sshd.conf` into
  `/etc/ssh/sshd_config.d/`.

Both snippets carry nothing beyond what every Workspace needs — no toolchain, and no
authentication tooling. The one Locker the compose snippet ships is Claude's, because every
Workspace receives a token; `gh` and `az` Lockers are the volume part of their recipes.

### Optional: auth tooling (`gh`, `az`)

A Workspace that runs `gh` or `az` opts in by merging a **recipe** from
[`templates/workspace/auth-tooling/`](https://github.com/rinman24/billet/tree/main/templates/workspace/auth-tooling).
Take `gh`, `az`, both, or neither — billet itself neither installs these CLIs nor reads
their credentials ([ADR-0011](adr/adr-0011-optional-auth-tooling-recipes.md)).

A recipe is two parts, and both are required:

| Part | Snippet | Why it is not optional |
| --- | --- | --- |
| Binary — the CLI, in the image | `<tool>.Dockerfile.snippet` | A *feature* will not install it (see the warning above), so the binary otherwise lands in `~/.local/bin` by hand — which is on no volume, so every `compose up --build` wipes it |
| Locker — its credentials, on a named volume | `<tool>.docker-compose.snippet.yml` | The token is written to the container filesystem, so without the volume every rebuild demands `gh auth login` / `az login` again |

`gh` mounts `<service>_gh_config` on `~/.config/gh`, `az` mounts `<service>_azure_home` on
`~/.azure` — the same persistence pattern as `<service>_claude_home`
([ADR-0006](adr/adr-0006-claude-token-injection.md)). There is no third, image-side part:
the entrypoint re-owns a fresh Locker at container start
([ADR-0013](adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md)), so a Locker is
one compose `volumes:` line and nothing else. Adopting only one part looks fine until the
next rebuild, which is exactly when it is hardest to connect to the merge that caused it.
[`auth-tooling/README.md`](https://github.com/rinman24/billet/blob/main/templates/workspace/auth-tooling/README.md)
has the merge detail: where the binary part goes, why the `az` binary part requires a
consumer-built image (the shared toolchain image never ships `azure-cli`), and the caveat
that nothing migrates a running container's existing token onto the fresh volume — so
adopting a recipe costs one last `gh auth login` / `az login`.

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
- If the repo's workflow uses `gh` or `az`, **both** parts of that recipe are merged — the
  binary into the Dockerfile (or already in the image), and its Locker both mounted on the
  service and declared under the compose file's top-level `volumes:`. A volume that is
  mounted but never declared fails at `up`, i.e. only on the VM.
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
- `host_bootstrap_cmd` — optional; a general hook that runs in `repo_dir` on the Host
  before every `compose up`, defaulting to a no-op (`":"`), which is why the block above
  omits it. Nothing about `authorized_keys` belongs here: billet exports
  `BILLET_AUTHORIZED_KEYS` (the Host admin user's `~/.ssh/authorized_keys`) and
  `BILLET_CONTAINER_SSH_PORT` itself before every compose call. Re-running `start`
  fetches and, when it is safe to do so, fast-forwards the Host checkout to upstream
  (ADR-0007); untracked files a hook writes are not treated as a dirty tree, so they
  survive the advance.

**Migrating from billet < 0.4.0.** Earlier versions set `BILLET_AUTHORIZED_KEYS` through a
`.env` file, wired by `host_bootstrap_cmd = "cp -n .devcontainer/.env.example
.devcontainer/.env"`. Delete those two lines from `~/.config/billet/config.toml` (one per
Workspace that carried it) and drop `.env.example` from the repo's `.devcontainer/`. The
untracked `.env` already on a Host is inert — a shell export outranks compose's `.env`
interpolation — so nothing on the Host needs deleting, and the hook itself is not
deprecated.

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

Allocation starts at 2222 and climbs; the shared Host currently has 2222 and 2224–2228
assigned. That range is a snapshot and will age, so read the live answer out of your config
rather than trusting this line:

```bash
grep -n container_ssh_port ~/.config/billet/config.toml
```

`billet add` rejects a collision on the same Host, but only among the Workspaces your
config knows about — a port another operator assigned and never wrote down is invisible to
it, and the clash surfaces as a container that will not bind on the next `start`.
