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
| `env.example` | `.devcontainer/.env.example` | Points sshd at the VM's real `authorized_keys` (gitignore `.devcontainer/.env`) |

Merge into existing files (placeholders: `<service>`, `<workspaceFolder>`, `<port>`,
`<repo>`):

| Template | Merge into | Purpose |
| --- | --- | --- |
| `docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | Loopback port publish, entrypoint, `init`, key mounts |
| `Dockerfile.snippet` | the repo's dev-container Dockerfile | `openssh-server`, `dev` user (uid 1000), sshd drop-in |

These mirror billet's own `.devcontainer/` (the first proof that a second Workspace runs
beside gswa-backend on one Host); squadra adopted from these templates next.

## Revision log

These templates carry no version marker; this log is the record. A consuming repo picks a
change up only by re-copying the named file — nothing here is applied to an adopted repo
automatically.

| Date | Change | To adopt |
| --- | --- | --- |
| 2026-09-04 | `dev-entrypoint.sh` snapshots the container's environment into `/etc/environment` before starting sshd, so image `ENV` and compose `environment:` values are visible in sshd login shells (`billet connect`, tmux, fleet runners). Non-secrets only — the file is world-readable ([ADR-0003 amendment](../../docs/adr/adr-0003-workspace-port-binding-contract.md)). | Re-copy `dev-entrypoint.sh` verbatim into `.devcontainer/`. No compose, Dockerfile, or config change needed. |
| 2026-09-05 | `Dockerfile.snippet` pre-creates `~/.config` and `~/.config/gh` (0700, dev-owned) and `docker-compose.snippet.yml` mounts a `<service>_gh_config` named volume on `~/.config/gh`, so the `gh` credential store (`hosts.yml`) survives `compose up --build` instead of forcing `gh auth login` after every rebuild. Credentials only — the `gh` binary itself is still the image's or `postCreateCommand`'s job, since devcontainer *features* do not run under billet. | Re-merge both snippets: the two `install -d` lines into the `dev` user RUN layer of the Dockerfile, and the mount plus its top-level `volumes:` entry into the compose file, naming the volume `<service>_gh_config`. The Dockerfile change only takes effect on a rebuild — the next `billet start` (`compose up -d --build`) does it. The existing container's current `gh` auth is not migrated onto the fresh volume, so `gh auth login` is needed one more time after adopting; it persists from then on. |
