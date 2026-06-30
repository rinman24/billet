# billet

A stateless, configurable manager for cloud development **Hosts** and the repos'
devcontainer **Workspaces** that run on them.

`billet` provisions and drives an Azure VM (a **Host**), then starts, stops, and connects
to one or more repositories' devcontainers (**Workspaces**) running on it. Each repository
keeps owning its own `.devcontainer/`; `billet` owns the VM lifecycle, connectivity, and a
registry-driven dispatcher. It is decomposed by volatility (Löwy closed architecture); the
swappable `HostProvider` is the load-bearing seam (Azure VM today; DevPod / Dev Box later).

Stateless by design: operator intent lives in one `config.toml`; IP address, power state,
and the host↔workspace mapping are derived live from Azure and resource tags.

## Status

The **Host** subsystem ships in Python: `billet host up|stop|pin-ip` drives the VM behind
the `HostProvider` seam, with a dry-run plan and a confirm gate on billable cold-create.
The lifted cloud-devbox shell scripts (`scripts/devbox/`) remain as the connect/Workspace
path until the Workspace subsystem (`billet add|start|connect …`) lands in later slices.
The architecture is recorded in
[ADR-0001](docs/adr/adr-0001-closed-architecture-decomposition.md).

## Install

```bash
uv tool install git+https://github.com/rinman24/billet
```

(PyPI publication is deferred; install from GitHub for now.)

## Usage

```bash
mkdir -p ~/.config/billet && cp config.example.toml ~/.config/billet/config.toml
# edit config.toml: subscription, [hosts.<key>], [workspaces.<key>]

billet host up --dry-run   # show the plan (cold-create / resume, auto-detected)
billet host up             # create or resume the VM (cold-create asks to confirm)
billet host pin-ip         # re-pin inbound SSH to your current egress IP/32
billet host stop           # deallocate the VM (stops compute billing)
```

## Ubiquitous language

- **Host** — a cloud VM that runs containers.
- **Workspace** — a repository's devcontainer running on a Host.
- **HostProvider** — the backend seam that implements Host lifecycle (Azure VM today).
- **devbox** — the informal name for the shared Host.

## Development

```bash
uv sync                 # create .venv and install dev tooling
make lint               # ruff + pyright (strict)
make imports            # import-linter layer contract
make test               # pytest
```

## License

MIT © 2026 Rich Inman, PhD
