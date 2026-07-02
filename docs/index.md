# billet

A stateless, configurable manager for cloud development **Hosts** (Azure VMs) and the
repos' devcontainer **Workspaces** that run on them.

## Concepts

- **Host** — a cloud VM that runs containers.
- **Workspace** — a repository's devcontainer running on a Host.
- **HostProvider** — the backend seam that implements Host lifecycle (Azure VM today;
  DevPod / Dev Box later).
- **devbox** — the informal name for the shared Host.

## Architecture

`billet` is decomposed by volatility (Löwy closed architecture). Higher layers may import
lower ones; never the reverse:

| Layer | Role |
| --- | --- |
| `billet.cli` | Typer client / composition root |
| `billet.workspace` | Workspace subsystem (contracts, engine, manager) |
| `billet.host` | Host subsystem (manager) |
| `billet.access` | ResourceAccess (Azure VM provider, registry, ssh-config, container, source) |
| `billet.contracts` | data contracts + service Protocols (the `HostProvider` seam) |
| `billet.infrastructure` | side-effecting primitives (`az`, `ssh`, `process`) |
| `billet.shared` | cross-cutting utilities |

The rationale for this decomposition — the volatility axes, the dedicated `contracts`
layer, the `HostProvider` seam, and dry-run/plan layering — is recorded in
[ADR-0001](adr/adr-0001-closed-architecture-decomposition.md). The Workspace subsystem and
its `devcontainer.json`-as-data-contract boundary are recorded in
[ADR-0002](adr/adr-0002-workspace-subsystem.md); the multi-workspace port↔container binding
contract in [ADR-0003](adr/adr-0003-workspace-port-binding-contract.md).

## Status

Both subsystems run in Python. The Host subsystem (`billet host up|stop|pin-ip`) drives the
VM; the Workspace subsystem (`billet add|ls|start|stop|connect|ssh-config|rm`) clones,
builds, bootstraps, and connects a repo's devcontainer on a Host, reading each repo's
`.devcontainer/devcontainer.json` as a read-only data contract. The Python tool now fully
replaces the original cloud-devbox shell scripts, which have been removed.
