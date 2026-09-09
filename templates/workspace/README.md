# Workspace adoption templates

The repo-side files a repository needs to run as a billet **Workspace**. The full
walkthrough is [docs/adopting-a-repo.md](../../docs/adopting-a-repo.md) (rendered at
<https://rinman24.github.io/billet/adopting-a-repo/>).

Copy verbatim into the repo's `.devcontainer/`:

| Template | Lands as | Purpose |
| --- | --- | --- |
| `sshd.conf` | `.devcontainer/sshd.conf` | Key-only, dev-only sshd hardening drop-in |
| `dev-entrypoint.sh` | `.devcontainer/dev-entrypoint.sh` | Generates persisted host keys, publishes the container environment to `/etc/environment`, starts sshd, execs the CMD |
| `authorized_keys-stub` | `.devcontainer/authorized_keys-stub` | Empty fallback so non-VM builds never hard-fail |
| `env.example` | `.devcontainer/.env.example` | Sets `BILLET_AUTHORIZED_KEYS`, pointing sshd at the VM's real `authorized_keys` (gitignore `.devcontainer/.env`) |

Merge into existing files (placeholders: `<service>`, `<workspaceFolder>`, `<port>`,
`<repo>`):

| Template | Merge into | Purpose |
| --- | --- | --- |
| `docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | Loopback port publish, entrypoint, `init`, `authorized_keys` bind mount, host-keys volume |
| `Dockerfile.snippet` | the repo's dev-container Dockerfile | `openssh-server`, `dev` user (uid 1000), `~/.ssh` mountpoint, sshd drop-in |

Both are **sshd-only**: they install no CLI and mount no credential volume. Everything a
Workspace needs beyond being reachable is its own repo's business — including `gh` and
`az`, which are opt-in recipes below.

Optional, opt-in — merge only into a Workspace that actually calls the CLI
([`auth-tooling/README.md`](auth-tooling/README.md) has the merge detail,
[ADR-0011](../../docs/adr/adr-0011-optional-auth-tooling-recipes.md) the reasoning):

| Recipe | Merge into | Purpose |
| --- | --- | --- |
| `auth-tooling/gh.Dockerfile.snippet` | the repo's dev-container Dockerfile | `gh` from GitHub's GPG-pinned apt source; `~/.config/gh` mountpoint |
| `auth-tooling/gh.docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | `<service>_gh_config` volume on `~/.config/gh` — the credential store survives a rebuild |
| `auth-tooling/az.Dockerfile.snippet` | the repo's dev-container Dockerfile | `azure-cli` from Microsoft's GPG-pinned apt source; `~/.azure` mountpoint |
| `auth-tooling/az.docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | `<service>_azure_home` volume on `~/.azure` — the `az login` token survives a rebuild |

A recipe is two halves and needs both. Take `gh`, `az`, both, or neither; a Workspace that
opts into nothing carries nothing extra.

These mirror billet's own `.devcontainer/` (the first proof that a second Workspace runs
beside gswa-backend on one Host); squadra adopted from these templates next. billet
implements both auth-tooling recipes, since it needs `az` to manage Hosts and `gh` for its
pull-request workflow.

## Revision log

These templates carry no version marker; this log is the record. A consuming repo picks a
change up only by re-copying the named file — nothing here is applied to an adopted repo
automatically.

| Date | Change | To adopt |
| --- | --- | --- |
| 2026-09-04 | `dev-entrypoint.sh` snapshots the container's environment into `/etc/environment` before starting sshd, so image `ENV` and compose `environment:` values are visible in sshd login shells (`billet connect`, tmux, fleet runners). Non-secrets only — the file is world-readable ([ADR-0003 amendment](../../docs/adr/adr-0003-workspace-port-binding-contract.md)). | Re-copy `dev-entrypoint.sh` verbatim into `.devcontainer/`. No compose, Dockerfile, or config change needed. |
| 2026-09-05 | `Dockerfile.snippet` pre-creates `~/.config` and `~/.config/gh` (0700, dev-owned) and `docker-compose.snippet.yml` mounts a `<service>_gh_config` named volume on `~/.config/gh`, so the `gh` credential store (`hosts.yml`) survives `compose up --build` instead of forcing `gh auth login` after every rebuild. Credentials only — the `gh` binary itself is still the image's or `postCreateCommand`'s job, since devcontainer *features* do not run under billet. | Re-merge both snippets: the two `install -d` lines into the `dev` user RUN layer of the Dockerfile, and the mount plus its top-level `volumes:` entry into the compose file, naming the volume `<service>_gh_config`. The Dockerfile change only takes effect on a rebuild — the next `billet start` (`compose up -d --build`) does it. The existing container's current `gh` auth is not migrated onto the fresh volume, so `gh auth login` is needed one more time after adopting; it persists from then on. |
| 2026-09-05 | `env.example`, `authorized_keys-stub`, and the `authorized_keys` mount in `docker-compose.snippet.yml` rename `DEVBOX_AUTHORIZED_KEYS` to `BILLET_AUTHORIZED_KEYS`, so the one variable a Workspace's `.env` sets matches its sibling `BILLET_CONTAINER_SSH_PORT` and every other billet-owned name. The mismatch failed silently: a repo that copied the templates verbatim while following the docs' `BILLET_*` naming left the variable unset, compose mounted the empty stub anyway, and the container's sshd trusted no keys — `billet connect` then failed for a non-obvious reason. The mount is now `${BILLET_AUTHORIZED_KEYS:-${DEVBOX_AUTHORIZED_KEYS:-./authorized_keys-stub}}`: the new name wins, the old one is honoured as a deprecated fallback, and the tracked empty stub is still the final default for a non-VM build. That fallback is removed in billet 0.2.0. | Re-copy `env.example` and `authorized_keys-stub` verbatim into `.devcontainer/`, and re-merge the `authorized_keys` mount line from `docker-compose.snippet.yml` — take it whole, nested default included, rather than flattening it back to a single-level `${BILLET_AUTHORIZED_KEYS:-./authorized_keys-stub}`. No Dockerfile or rebuild is involved; compose re-reads `.env` and the mount spec on the next `billet start`. The caveat is the Host's own `.devcontainer/.env`: it is gitignored and untracked, so re-copying the template does not update it and `host_bootstrap_cmd`'s `cp -n` will not overwrite it. Nothing breaks if it is left setting the old name — the deprecated fallback covers it — but rename the key there (or delete the file and let the next `start` re-copy it) before 0.2.0. |
| 2026-09-08 | **Breaking (billet 0.2.0).** The deprecated `DEVBOX_AUTHORIZED_KEYS` fallback is removed: the `authorized_keys` mount in `docker-compose.snippet.yml` is now the single-level `${BILLET_AUTHORIZED_KEYS:-./authorized_keys-stub}`, and the deprecation notes are gone from `env.example` and `authorized_keys-stub`. The 2026-09-05 row above records the rename itself. | Re-merge the mount line and re-copy both files. Before the next `billet start`, make sure that Host's gitignored `.devcontainer/.env` sets `BILLET_AUTHORIZED_KEYS` — it is untracked, so nothing updates it for you, and with the fallback gone an `.env` still on the old name resolves to the empty stub, leaving the container's sshd trusting no keys. |
| 2026-09-08 | **Breaking (billet 0.3.0).** Auth tooling becomes opt-in. `Dockerfile.snippet` and `docker-compose.snippet.yml` are sshd-only again: the `~/.config` and `~/.config/gh` `install -d` lines are gone from the `dev` user RUN layer, and the unconditional `<service>_gh_config` volume the 2026-09-05 row added is gone from the compose snippet. In their place a new `auth-tooling/` directory ships one pair of snippets per CLI — `gh.Dockerfile.snippet` + `gh.docker-compose.snippet.yml`, `az.Dockerfile.snippet` + `az.docker-compose.snippet.yml` — each pairing the CLI itself, apt-installed from its GPG-pinned vendor source, with its credential directory on a named volume. The 2026-09-05 change persisted only the *credentials*: the `gh` binary was still installed by hand into `~/.local/bin`, which is on no volume, and rebuilds wiped it three times. It takes both halves for a tool to survive `compose up --build`, and a Workspace that calls neither CLI should carry neither volume — hence one recipe per tool, adopted deliberately ([ADR-0011](../../docs/adr/adr-0011-optional-auth-tooling-recipes.md)). | Re-merge the base `Dockerfile.snippet` §2 RUN layer and the base `docker-compose.snippet.yml`, both now sshd-only. Then, only if the repo actually uses `gh` or `az`, merge that recipe's pair from `auth-tooling/`: the Dockerfile snippet's section A as its own layer and its section B appended to the §2 RUN layer (both before the final `USER dev`, since they need root), plus the compose mount and its top-level `volumes:` entry — a named volume that is mounted but not declared fails at `up`, i.e. only on the VM. **A repo that already took the 2026-09-05 unconditional `gh` volume must not re-copy the base files alone**: that silently drops the mount, its `volumes:` declaration and the `~/.config/gh` mountpoint, nothing fails at merge time, and the next rebuild demands `gh auth login` again with the old volume left orphaned on the Host. Either keep the existing `gh` wiring as it stands or adopt the `gh` recipe deliberately — the recipe is that same wiring plus the binary it was missing. The Dockerfile half only takes effect on a rebuild; the next `billet start` (`compose up -d --build`) does it. Adopting a recipe costs one login: a running container's current credentials are not migrated onto the fresh volume, so `gh auth login` / `az login` happens one more time and persists from then on. |
