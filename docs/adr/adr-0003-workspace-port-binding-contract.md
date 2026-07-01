# ADR-0003: The Workspace port↔container binding contract

## Status

Accepted (2026-07-01). Extends [ADR-0002](adr-0002-workspace-subsystem.md) for the
multi-workspace slice (slice 6). Governs how a repo's in-container sshd binds the loopback
port billet assigns it.

## Context

billet reaches each Workspace's container through a distinct loopback port on the shared
Host — `127.0.0.1:<port>`, behind the ssh-config `ProxyJump` (ADR-0002 §3). The port is
billet's operator intent, declared once as `WorkspaceSpec.container_ssh_port` and validated
for per-host uniqueness by `PortAllocator`.

But billet declaring the port is only half the contract. The container's **own** sshd must
actually *listen* on that loopback port, which is a `ports:` mapping in the repo's compose
file — a file billet reads but does not own (ADR-0002 §1). Today gswa's compose hardcodes
`127.0.0.1:2222:22`. That works for one Workspace; the moment a second repo lands on the
same Host it would also try to publish `2222` and collide. Something has to carry billet's
assigned port *into* the repo's compose.

## Decision

**billet passes the assigned port to compose as an environment variable,
`BILLET_CONTAINER_SSH_PORT`, and the repo's compose interpolates it with a `2222` default.**

billet exports `BILLET_CONTAINER_SSH_PORT=<container_ssh_port>` in the remote shell before
*every* `docker compose` invocation (`up`, `exec`, `stop`, `ps`) — so compose interpolation
is consistent across the lifecycle, not just at `up`. The repo's compose publishes sshd as:

```yaml
services:
  <service>:
    ports:
      - "127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-2222}:22"
```

Why this shape (volatility + DDD):

- **One source of truth for the port.** The port is billet's intent and lives once in
  `config.toml`. The repo's compose is *parameterized by* it, never a second hardcoded copy
  that can drift or collide. This mirrors ADR-0002's stance: billet reads the repo's
  container facts, and here it *provides* the one value the repo cannot know (which loopback
  port billet picked for it on this Host).
- **The contract is a single well-known name**, not a bespoke override file or a compose
  rewrite. billet stays a *reader/parameterizer* of the repo's compose, never an editor of
  it — the same ownership boundary as ADR-0002 §4 (`add` never writes `config.toml`).
- **Backward-compatible via the `:-2222` default.** A repo that has not yet adopted the
  variable (like gswa today, with a literal `2222`) is unaffected: billet exporting the
  variable is a harmless no-op for a compose that does not reference it, and a compose that
  *does* reference it falls back to `2222` when run outside billet. Adoption is therefore
  incremental and per-repo — the first repo can keep `2222`; only the second repo onward must
  parameterize. No flag day.

## Consequences

- Registering a second repo on a shared Host is a matter of config (`container_ssh_port =
  2223`) plus a one-line compose change in *that* repo — no billet change.
- `PortAllocator.assert_unique` (per-host) and `SshConfigEngine`'s per-container
  `HostKeyAlias` (already built in slice 5) complete the multi-workspace story: distinct
  ports, collision-free known-hosts entries, still a single Host `Include`/NSG rule.
- gswa keeps its literal `2222` for now; adopting `${BILLET_CONTAINER_SSH_PORT:-2222}` is a
  small, optional gswa-side change (a separate gswa PR) that only becomes necessary if gswa
  ever shares its Host with another Workspace.
- The variable is not a secret (a port number); it is safe to export over SSH and appears in
  no persisted state.

## Alternatives considered

- **billet generates a per-Workspace compose override file** (`-f base.yml -f
  billet.override.yml`) that remaps the port. Rejected for now: it makes billet a *writer* of
  compose artifacts on the Host (state to manage, clean up, and reconcile), heavier than a
  single env var, for no benefit at slice-6 scale. The env-var contract can be swapped for an
  override generator later behind the same `ComposeContainerAccess` seam if a repo ever needs
  billet to remap more than the port.
- **Each repo hardcodes a distinct port.** Rejected: duplicates billet's intent into every
  repo, cannot be reassigned by billet, and reintroduces exactly the collision risk this ADR
  removes.
- **billet SSHes in and edits the repo's compose.** Rejected outright: violates the
  reader-not-editor boundary (ADR-0002) and would fight the repo's own version control.
