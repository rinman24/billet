# billet

A stateless, configurable manager for cloud development **Hosts** (Azure VMs) and the
repos' devcontainer **Workspaces** that run on them.

## Concepts

- **Host** — a cloud VM that runs containers.
- **Workspace** — a repository's devcontainer running on a Host.
- **HostProvider** — the backend seam that implements Host lifecycle (Azure VM today;
  DevPod / Dev Box later).
- **Berth** — the Workspace runtime contract billet publishes under `templates/workspace/`,
  versioned independently of billet by `berth.version`
  ([ADR-0012](adr/adr-0012-the-berth.md)).
- **Locker** — one named compose volume persisting one tool's state under the login user's
  home, declared only in the consumer's compose file.

The full glossary, the map of the five contexts billet collaborates with, the live consumer
inventory and the open questions are in the [context map](CONTEXT-MAP.md).

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
contract in [ADR-0003](adr/adr-0003-workspace-port-binding-contract.md). What billet does
and does not create in the cloud — it owns registry-described *instances* (cold provision,
start, deallocate, connectivity) and only *adopts* durable infrastructure like networks and
identity — is recorded in [ADR-0005](adr/adr-0005-instance-lifecycle-ownership.md).

The runtime contract a Workspace implements for billet to reach it is the **Berth**, named and
versioned in [ADR-0012](adr/adr-0012-the-berth.md). Its entrypoint repairs the ownership of
named-volume mount targets at container start instead of relying on the image to pre-create
them ([ADR-0013](adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md)). What billet
may write into a container it started, and what it never writes — anything a build reads — is
the definition/state boundary of [ADR-0014](adr/adr-0014-definition-versus-state.md). The
`doctor` verb that will compute Berth drift and Locker ownership over the registry is decided,
and deferred, in [ADR-0015](adr/adr-0015-billet-doctor.md).

## Status

Both subsystems run in Python. The Host subsystem (`billet host up|stop|pin-ip|specs`) drives the
VM; the Workspace subsystem (`billet add|ls|start|stop|connect|ssh-config|rm`) clones,
builds, bootstraps, and connects a repo's devcontainer on a Host, reading each repo's
`.devcontainer/devcontainer.json` as a read-only data contract. The Python tool now fully
replaces the original shell scripts lifted from gswa-backend, which have been removed.

Because those facts are read live, `billet ssh-config` renders only the Workspaces already
cloned on a running Host; any other is skipped with a warning instead of failing the whole
run, so one un-started Workspace never withdraws the other aliases
([ADR-0010](adr/adr-0010-ssh-config-partial-success.md)).
