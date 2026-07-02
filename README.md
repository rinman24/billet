<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="brand/logo/logo-horizontal-on-dark.png">
  <img alt="billet" src="brand/logo/logo-horizontal-on-light.png" width="300">
</picture>

### A berth for every repo.

A stateless CLI that posts any repo's devcontainer to a shared cloud **Host** — from the command line.

<!-- style=flat rounds the badge corners (berth shape); each <picture> lifts the
     label color from aubergine to violet in GitHub dark mode so the left side stays legible.
     Keep each <a><picture>…</picture></a> on ONE line: a newline between <picture> and its
     <img> renders as a leading space inside the link, which GitHub underlines (a stray line
     under each badge). The single space between the anchors is outside the links, so it just
     spaces the badges without underlining. -->
<p>
  <a href="https://pypi.org/project/billet/"><picture><source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/pypi-billet-C05CE0?style=flat&labelColor=3A2A4D&logo=pypi&logoColor=white"><img alt="PyPI" src="https://img.shields.io/badge/pypi-billet-C05CE0?style=flat&labelColor=17101F&logo=pypi&logoColor=white"></picture></a>
  <a href="#installation"><picture><source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/python-3.11%2B-9C8BB2?style=flat&labelColor=3A2A4D&logo=python&logoColor=white"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-9C8BB2?style=flat&labelColor=17101F&logo=python&logoColor=white"></picture></a>
  <a href="LICENSE"><picture><source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/license-MIT-3FD2BE?style=flat&labelColor=3A2A4D"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-3FD2BE?style=flat&labelColor=17101F"></picture></a>
</p>

<sub><a href="#installation">Install</a> &nbsp;·&nbsp; <a href="#quick-start">Quick start</a> &nbsp;·&nbsp; <a href="#commands">Commands</a> &nbsp;·&nbsp; <a href="https://rinman24.github.io/billet/brand/guidelines.html">Brand</a> &nbsp;·&nbsp; <a href="https://github.com/rinman24/billet">Source</a></sub>

</div>


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

### A Host without Workspaces (the fleet-host)

Some Hosts are managed by billet purely for their VM lifecycle and carry no Workspaces — for
example a fleet-host whose runtime is owned elsewhere. Declare such a Host with
`manages_workspaces = false`:

```toml
[hosts.fleet]
resource_group     = "GSWA-FLEET-HOST-RG"
vm_name            = "gswa-fleet-host"
location           = "westus3"
admin_user         = "azureuser"
vm_size            = "Standard_D4s_v5"
manages_workspaces = false
# ... vm_image / public_ip_sku / os_disk_gb / storage_sku as for any host
```

Its VM lifecycle works like any other Host:

```bash
billet host up --host fleet --dry-run   # adopt → pin → start → wait
billet host stop --host fleet           # deallocate
```

But it registers no `[workspaces.*]`. The workspace verbs (`add`/`start`/`stop`/`connect`/
`ssh-config`) refuse a Host with `manages_workspaces = false`, and `billet ls` flags any
Workspace wrongly placed on one as `INVALID` rather than probing it. See
[ADR-0004](docs/adr/adr-0004-host-manages-workspaces.md).

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
