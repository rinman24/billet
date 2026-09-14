# ADR-0015: `billet doctor` — provider verification over the registry

## Status

Proposed (2026-09-14); **implementation deferred to a later cycle.** Accepted as the decision
record for the verification half of [ADR-0012](adr-0012-the-berth.md) and
[ADR-0013](adr-0013-mountpoint-ownership-repaired-at-mount-time.md). Amends
[ADR-0002](adr-0002-workspace-subsystem.md) §1 to permit billet to *read* the compose files it
already resolves by name, for `doctor` only; the amendment takes effect when the `doctor` verb
lands, not before ([ADR-0014](adr-0014-definition-versus-state.md) item 4). Nothing in billet
0.4.0 implements this ADR.

## Context

The split invariant ADR-0013 describes — one half authored in an image repository, the other in
a consumer's compose file — was not un-seeable, it was un-owned. billet's registry already
enumerates every Workspace and can reach every Host; it is the only artifact in the system that
can see both halves. Yet billet's template tests see only billet's own repository
(`tests/unit/templates/`, `_REPO_ROOT`), the shared image's `verify-image.sh` tests tool versions
and nothing about the Berth, and the consumer-side `check-image-pin.sh` was, when checked, present
in one consumer and run by no workflow.

Three concrete questions had no computable answer in 2026-09:

1. *Is this consumer on the current Berth?* Answered by a human reading a prose log.
2. *Can the `DEVBOX_*` → `BILLET_*` deprecation window close?* PR #70 closed it on a human
   recollection; Compose interpolation (`${A:-${B:-stub}}`) is silent by construction and could
   not warn.
3. *Does any Workspace mount a Locker the login user cannot write?* Answered by the tool failing
   at first use.

ADR-0012 gives question 1 a stamp and a hash definition; ADR-0013 makes question 3 hold by
construction. What remains is a component that computes the answers over the fleet and reports
them. It must not be `start`: `start`'s job is to bring a Workspace up, and ADR-0013 keeps every
start-time check out of 0.4.0 (operator decision Q26) because a check that renders nothing yet
is a capability nothing exercises.

## Decision

**billet gains a `doctor` verb that renders a report over the registry and never mutates. It
computes Berth drift by directive hash, scans resolved compose files for deprecated names and
undeclared volumes, and, per Host, reads back Locker ownership inside running containers.
Warn, never fail. No Dockerfile parsing.**

1. **Shape.** `billet doctor [--host <name>] [--workspace <name>]`. Without `--host` it reads
   only local files: the registry and each Workspace's checkout as billet already resolves it
   (`repo_dir`, `dockerComposeFile` list). With `--host` it additionally runs read-only commands
   over the existing single SSH session per Host. Output is one section per Workspace with
   `ok` / `warn` lines; exit status is 0 unless `doctor` itself failed to run. `doctor` never
   changes `start`'s plan and never adds work to `connect` (ADR-0009 invariant).

2. **What it checks, and the vocabulary each check uses.**

   | Check | Source | Report |
   |---|---|---|
   | Berth version stamp vs the version billet ships | `.devcontainer/berth.version` in the checkout; `templates/workspace/berth.version` in billet | `behind by N` — then, per Berth file, whether its **directive hash** differs from billet's copy (ADR-0012 item 5); byte differences that agree in directives are `ok` |
   | Deprecated variable names interpolated in compose | the compose files billet resolves onto `DevcontainerFacts.compose_files`, now opened | `warn: DEVBOX_AUTHORIZED_KEYS interpolated at <file>:<line>` — the Kubernetes-style deprecation `Warning` the 2026-09 window shipped without |
   | Named volume mounted under `$HOME` with no `volumes:` declaration | `docker compose config` in the Host shell (behind `--host`) | `warn: undeclared volume <name>` — turns a VM-only `up` failure into a named preflight message |
   | Locker ownership | `stat` of each mount target inside the running container (behind `--host`) | `warn: <path> owned by uid <n>, not writable by dev` |
   | Berth readiness | presence of a marker the Berth 1+ entrypoint may write after its repair block (a later Berth revision) | informational |

   Every scanner carries a **vacuity guard**: a regex or parser that matched nothing across the
   whole registry fails the scanner's own test, so a rotted pattern cannot pass forever
   (`test_every_billet_owned_variable_uses_the_billet_prefix` in `test_env_var_naming.py` is
   the precedent).

3. **What it may read that billet could not before.** The consumer's compose files, opened as
   text, for the two scans above. This is the ADR-0002 §1 amendment. It is granted to `doctor`
   only: `start` and `connect` continue to read five fields of one file. It is a read of a
   *definition* (ADR-0014 item 1) and stays a read; `doctor` writes nothing anywhere.

4. **What it must not do.** Parse a Dockerfile (a build recipe is not a contract surface and the
   image publishes what it needs to as labels or behavior). Validate recipe pairing (ADR-0014
   item 5). Fail `start`. Repair anything: ADR-0013's entrypoint repairs; `doctor` reports what
   the repair could not fix (populated root-owned targets) and what it did fix (from the container
   log).

5. **Where it lives in the architecture.** Unchanged layers, three additions at existing seams:

   ```
   billet.cli            + `doctor` verb (renders; never mutates)
   billet.workspace      + BerthPolicy engine — pure: directive hashes, version compare,
                           deprecated-name scan; structurally like PortAllocator/HostPlacementPolicy
   billet.access         ContainerAccess + read_mount_report() (owner/mode per mount, in-container)
   billet.contracts      + BerthStatus, MountReport (frozen dataclasses)
   ```

   `read_image_lockers()` from the research is **not** added: after ADR-0013 the shared image
   publishes no Locker set, so there is no label to read. A Berth-version label is a matter for
   the ADR that bakes the Berth into the image (ADR-0016, future).

## Consequences

- Questions 1–3 above become computable over the registry, which is the only place they can be.
  PR #70's "can the window close?" would have been one `doctor` run.
- billet learns to open compose files. That is a real widening of ADR-0002 §1 and is why this is
  an ADR and not a feature ticket. It is granted deliberately, to one verb, read-only.
- The stamp ADR-0012 ships in 0.4.0 is inert until this lands. That is intended: the alternative,
  an integer comparison in `start`, produces the byte-vs-directive false positive.
- The Berth readiness marker, deferred from ADR-0013, has a home: a later Berth revision writes
  it after the repair block, `doctor` reads it, and `start` still never waits on it.
- `connect` is untouched. Any change that adds work to `connect` is out of scope for this ADR
  by construction.

## Alternatives considered

- **Check in `start` instead** (the research's "CONFORM step" appended to the piped script).
  Deferred, not rejected: the wire cost is zero but the read verbs and the report renderer would
  ship with nothing to render them until `doctor` exists. When `doctor` lands, `start` may reuse
  `read_mount_report()` to print the same warnings; that is a one-line follow-up, not a decision.
- **Consumer-side CI only** (`check-image-pin.sh` extended with a Berth stamp check). Kept as a
  complement, added to genshift-brand in this cycle, but it sees one consumer at a time and
  cannot see inside a running container.
- **Image-side CI** (`verify-image.sh`). Extended in this cycle for Berth conformance of the
  image itself, but it cannot see a consumer.
- **A Pact-style contract test between billet and each consumer.** The right idea at the wrong
  scale for four consumers and one operator; the directive hash is the same check with one file.
