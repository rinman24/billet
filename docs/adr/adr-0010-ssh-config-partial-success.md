# ADR-0010: `ssh-config` renders what it can (per-Workspace degradation, not all-or-nothing)

## Status

Accepted (2026-09-05). Refines [ADR-0002](adr-0002-workspace-subsystem.md) §3, which made
`billet.conf` a tool-owned file billet overwrites wholesale, and applies
[ADR-0004](adr-0004-host-manages-workspaces.md) §2's read-side/write-side split — a
diagnostic that blanks the whole listing because one row is broken is a worse citizen than
one that names the broken row — to the one command verb whose output is *also* infrastructure
the operator depends on. Governs `billet ssh-config` only.

## Context

`billet ssh-config` renders one `SshConfigBlock` per configured Workspace and installs them
as `~/.ssh/config.d/billet.conf`. Deriving a single block is not a pure function of
`config.toml`: it needs two **live** reads.

- The Host's public IP, from `provider.status(host)` — the file must contain a literal
  `HostName`, so unlike `connect`/`ls`/`stop` this verb cannot reach the Host through its own
  alias (ADR-0002 §1).
- The container's `remoteUser`, from `manager.read_facts(ws, remote)`, which SSHes to the
  Host and parses `<repo_dir>/.devcontainer/devcontainer.json`.

That second read is not incidental, and it is not removable. `devcontainer.json` is the
repo's published contract, read live through an anti-corruption boundary
([ADR-0001](adr-0001-closed-architecture-decomposition.md) §6, ADR-0002 §1); `remoteUser` is
deliberately **not** duplicated into `config.toml`, precisely so billet's copy cannot go stale
when the repo changes it. So a block for a Workspace can only be derived once that Workspace's
repo is actually cloned on a Host that is actually up.

Both reads therefore fail for reasons that are ordinary per-Workspace *state*, not operator
error: the repo has never been `start`ed, the Host is deallocated, the checkout was removed,
the file does not parse. The original implementation let the first such failure propagate out
of the loop, so the whole command exited 1 and wrote nothing.

The consequence, observed on the operator's real config: a single declared-but-never-started
Workspace (`OpenCodeSandbox`) denied SSH connectivity to **every other** Workspace. The
aliases for healthy, running Workspaces could not be written because an unrelated one had
never been cloned. Failure coupling ran the wrong way — a Workspace's own unreadiness took
out the connectivity of its neighbours — and the workaround was to hand-edit `config.toml` to
delete the offending table, which is exactly the operator-authored intent billet is not
supposed to make people churn.

The subtlety is that `ssh-config` is a **command** verb, and ADR-0004 §2 puts command verbs on
the hard-fail side of the line. But what makes hard-fail right for `add`/`start`/`stop` is
that they act on *one* named Workspace, so refusing is total and local. `ssh-config` takes no
key: it is a fan-out over the whole registry whose product is a single file. Its failure mode
is not "this operation was refused" but "connectivity to unrelated things was withdrawn."

## Decision

**`ssh-config` is partial-success. A failure to derive one Workspace's block is caught per
Workspace: that Workspace is skipped with a `caution` line naming it and the cause, and every
other Workspace is still rendered and installed. A fault in the *configuration* still raises
and aborts the whole run.**

### The exit-code rule, exactly

| Condition | Behavior |
| --- | --- |
| No `[workspaces.*]` declared at all | Empty-state hint, exit 0 (unchanged) |
| Some Workspaces derived, some skipped | Write the file with what derived; `caution` per skip; exit 0 |
| Every declared Workspace skipped | `HostOperationError` naming the skipped keys; exit 1 |
| Config fault (unknown host ref, ADR-0004 misplacement) | Raise; exit 1 |

The "everything was skipped" case is exit 1 on purpose. It is not a partial view — it is a run
that produced nothing, and silently overwriting `billet.conf` with an empty file would remove
working aliases and report success while doing it. The message names the skipped keys and
points at `billet start <key>`.

The `wrote …` success line gains a muted tail — `ensured Include in ~/.ssh/config · skipped a,
b` — so a partial render is never mistaken for a complete one at a glance. The dry-run path
degrades identically; the panel it prints is the file that *would* be written.

### Why configuration faults are deliberately not degraded

Two failures inside the loop are raised outside the per-Workspace guard: a `[workspaces.<key>]`
pointing at an undefined `[hosts.<key>]`, and an ADR-0004 `manages_workspaces = false`
misplacement.

These are not state; they are mistakes in the one file the operator authors, and they are
*already* true before any live call is made. Skipping them would degrade the wrong thing:
it would let a typo'd host key sit unnoticed in `config.toml` indefinitely, rendered as one
grey caution line among the ordinary "not started yet" ones — the fault indistinguishable from
the benign case. It would also make ADR-0004's placement rule advisory on this verb while it
stays mandatory on `add`/`start`/`stop`/`connect`, which is precisely the inconsistency ADR-0004
§2 was written to avoid. The rule there is that *command* verbs fail fast and identically; this
ADR narrows that to "on config faults," and does not repeal it.

The distinction the code draws is therefore: **a fault in the config raises; a fault in the
live derivation is reported and stepped over.**

### Where the policy lives, and why it is not in the manager

The skip loop lives in the CLI (`_renderable_blocks` in
`src/billet/cli/workspace_commands.py`), not in `WorkspaceManager`.

The CLI is the composition root and it is the actor that owns this loop. Building a block
needs `provider.status(host)` for the live IP, and `WorkspaceManager` deliberately does not
depend on the `HostProvider` — it takes the narrow, reach-only `RemoteHost` (ADR-0002 §5,
ADR-0004 "Alternatives considered"). Moving the loop into the manager would drag the provider
across that boundary to buy nothing: the fan-out, the per-item recovery, and the operator-facing
caution lines are all client concerns, and ADR-0001 §4 already keeps client-side concerns
(rendering, gating, what to tell the human) at the client.

Contrast `billet ls`, whose equivalent degradation *does* live in the manager
(`WorkspaceManager._probe`, which turns an unreachable Host into a `reachable=False` status
rather than an exception). That is correct there for the same reason: `status_all` is the
manager's own fan-out, so the manager owns the per-item recovery inside it. The rule is not
"degradation belongs in layer X" — it is **whoever owns the loop owns its per-item failure
policy.**

## Consequences

- One un-cloned or unreachable Workspace can no longer withdraw SSH connectivity from the
  others. The blast radius of a Workspace's state is that Workspace.
- A newly declared `[workspaces.<key>]` no longer has to be commented out of `config.toml`
  before `ssh-config` will run. Declare it, run `ssh-config`, get a caution; `billet start` it,
  re-run, get the alias.
- **The honest downside: a rendered `billet.conf` is now a partial view of `config.toml`.**
  Where the file was previously all-or-nothing — it existed and was complete, or the command
  failed — a missing alias is now ambiguous at the shell prompt: `ssh <alias>` just says
  "Could not resolve hostname." The caution lines say which Workspaces were left out at render
  time, but nothing in the file itself records the omission. In practice a missing alias means
  **"not started yet"**, and the fix is always the same: `billet start <key>`, then re-run
  `billet ssh-config`. This is the natural `start` → `ssh-config` → `connect` order ADR-0002
  already reinforces; partial success makes an out-of-order run survivable rather than fatal.
- Exit codes stay meaningful for scripting: 0 means the file on disk is the best available
  rendering, 1 means nothing usable was produced or the config is wrong.
- The Workspace subsystem is untouched — no manager, engine, or access change. The policy is
  ~15 lines at the composition root, which is also where it can be revisited without
  disturbing the domain.

## Alternatives considered

- **Keep the hard fail and tell operators to comment out un-started Workspaces.** Rejected:
  it makes the operator hand-edit authored intent to work around transient state, and it
  guarantees the edit is forgotten and re-litigated on the next `start`.
- **Skip config faults too, for uniformity.** Rejected in Decision §"Why configuration faults
  are deliberately not degraded": a typo'd host key would become permanently invisible, and
  ADR-0004's placement rule would silently weaken on one verb.
- **Merge into the existing `billet.conf` instead of overwriting, so a skipped Workspace keeps
  its previous stale entry.** Rejected: ADR-0002 §3 makes `billet.conf` wholly tool-owned and
  rendered from truth. Reading back the file billet wrote would make it a state store —
  against ADR-0001 §5's statelessness — and would preserve entries whose IP has since changed,
  turning "no alias" (an honest, legible failure) into "an alias that times out."
- **Move the loop and its policy into `WorkspaceManager`.** Rejected: it requires the
  `HostProvider` inside the manager for the live IP, crossing the boundary ADR-0002 §5 and
  ADR-0004 drew deliberately, in exchange for no reuse — there is no second client of this
  fan-out.
- **Exit non-zero on any skip while still writing the file.** Rejected: the common case — a
  declared Workspace not started yet, or a Host deallocated overnight — is normal operation,
  not an error, and a verb that always exits 1 on a healthy fleet trains operators (and CI) to
  ignore its exit code.
