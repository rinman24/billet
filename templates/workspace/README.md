# Workspace adoption templates

The repo-side files a repository needs to run as a billet **Workspace**. The full
walkthrough is [docs/adopting-a-repo.md](../../docs/adopting-a-repo.md) (rendered at
<https://rinman24.github.io/billet/adopting-a-repo/>).

## The Berth

Together these files implement the **Berth**: the Workspace runtime contract billet
publishes and every consuming repo carries ([ADR-0012](../../docs/adr/adr-0012-the-berth.md)).
A Workspace is on the Berth when it exhibits:

- sshd listening on `127.0.0.1:${BILLET_CONTAINER_SSH_PORT}`, key-only, `dev` only
  (`sshd.conf`, the compose snippet's `ports:`);
- a `dev` login user at uid/gid 1000 with passwordless sudo (`Dockerfile.snippet`);
- sshd host keys persisted on a named volume (`dev-entrypoint.sh`, the `<service>-sshd-keys`
  volume);
- the container environment republished to login shells via `/etc/environment`
  (`dev-entrypoint.sh`);
- named-volume mount targets under `/home/dev` repaired to dev ownership when they come up
  root-owned and empty, and `~/.ssh` ensured dev-owned 0700
  (`dev-entrypoint.sh`, [ADR-0013](../../docs/adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md));
- `authorized_keys` bind-mounted from the Host admin user's file, falling back to the
  tracked stub (the compose snippet, `authorized_keys-stub`);
- the startup line `dev-entrypoint: berth=N` (`dev-entrypoint.sh`, `berth.version`).

The Berth has a **version**: one positive integer in `berth.version`, independent of
billet's own release number. It increments when a Berth file changes in a way that alters a
directive (a shell statement, an sshd directive, a compose key, a Dockerfile instruction);
comment-only changes do not bump it. The entrypoint reads the sibling `berth.version` at
start and logs `dev-entrypoint: berth=N` — or `berth=unknown` when the file is missing. So
**copy `berth.version` too**, every time you re-copy a Berth file: the number in
`docker compose logs` is how a Workspace answers "which Berth am I on?", and the revision
log below tells you what changed above your number.

The Berth is not the container, the image, `devcontainer.json`, or any **Locker** — a named
compose volume persisting one tool's state under `/home/dev` (`<service>_claude_home`,
`<service>_gh_config`, `<service>_azure_home`). Lockers are declared only in the consumer's
compose file; they need no pre-created mountpoint in the image because the Berth entrypoint
repairs ownership at mount time. `~/.ssh` is Berth infrastructure, not a Locker.

Copy verbatim into the repo's `.devcontainer/`:

| Template | Lands as | Purpose |
| --- | --- | --- |
| `sshd.conf` | `.devcontainer/sshd.conf` | Key-only, dev-only sshd hardening drop-in |
| `dev-entrypoint.sh` | `.devcontainer/dev-entrypoint.sh` | Logs the Berth version, ensures `~/.ssh`, repairs root-owned empty named-volume mount targets under `/home/dev`, generates persisted host keys, publishes the container environment to `/etc/environment`, starts sshd, execs the CMD |
| `berth.version` | `.devcontainer/berth.version` | The Berth version this copy of the files carries; the entrypoint reads it from beside itself and logs `berth=N` |
| `authorized_keys-stub` | `.devcontainer/authorized_keys-stub` | Empty fallback so non-VM builds never hard-fail |

Merge into existing files (placeholders: `<service>`, `<workspaceFolder>`, `<port>`,
`<repo>`):

| Template | Merge into | Purpose |
| --- | --- | --- |
| `docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | Loopback port publish, entrypoint, `init`, `authorized_keys` bind mount, host-keys volume, the Claude Locker (`<service>_claude_home` on `~/.claude` + `CLAUDE_CONFIG_DIR`) |
| `Dockerfile.snippet` | the repo's dev-container Dockerfile | `openssh-server`, `dev` user (uid 1000), `~/.ssh` mountpoint, sshd drop-in |

Both install no CLI. The one Locker the base compose snippet carries is Claude's: every
billet Workspace receives a token ([ADR-0006](../../docs/adr/adr-0006-claude-token-injection.md)),
so it is not opt-in. Everything else a Workspace needs beyond being reachable is its own
repo's business — including `gh` and `az`, which are opt-in recipes below.

Both `BILLET_*` variables the compose snippet interpolates — `BILLET_CONTAINER_SSH_PORT`
and `BILLET_AUTHORIZED_KEYS` (the Host admin user's `~/.ssh/authorized_keys`) — are
exported by `billet start` before every compose call. No file is copied anywhere to set
them. **Migrating from billet < 0.4.0**, which set `BILLET_AUTHORIZED_KEYS` through an
`.env` file: delete the two
`host_bootstrap_cmd = "cp -n .devcontainer/.env.example .devcontainer/.env"` lines from
`~/.config/billet/config.toml` (the hook then falls back to its `":"` default; the hook
itself is a general one and stays), and drop `.env.example` from the repo's
`.devcontainer/`. The untracked `.env` already on a Host is inert — a shell export outranks
compose's `.env` interpolation — so nothing on the Host needs deleting.

Optional, opt-in — merge only into a Workspace that actually calls the CLI
([`auth-tooling/README.md`](auth-tooling/README.md) has the merge detail,
[ADR-0011](../../docs/adr/adr-0011-optional-auth-tooling-recipes.md) the reasoning):

| Recipe | Merge into | Purpose |
| --- | --- | --- |
| `auth-tooling/gh.Dockerfile.snippet` | the repo's dev-container Dockerfile | `gh` from GitHub's GPG-pinned apt source |
| `auth-tooling/gh.docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | `<service>_gh_config` Locker on `~/.config/gh` — the credential store survives a rebuild |
| `auth-tooling/az.Dockerfile.snippet` | the repo's dev-container Dockerfile | `azure-cli` from Microsoft's GPG-pinned apt source (requires a consumer-built image) |
| `auth-tooling/az.docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | `<service>_azure_home` Locker on `~/.azure` — the `az login` token survives a rebuild |

A recipe is two parts — binary and Locker — and needs both; there is no image-side
mountpoint to create, since the Berth entrypoint re-owns a fresh Locker at container start
([ADR-0013](../../docs/adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md)). Take
`gh`, `az`, both, or neither; a Workspace that opts into nothing carries nothing extra.

These mirror billet's own `.devcontainer/` (the first proof that a second Workspace runs
beside gswa-backend on one Host); squadra adopted from these templates next. billet
implements both auth-tooling recipes, since it needs `az` to manage Hosts and `gh` for its
pull-request workflow.

## Revision log

Rows are keyed by the Berth version that introduced them (`berth.version`). A consuming repo
picks a change up only by re-copying the named files — nothing here is applied to an adopted
repo automatically — and every re-copy includes `berth.version`, so the container's
`dev-entrypoint: berth=N` line states which row it is on.

| Berth | Date | Change | To adopt |
| --- | --- | --- | --- |
| 1 | 2026-09-14 | **Berth 1** ([ADR-0012](../../docs/adr/adr-0012-the-berth.md), [ADR-0013](../../docs/adr/adr-0013-mountpoint-ownership-repaired-at-mount-time.md)). `dev-entrypoint.sh` repairs mount-target ownership at container start: it reads `/proc/self/mountinfo`, and every Docker named volume mounted under `/home/dev` whose target is a directory owned by uid 0 and empty is re-owned to the login user with `sudo -n install -d -o <uid> -g <gid> -m 0700` (logged as `dev-entrypoint: repaired <path> (was root:<group> <mode>)`). A populated root-owned target, or one owned by a third uid, is reported (`dev-entrypoint: warning: <path> is root-owned and not empty; not repaired` / `owned by uid <n>; not repaired`) and left alone; a target already owned by the login user is untouched, mode included; a failed repair warns (`repair of <path> failed; continuing`) and sshd still starts. Never recursive, never `chown -R`. `~/.ssh` is ensured dev-owned 0700 by the same rule, created if missing. The block runs before `ssh-keygen`, the slow cold-start step. The entrypoint also prints `dev-entrypoint: berth=N` as its first line, read from the new sibling `berth.version`. Consequence: an image no longer needs to pre-create a Locker's mountpoint — `Dockerfile.snippet` keeps only `~/.ssh`, and billet's own `.devcontainer/Dockerfile` drops `~/.claude` and `~/.azure`. | Re-copy `dev-entrypoint.sh` and copy the new `berth.version` beside it into `.devcontainer/`. No compose or config change. Optionally drop the Locker `install -d` lines from the Dockerfile's `dev` RUN layer (keep `~/.ssh`); that takes effect on the next rebuild. **Migration:** a Locker volume that already came up root-owned and empty (the `Permission denied` on first `gh auth login` / `claude`) is fixed by the next `billet start` running this entrypoint — no `docker volume rm`, and nothing is lost because the volume was empty. A populated root-owned volume is only reported, never touched: inspect what it holds (`sudo ls -la <path>` inside the container) and decide what that content is and who should own it — the right move depends on whether it is salvageable state, a stray root-written file, or the wrong volume mounted. |

### Pre-versioning

The rows below predate `berth.version` and are not retroactively numbered. A Workspace that
adopted all of them and nothing since is at "pre-versioning", which the entrypoint reports
as `berth=unknown` until `berth.version` is copied alongside the Berth 1 entrypoint.

| Date | Change | To adopt |
| --- | --- | --- |
| 2026-09-04 | `dev-entrypoint.sh` snapshots the container's environment into `/etc/environment` before starting sshd, so image `ENV` and compose `environment:` values are visible in sshd login shells (`billet connect`, tmux, fleet runners). Non-secrets only — the file is world-readable ([ADR-0003 amendment](../../docs/adr/adr-0003-workspace-port-binding-contract.md)). | Re-copy `dev-entrypoint.sh` verbatim into `.devcontainer/`. No compose, Dockerfile, or config change needed. |
| 2026-09-05 | `Dockerfile.snippet` pre-creates `~/.config` and `~/.config/gh` (0700, dev-owned) and `docker-compose.snippet.yml` mounts a `<service>_gh_config` named volume on `~/.config/gh`, so the `gh` credential store (`hosts.yml`) survives `compose up --build` instead of forcing `gh auth login` after every rebuild. Credentials only — the `gh` binary itself is still the image's or `postCreateCommand`'s job, since devcontainer *features* do not run under billet. | Re-merge both snippets: the two `install -d` lines into the `dev` user RUN layer of the Dockerfile, and the mount plus its top-level `volumes:` entry into the compose file, naming the volume `<service>_gh_config`. The Dockerfile change only takes effect on a rebuild — the next `billet start` (`compose up -d --build`) does it. The existing container's current `gh` auth is not migrated onto the fresh volume, so `gh auth login` is needed one more time after adopting; it persists from then on. |
| 2026-09-05 | `env.example`, `authorized_keys-stub`, and the `authorized_keys` mount in `docker-compose.snippet.yml` rename `DEVBOX_AUTHORIZED_KEYS` to `BILLET_AUTHORIZED_KEYS`, so the one variable a Workspace's `.env` sets matches its sibling `BILLET_CONTAINER_SSH_PORT` and every other billet-owned name. The mismatch failed silently: a repo that copied the templates verbatim while following the docs' `BILLET_*` naming left the variable unset, compose mounted the empty stub anyway, and the container's sshd trusted no keys — `billet connect` then failed for a non-obvious reason. The mount is now `${BILLET_AUTHORIZED_KEYS:-${DEVBOX_AUTHORIZED_KEYS:-./authorized_keys-stub}}`: the new name wins, the old one is honoured as a deprecated fallback, and the tracked empty stub is still the final default for a non-VM build. That fallback is removed in billet 0.2.0. | Re-copy `authorized_keys-stub` verbatim into `.devcontainer/` and re-merge the `authorized_keys` mount line from `docker-compose.snippet.yml`. No Dockerfile or rebuild is involved. *(This row originally also covered `env.example` and the Host's `.devcontainer/.env`; both are retired — billet exports the variable itself, see above.)* |
| 2026-09-08 | **Breaking (billet 0.2.0).** The deprecated `DEVBOX_AUTHORIZED_KEYS` fallback is removed: the `authorized_keys` mount in `docker-compose.snippet.yml` is now the single-level `${BILLET_AUTHORIZED_KEYS:-./authorized_keys-stub}`, and the deprecation notes are gone from `env.example` and `authorized_keys-stub`. The 2026-09-05 row above records the rename itself. | Re-merge the mount line and re-copy `authorized_keys-stub`. *(The `.env` step this row originally required is retired — billet exports `BILLET_AUTHORIZED_KEYS` itself, see above.)* |
| 2026-09-08 | **Breaking (billet 0.3.0).** Auth tooling becomes opt-in. `Dockerfile.snippet` and `docker-compose.snippet.yml` are sshd-only again: the `~/.config` and `~/.config/gh` `install -d` lines are gone from the `dev` user RUN layer, and the unconditional `<service>_gh_config` volume the 2026-09-05 row added is gone from the compose snippet. In their place a new `auth-tooling/` directory ships one pair of snippets per CLI — `gh.Dockerfile.snippet` + `gh.docker-compose.snippet.yml`, `az.Dockerfile.snippet` + `az.docker-compose.snippet.yml` — each pairing the CLI itself, apt-installed from its GPG-pinned vendor source, with its credential directory on a named volume. The 2026-09-05 change persisted only the *credentials*: the `gh` binary was still installed by hand into `~/.local/bin`, which is on no volume, and rebuilds wiped it three times. It takes both halves for a tool to survive `compose up --build`, and a Workspace that calls neither CLI should carry neither volume — hence one recipe per tool, adopted deliberately ([ADR-0011](../../docs/adr/adr-0011-optional-auth-tooling-recipes.md)). | Re-merge the base `Dockerfile.snippet` §2 RUN layer and the base `docker-compose.snippet.yml`, both now sshd-only. Then, only if the repo actually uses `gh` or `az`, merge that recipe's pair from `auth-tooling/`: the Dockerfile snippet's section A as its own layer and its section B appended to the §2 RUN layer (both before the final `USER dev`, since they need root), plus the compose mount and its top-level `volumes:` entry — a named volume that is mounted but not declared fails at `up`, i.e. only on the VM. **A repo that already took the 2026-09-05 unconditional `gh` volume must not re-copy the base files alone**: that silently drops the mount, its `volumes:` declaration and the `~/.config/gh` mountpoint, nothing fails at merge time, and the next rebuild demands `gh auth login` again with the old volume left orphaned on the Host. Either keep the existing `gh` wiring as it stands or adopt the `gh` recipe deliberately — the recipe is that same wiring plus the binary it was missing. The Dockerfile half only takes effect on a rebuild; the next `billet start` (`compose up -d --build`) does it. Adopting a recipe costs one login: a running container's current credentials are not migrated onto the fresh volume, so `gh auth login` / `az login` happens one more time and persists from then on. |
