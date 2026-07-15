# ADR-0007: `start` converges a Workspace's checkout to upstream (clean-only fast-forward)

## Status

Accepted (2026-07-15). Refines [ADR-0005](adr-0005-instance-lifecycle-ownership.md): the
Host checkout billet creates via `ensure_clone` is instance-scoped state billet owns, so
converging it to upstream on `start` is instance lifecycle — while the clean-only guard is
ADR-0005's "adopt, don't own" half applied to a checkout an operator or bootstrap has
touched.

## Context

`ensure_clone` (`billet.access.source.GitSourceAccess`) emits a remote bash script, run over
an agent-forwarded `ssh -tA` as the Host admin user, that placed source on the Host. On
first use it clones; otherwise it ran only `git -C "$REPO_DIR" fetch --prune`.

Fetch updates the remote-tracking refs but never touches the working tree. So once a repo
was cloned, `billet start` never advanced the checked-out branch. Changes merged to the
repo's default branch — `devcontainer.json` `workspaceFolder`, the `Dockerfile`, the compose
files — silently never took effect: `compose up --build` rebuilt from the stale tree, and the
only recovery was a manual `git pull` on the Host or an `rm -rf` + re-clone. For a fleet
whose whole value is one-command convergence, a `start` that ignores merged infrastructure
changes is a latent footgun.

The naive fix — `git reset --hard @{u}` after fetch — is unacceptable here. Host-side state
is real and must survive `start`:

- `host_bootstrap_cmd` writes **untracked** files before every `compose up` (squadra does
  `cp -n .devcontainer/.env.example .devcontainer/.env`, which wires the real
  `authorized_keys` path on first cold start). That `.env` is permanent and untracked.
- Repos accumulate per-session artifacts in the working tree.

A hard reset — or any dirty check that counts untracked files — would either destroy that
state or, in the `.env` case, block the advance forever (the file is never going away).

## Decision

**After the fetch, `start` fast-forwards the checked-out branch to its upstream, but strictly
non-destructively and idempotently. It advances ONLY when every guard passes; on any skip it
prints a one-line `[billet/source]` warning and exits 0 — a checkout it cannot safely advance
is adopted state, never a reason to fail the `start` lifecycle. There is no config flag: the
behavior is default-on.**

The emitted script's already-present branch becomes:

```bash
cd "$REPO_DIR"
git fetch --prune
branch=$(git symbolic-ref --quiet --short HEAD || true)
if [ -z "$branch" ]; then
  # detached HEAD → skip + warn
elif ! upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
  # no upstream → skip + warn
elif [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  # dirty TRACKED files → skip + warn
elif git merge --ff-only '@{u}'; then
  # advanced (or already up to date)
else
  # diverged / non-ff / untracked file would be overwritten → skip + warn
fi
```

### Why clean-only, ff-only, and `--untracked-files=no`

- **`git merge --ff-only @{u}`** is the whole mechanism. It is a no-op ("Already up to
  date") when the branch is current or ahead — it *never rewinds* — fails cleanly when the
  branch has diverged, and aborts non-destructively when an untracked file would be
  overwritten by the checkout. It only ever moves HEAD forward along a linear history, which
  is exactly "adopt merged upstream changes, change nothing else."
- **`--untracked-files=no`** on the dirty check is load-bearing, not an optimization: the
  squadra `.env` is a permanent untracked file, so a dirty check that included untracked
  files would treat every clean checkout as dirty and skip forever. Only *tracked* changes
  block the advance.
- **The guards are ordered probes, each in a conditional**, so a non-zero probe cannot trip
  `set -euo pipefail`, and the `merge` is guarded the same way so its failure warns and
  continues rather than killing the script.

### Why default-on, no flag (against ADR-0005)

The checkout at `~admin/<repo_dir>` is **instance-scoped state that billet itself created**
via `ensure_clone`. ADR-0005 draws its line at *ephemeral instances vs. durable
infrastructure*: billet owns the full lifecycle of instances in its registry. Converging
that checkout to upstream on `start` is instance lifecycle — the same reconcile-on-every-`up`
posture as `ensure_tags`. A flag would imply the convergence is optional policy; it is not,
any more than "start the VM" is.

The clean-only/ff-only guard is the other half of ADR-0005 — "adopt, don't own." The moment
a checkout carries dirty tracked files, a diverged branch, a detached HEAD, or no upstream,
it has been touched by an operator or a bootstrap: that is **adopted state**, and billet
leaves it strictly alone with a warning rather than converging it. billet owns the state it
created and can safely fast-forward; it refuses to arbitrate anything a human or a hook has
changed.

Because there is no flag, `config.example.toml` is untouched and the change is fully
backward compatible: existing Workspaces gain the advance on their next `start` with no
config edit.

## Consequences

- Merged changes to a repo's default branch (devcontainer/Dockerfile/compose) take effect on
  the next `start` without a manual `git pull` or re-clone.
- Untracked host-side state — the squadra `.env`, per-session artifacts — always survives; it
  is never counted as dirty and `--ff-only` aborts rather than clobber it.
- `start` never fails because of the advance: every skip path exits 0 with a
  `[billet/source]` warning naming what was skipped and why (detached, no upstream, dirty
  tracked files, non-ff/diverged).
- The first-clone path is unchanged; the whole script stays idempotent.
- A `fetch` failure still aborts `start` (unchanged) — only the *advance* is best-effort.

## Alternatives considered

- **`git reset --hard @{u}` after fetch.** Rejected: destroys uncommitted and untracked
  Host-side state, including the permanent squadra `.env` — the exact state ADR-0005 says
  billet adopts and must not own.
- **`git pull` (fetch + merge, non-ff-only).** Rejected: it can create merge commits and
  invoke conflict resolution on the Host, turning an unattended `start` into an interactive
  or divergent one. `--ff-only` fails cleanly instead.
- **A config flag to opt in/out.** Rejected: converging billet-created instance state is
  lifecycle, not operator policy (ADR-0005); a flag would misframe it as optional and add a
  knob with no real second setting anyone should choose.
- **Include untracked files in the dirty check.** Rejected: the permanent untracked `.env`
  would block the advance on every start forever — the feature would never fire on the fleet
  it exists for.
