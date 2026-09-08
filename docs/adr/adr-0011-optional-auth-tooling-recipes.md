# ADR-0011: Auth tooling is an opt-in per-Workspace recipe, not a template default

## Status

Accepted (2026-09-08). Applies [ADR-0005](adr-0005-instance-lifecycle-ownership.md)'s
adopt-don't-own boundary to the repo side of the contract, and narrows the 2026-09-05
Workspace templates, which shipped a `gh` credential volume to every adopting repo whether
or not that repo ever ran `gh`. Governs `templates/workspace/` only: no billet code
changes, and [ADR-0002](adr-0002-workspace-subsystem.md) §1's rule that the repo owns its
own `.devcontainer/` is untouched — this ADR settles what billet *ships as a suggestion*,
never what it applies.

## Context

Two independent facts about how billet runs a Workspace collided in the templates.

**devcontainer *features* do not run.** billet drives the stack with raw
`docker compose up -d --build`, not the devcontainer CLI, so the `features` block is VS
Code tooling and is not among the five fields billet reads out of `devcontainer.json`
(ADR-0002 §1). Any CLI a feature would install has to be in the image or in
`postCreateCommand`.

**The container filesystem does not survive a rebuild,** and billet rebuilds on every
`start`. A binary in `~/.local/bin` and a token in `~/.config/gh/hosts.yml` are equally
gone at the next `compose up --build`; only a named volume persists.

Both halves of that were learned the expensive way. The `gh` binary was installed by hand
into `~/.local/bin` and wiped by rebuilds on 2026-07-12, 2026-07-15 and 2026-09-04, each
time costing a session the same rediscovery before it could open a pull request. The
2026-09-05 templates fixed the *other* half and only for `gh`: `Dockerfile.snippet`
pre-created `~/.config/gh` and `docker-compose.snippet.yml` mounted a `<service>_gh_config`
volume, unconditionally, for every adopting repo. The CLI half was solved only for `az`,
only inside billet's own image, and only incidentally — because billet needs `az` to manage
Hosts at all.

So the base templates were wrong in both directions at once. Every adopting repo got a
credential volume for a tool it might never call, and no adopting repo got the tool that
volume exists to serve. A repo that took the templates verbatim ended up with the durable
half of a CLI it did not have.

That is the shape of the failure worth recording:

| What a repo has merged | Symptom | When it shows |
| --- | --- | --- |
| Credential volume only | `gh: command not found` — or a hand-installed binary that vanishes | first use, then every rebuild |
| CLI only | `gh auth login` / `az login` demanded again | next rebuild |
| Both halves | works, indefinitely | — |
| Neither | nothing installed, nothing mounted | — |

The two broken rows share the property that makes this an ADR rather than a README note:
**the container works when it is built and fails later.** Neither state is visible in the
PR that creates it, or in the `billet start` that follows it — only in the rebuild after
that, by which time the change is nobody's recent memory.

## Decision

**Auth tooling ships as opt-in recipes under `templates/workspace/auth-tooling/`, one pair
of snippets per CLI. The base templates go back to being sshd-only. A Workspace merges
`gh`, `az`, both, or neither — and merging a recipe means merging both of its halves.
Nothing about this enters billet the tool.**

### Nothing goes into billet

Two rules already in force point the same way. ADR-0005 draws the line at instances billet
describes in its registry: it owns their lifecycle and merely *adopts* everything durable
around them. ADR-0002 §1 puts `devcontainer.json` and the compose stack it names on the far
side of that line — billet reads them as a data contract and does not write them.

Installing `gh` into a Workspace would make billet the owner of an image it does not build;
reading `~/.config/gh/hosts.yml` would make it the custodian of a credential whose
rotation, revocation and audit it has no business owning — the same reason
[ADR-0006](adr-0006-claude-token-injection.md) has billet hold a *command* that prints a
token and never the token itself. So: **billet neither installs these CLIs nor reads their
credentials.** What ships is a documented recipe plus copyable snippets, with exactly the
status every other file under `templates/workspace/` has — adopted by hand, applied to an
adopted repo by nothing.

Nor is there a mandatory section in the base templates, which is the softer version of the
same claim. A `gh` volume every Workspace carries puts a tool's storage in repos that never
asked for it, and it outlives the mistake: `docker compose down` does not remove named
volumes, so the artifact of a default nobody chose sits on the Host until someone prunes it
by hand.

### One recipe per CLI, not one parameterized section

`<service>` is the only parameterization these templates have ever needed, and adding a
second axis — a shared "auth tooling" section with per-tool branches to comment in or out —
would make every adopting repo read and edit both tools' fragments in order to take one.
Four small files cost the template directory four files and cost a repo exactly the ones it
uses.

The tools also differ in ways a single section would have to special-case rather than
share. GitHub publishes an already-dearmored keyring and Microsoft an ASCII-armored key
that must go through `gpg --dearmor`; `gh` needs `~/.config` pre-created as well as
`~/.config/gh`, because `install -d` does not apply `-o/-g/-m` to the parents it creates,
while `az` needs only `~/.azure`; `az` gets a telemetry opt-out `ENV` and `gh` has no
equivalent. Per-CLI files let each carry its own commentary at full detail, which is where
the reasoning belongs for something a human merges by hand.

### A recipe is two halves, and both are required

| Half | Fragments | What its absence costs |
| --- | --- | --- |
| The CLI, in the image | `<tool>.Dockerfile.snippet` §A | The binary is installed by hand into `~/.local/bin`, which is on no volume — every rebuild wipes it. Features cannot cover this (below) |
| Its credentials, on a named volume | `<tool>.docker-compose.snippet.yml` plus `<tool>.Dockerfile.snippet` §B | The token is written to the container filesystem, so every rebuild forces `gh auth login` / `az login` again |

`gh` mounts `<service>_gh_config` on `~/.config/gh`; `az` mounts `<service>_azure_home` on
`~/.azure`. Both follow the `*_claude_home` pattern ADR-0006 already established: the
credential belongs to the operator, not to the image, so it lives on a volume and never in
a layer. §B of each Dockerfile snippet pre-creates the mountpoint dev-owned 0700, because a
volume mounted onto a path the image never created lands root-owned and the CLI cannot
write to it.

Requiring both halves is a documentation rule, and deliberately not a validated one.
Checking it would mean billet inspecting the repo's image or parsing its Dockerfile —
across the boundary the first subsection just drew — and would still fail closed on a repo
that installs the tool some other legitimate way.

### apt from a GPG-pinned third-party source

Both recipes install from the vendor's own apt repository, keyring under
`/etc/apt/keyrings`, `signed-by=` on the source line. That is the shape the dev-container
Dockerfiles already use for nodesource, for azure-cli and for Debian backports, and
consistency is the whole argument: one convention for third-party sources means one place
to audit key handling, one way to retarget a suite when the base image moves, and no second
update mechanism to remember. The tool's version floats with the pinned repository, exactly
as every other apt package in the image does; reproducibility comes from the base image
tag.

The rejected-but-noted alternative is a **pinned release tarball into `/usr/local/bin`**.
It buys an exact, reproducible version and skips the keyring ceremony, and it costs a
manual version bump forever plus a hand-rolled checksum step to get back the integrity the
apt source gives for free. It is the right trade only for a Workspace that must hold a
specific `gh` or `az` version; the snippets say so where a reader will meet the question.

### Why not a devcontainer feature

A feature is the obvious packaging for "optional tool in a devcontainer", and
`ghcr.io/devcontainers/features/github-cli` already exists. It does not run here. billet
composes the stack with `docker compose`, never the devcontainer CLI, so a repo that puts
`gh` in a `features` block gets it when someone opens the folder in VS Code and does not
get it under `billet start` — the exact trap the adoption guide's "features are not
applied" warning exists to name. Supporting features would mean requiring the devcontainer
CLI on every Host and building through it, which is a far larger claim on the repo's build
than reading five fields out of a JSON file.

`postCreateCommand` is the other hook billet does run, and it is a real option for the CLI
half rather than an impossible one — which is why it is rejected on cost, not capability.
It moves a build-time concern to run time: every cold `start` pays the apt fetch, an
upstream outage turns into a start failure rather than a stale image, and the tool is
absent from a container brought up any other way. An image is where a tool belongs.

## Consequences

- A Workspace that never calls either CLI now carries neither: no apt source, no image
  weight, no volume. That is the majority case, and it is the case the base templates
  should serve.
- The failure that motivated this ends for a repo that adopts a recipe: binary in the
  image, credential on a volume, rebuild-proof on both sides, one login ever.
- **This is a breaking change for a repo that already merged the unconditional `gh` volume
  from the 2026-09-05 templates.** Nothing is removed from an adopted repo automatically,
  so such a repo keeps working exactly as it does today — but its `.devcontainer/` now
  differs from the base template, and re-copying the base `Dockerfile.snippet` §2 layer and
  `docker-compose.snippet.yml` *alone* would silently drop the `gh` mount, its top-level
  `volumes:` declaration and the `~/.config/gh` mountpoint. Nothing fails at merge time;
  the symptom arrives one rebuild later as `gh auth login`. Such a repo should keep its
  existing `gh` wiring or adopt the `gh` recipe deliberately — the recipe is that same
  wiring plus the binary it was always missing. The templates' revision log carries the
  instruction, because the revision log is the only version marker these templates have.
- billet's own `.devcontainer/` implements **both** recipes — it needs `az` to manage Azure
  Hosts and `gh` for its pull-request workflow — so the reference implementation still
  exercises every fragment shipped here. That is what keeps the snippets honest: they are
  extracted from a Workspace that runs, not written against one.
- **The credential is stored in plain text on the volume**, as `gh` itself warns at login;
  `az`'s token cache is no better. The volume is therefore exactly as private as the Host
  it lives on, and a Host is shared by every Workspace placed on it — anyone with a shell
  there can reach another Workspace's volume through the daemon. This is the same exposure
  ADR-0006 accepts for the injected `~/.claude/settings.json`, and it is acceptable for the
  same reason: a single-operator dev Host. It would not be acceptable on a multi-tenant
  one, and that is the constraint to revisit first if Hosts ever stop being single-operator.
- Adopting a recipe costs one login. Nothing migrates a running container's existing
  credential onto the fresh volume, so the first `gh auth login` / `az login` after the
  merge happens one more time and never again.
- The recipe set grows by adding a pair of files. A third CLI does not re-litigate a
  template section, and no repo has to re-merge anything in order to ignore it.

## Alternatives considered

- **Keep the `gh` volume in the base templates and add the binary there too.** Rejected:
  it hands every Workspace a GitHub CLI and a credential store to serve the subset that
  runs `gh`, and the argument that admits `gh` admits `az` next, and then whatever follows.
  The base templates are the sshd contract — the one thing every Workspace genuinely needs.
- **Put both CLIs in billet's own image and let Workspaces inherit them.** Rejected: they
  do not inherit anything. billet's image is billet's own Workspace, not a base for the
  others; each repo builds from whatever base its toolchain requires.
- **Ship it as a devcontainer feature.** Rejected in Decision §"Why not a devcontainer
  feature": features do not run under `billet start`, so the tool would be present in VS
  Code and absent on the Host — a difference that shows up as a broken workflow, not as an
  error.
- **Install the CLI from `postCreateCommand` instead of baking it into the image.**
  Rejected on cost: it pays an apt fetch on every cold start, converts an upstream outage
  into a failed `billet start`, and leaves the tool missing from any container not brought
  up through billet.
- **A billet config key — `[workspaces.<key>] auth_tools = ["gh"]` — that injects a compose
  override.** Rejected: it makes billet write part of the repo's container definition,
  crossing ADR-0005's ownership boundary and ADR-0002 §1's read-only data contract in a
  single step. The repo's compose file would stop describing its own container, and the
  Workspace subsystem would grow a code path for a problem that a copy-paste already
  solves.
- **A pinned release tarball into `/usr/local/bin` as the default install method.** Noted
  in Decision §"apt from a GPG-pinned third-party source" and rejected there: a permanent
  manual version bump and a hand-rolled checksum, in exchange for pinning that only a
  version-sensitive Workspace actually needs.
- **Validate at `start` that a repo mounting `<service>_gh_config` also installs `gh`.**
  Rejected: it requires inspecting the image or the Dockerfile — the ownership line again —
  and it would refuse a repo that installs the tool some other legitimate way. The pairing
  is enforced by the recipe README and the revision log, where a human merging by hand will
  actually read it.
