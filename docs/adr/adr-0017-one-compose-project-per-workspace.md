# ADR-0017: One compose project per Workspace

## Status

Accepted (2026-10-01). Every consumer compose file names its compose project after its
Workspace key, and every volume key in it is bare and underscore-only. Supersedes the Locker
naming convention that [ADR-0006](adr-0006-claude-token-injection.md) (as amended 2026-09-14),
[ADR-0011](adr-0011-optional-auth-tooling-recipes.md) and [ADR-0012](adr-0012-the-berth.md)
state: a volume key prefixed with the compose service (`<service>_claude_home`,
`<service>-sshd-keys`) in a project Compose names after the `.devcontainer/` directory. It also
supersedes that convention's clause that existing consumer volume names stay grandfathered:
every consumer migrates. ADR-0006, 0011, 0012, 0013 and 0015 carry a dated amendment line
pointing here wherever they state the old convention or the shared project.

Governs consumer compose files and billet's templates and docs only. No billet code changes,
no `doctor` check, and no Berth change: the three hashed Berth files are untouched and
`berth.version` stays `2`.

Amended (2026-10-05, A8): the rule is now warned by `billet doctor`
([ADR-0015](adr-0015-billet-doctor.md) item 2). A running Workspace whose containers run
under a compose project other than its key, and a project two running Workspaces on one Host
share, are each a `warn`, exit 0. `billet start` still refuses nothing. Decision 4 and the
Consequences say so below. Still no Berth change.

## Context

Compose names a project after the directory of its first compose file unless the file or the
command line names it. Every billet consumer keeps its compose file in `.devcontainer/`, so on
one Host every Workspace without a top-level `name:` is the same project, `devcontainer`. On
2026-10-01 billet, genshift-brand and gswa-backend all shared it on the `devbox` Host; squadra,
which already set `name: squadra`, was the one Workspace that did not. Sharing a project has
three consequences, none of them chosen:

1. **One network.** `devcontainer_default` attached every sibling's containers with
   service-name aliases, so gswa-backend's `sql` and `redis` resolved from billet's and
   genshift-brand's containers. Isolation between Workspaces on a Host was absent, and nothing
   said so.
2. **Service-name takeover.** Compose identifies a container by project and service. Two
   Workspaces in one project that declare the same service name are one service to Compose,
   so the second `up` recreates the first one's container from its own file. Only the fact
   that the service names happened to differ prevented it.
3. **Orphan noise.** Each `up` sees its siblings' containers as orphans of its project and
   prints `Found orphan containers`, recommending `--remove-orphans`, which would remove them.

billet's own code is not the cause and needs no change. Every compose command it runs names its
files with `-f` and passes no `-p`, `--project-directory`, `--remove-orphans` or `down`; `up`
and `stop` take no service, and `exec` and `ps --status running -q <service>` are scoped by
service ([ADR-0015](adr-0015-billet-doctor.md) item 1). billet hardcodes no project, container,
volume or network name. The project name is decided entirely by the consumer's compose file.

The service prefix in the old volume keys was standing in for the namespace the project should
have provided: it is what kept two Workspaces' Lockers apart inside one shared project. Once
each Workspace has its own project, the prefix doubles on the Host (`billet_billet_claude_home`).

## Decision

1. **A top-level `name:` equal to the Workspace key.** The value is the key of the Workspace's
   `[workspaces.<key>]` table in `config.toml`: `billet`, `genshift-brand`, `squadra`,
   `gswa-backend`. The compose file is the single source of truth, so billet, a hand-run
   `docker compose -f …` and the devcontainer CLI all resolve the same project. The template
   `docker-compose.snippet.yml` ships `name: <workspace-key>`; billet's own compose file says
   `name: billet`.
2. **Bare volume keys.** The project supplies the namespace, so a compose volume key carries no
   repo or service prefix. Key `claude_home` in project `billet` is the Host volume
   `billet_claude_home`; in general a Host volume is `<workspace-key>_<key>`. squadra's keys are
   stripped too: its project and container name (`squadra-squadra-1`) do not change, and its
   volumes move from `squadra_squadra_*` to `squadra_*`.
3. **Underscores only.** A hyphenated key becomes underscored (`sshd-keys` → `sshd_keys`,
   `redis-data` → `redis_data`). The canonical keys are the Lockers `claude_home`, `azure_home`
   and `gh_config`, and `sshd_keys` (Berth infrastructure, not a Locker); a consumer's own data
   volumes follow the same rule (gswa-backend's `postgres_data` and `redis_data`).
4. **Documented and warned, not guarded** (*amended 2026-10-05*, A8; was "Documented, not
   guarded", with no `doctor` check). `billet doctor` reads the `Project` of each running
   Workspace's containers from one `docker compose ps --format json` and warns, exit 0, when
   a Workspace runs under a project other than its key
   (`warn: runs as compose project <p>, expected <key> (ADR-0017)`), and, once per Host, when
   two or more running Workspaces share a project
   (`warn: compose project <p> shared by <ws1>, <ws2> (ADR-0017)`). The first catches a
   missing or wrong `name:` before a sibling collides. `doctor` sees only running containers,
   and opens no compose file. `billet start` still does not refuse a project named
   `devcontainer`, or any other. The rule lives in this ADR, the templates and the adoption
   guide; billet's template tests hold billet's own compose file and its snippets to it.
5. **VS Code by Remote-SSH or Attach, not Reopen.** The devcontainer CLI honours a top-level
   compose `name:`, so after this ADR it resolves the same project as billet. Dev Containers
   "Reopen in Container" on a billet Host checkout would recreate the Workspace's container
   with VS Code's own override, killing its tmux sessions, and the next `billet start` would
   recreate it back. The supported VS Code routes are Remote-SSH to the Workspace's sshd
   through billet's ssh alias, and Dev Containers "Attach to Running Container" over
   Remote-SSH to the Host. "Reopen in Container" on a billet Host is unsupported.

### Migration

Compose does not rename volumes: a new project name or a new key is a new, empty volume. Each
existing Workspace therefore copies each of its volumes into its new name once, with `cp -a` as
root in a throwaway container, which keeps the Claude, `gh` and `az` logins, the Postgres data,
and the sshd host keys, so every `known_hosts` entry on the operator's machine stays valid. The
old containers and volumes are deleted as soon as the migrated Workspace passes its checks and
the copy is verified path by path. The tool is a one-shot script kept outside every repository;
it is not a billet feature.

A merged but unmigrated Workspace must not be started. `billet stop` runs only `compose stop`
against the Host's current file, but `billet start` fast-forwards the Host checkout before `up`
([ADR-0007](adr-0007-source-fast-forward-on-start.md)), so a Workspace whose rename has merged
would come up on new, empty volumes with new sshd host keys. The operator merges and migrates
each Workspace in one sitting: merge, `billet stop`, copy, `billet start`, check, delete.

## Consequences

- Each Workspace has its own `<workspace-key>_default` network holding only its own services.
  gswa-backend's `sql` and `redis` no longer resolve from its siblings, and a service name
  shared by two Workspaces is no longer a takeover.
- `billet start` prints no `Found orphan containers` line for a sibling Workspace.
- Host volume names read `<workspace-key>_<key>`. The `*_claude_home` globs in ADR-0006,
  ADR-0013 and billet's code comments still match them.
- billet's code is unchanged. The service-scoped `ps` of ADR-0015 is no longer what keeps
  Workspaces apart; it stays correct, and harmless, for a consumer that has not adopted this
  rule yet.
- No Berth change. The compose snippet is merged, not copied, and `doctor` does not hash it
  ([ADR-0015](adr-0015-billet-doctor.md) item 2), so adopting this rule is not Berth drift and
  needs no `berth.version` bump.
- A consumer that has not adopted the rule keeps working under billet exactly as before, still
  sharing `devcontainer` with any sibling that has not either. Nothing detects it; a `doctor`
  warning for a shared project is left to a later cycle. *Amended 2026-10-05 (A8):* `doctor`
  now detects it while the Workspace runs (decision 4) and warns; `start` is unchanged.

## Alternatives considered

- **billet passes `-p <key>`.** Rejected: a hand-run `docker compose -f …` or the
  devcontainer CLI would still resolve `devcontainer`, giving one checkout two projects.
- **`COMPOSE_PROJECT_NAME` in `.devcontainer/.env`.** Rejected: those files are untracked Host
  state, so the name would drift from the repository.
- **Scope billet's `up` and `stop` by service only.** Rejected: fixes neither the shared
  network nor the takeover hazard.
- **Defer and document.** Rejected: the shared network is a live isolation gap.
- **Keep the old Host volume names** with a volume-level `name: devcontainer_<vol>`. Rejected:
  a "created for project devcontainer" warning on every `up` forever, an unverified
  config-hash recreate prompt, and names that keep saying `devcontainer`.
- **Declare the old volumes `external: true`.** Rejected: a fresh Host cannot bootstrap.
- **Start every Locker fresh.** Rejected: loses the Claude, `gh` and `az` logins, the Postgres
  data, and the sshd host keys.
- **Keep the service-prefixed keys.** A smaller diff, and squadra out of scope, but every Host
  volume name doubles its prefix. Rejected by the operator in favour of the extra scope.
- **A `doctor` warning for two Workspaces sharing a project on one Host**, or **`billet start`
  refusing a project named `devcontainer`.** Rejected for now: both are code, this change is
  docs only, and the warning belongs with later `doctor` work. *Amended 2026-10-05 (A8):* the
  `doctor` warning has landed (decision 4); `start` refusing a project stays out of scope.
- **Amending ADR-0012 alone.** Rejected: the rule spans the Lockers of ADR-0006 and ADR-0011
  and the shared-project reasoning of ADR-0015; one ADR that each of them points at is easier
  to find than five amendments.
- **A `billet migrate` command**, or **a copy-paste runbook**, for the one-time data move.
  Rejected: a permanent feature for a one-time job, and fifteen hand-typed volume copies.
