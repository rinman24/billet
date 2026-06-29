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

Early scaffold. This slice lifts the existing cloud-devbox shell scripts verbatim
(`scripts/devbox/`) and stands up the package skeleton, lint/type/test gates, and CI. The
Python `billet host …` / `billet add|start|connect …` commands replace the lifted bash in
later slices.

## Install

```bash
uv tool install git+https://github.com/rinman24/billet
```

(PyPI publication is deferred; install from GitHub for now.)

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
