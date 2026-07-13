# ADR-0005: billet owns instance lifecycle; it adopts durable infrastructure

## Status

Accepted (2026-07-13). Extends [ADR-0001](adr-0001-closed-architecture-decomposition.md).
Codifies the boundary already implied by PR #35 (optional provisioning keys, lazily
validated at cold provision).

## Context

`billet host up` can cold-provision a Host: `AzureVmProvider.create` runs
`az group create` + `az vm create` and stops. The obvious challenge — raised as a
best-practices question — is whether a dev-environment manager should create cloud
resources at all, or whether creation belongs to declarative IaC (Bicep / Terraform),
leaving billet only `start` / `deallocate` / connectivity.

The IaC argument is real: declarative provisioning gets plan/review, drift detection,
and least-privilege RBAC for the day-2 tool. But the framing that actually decides the
question is not *creation vs. lifecycle* — it is **ephemeral instances vs. durable
infrastructure**:

- Dev Hosts are cattle. Their entire desired state is a few lines of registry TOML, and
  one-command `up` from a cold registry entry is most of billet's value. Every comparable
  tool (Coder, DevPod, Codespaces) owns instance creation for exactly this reason.
- Durable platform resources — VNets, NSG policy, identity, role assignments, shared
  images — are where IaC's ceremony earns its keep: long-lived, shared, security-sensitive,
  and in need of drift management.
- The `HostProvider` Protocol names DevPod and Dev Box as future backends, and both own
  provisioning natively. Removing `create` from the seam would narrow it below what its
  future implementations provide.
- Statelessness cuts *for* in-tool creation: the registry is the single source of desired
  state. A Terraform split would add a `.tfstate` that can drift from the registry, a
  second toolchain, and a coordination problem — while billet would still need the adopt
  path anyway.

## Decision

**billet owns the full lifecycle of instances described in its registry** — cold
provision (`create`), `start`, `deallocate`, and connectivity. **billet deliberately does
not own durable infrastructure** — networks, NSG policy, identity, role assignments,
shared platform resources. Those it only *adopts*: an externally provisioned Host is a
first-class citizen, registered with placement keys alone.

Three rules keep the boundary honest:

1. **Creation stays thin.** `create` may make the resource group and the tagged VM —
   nothing else. The day a provider's `create` wants an NSG rule, a VNet, or a role
   assignment, the answer is "adopt a landing zone built elsewhere," not "grow the
   provider."
2. **Provisioning is optional per Host, validated lazily** (PR #35). A registry entry
   with placement keys only is complete; missing provisioning keys fail closed at the
   moment a CREATE step is actually planned, with an error naming the fix. This is also
   the least-privilege story: a principal without create permissions simply never
   registers provisioning keys, and billet degrades to a start/stop/connect tool.
3. **Reconciliation is idempotent regardless of provenance.** `ensure_tags` (and any
   future convergence step) runs on every `up` whether billet or an operator created the
   VM — adopted and billet-created Hosts are indistinguishable after registration.

## Consequences

- The `HostProvider` Protocol keeps `create`; future backends (DevPod, Dev Box) implement
  it natively rather than working around a deliberately narrowed seam.
- Least privilege is achieved per-Host by *omitting provisioning keys*, not by forking the
  tool into "provisioner" and "connector" halves.
- Blast radius of imperative creation stays bounded: two `az` calls into one tagged
  resource group. Partial-failure cleanup is `az group delete` — no orphan-hunting.
- Teams with an IaC-managed landing zone lose nothing: provision the VM in Terraform,
  register it with placement keys, and billet never issues a billable create.
- Scope creep in `create` is now a review-time policy violation with an ADR to cite, not
  a judgment call.

## Alternatives considered

- **Remove `create`; require IaC for all provisioning.** Rejected: it deletes the
  one-command cold-start that is the product's core loop, narrows the `HostProvider` seam
  below its future implementations, and trades a small imperative surface (two `az`
  calls) for a state file, a second toolchain, and registry/tfstate drift.
- **Embed IaC inside billet** (drive Bicep/Terraform from `create`). Rejected: billet
  inherits IaC's operational weight (state storage, plan/apply UX) without its review
  workflow, and the provider stops being a thin ResourceAccess over `az`.
- **Grow `create` toward a full landing zone** (VNet, NSG, identity). Rejected: that is
  durable, security-sensitive, shared infrastructure — exactly the territory where
  declarative IaC's drift detection and review process win, and where an imperative
  stateless CLI is the wrong tool.
