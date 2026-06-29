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
| `billet.host` | Host subsystem (contracts, manager) |
| `billet.access` | ResourceAccess (Azure VM provider, registry, ssh-config, container, source) |
| `billet.infrastructure` | side-effecting primitives (`az`, `ssh`, `process`) |
| `billet.shared` | cross-cutting utilities |

## Status

Early scaffold. The current state lifts the cloud-devbox shell scripts verbatim into
`scripts/devbox/`; the Python command surface replaces them in later slices.
