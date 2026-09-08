# Auth-tooling recipes (optional)

Workspaces need different authentication. billet itself lives on `az` — managing Azure
Hosts is its job — a repo whose workflow is pull requests lives on `gh`, and plenty of
Workspaces need neither. So neither CLI is in the base templates: a Workspace opts into
the ones it actually uses, and one that opts into nothing carries nothing extra.

A recipe is **two halves, and both are required**:

| Half | What it does | Why it is not optional |
| --- | --- | --- |
| CLI in the image | apt-installs the tool from its GPG-pinned source | Without it the binary is installed by hand into `~/.local/bin`, which is not on a volume, so every `compose up --build` wipes it |
| Credential dir on a named volume | mounts the tool's config directory | Without it the container filesystem holds the token, so every rebuild forces `gh auth login` / `az login` again |

Adopting one half only is the failure this exists to end: it looks like it works until the
next rebuild, and then costs a session the same rediscovery.

## The recipes

| Recipe | Merge into | Provides | Volume |
| --- | --- | --- | --- |
| `gh.Dockerfile.snippet` | the repo's dev-container Dockerfile | `gh` from `cli.github.com/packages` | — |
| `gh.docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | — | `<service>_gh_config` → `~/.config/gh` |
| `az.Dockerfile.snippet` | the repo's dev-container Dockerfile | `azure-cli` from `packages.microsoft.com` | — |
| `az.docker-compose.snippet.yml` | `.devcontainer/docker-compose.yml` | — | `<service>_azure_home` → `~/.azure` |

Take `gh`, `az`, both, or neither. Placeholders match the base templates: `<service>` is
the compose service `devcontainer.json` names.

## Adopting one

1. Merge the recipe's **Dockerfile** snippet: section A as its own layer, section B's
   `install -d` lines appended to the `dev` user RUN layer in
   [`../Dockerfile.snippet`](../Dockerfile.snippet) §2. Both must precede the final
   `USER dev` — setting ownership needs root.
2. Merge the recipe's **compose** snippet: the mount into the service's `volumes:` list,
   and the volume name into the file's top-level `volumes:` mapping. A named volume that
   is mounted but not declared fails at `up`, i.e. only on the VM.
3. The Dockerfile half only takes effect on a rebuild — the next `billet start`
   (`compose up -d --build`) does it.
4. Authenticate once (`gh auth login`, `az login`). It persists from then on. An existing
   container's current credentials are *not* migrated onto the fresh volume, so the first
   login after adopting happens one more time.

## Why this is a recipe and not a billet feature

billet reads a repo's `.devcontainer/` as a data contract; the repo owns it
([ADR-0005](../../../docs/adr/adr-0005-instance-lifecycle-ownership.md),
[ADR-0011](../../../docs/adr/adr-0011-optional-auth-tooling-recipes.md)). Nothing here is
applied automatically, and billet neither installs these CLIs nor reads their credentials.
A devcontainer *feature* would be the obvious alternative, but features do not run under
billet — it drives the stack with raw `docker compose`, not the devcontainer CLI — so the
tool must be in the image or installed from `postCreateCommand`.
