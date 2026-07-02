# ADR-0004: Enforcing `manages_workspaces` — the Host→Workspace placement rule

## Status

Accepted (2026-07-02). Extends [ADR-0001](adr-0001-closed-architecture-decomposition.md)
and [ADR-0002](adr-0002-workspace-subsystem.md) for the fleet-host slice (slice 7). Governs
which Hosts may carry Workspaces.

## Context

Slice 7 brings a *second* Host under billet: `gswa-fleet-host`, the VM the AFK fleet
(squadra) runs on. billet manages that VM's **lifecycle** (`billet host up|stop|pin-ip
--host fleet`) but registers **no Workspaces** on it — the fleet runtime is squadra's, not a
devcontainer billet clones and connects to.

`HostSpec` already carries a `manages_workspaces: bool` field (defaulting to `true`), parsed
by `RegistryAccess` but **referenced nowhere** — parsed-but-unused. With a fleet Host now
real, that field must actually *govern* behavior: a Workspace must never be placed on a Host
whose `manages_workspaces = false`.

The Host subsystem is already host-agnostic — `HostManager` / `HostProvider` never branch on
`manages_workspaces`, so `billet host … --host fleet` works today with no code change. The
gap is entirely on the Workspace side: nothing stops an operator from authoring a
`[workspaces.*]` whose `host` points at a non-managing Host.

Three questions had to be resolved (the hard calls this ADR records):

1. **Where does the rule live?**
2. **How is it enforced across the verbs — and can a future non-CLI client bypass it?**
3. **Is `manages_workspaces` the right model, or should Host gain a role/type?**

## Decision

### 1. The rule is a pure placement policy in the Workspace subsystem's engine layer.

"A Workspace may only be placed on a Host that manages Workspaces" is a domain invariant of
the Workspace bounded context — structurally identical to `PortAllocator`'s "ports unique per
Host." It is a **pure predicate over contract DTOs**, no I/O. So it lives where
`PortAllocator` lives: a `HostPlacementPolicy` engine in `workspace/engine/placement.py`,
unit-tested in isolation, held by `WorkspaceManager` (`self._placement`) and exposed as one
manager method, `assert_placement(host)`.

It does **not** live in `RegistryAccess` — a ResourceAccess reads the TOML resource and
returns DTOs; encoding a domain invariant there would make the data-access layer a rules
engine (against ADR-0001's layering). It does **not** live scattered as free helpers in the
CLI verbs — the CLI is the composition root, not the owner of domain rules.

### 2. The CLI composition root triggers the manager rule on the *command* verbs; the *query* verb tolerates and annotates.

Every workspace **command** verb (`add` / `start` / `stop` / `connect` / `ssh-config`)
resolves `host = registry.host(ws.host)` and then calls `manager.assert_placement(host)`
before proceeding — exactly as `add` already triggers `manager.register(...)`. A
non-managing Host raises `ConfigError` pointing the operator at `billet host … --host <key>`
for its lifecycle. `billet host` verbs never call it, so fleet lifecycle is untouched.

`billet ls` is a **query** (read-side projection), not a command. Coupling a query's success
to a command-side invariant conflates the read and write models (CQRS). At scale `ls` is the
observability surface — a diagnostic that blanks the whole listing because one of twenty rows
is misconfigured is a worse citizen than one that shows you *which* row is broken. So `ls`
stays robust: it reads `host.manages_workspaces` (a DTO field it already has in hand) and
renders the offending row `INVALID` rather than raising. Reading a field for a projection is
not enforcing a rule — the enforcement *semantics* (the raise) remain solely in the engine,
triggered only by commands.

### 3. `manages_workspaces` stays a `bool`.

The only behavioral distinction today is binary — a Host either carries Workspaces or it does
not. A `bool` already exists, is already parsed, and expresses exactly that. Promoting it to a
`role`/`type` enum (`"devbox" | "fleet"`) would model distinctions that do not yet drive any
behavior — speculative generality the volatility-based method warns against. If a third Host
kind ever needs *different* behavior, the enum can be introduced then, behind the same field.

## Consequences

- Registering the fleet Host is a config table (`[hosts.fleet]` with `manages_workspaces =
  false`) plus documentation — no Host-subsystem code. `billet host up|stop|pin-ip --host
  fleet` already works.
- A misconfigured Workspace (one placed on a non-managing Host) fails fast and identically on
  every command verb, with a message that names the fix. `ls` still lists it, flagged
  `INVALID`, so the operator can see and correct it.
- The enforcement primitive is `WorkspaceManager.assert_placement` — a reusable method, not
  CLI-local logic. A future non-CLI client (an application-service facade, a daemon) enforces
  the same invariant by calling the same method; we do not pre-thread it through the
  lifecycle to guard a client that does not exist.
- `manages_workspaces` defaults to `true`, so every existing single-Host config is unaffected;
  only a Host that opts out (`= false`) is refused as a Workspace target.

## Alternatives considered

- **Thread the Host into every `WorkspaceManager` use-case method** (`plan_start`,
  `apply_start`, `plan_stop`, `read_facts`, `status_all`, block-build), enforcing internally
  so the invariant holds regardless of caller. Rejected as speculative generality here: (a) it
  collides with ADR-0001's boundary — the manager deliberately takes the narrow `RemoteHost`
  (reach-only) and never depends on the `HostProvider`; the live IP `RemoteHost` needs comes
  from `provider.status()`, which the manager must not call, so threading the full host either
  drags the provider into the manager or invents a new DTO through ~6 methods; (b) the
  bypass it defends against — a second client skipping the CLI — is answered by exposing
  `assert_placement` as a reusable manager method, added to a real facade when one exists; (c)
  the actual risk is a single config mistake on one Host that, by construction, has zero
  Workspaces pointing at it. Defense-in-depth threaded through the whole lifecycle is
  decomposition against a requirement that is not there.
- **Enforce in `RegistryAccess.workspace(key)`** (eager, at parse time). Rejected: it couples
  workspace *parsing* to Host *semantics* and turns a ResourceAccess into a rules engine
  (against ADR-0001 layering). The identical failure would also make `billet ls` throw, losing
  the query-robustness above.
- **Make `ls` hard-fail uniformly** like the command verbs. Rejected on the CQRS grounds in
  Decision §2: a read projection should surface anomalies, not refuse to answer.
- **Promote `manages_workspaces` to a Host `role`/`type` enum.** Rejected as premature — see
  Decision §3.
