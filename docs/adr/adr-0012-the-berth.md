# ADR-0012: The Berth — naming and versioning the Workspace runtime contract

## Status

Accepted (2026-09-14). Names the runtime surface billet has published since
[ADR-0003](adr-0003-workspace-port-binding-contract.md) and gives it a version. Amends
[ADR-0002](adr-0002-workspace-subsystem.md) §1 only in vocabulary: the repo still owns its
`.devcontainer/`, and billet still reads five fields out of one file. The directive-hash
comparison this ADR defines is implemented by `billet doctor`
([ADR-0015](adr-0015-billet-doctor.md)); this cycle ships the stamp, not the comparison.

## Context

billet reaches a Workspace through a runtime surface it publishes and the consuming repo
implements: an in-container sshd on the loopback port billet assigns, a `dev` login user at
uid/gid 1000 with passwordless sudo, and an entrypoint that persists sshd host keys on a
volume, republishes the container's non-secret environment into login shells, and starts
sshd. That surface is distributed as files under `templates/workspace/` that each adopting
repo copies into its own `.devcontainer/` — four copied whole (`dev-entrypoint.sh`,
`sshd.conf`, `authorized_keys-stub`, and from this cycle `berth.version`) and two merged into
files the repo already has (`Dockerfile.snippet`, `docker-compose.snippet.yml`).

The surface has never had a name or a version. billet's `devcontainer.json` reader has both
(`DevcontainerFacts`, ADR-0002 §1); the runtime files have a prose revision log in
`templates/workspace/README.md` and nothing else. Between 2026-09-04 and 2026-09-08 five
template revisions merged (#62, #65, #68, #70, #74), two marked breaking, including a
self-reversal in which #65 added an unconditional `gh` credential volume and #74 removed it
3.5 days later. Each consumer learned about each by a human reading the log and re-copying.
The 0.3.0 release (`7d3d4b7`) bumped billet's own version for a template change, which is the
only version signal a consumer had, and it is the wrong one: billet's SemVer describes the
CLI and its config schema, not files that live in someone else's repository.

The consequence that makes this an ADR rather than a README note: billet's own template tests
(`tests/unit/templates/`) all set `_REPO_ROOT` to the billet checkout. They detect billet's own
`.devcontainer/` falling behind billet's own templates and nothing else. The drift class this
contract exists to prevent is invisible to every test billet has, by construction.

Three live consumers implement the surface today (squadra, gswa-backend, genshift-brand) and one
shared toolchain image (`ghcr.io/genshift-energy/devcontainer`) bakes part of it. None of them
can answer "which revision of the contract do I carry?" from any file they hold.

## Decision

**The runtime surface is named the Berth. It carries a monotonic integer version, independent
of billet's SemVer, shipped as `templates/workspace/berth.version` and copied by every consumer
to `.devcontainer/berth.version`. The entrypoint prints the version it was copied with.**

1. **What the Berth is.** The Berth is the set of behaviors a Workspace must exhibit for billet
   to reach and operate it, together with the files that produce them:

   | Behavior | Produced by |
   |---|---|
   | sshd listening on `127.0.0.1:${BILLET_CONTAINER_SSH_PORT}` inside the container, key-only, `dev` only | `sshd.conf`, `docker-compose.snippet.yml` `ports:` |
   | `dev` at uid/gid 1000 with passwordless `sudo` | `Dockerfile.snippet` |
   | sshd host keys persisted on a named volume (stable host identity) | `dev-entrypoint.sh`, compose `*-sshd-keys` volume |
   | container environment republished to login shells via `/etc/environment` | `dev-entrypoint.sh` (ADR-0003 amendment) |
   | named-volume mount targets under `$HOME` repaired to login-user ownership when root-owned and empty; `~/.ssh` ensured | `dev-entrypoint.sh` ([ADR-0013](adr-0013-mountpoint-ownership-repaired-at-mount-time.md)) |
   | `authorized_keys` bind-mounted from the Host admin user's file, falling back to the tracked empty stub | compose snippet, `authorized_keys-stub`, `BILLET_AUTHORIZED_KEYS` |
   | the startup line `dev-entrypoint: berth=N` | `dev-entrypoint.sh`, `berth.version` |

   The Berth is **not** the container, the image, `devcontainer.json`, or any Locker (named
   credential volume). `~/.ssh` is Berth infrastructure, not a Locker.

2. **Versioning rules.** `berth.version` holds one positive integer. It increments when any
   Berth file changes in a way that alters a directive — a shell statement, an sshd directive,
   a compose key, a Dockerfile instruction. Comment-only and whitespace-only changes do not
   bump it. There is no minor/patch structure: a consumer is either at the shipped version or
   *behind by N*, and "behind" is the only question the number answers. Berth 1 is the revision
   that ships ADR-0013's repair. The five 2026-09 merges are recorded in the revision log as
   pre-versioning history and are not retroactively numbered.

3. **Independence from billet's version.** From billet 0.4.0 the two move separately. A billet
   release that changes no Berth file does not bump `berth.version`; a Berth bump does not by
   itself require a billet release, though in practice the two ship together because the
   templates live in billet's repository. Release notes state which Berth version a billet
   release ships.

4. **The revision log is keyed by Berth version.** `templates/workspace/README.md` keeps its
   table. From Berth 1 each row is headed by the version it introduced, states which files
   changed, and gives the re-copy instruction. A second changelog file is not introduced: it
   would be one more file for consumers to re-copy.

5. **The unit of drift is the directive hash.** Two copies of a Berth file are the same revision
   if they agree after comment lines are dropped and line continuations are folded, and differ
   otherwise. Byte equality is the wrong test: all four `sshd.conf` copies in the fleet differ
   in header comments and agree in every directive. This definition is what `doctor` will
   compute (ADR-0015); nothing in this cycle compares hashes.

6. **The entrypoint reports the stamp it shipped with.** `dev-entrypoint.sh` reads the sibling
   `berth.version` and prints `dev-entrypoint: berth=N` (or `berth=unknown` if the file is
   missing) among its first log lines, before the repair block. Reading the file rather than hardcoding the number means a
   re-copied entrypoint cannot misreport, and a consumer who copied the entrypoint but forgot
   the version file gets `unknown`, which is the true state.

7. **Vocabulary.** "Berth" and "Locker" (one named compose volume persisting one tool's state
   under the login user's home) are the canonical nouns in billet's docs, tests and ADRs, recorded
   in [`docs/CONTEXT-MAP.md`](../CONTEXT-MAP.md) together with the context map of the five
   collaborating contexts.
   The following terms are retired from the docs: *devbox* (the `config.toml` table key and alias
   string keep working; the word leaves prose), *Half A/B/C* (a recipe has two parts, see
   ADR-0013), *self-consumption drift* and *canary* (name nothing that exists).

## Consequences

- A consumer can answer "am I current?" by comparing one integer, and "what changed?" by reading
  the rows above its number. That is the whole of the propagation mechanism until `doctor`
  exists, and it is already more than a prose log.
- The stamp is inert data in this cycle. billet's `start` does not read it. The first code to
  read it is the `BerthPolicy` engine of ADR-0015; adding a version comparison before the
  directive-hash engine exists would report "behind" for byte differences that agree in
  directives, the exact false positive item 5 rules out.
- Two files, not one, must be re-copied for a Berth bump to be visible: the changed Berth file
  and `berth.version`. Item 6 makes forgetting the second one visible (`berth=unknown` or a stale
  number in the container log).
- billet's 0.3.0 was cut for a Berth change. That is not undone; it is the last time it happens.
- [`docs/CONTEXT-MAP.md`](../CONTEXT-MAP.md) becomes the place a new reader learns the nouns.
  `CLAUDE.md` points at it.

## Alternatives considered

- **SemVer for the Berth.** Rejected. Consumers never depend on a range; they are at a revision
  or behind it. Minor/patch distinctions would have to be invented per change and would carry no
  information the revision log row does not.
- **Use billet's minor version as the Berth version** (the de facto state after `7d3d4b7`).
  Rejected. It couples an operator tool's release cadence to files in other repositories and
  forces a billet release for a one-line sshd directive.
- **A `.cruft.json` / `.copier-answers.yml` stamp and `cruft check`.** Considered and kept as a
  reference shape: the stamp file here is the same idea with one integer. copier/cruft themselves
  cannot manage the two merge snippets, which are fragments inside another repo's `RUN` layer
  and compose service, so the tooling would cover only four of six files.
- **Plain names ("Workspace Runtime Contract v1", "Credential Persistence Set").** Acceptable and
  duller; the operator chose the nautical names, which match billet's existing metaphor ("A
  berth for every repo").
