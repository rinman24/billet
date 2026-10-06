# Context map

The nouns billet's docs, tests and ADRs use, the contexts billet collaborates with and how each
relationship is shaped, the live consumers of the Berth, and the questions this cycle left
open. Vocabulary was settled on 2026-09-14 with [ADR-0012](adr/adr-0012-the-berth.md); this
page is where a new reader learns the terms and where the deferred decisions are listed until
an ADR takes each one.

## Vocabulary

| Term | Meaning |
|---|---|
| **Host** | A cloud VM that runs Workspace containers ([ADR-0001](adr/adr-0001-closed-architecture-decomposition.md)). Not a role: a Host either manages Workspaces or does not ([ADR-0004](adr/adr-0004-host-manages-workspaces.md)). |
| **Workspace** | One repository's devcontainer running on a Host, reachable through the Berth ([ADR-0002](adr/adr-0002-workspace-subsystem.md)). Not the repo, not the container, not the `.devcontainer/` directory. |
| **HostProvider** | The one backend seam (Azure VM today). |
| **Berth** | The Workspace runtime contract billet publishes: sshd on the assigned loopback port, `dev` at uid/gid 1000 with passwordless sudo, the entrypoint's behaviors (host-key persistence, environment snapshot to `/etc/environment`, mount-target ownership repair, `~/.ssh` ensured), and the `berth=N` startup line. Distributed as the files under `templates/workspace/` ([ADR-0012](adr/adr-0012-the-berth.md)). Not the container, the image, `devcontainer.json`, or any Locker. |
| **Berth version** | Monotonic integer in `templates/workspace/berth.version`, independent of billet's SemVer. Starts at 1 with the 2026-09 cycle; earlier template revisions are pre-versioning. |
| **Locker** | One named compose volume persisting one tool's state under the login user's home, e.g. `claude_home:/home/dev/.claude`: a bare, underscore-only key in a compose project named for the Workspace, so on the Host it is `<workspace-key>_claude_home` ([ADR-0017](adr/adr-0017-one-compose-project-per-workspace.md)). Declared only in the consumer's compose file; nothing in an image or in billet's code declares one. `~/.ssh` is Berth infrastructure, never a Locker. |
| **Recipe** | An opt-in `auth-tooling/` pair: the CLI's install snippet (binary) and its Locker snippet (volume). Two parts, not three: the image-side mountpoint part is gone ([ADR-0011](adr/adr-0011-optional-auth-tooling-recipes.md) as amended by [ADR-0013](adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md)). |
| **Facts contract** | The five `devcontainer.json` fields billet reads (`ContainerAccess` → `DevcontainerFacts`: `service`, `dockerComposeFile`, `workspaceFolder`, `remoteUser`, `postCreateCommand`). Unchanged by this cycle ([ADR-0002](adr/adr-0002-workspace-subsystem.md) §1). |
| **Definition vs state** | billet never writes a file Docker, Compose or the devcontainer tooling reads to build or create a container; it may write runtime state into a container it started ([ADR-0014](adr/adr-0014-definition-versus-state.md)). |
| **Directive hash** | SHA-256 of a Berth file with comment lines dropped and continuations folded; the unit of drift. Defined in ADR-0012 item 5, computed by `doctor` ([ADR-0015](adr/adr-0015-billet-doctor.md), later cycle). |
| **Vacuity guard** | A scanner that matched nothing across its whole input fails its own test, so a rotted pattern cannot pass forever (`test_every_billet_owned_variable_uses_the_billet_prefix` is the precedent). |
| **Adopt** | Reserved to [ADR-0005](adr/adr-0005-instance-lifecycle-ownership.md)'s sense: billet uses durable infrastructure it does not own. Not copying a template or merging a recipe. |

Retired from prose in billet and in the shared toolchain image's repository: *devbox* (the
`config.toml` table key `[hosts.devbox]` and the `gswa-devbox` alias string keep working; the
word is no longer a concept and leaves billet's package metadata too — the `pyproject.toml`
keyword is dropped), *Half A/B/C* (a recipe has two parts), *self-consumption drift*
and *canary* (both named nothing that exists).

## The five contexts

billet, the product repositories that run as Workspaces, the shared GenShift toolchain image
(`ghcr.io/genshift-energy/devcontainer`, built from the `genshift-devcontainer` repository),
the operator's dotfiles, and the operator ledger (`~/.config/billet/config.toml`). Relationships
are named in DDD terms; each row states what fixes the pattern.

| Upstream | Downstream | Pattern | What fixes it |
|---|---|---|---|
| Product repo | billet | Conformist behind an anti-corruption layer | The repo authors `devcontainer.json` on its own cadence; billet adapts through `facts_from_json` (`access/container/compose_script.py`) → `DevcontainerFacts` (JSONC stripped, paths re-rooted, `postCreateCommand` normalized, the object form refused with a named error). billet never edits the repo's compose file ([ADR-0003](adr/adr-0003-workspace-port-binding-contract.md)). |
| billet | Product repo | Open Host Service publishing a versioned Published Language; the consumer conforms by copy | The Berth: `templates/workspace/` plus [the adoption guide](adopting-a-repo.md). Until Berth 1 the language had no version and this row was a Shared Kernel replicated by hand; `berth.version` and the directive hash are what make it an OHS ([ADR-0012](adr/adr-0012-the-berth.md)). |
| Shared toolchain image | Product repo (image pinner) | Customer/Supplier over a well-versioned Published Language | The consumer's Dockerfile is one digest-pinned `FROM` line and its CI `container:` must match. Toolchain versions are pinned in `versions.env`, tested by `verify-image.sh`, propagated by Renovate. From image 2.0.0 (2026-09-14) `verify-image.sh` checks Berth conformance: `dev` at uid/gid 1000, `sudo -n`, `sshd`, the baked `sshd.conf`, `~/.ssh` dev-owned 0700; 2.1.0 added four `sshd -T` directive checks. |
| billet | Shared toolchain image | Conformist, undeclared | The image implements the Berth's build-time half (`dev` at uid 1000 with passwordless sudo, `openssh-server`, billet's sshd drop-in, `~/.ssh`) and says so; billet does not know the image exists. **The image carries no Lockers**: from 2.0.0 it pre-creates no credential directory with an `install -d` line, because the Berth entrypoint repairs mount-target ownership at start ([ADR-0013](adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md)). It does still ship a populated dev-owned `/home/dev/.claude` as a side effect of the Claude Code install, so that Locker is protected by copy-on-empty while `~/.config/gh` is the one target the repair handles. Lockers exist only in consumer compose files. |
| Shared toolchain image | billet | Separate Ways, deliberately | billet builds its own reference Workspace from `python:3.11-bookworm` and must stay usable by a repository outside GenShift. No billet change may require the shared image. |
| billet | Dotfiles | Open Host Service + Published Language, deliberately unvalidated | Three `@billet_*` tmux options, closed at three, with a stated test for a fourth ([ADR-0008](adr/adr-0008-workspace-identity-publication.md), [ADR-0009](adr/adr-0009-scope-of-identity-publication.md)). The healthiest relationship in the map. |
| Dotfiles | Product repo and image | Shared Kernel by convergence, two invocation owners | The image bakes `chezmoi` but never runs it; the pull happens through billet's global `personal_bootstrap_cmd` and each repo's `postCreateCommand`. See the lifecycle-hooks question below. |
| Operator ledger | everything | Unmodeled | `config.toml` is the sole port ledger and the home of every per-Workspace hook, under no CI and in no repository. From billet 0.4.0, which exports `BILLET_AUTHORIZED_KEYS` from the same remote-script prelude as `BILLET_CONTAINER_SSH_PORT` ([ADR-0003](adr/adr-0003-workspace-port-binding-contract.md)), the per-Host `.devcontainer/.env` half of the ledger is gone; the `config.toml` half remains. |

Where each Berth behavior comes from, file by file, is the table in ADR-0012 item 1. What billet
reads and writes across every one of these boundaries is ADR-0014.

## Live consumer inventory

Verified 2026-10-06 against each consumer's `origin/main` and by `billet doctor` 0.5.0 on the
Host: all four Workspaces are on Berth 2, the Berth billet 0.5.0 ships, and each runs
`berth=2` with every published port on loopback. Each moved to Berth 2 by re-copying
`dev-entrypoint.sh` and `berth.version` (see the revision log in `templates/workspace/README.md`).

| Repo | Builds from | Berth | Lockers mounted (compose keys, ADR-0017) |
|---|---|---|---|
| genshift-brand | shared image `devcontainer:2.2.1@sha256:5626b81…` (one-line `FROM`) | 2 | `claude_home`, `gh_config` (plus `sshd_keys`, Berth infrastructure) |
| squadra | own Dockerfile from `python:3.11-bookworm` | 2 | `claude_home`, `gh_config` (plus `sshd_keys`) |
| gswa-backend | own Dockerfile from `python:3.12-bookworm` | 2 | `claude_home`, `azure_home` (plus `sshd_keys`, and the data volumes `postgres_data` and `redis_data`); `sql` also publishes `127.0.0.1:5432`, and its compose lists three variables in `BILLET_ENV_PUBLISH` |
| billet (reference Workspace) | own Dockerfile | 2 | `claude_home`, `azure_home`, `gh_config` (plus `sshd_keys`) |
| genshift-devcontainer | not a Workspace: its `sshd.conf` is a build input of the image | n/a | none |

Every consumer compose file names its compose project after its Workspace key and uses bare,
underscore-only volume keys ([ADR-0017](adr/adr-0017-one-compose-project-per-workspace.md)), so each Workspace has
its own network and its own `<workspace-key>_<key>` volumes on the Host; each consumer landed
the keys above with its own rename and volume migration. This replaced the earlier rule that
left existing consumer volume names as they were and gave service-prefixed names to new
adopters only. Every sshd port line is `127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-<n>}:22`. The
shared image's 2.0.0 release (2026-09-14, drops Locker pre-creation) requires consumers on
Berth 1 or later, because a fresh `gh` Locker mounted by a pre-Berth-1 entrypoint into a 2.0.0
image would stay root-owned with nothing to repair it.

## Open questions

Deferred on 2026-09-14, in the order they are likely to be taken up. Each needs its own ADR or
an amendment before code. Updated 2026-09-29: questions 2 and 6 are closed, 4 is partly
implemented, and 5 is deferred again.

1. **`gh_token_cmd` (N4).** Deliver `GH_TOKEN` the way [ADR-0006](adr/adr-0006-claude-token-injection.md)
   delivers the Claude token, which would remove the `gh` Locker entirely. Needs an ADR-0011
   amendment distinguishing *reading a credential store* (which billet never does) from
   *delivering an operator-supplied token*; the token must not travel through the
   world-readable `/etc/environment`.
2. **Lifecycle hooks.** *Closed (2026-09-29).* billet runs `postCreateCommand` and the
   operator-global `personal_bootstrap_cmd` on every `start`, which already covers what
   `postStartCommand` would, and adopts no further lifecycle keys (`onCreateCommand`,
   `updateContentCommand`, `postStartCommand`, `postAttachCommand` stay unread). ADR-0002 §1 is
   unchanged (A2 dropped, 2026-09-29).
3. **Berth baked into the shared image (ADR-0016).** The entrypoint, `sshd.conf` and stub live
   in the image and the consumer's compose points at them (`entrypoint:
   ${BILLET_ENTRYPOINT:-…}`), with a Berth-version OCI label. Precondition: the shared image's
   ADR record states that Docker, not billet, reads those files from the repo (its ADR-0001
   says billet reads them; its ADR-0002, shipped with 2.0.0 on 2026-09-14, corrects that). Must not
   break Separate Ways: a non-GenShift consumer keeps copying files.
4. **`doctor` implementation** ([ADR-0015](adr/adr-0015-billet-doctor.md)). *Berth drift
   implemented (2026-09-29):* the pure `berth_policy` engine, the `DoctorAccess` seam (one
   sectioned SSH probe per Host) and the `BerthStatus`/`BerthFileStatus` contracts. *Runtime
   report implemented (2026-09-29):* in the same SSH session, `docker compose ps` finds each
   service's running container and `docker logs` gives the entrypoint's lines. The pure
   `runtime_policy` engine turns them into a `RuntimeReport`: the running Berth against the
   checkout's stamp, and the Locker-ownership repairs and warnings. `doctor` never execs into a
   container. *Compose project and published ports implemented (2026-10-05, A8):* one more
   `docker compose ps --format json` per running Workspace, parsed in the access layer; the
   pure `compose_policy` engine warns on a Workspace running under a project other than its
   key, on a project two Workspaces on one Host share (ADR-0017), and on any port a container in
   the project publishes on a non-loopback address (ADR-0003). The compose-file read ADR-0002
   §1 would grant to `doctor` stays proposed: no check opens a compose file yet.
5. **Start-time CONFORM readback and compose preflight.** Neither a `stat` of Locker ownership
   appended to the `start` script nor a `docker compose config -q` preflight for undeclared
   volumes ships in 0.4.0. Both are `doctor` checks first; `start` may reuse the read verb
   afterwards. *Deferred again (2026-09-29):* nothing has bitten since 2026-09-14, and the
   readback would reuse the read verb of `doctor`'s runtime-report slice. The compose scans left
   `doctor` too (ADR-0015 item 2): the deprecated-name scan returns at the next variable
   deprecation, the undeclared-volume preflight if an undeclared volume ever reaches a `start`.
6. **Readiness marker.** *Closed (2026-09-29): dropped.* The race it guarded is closed by the
   writer-ensures-target rule of ADR-0013 item 6, which holds on every Berth revision; `start`
   never waits; and `doctor` runs long after readiness, so a marker would report nothing a
   human needs.
