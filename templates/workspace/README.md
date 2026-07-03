# Workspace adoption templates

The repo-side files a repository needs to run as a billet **Workspace**. The full
walkthrough is [docs/adopting-a-repo.md](../../docs/adopting-a-repo.md) (rendered at
<https://rinman24.github.io/billet/adopting-a-repo/>).

Copy verbatim into the repo's `.devcontainer/`:

| Template | Lands as | Purpose |
| --- | --- | --- |
| `sshd.conf` | `.devcontainer/sshd.conf` | Key-only, dev-only sshd hardening drop-in |
| `dev-entrypoint.sh` | `.devcontainer/dev-entrypoint.sh` | Generates persisted host keys, starts sshd, execs the CMD |
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
