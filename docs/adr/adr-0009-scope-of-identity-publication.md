# ADR-0009: the published set is closed at three, and a test decides any fourth

## Status

Proposed (2026-07-25). Extends [ADR-0008](adr-0008-workspace-identity-publication.md), which
established *that* billet publishes Workspace identity as tmux user options but did not bound
*what*. Decision only: no behavior changes, and — uniquely among the ADRs here — no
implementation is expected to follow. The outcome is that `TmuxStatusEngine` stays as
ADR-0008 left it.

## Context

ADR-0008 rule 1 names three published options — `@billet_workspace`, `@billet_host`,
`@billet_color` — and describes them as "identity". It does not say what else qualifies, and
the namespace is free to extend: `set @foo` always succeeds, costs one more command in an
already-chained prelude, and breaks nothing. A contract that is free to widen and never says
where it stops will widen.

Six candidates were raised when ADR-0008 was written and deferred without evaluation: Host
power state, Host public IP, container running state, repo branch / dirty state, the resolved
devcontainer service name, and a "cold-provisioned-this-session" marker.

### What the connect path actually holds

`connect` (`src/billet/cli/workspace_commands.py:363-378`) resolves exactly three things
before building the argv: the `WorkspaceSpec`, the `HostSpec`, and the `DevcontainerFacts`
read live from the Host. It makes **no cloud-provider call** — `_remote_via_alias`
(`workspace_commands.py:124-126`) reaches the Host through its ssh-config alias precisely so
that no `az` round-trip is needed. `connect` is the only Workspace lifecycle command with
that property, and it is why attaching is fast.

Everything on `HostStatus` — `power_state`, `public_ip`, `vm_size` — is therefore *not* in
hand. Publishing any of it means adding a provider call to the one path that has none.

Separately, billet is stateless between invocations (`workspace_commands.py:132-136`: "billet
is stateless: it never writes config.toml"), so `connect` cannot know anything that only
`start` observed.

### How the channel behaves — measured on tmux 3.7b

The prelude is *leading*: `tmux <prelude> new-session -A -s <session>`. That position was
chosen in ADR-0008 to survive the re-attach short-circuit, and it has a consequence that ADR
did not draw out — **the prelude re-runs on every `billet connect`, so published values are
refreshed per connect, not fixed for the life of the session.** Verified against an
already-existing session:

```
$ tmux -L s set -g @billet_x first
$ tmux -L s set -g @billet_x second \; new-session -A -d -s ws-alpha
$ tmux -L s display -p '#{@billet_x}'
second
```

The staleness window is therefore "between two connects" — which is bounded for an operator
who reconnects daily and unbounded for a session left attached for a week.

Three mechanisms could carry state that changes faster than that, and all three are closed:

- **billet pushing updates.** An external `tmux set -g @opt` *does* update a live session
  immediately, verified: pushing `alpha` then `beta` to `@billet_workspace` on an attached
  session re-rendered `status-left` as `ws=alpha` then `ws=beta` with no reattach. But billet
  is not alive to push. `connect` ends in `_execvp(argv)` (`workspace_commands.py:378`),
  replacing the billet process with `ssh`; there is no daemon and no return.
- **An in-container billet agent** doing the pushing. This would require writing into the
  container, which ADR-0008 already rejected for the config-fragment alternative ("it would
  require writing files into the container, which the connect path deliberately does not
  do"), and it would hand billet a supervised process lifecycle it does not currently own.
- **`#()` shell formats on the consumer side.** This works and is the operator's to use. Its
  cost, measured: one fork per attached client per `status-interval` — 6 invocations in ~6 s
  at `status-interval 1`, and 0 in ~8 s at `status-interval 15`. Bounded and predictable, but
  paid in the container, by the theme, on every tick, forever.

So the boundary is not a preference. Anything mutating faster than the reconnect cadence
cannot reach a user option at all, and the only live channel available runs on the far side
of the ownership line ADR-0008 drew.

## Decision

**The published set is closed at `@billet_workspace`, `@billet_host`, and `@billet_color`.
None of the six candidates is published. A fourth option is admitted only by passing all four
criteria below.**

1. **In hand.** Available on the connect path with no new I/O — no provider call, no extra
   ssh round-trip. `connect`'s zero-cloud-call property is a feature and is not for sale.
2. **Stable across connects.** It cannot change between two `billet connect` invocations, or
   it changes only via an event that itself forces a reconnect (so the republish is
   self-healing and the stale value is never observed).
3. **Not derivable in-session.** A consumer inside the container cannot compute it more
   cheaply and more accurately than billet can hand it over.
4. **Identity, not telemetry.** It answers "which Workspace is this?" (ADR-0008 rule 1). The
   user-option namespace is an identity channel, not a metrics bus.

Against those, the six candidates and the other values `connect` happens to hold:

| Candidate | 1 in hand | 2 stable | 3 not derivable | 4 identity | Verdict |
|---|---|---|---|---|---|
| Host power state | no — needs `az` | yes | **no** — tautology | no | reject |
| Host public IP | no — needs `az` | yes, self-healing | yes | **no** | reject |
| Container running state | yes | yes | **no** — tautology | no | reject |
| Repo branch / dirty | **no** | **no** | **no** — `#()` | no | reject |
| Devcontainer service name | yes | yes | marginal | **no** | reject |
| Cold-provisioned marker | **no** | **no** | n/a | no | reject |
| `workspace_folder`, `remote_user`, `container_alias`, `container_ssh_port` | yes | yes | **no** | no | reject |

Four of these deserve their reasoning stated rather than tabulated:

- **Power state and container-running are tautologies.** At the instant the prelude runs, the
  operator has an SSH session through a running Host into a running container. Both values
  are the constant `true`, bought — in the Host's case — with a cloud round-trip. Publishing
  a constant spends contract surface to convey nothing, which is rule 4 of ADR-0008 applied
  to data instead of glyphs.
- **Public IP is the closest thing to a real miss.** It is genuinely stable (a deallocate/start
  cycle that changes the IP also destroys the container and the tmux session, forcing a
  reconnect that republishes) and genuinely not derivable from inside. It fails on cost and on
  criterion 4: it is a routing fact, not an identity, it already lives in
  `~/.ssh/config.d/billet.conf`, and `billet ls` already reports it. A status bar is not where
  an IP is needed.
- **Branch and dirty state is the strongest thing an operator will actually want, and it is
  the clearest non-candidate.** It changes many times an hour, so no per-connect publication
  can be honest about it. It is also the cheapest thing to compute in-session. This is not
  billet declining to provide a feature; it is the feature belonging in the theme, as
  `#()` running `git -C … rev-parse --abbrev-ref HEAD`, at the cost measured above.
- **The devcontainer service name is the genuine judgment call.** It passes criteria 1 and 2
  cleanly and is the only rejected candidate that does. It fails 4: a service name is a *repo*
  fact, and ADR-0002 already drew that exact line — `src/billet/contracts/workspace.py:1-9`
  keeps container facts out of `WorkspaceSpec` on the grounds that they "change for different
  reasons, on different cadences, authored by different people". Publishing `facts.service`
  under a `@billet_` prefix would re-cross a boundary the project drew deliberately, and it
  would do so to disambiguate multi-service stacks that the shell prompt and hostname already
  distinguish. A reasonable reader could decide this row the other way; if the multi-service
  case ever bites in practice, this is the row to revisit.

## Consequences

- The question is closed with a test rather than a list. A seventh candidate gets adjudicated
  in four lines instead of relitigated, and the answer is auditable after the fact.
- `TmuxStatusEngine.render_prelude` keeps its three-value signature, its golden-string tests
  stay exhaustive, and the prelude stays short enough to read in a `ps` line.
- `connect` keeps its zero-cloud-call property. That is now a stated invariant with a rule
  defending it, not an accident of how the feature happened to be built.
- Live state is explicitly relocated, not refused: `docs/adopting-a-repo.md` should carry the
  `#()` guidance and the measured per-tick cost alongside the ADR-0008 consuming snippet, so
  an operator who wants branch-on-the-bar knows exactly where it goes and what it costs.
- The cost is the one ADR-0008 already accepted, now applied to a wider surface: everything
  interesting an operator might want on the bar lives in a second repo with no CI and no
  tests. This ADR makes that permanent for live state specifically.
- Criterion 4 is the softest of the four — "identity" is a judgment, not a predicate — and the
  service-name row is where that softness bites. Criteria 1–3 are mechanical; 4 is where a
  future disagreement will land.

## Alternatives considered

- **Publish everything already in hand** (service, workspace folder, remote user, container
  alias, port) on the reasoning that each is one more `set` in a chain that already exists.
  Rejected: free to write is not free to own. Every published name is contract surface a
  second, untested repo may start consuming, and ADR-0008 already records that neither
  repository can validate the other. Widening the untestable half to save a few keystrokes in
  the tested half is the wrong direction.
- **Add a provider probe to `connect`** so `public_ip`, `power_state`, and `vm_size` become
  publishable. Rejected: it buys status-bar decoration with an `az` round-trip on the most
  latency-sensitive command billet has, and it does so on every attach, including the many
  attaches to an already-running session.
- **An in-container agent pushing live options** via `tmux set -g`. Verified to work — this is
  the only mechanism that would make branch/dirty publishable — and rejected on ownership:
  it requires writing into the container, and it makes billet responsible for a long-running
  process's lifecycle inside a container it otherwise only visits.
- **A generic passthrough** — `--publish k=v`, or a `[workspaces.<key>.publish]` table letting
  the operator publish arbitrary values. Rejected: it does not answer the boundary question,
  it relocates it to the operator one Workspace at a time, and every value they would
  plausibly reach for is one of the rows above — so it would mostly serve as a supported way
  to publish stale data. Worth reconsidering only for a concrete need that passes criteria 1–3
  and fails only 4.
- **Say nothing and decide case by case.** Rejected: that is the status quo that produced six
  unevaluated candidates and a namespace with no stated edge. The cheapest moment to bound a
  contract is before anything consumes it.
