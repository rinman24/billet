# ADR-0001: Closed-architecture decomposition of billet

## Status

Accepted (2026-06-30). Establishes the architecture the Host subsystem (slice 4) and every
later slice build on.

## Context

billet lifts the cloud-devbox orchestration out of `gswa-backend` into a standalone tool
that provisions/manages Azure **Hosts** and starts/stops/connects multiple repos'
devcontainer **Workspaces** on them. The lifted shell (`scripts/devbox/*.sh`) conflates two
concerns that change for different reasons:

- **Host** — the cloud VM lifecycle (create / start / deallocate / pin the inbound SSH rule
  / install the base supply chain). This is where the *backend* volatility lives: an Azure
  VM today; DevPod or Microsoft Dev Box later.
- **Workspace** — a repo's devcontainer on a Host (clone, compose up, bootstrap, connect,
  ssh-config). This changes per-repo and per-connectivity-decision, not per-cloud-backend.

We decompose by **volatility** (Juval Löwy's IDesign method / closed architecture) rather
than by function or by the lifted scripts' accidental structure. The goal is a tool that
scales to new backends and many workspaces without reshaping the core.

## Decision

### 1. Layered closed architecture, calls go down only

Components are ordered by volatility; a higher layer may call any lower layer, never the
reverse. The import graph is enforced in CI by `import-linter` (a `layers` contract):

```
billet.cli            ── Typer client / composition root (wires concretes to Protocols)
billet.workspace      ── Workspace subsystem (contracts/engine/manager)   [slice 5+]
billet.host           ── Host subsystem (manager)
billet.access         ── ResourceAccess: AzureVmHostProvider, RegistryAccess, …
billet.contracts      ── data contracts + service Protocols (the seam)     ← new this slice
billet.infrastructure ── side-effecting primitives (az, ssh, process)
billet.shared         ── cross-cutting utilities (paths, logging, errors)
```

`workspace` sits above `host` because `WorkspaceManager.start` calls `HostManager.up`
(slice 5). `host` sits above `access` because the manager orchestrates ResourceAccess.

### 2. A dedicated `billet.contracts` layer (the load-bearing decision)

The cross-layer data contracts — `HostSpec`, `WorkspaceSpec`, `HostStatus`,
`HostPowerState` — and the **service Protocols** (`HostProvider`) live in their own
`billet.contracts` layer, **not** under `host/contracts/` as the working plan first
sketched.

Why: `access` sits *below* `host` in the call graph, yet `AzureVmHostProvider` (in
`access/`) must produce a `HostStatus` and accept a `HostSpec`, and the `HostManager` (in
`host/`) must depend on the `HostProvider` interface. If those types lived in `host/`, the
access-layer implementation would import *upward* into `host/`, inverting the graph and
breaking the layer contract.

This is precisely Löwy's prescription: service **contracts (interfaces + the data
contracts they exchange) belong in a separate Contracts component** that everything
references and that itself references nothing domain-specific. It is Dependency Inversion
realized as a module — both the high-level policy (`HostManager`) and the low-level detail
(`AzureVmHostProvider`) depend on the abstraction; the abstraction depends on nothing but
`shared`. `contracts` is placed directly above `infrastructure` so that `infrastructure`
stays pure (it cannot import domain contracts — that would be an upward import), while
`access`, `host`, `workspace`, and `cli` may all depend on `contracts` downward.

Contracts are pure data + interface declarations (no behavior, no I/O), so modelling them
as the bottom-most *callable-free* layer is faithful: a layer that everything references
and that calls nothing.

### 3. `HostProvider` is the volatility seam; the backend is fully encapsulated

`HostProvider` (a `typing.Protocol` in `contracts/`) is the single seam that absorbs
cloud-backend volatility. Its surface is the minimal set of operations a backend must
provide to make a Host usable:

```python
class HostProvider(Protocol):
    def status(self, spec: HostSpec) -> HostStatus: ...          # power state + public IP
    def create(self, spec: HostSpec) -> None: ...                # cold provision (BILLABLE)
    def start(self, spec: HostSpec) -> None: ...                 # resume a deallocated host
    def deallocate(self, spec: HostSpec) -> None: ...            # stop compute billing
    def pin_inbound(self, spec: HostSpec) -> str: ...            # re-pin SSH NSG to operator /32
    def wait_until_reachable(self, spec: HostSpec) -> None: ...  # block until SSH answers
    def ensure_supply_chain(self, spec: HostSpec) -> None: ...   # install Docker (idempotent)
```

`create` and `start` are deliberately **separate** (not a single `ensure_up`) so the
*billable* cold-create is an explicit, gateable step in the plan. `wait_until_reachable`
and `ensure_supply_chain` are on the provider — not called directly by the manager over
`infrastructure.ssh` — because *what it takes to make a host usable is itself
backend-specific*: an Azure VM needs Docker apt-installed over SSH; a Dev Box ships with it.
The manager stays backend-agnostic; a future `DevPodHostProvider` / `DevBoxHostProvider` is
a new adapter in `access/` implementing the same Protocol, with **zero** changes to `host/`
or `contracts/`.

`AzureVmHostProvider` (in `access/host/`) implements the Protocol over
`infrastructure/{az,ssh,process}.py`. The composition root (`cli`) constructs the concrete
provider and injects it into `HostManager`; the manager only ever sees the Protocol.

### 4. dry-run / confirm live at the client/plan layer

The manager builds a **Plan** (an ordered, typed list of `PlanStep`s) from the `HostSpec`
and the live `HostStatus`; the client (`cli`) either *renders* it (`--dry-run`) or
*executes* it. `access` stays purely side-effecting — it never decides whether to run.

- A read of current status (a non-mutating `az` query) happens during planning even under
  `--dry-run`, so the rendered plan is accurate (mirrors the lifted bash, which queries VM
  state under `--dry-run` but gates mutations).
- The **billable cold-create gate** is a confirm prompt in `cli`, fired when the plan
  contains a `CREATE` step, bypassable with `--yes`, and skipped under `--dry-run` (nothing
  executes). This keeps the cost gate where the human is.

### 5. Stateless; operator intent in one `config.toml`; tags as truth

There is no local state file. Operator intent lives in a single `config.toml`
(`~/.config/billet/config.toml` by default; `--config` / `BILLET_CONFIG` override).
`RegistryAccess` is the sole reader of that file and parses it into `HostSpec` /
`WorkspaceSpec`. Everything else — power state, public IP, host↔workspace mapping — is
derived live from Azure and **resource tags**. On `create`, the VM is stamped
`managed-by=billet` and `billet-host=<host-key>` so later discovery (slice 5+) reads truth
from Azure rather than a cache that can drift.

### 6. `devcontainer.json` is a read-only data contract, not a lifecycle engine

Each repo keeps owning its `.devcontainer/`. The Workspace subsystem (slice 5) **reads**
`devcontainer.json` (service, compose file, workspace folder, postCreate) as facts and
drives compose + postCreate over SSH itself. We do **not** adopt `@devcontainers/cli` as
the lifecycle engine — billet owns the VM lifecycle and connectivity; the repo owns its
container definition; `devcontainer.json` is the contract between them.

## Consequences

- The `HostProvider` Protocol is testable with a single in-memory fake typed *as the
  Protocol*; pyright-strict verifies the concrete `AzureVmHostProvider` conforms
  structurally. Managers are tested against the fake; access is tested by spying on the
  exact `az`/`ssh` argv.
- Adding a cloud backend is additive: one new adapter, no core change.
- The billable cold-create can never run without passing through the `cli` confirm gate;
  the inbound SSH rule is always pinned to a single `/32` (never `0.0.0.0/0`) — both are
  enforced by security-analog unit tests.
- Slight indirection cost: contributors must learn that contracts live in `contracts/`, not
  beside the subsystem that "owns" them. The CI layer contract makes violations loud.
- This ADR refines the working plan's `host/contracts/` placement and its single `ensure_up`
  provider verb; `docs/contributing/` and the plan should be read through this ADR.

## Alternatives considered

- **Contracts under `host/contracts/` (per the initial plan), with `import-linter`
  exceptions for `access → host`.** Rejected: normalizes upward imports and punches holes
  in the layer contract; the inversion would only spread as more access adapters land.
- **Protocols in `host/`, DTOs in `shared/` (structural typing lets `access` skip importing
  the Protocol).** Workable but splits the contract across two homes and overloads `shared`
  (paths/logging/errors) with domain data; less discoverable than one named layer.
- **A single `ensure_up` provider method.** Rejected: it hides the cold/resume distinction
  and the billable boundary from the manager, defeating the plan-layer cost gate.
- **`@devcontainers/cli` as the container lifecycle engine.** Rejected: it would own a
  concern (per-repo container lifecycle) that belongs to each repo, and pull a Node toolchain
  into a Python tool whose job is the *host* and *connectivity*.
- **A local state/cache file for host↔workspace mapping and IPs.** Rejected: drifts from
  reality; Azure + tags are the single source of truth.
