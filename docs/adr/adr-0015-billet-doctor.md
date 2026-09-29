# ADR-0015: `billet doctor` — provider verification over the registry

## Status

Accepted (2026-09-29). Implemented for the Berth-file checks: `billet doctor` compares each
Workspace's copied Berth files with the Berth the installed billet ships. Implemented for the
runtime report (2026-09-29): for each running Workspace, `doctor` reports the Berth the
container started with against the checkout's stamp, and the Locker-ownership repairs and
warnings, all from the entrypoint's own log. The compose-file read below remains **proposed and
not in effect**: no check in this cycle opens a compose file.

Proposed 2026-09-14 as the decision record for the verification half of
[ADR-0012](adr-0012-the-berth.md) and
[ADR-0013](adr-0013-mountpoint-ownership-repaired-at-mount-time.md). Amends
[ADR-0002](adr-0002-workspace-subsystem.md) §1 so `doctor` alone may *read* the Berth files a
consumer copied (in effect from this release) and, when a check needs them, the compose files
billet already resolves by name (proposed, [ADR-0014](adr-0014-definition-versus-state.md)
item 4).

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
computes Berth drift by directive hash against the Berth the installed billet ships and reports
each running Workspace's runtime state and Locker ownership from the entrypoint's own log. Warn, never fail. No Dockerfile parsing.**

1. **Shape** (rewritten 2026-09-29). `billet doctor [--host <name>] [--workspace <name>]`. Every
   check goes through the Host: `repo_dir` is a Host path, relative to the admin user's home,
   and billet has no Mac-side checkout path anywhere, so the file Docker reads is the file
   `doctor` reads. `--host` and `--workspace` are filters. Each Host is probed with **one
   sectioned script over one SSH session** (`bash -se`, the `host specs` precedent), holding a
   section per Workspace. The script is sent in two parts over that one session. The *reads*
   run only `git -C <repo_dir> rev-parse --short HEAD` and `cat`: of the copied Berth files, and
   of `devcontainer.json`. The *runtime* part is then built by billet from that
   `devcontainer.json`, with the parser `start` uses (*amended 2026-09-29*). Per Workspace, under
   the same compose prelude as `start` (`cd <repo_dir>`, billet's exports), it runs
   `docker compose -f … ps --status running -q <service>` and then
   `docker logs <id> 2>&1 | grep '^dev-entrypoint: '`. The lookup is scoped by service name,
   so Workspaces that share a compose project name on one Host stay apart. A Workspace with no
   running container reports `skipped: not running`. `doctor` never execs into a container and
   never runs a compose verb that changes state. An
   unreachable Host is reported `skipped: host <name> unreachable`, following `billet ls`, and is
   never started or allocated: `doctor` reaches a Host through its ssh-config alias and makes no
   `az` call. It reads the checkout as it is and never fetches; each Workspace's section shows
   the checkout's short HEAD, so a checkout lagging its remote is visible. Output is one section
   per Workspace with `ok` / `warn` lines. The exit status is 0 whatever the report says, and
   non-zero only when `doctor` itself could not run (a config error, or an install that carries
   no packaged Berth). `doctor` never changes `start`'s plan and never adds work to `connect`
   (ADR-0009 invariant).

2. **What it checks, and the vocabulary each check uses.**

   | Check | Source | Report |
   |---|---|---|
   | Berth version stamp and Berth-file drift | `.devcontainer/` in the Host checkout; billet's side is the installed package's own copy of `templates/workspace/` (force-included into the wheel, read through `importlib.resources`, never a repo path) | the stamp: `ok`, or a `warn` reading `behind by N`, `ahead (upgrade billet)` for a consumer newer than a stale install, or `unknown` when `berth.version` is missing. Then per copied file (`dev-entrypoint.sh`, `sshd.conf`, `authorized_keys-stub`): `ok`; `warn: <file> missing` when the file is absent; or `warn: <file> directive drift (N lines)` followed by a unified diff of the *normalized* lines, capped at 20 lines with `… (M more)`. The **directive hash** is ADR-0012 item 5 with its 2026-09-29 clarification: fold continuations, strip each line, drop blank lines, drop lines starting `#`. The two merged snippets (`Dockerfile.snippet`, `docker-compose.snippet.yml`) are not checked, and the report says so in one line: a snippet-subset check would false-positive on everything ADR-0003 grandfathered |
   | Runtime and Locker ownership (in effect 2026-09-29) | the entrypoint's own log (`docker logs` of the service's running container): its `berth=N` line and the ADR-0013 repair lines. Only the current run counts: a restarted container keeps its log, so the report reads from the last `berth=` line on. `doctor` never execs into a container | the running Berth against the checkout's stamp: `ok: running berth=N`, or a `warn` reading `running berth=N, checkout stamp M` (also for `berth=unknown` or a missing stamp) or `running berth not logged`. Each `repaired` line: `ok (repaired at start): <path> (was <owner>:<group> <mode>)`. Each `warning:` or `WARNING:` line: `warn:` and the entrypoint's own text, such as `warn: <path> owned by uid <n>; not repaired`. Other entrypoint lines (`created …`, `skipping …`) are informational and not printed. A stopped container: `skipped: not running`. *Accepted limitation:* ownership that changes after start is not seen; nothing in the fleet does that |

   The report header names the installed billet's version and the Berth version it ships.

   **Deferred (2026-09-29): the two compose-reading scans leave this cycle,** to be reinstated
   when they have something to find:

   - *Deprecated variable names interpolated in compose* (`warn: DEVBOX_AUTHORIZED_KEYS
     interpolated at <file>:<line>`). No `DEVBOX_*` name is interpolated in any consumer's
     `main`, so the scan has nothing to find. Reinstate it at the next variable deprecation.
   - *Named volume mounted under `$HOME` with no `volumes:` declaration* (a `docker compose
     config` preflight). It would only move Compose's own immediate, named error earlier, and it
     needs `docker compose config`. Reinstate it if an undeclared volume ever reaches a `start`.

   The Berth-readiness row is removed (2026-09-29): the readiness marker is dropped (see
   Consequences).

   Every scanner carries a **vacuity guard**: a regex or parser that matched nothing across the
   whole registry fails the scanner's own test, so a rotted pattern cannot pass forever
   (`test_every_billet_owned_variable_uses_the_billet_prefix` in `test_env_var_naming.py` is
   the precedent).

3. **What it may read that billet could not before** (amended 2026-09-29). The four Berth files
   a consumer copied whole into `.devcontainer/` (`dev-entrypoint.sh`, `sshd.conf`,
   `authorized_keys-stub`, `berth.version`), read with `cat` from the Host checkout. **This
   grant is in effect.** Opening the consumer's compose files as text stays **proposed, not in
   effect**: no check in this cycle opens one, and a grant nothing exercises reintroduces the
   drift between ADR text and code (ADR-0014 item 4). The runtime report does not change this.
   It reads `devcontainer.json`, the file `start` and `connect` already read, and passes the
   compose files it names to `docker compose ps` *by name*, as `start` passes them to
   `docker compose up`. Compose reads those files; `doctor` does not. Both are the ADR-0002 §1 amendment, and
   both are granted to `doctor` only: `start` and `connect` continue to read five fields of one
   file. They are reads of a *definition* (ADR-0014 item 1) and stay reads; `doctor` writes
   nothing anywhere.

4. **What it must not do.** Parse a Dockerfile (a build recipe is not a contract surface and the
   image publishes what it needs to as labels or behavior). Validate recipe pairing (ADR-0014
   item 5). Fail `start`. Exec into a container (added 2026-09-29): what the container knows,
   `doctor` reads from its log. Repair anything: ADR-0013's entrypoint repairs; `doctor` reports what
   the repair could not fix (populated root-owned targets) and what it did fix (from the container
   log).

5. **Where it lives in the architecture** (amended 2026-09-29). Unchanged layers, additions at
   existing seams:

   ```
   billet.cli            + `doctor` verb and its renderer in _ui.py (renders; never mutates)
   billet.workspace      + berth_policy engine — pure: normalize, directive_hash, compare_stamp,
                           compare_file, the 20-line diff cap; imports only contracts
                         + runtime_policy engine — pure: entrypoint log lines to RuntimeReport
                           (running Berth, repairs, warnings), and the running-versus-checkout
                           Berth compare; imports only contracts
                         + WorkspaceManager.doctor(): groups Workspaces by Host, one probe each
   billet.access         + SshDoctorAccess — the DoctorAccess seam: one sectioned probe per Host,
                           reads then runtime over one SSH session
                         + packaged_berth — the shipped Berth, through importlib.resources
   billet.contracts      + DoctorAccess (Protocol); BerthStatus, BerthFileStatus, StampStatus,
                           RuntimeReport, PackagedBerth, WorkspaceBerthRead,
                           WorkspaceRuntimeRead, WorkspaceProbe, DoctorReport (frozen dataclasses)
   billet.infrastructure + ConversationRunner — one process whose stdin script is written in two
                           parts, the second computed from the first part's output
   ```

   `DoctorAccess` is its own seam rather than a method on `ContainerAccess`: it has one caller
   and a different session shape (one probe per Host, not a call per Workspace). An import-linter
   contract forbids `billet.workspace.engine` from importing `access`, `host`, `infrastructure`,
   `cli` or the manager, so the engine does no I/O by construction.

   `read_image_lockers()` from the research is **not** added: after ADR-0013 the shared image
   publishes no Locker set, so there is no label to read. A Berth-version label is a matter for
   the ADR that bakes the Berth into the image (ADR-0016, future).

## Consequences

- Questions 1–3 above become computable over the registry, which is the only place they can be.
  PR #70's "can the window close?" would have been one `doctor` run.
- billet learns to open compose files. That is a real widening of ADR-0002 §1 and is why this is
  an ADR and not a feature ticket. It is granted deliberately, to one verb, read-only.
  *2026-09-29:* the first cycle opens no compose file, so that grant stays proposed; what is in
  effect is the read of the four copied Berth files (item 3).
- The stamp ADR-0012 ships in 0.4.0 is inert until this lands. That is intended: the alternative,
  an integer comparison in `start`, produces the byte-vs-directive false positive.
  *2026-09-29:* `doctor` now reads it, beside the directive hash, never instead of it.
- The Berth readiness marker, deferred from ADR-0013, has a home: a later Berth revision writes
  it after the repair block, `doctor` reads it, and `start` still never waits on it.
  *Dropped 2026-09-29:* the race it guarded is closed by writer-ensures-target (ADR-0013 item 6),
  `start` never waits, and `doctor` runs long after readiness. No Berth revision writes a marker.
- `connect` is untouched. Any change that adds work to `connect` is out of scope for this ADR
  by construction.

## Alternatives considered

- **Check in `start` instead** (the research's "CONFORM step" appended to the piped script).
  Deferred, not rejected: the wire cost is zero but the read verbs and the report renderer would
  ship with nothing to render them until `doctor` exists. When `doctor` lands, `start` may reuse
  its read verb to print the same warnings; that is a one-line follow-up, not a decision.
  *Deferred again 2026-09-29:* no drift has bitten `start` since 2026-09-14, and the follow-up
  reuses a read verb that lands with the runtime-report slice.
- **Fetch billet's side of the comparison from GitHub `main` at run time.** Rejected
  (2026-09-29): a network dependency, and it disagrees with the billet actually installed.
  A local billet-checkout config key was rejected too: registry surface for one verb.
- **Consumer-side CI only** (`check-image-pin.sh` extended with a Berth stamp check). Kept as a
  complement, added to genshift-brand in this cycle, but it sees one consumer at a time and
  cannot see inside a running container.
- **Image-side CI** (`verify-image.sh`). Extended in this cycle for Berth conformance of the
  image itself, but it cannot see a consumer.
- **A Pact-style contract test between billet and each consumer.** The right idea at the wrong
  scale for four consumers and one operator; the directive hash is the same check with one file.
