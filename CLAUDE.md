# billet — Claude Code rules manifest

## Mission

`billet` is a stateless, configurable manager for cloud development **Hosts** (Azure VMs)
and the repos' devcontainer **Workspaces** that run on them. It owns VM lifecycle,
connectivity (SSH config generation), and a registry-driven dispatcher; each consumed
repository owns its own `.devcontainer/`, which `billet` reads as a data contract.

## Architecture (Löwy closed architecture, decomposed by volatility)

Higher layers may import lower ones; never the reverse. Enforced by import-linter
(`make imports`).

- `billet.cli` — Typer client / composition root (top)
- `billet.workspace` — Workspace subsystem (`contracts/`, `engine/`, `manager/`)
- `billet.host` — Host subsystem (`contracts/`, `manager/`)
- `billet.access` — ResourceAccess (Azure VM provider, registry, ssh-config, container, source)
- `billet.infrastructure` — side-effecting primitives (`az`, `ssh`, `process`)
- `billet.shared` — cross-cutting utilities (`paths`, `logging`, `errors`) (bottom)

The swappable `HostProvider` Protocol is the one seam that earns its abstraction
(Azure VM today; DevPod / Dev Box later).

Ubiquitous language: **Host** (a cloud VM), **Workspace** (a repo's devcontainer on a Host),
**HostProvider** (the backend seam), **devbox** (informal name for the shared Host).

## Ownership boundary (ADR-0005)

billet owns the lifecycle of **instances described in its registry** (cold provision →
start → deallocate, plus connectivity). It deliberately does **not** own durable
infrastructure — networks, NSG policy, identity, shared platform resources — those it only
*adopts*. Provider `create` stays thin (resource group + tagged VM, nothing else);
provisioning keys are optional per Host and validated lazily at cold provision. PRs that
grow `create` toward landing-zone resources violate this boundary.

## Stack & conventions

- Python 3.11; `uv` for dependency management (PEP 621 + `uv.lock`); `hatchling` build backend.
- Strong typing everywhere; `pyright` strict mode, **0 errors** on every file touched.
- `ruff` for lint + format (numpy docstring convention).
- Modern syntax: `X | None`, `list[X]`, `dict[K, V]`; explicit `-> None`.
- Tests with `pytest`; pure engines tested directly, managers tested against typed
  Protocol fakes, access tested by mocking the process runner (not the binary).
- Docs are MkDocs under `docs/`.

## Validation before hand-off

- `make lint` (ruff + pyright strict) — 0 errors
- `make imports` (import-linter layer contract)
- `make test`

## Git

- `main` is PR-based. Conventional-commit subjects (`feat:`, `fix:`, `docs:`, `chore:`…).
- Do **not** add Claude/Anthropic authorship or attribution lines to commits, PRs, or tags.
