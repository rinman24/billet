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

Both subsystems ship in Python. The **Host** subsystem drives the VM behind the
`HostProvider` seam (`billet host up|stop|pin-ip`), with a dry-run plan and a confirm gate on
billable cold-create. The **Workspace** subsystem clones, builds, bootstraps, and connects a
repo's devcontainer on a Host (`billet add|ls|start|stop|connect|ssh-config|rm`), reading each
repo's `.devcontainer/devcontainer.json` as a read-only data contract. The Python tool now
fully replaces the original cloud-devbox shell scripts, which have been removed. The
architecture is recorded in
[ADR-0001](docs/adr/adr-0001-closed-architecture-decomposition.md) and
[ADR-0002](docs/adr/adr-0002-workspace-subsystem.md).

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

Then run a repository's devcontainer Workspace on the Host:

```bash
billet add gswa-backend          # validate the [workspaces.<key>] block
billet start gswa-backend        # bring the Host up, then clone + compose up + bootstrap
billet ssh-config                # write ~/.ssh/config.d/billet.conf (+ one Include line)
billet connect gswa-backend      # ssh in and attach to the tmux session
billet ls                        # show each Workspace and whether it is running
billet stop gswa-backend         # stop the container (non-destructive)
```

The compose `service`, compose file(s), `workspaceFolder`, `remoteUser`, and
`postCreateCommand` are read live from each repo's `.devcontainer/devcontainer.json` — billet
does not duplicate them in `config.toml`.

### Multiple Workspaces on one Host

Several repos can share one VM. Give each a distinct `container_ssh_port`
(`billet add` validates per-host uniqueness), and have each repo's compose bind its sshd to
billet's assigned port — billet exports `BILLET_CONTAINER_SSH_PORT` before every
`docker compose`, so the repo publishes:

```yaml
services:
  <service>:
    ports:
      - "127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-2222}:22"
```

The `:-2222` default keeps an un-adopted repo working unchanged; only the second repo onward
must parameterize its port. `billet ssh-config` then renders both containers behind the one
Host (`ProxyJump`), each with its own port and a collision-free `HostKeyAlias`, and the Host
still needs a single NSG rule. See
[ADR-0003](docs/adr/adr-0003-workspace-port-binding-contract.md).

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
