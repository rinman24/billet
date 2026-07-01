# ADR-0002: The Workspace subsystem — devcontainer.json as the data contract

## Status

Accepted (2026-06-30). Builds on [ADR-0001](adr-0001-closed-architecture-decomposition.md)
and governs the Workspace subsystem (slice 5): `billet add | ls | start | stop | connect |
ssh-config | rm`.

## Context

The Host subsystem (ADR-0001) makes a cloud VM exist, reachable, and adopted. The Workspace
subsystem is what runs a *repo's devcontainer* on that Host and connects the operator to it:
clone the repo, `docker compose up`, run the devcontainer bootstrap, render an ssh-config, and
drop the operator into a tmux session — reproducing, in Python, the connect path the lifted
`scripts/devbox/*.sh` provided.

Several decisions here are load-bearing and were deliberately revisited from the first cut the
Host slice shipped. They share one theme: **decompose by volatility, and let each thing be
owned by the actor who actually changes it.**

## Decisions

### 1. `WorkspaceSpec` carries only billet intent; container facts come from `devcontainer.json`

The first cut (slice 4) pinned `service` / `compose_file` / `workspace_folder` /
`container_user` / a bootstrap command onto `WorkspaceSpec`. Slice 5 removes them. The slim
`WorkspaceSpec` is billet's **operator intent** only: `host`, `repo_url`, `repo_dir`, the
loopback `container_ssh_port`, `host_alias`, `container_alias`, `tmux_session`,
`agent_teams_flag`, `host_bootstrap_cmd`, `verify_cmd`.

The repo's **container facts** — the compose `service`, the `dockerComposeFile`(s), the
`workspaceFolder`, the `remoteUser`, and the `postCreateCommand` — are read live from the
repo's own `.devcontainer/devcontainer.json` into a `DevcontainerFacts` value object by
`ContainerAccess`.

Why (volatility + DDD): those facts change for the *repo's* reasons, on the *repo's* cadence,
authored by the *repo owner* — a different volatility axis and a different actor than billet's
operator intent. Löwy says encapsulate each volatility behind its own boundary; DDD frames
`devcontainer.json` as the repo's *published contract*, which billet reads through an
anti-corruption boundary (`ContainerAccess` → `DevcontainerFacts`) rather than re-declaring.
Carrying those facts in billet's `config.toml` would split one source of truth into two that
drift (the repo renames its service or bumps `workspaceFolder`; billet's copy goes stale).
This also realizes ADR-0001 §6 ("devcontainer.json is a read-only data contract").

Consequence: `connect` and `ssh-config` read `workspaceFolder` / `remoteUser` from facts. The
cost is JSONC parsing (below) and an SSH read of the file on the Host — a bounded, acceptable
price for a single source of truth. `connect`/`ls`/`stop` read it through the **ssh-config
host alias**, so they need no `az` call; `ssh-config` resolves the live IP (`az`) because the
config it writes must contain a literal `HostName`.

### 2. JSONC is parsed in-repo, dependency-free

`devcontainer.json` is JSONC (comments + trailing commas), which `json.loads` rejects. We
strip both — string-aware, so a `//` or `,]` inside a value survives — then use the stdlib
parser (`billet/shared/jsonc.py`), keeping billet's runtime dependency surface at exactly one
package (`typer`). The strip is the seam: if real-world files ever defeat it, swap the body of
`jsonc.loads` for `pyjson5` with no other change. Golden-tested against gswa's real file.

### 3. ssh-config moves from an in-place block to an Include file

The lifted `install-ssh-config.sh` wrote a marker-delimited block *into* `~/.ssh/config`.
Slice 5 evolves this to a tool-owned `~/.ssh/config.d/billet.conf` plus exactly one `Include`
line near the top of `~/.ssh/config`. billet overwrites `billet.conf` wholesale (it owns it)
and otherwise never touches the operator's hand-maintained config. Each Workspace renders a
container entry with `ProxyJump` + a stable `HostKeyAlias` (keying known-hosts on a
collision-free name, not the volatile `127.0.0.1:<port>` — needed once multiple Workspaces
share a Host in slice 6) and **no `RemoteCommand`** (IDE Remote-SSH needs a plain login shell;
tmux is `connect`'s job). The rendered text is golden-tested (`SshConfigEngine`). Tooling
decision: inline expected strings, no snapshot library — the block is small and stable.

### 4. `billet add` validates and prints; it never writes `config.toml`

`tomllib` is read-only by design (PEP 680). Rather than add a TOML writer, `billet add`
validates the operator-authored `[workspaces.<key>]` block (the host exists, the loopback port
is unique across Workspaces on that host) and echoes the canonical block. It does **not** edit
`config.toml`.

Why (volatility + DDD): statelessness is a load-bearing invariant (ADR-0001 §5), not a
convenience — operator intent lives in one operator-authored file; everything else is derived
from Azure + tags. A writer would take on a mutation concern (round-trip, comment/format
preservation, partial-write recovery) that is pure accidental complexity against billet's
mission, and it would cross the ownership boundary between "operator authors intent" and "tool
executes intent" (the same reason `terraform`/`kubectl` don't rewrite your files). An in-place
writer is a YAGNI deferral to slice 6, if hand-editing many Workspaces ever proves painful.

### 5. Host orchestration for `start` lives at the CLI, not inside `WorkspaceManager`

ADR-0001 §1 noted `workspace` sits above `host` because `start` needs the Host up. Slice 5
realizes that dependency at the **CLI client**, not as a `WorkspaceManager` → `HostManager`
call: `billet start` runs the Host plan (with its billable cold-create gate) and *then* the
Workspace plan. This keeps the cost gate where the human is (ADR-0001 §4) — burying it inside a
manager would force the manager to prompt, which managers must not do — and keeps each manager
single-responsibility. The layer ordering (`workspace` may call `host`) is unchanged and still
valid; it is simply not exercised by a manager-to-manager call in this slice.

### 6. VM adoption is an idempotent `HostProvider.ensure_tags` step

To "adopt the live VM," billet stamps `managed-by=billet` + `billet-host=<key>` on a VM it did
not create. Because tagging is backend-specific, it lives behind the `HostProvider` seam
(`ensure_tags`) and is scheduled as an idempotent, non-billable `ENSURE_TAGS` step on the
resume/running branches of `HostManager.plan_up` — so any `host up` (and therefore any
`workspace start`) adopts the VM. Tags-as-truth (ADR-0001 §5) stays real for slice-6 discovery.

### 7. `connect` returns an argv the client `exec`s

`WorkspaceManager.connect_target` returns the `ssh -t … tmux` argv; the CLI hands the terminal
off via `os.execvp`. The manager never replaces the process, so it stays unit-testable (assert
the argv; never exec in a test). Mirrors the lifted `connect.sh`.

## Consequences

- Container facts have one home (the repo). billet config shrinks and cannot drift from the
  repo's container definition.
- billet stays stateless and dependency-light (still only `typer` at runtime).
- The Workspace seams (`SourceAccess`, `ContainerAccess`, `SshConfigAccess`) are testable with
  Protocol-typed fakes; access is tested by spying the exact `ssh` / `docker compose` argv.
- `start` / `ssh-config` require the repo to be cloned and the Host reachable to read facts —
  the natural order is `start` → `ssh-config` → `connect`, which the CLI messages reinforce.
- The `postCreateCommand` **object** form (named parallel commands) is not yet supported; a
  string or array is. This is an explicit, surfaced limitation (a clear error), not a silent
  gap.

## Alternatives considered

- **Keep container facts on `WorkspaceSpec`.** Rejected: duplicates the repo's own
  `devcontainer.json`, drifts, and contradicts ADR-0001 §6.
- **A TOML writer for `billet add` (`tomlkit` or hand-rolled).** Rejected for slice 5: breaks
  statelessness and the intent-ownership boundary for a convenience YAGNI until multi-workspace.
- **`pyjson5` for JSONC.** Deferred: the in-repo strip keeps the dependency surface minimal and
  is golden-tested; the function is the swap-in seam if needed.
- **`WorkspaceManager.start` calls `HostManager.up`.** Rejected: would bury the billable gate
  inside a manager (ADR-0001 §4); the CLI orchestrates both phases instead.
- **A snapshot library (`syrupy`) for ssh-config golden tests.** Deferred: inline strings are
  zero-dependency and the rendered block is small and stable.
