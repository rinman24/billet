# ADR-0007: `start` converges a Workspace's checkout to upstream (clean-only fast-forward)

## Status

Accepted (2026-07-15). Refines [ADR-0005](adr-0005-instance-lifecycle-ownership.md): the
Host checkout billet creates via `ensure_clone` is instance-scoped state billet owns, so
converging it to upstream on `start` is instance lifecycle — while the clean-only guard is
ADR-0005's "adopt, don't own" half applied to a checkout an operator or bootstrap has
touched.

Amended (2026-09-04): every git the emitted script runs on the Host is now non-interactive,
and the script reports — without rewriting — drift between the checkout's `origin` and the
configured `repo_url`. The clean-only, ff-only convergence rule recorded here is unchanged —
see [Amendment (2026-09-04)](#amendment-2026-09-04-non-interactive-git-on-the-host) below.

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

## Amendment (2026-09-04): non-interactive git on the Host

### A git that CAN prompt does not fail — it hangs

`billet start` is unattended, and the remote script runs behind a captured `ssh -t` channel:
nothing the Host writes while the command is in flight reaches the operator's terminal. Any
credential prompt therefore becomes a silent stall. Observed on a Host checkout whose
`origin` had been switched to an HTTPS URL: the `git fetch --prune` above sat at `Username:`
for minutes, with an empty screen and no exit code. The failure mode is not a poor error
message — it is the absence of one, for an unbounded time.

The ADR above names this hazard once already: `git pull` was rejected partly because
conflict resolution "turn[s] an unattended `start` into an interactive one." That reasoning
was applied to the merge step only. The two steps that talk to the network — where the
credential prompt actually lives — were left interactive, and none of the guards above help:
the hang happens before the first probe is reached.

### The fix: non-interactive by construction

The emitted script exports two variables before any git runs, so they cover the clone and
the fetch alike:

```bash
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
```

`GIT_TERMINAL_PROMPT=0` removes git's own `Username:`/`Password:` prompt (the HTTPS path);
`BatchMode=yes` removes ssh's passphrase and host-key confirmation prompts (the ssh path).
Together they convert every "waiting for a human" state into a non-zero exit. **A credential
git cannot obtain is now a fast, legible failure rather than a hang.**

This supersedes one line in the Consequences above: the first-clone path is no longer
unchanged. It is wrapped and it runs under the same two variables — deliberately, because a
first clone against a remote it cannot authenticate to hangs exactly like a fetch does.

### Why `accept-new` rides along, and is not a loosening

`BatchMode=yes` on its own also refuses an **unknown** host key: with no way to ask "are you
sure you want to continue connecting?", ssh declines the connection. On a Host that has never
met the forge that would break the *first* clone — trading one hang for a new failure, in the
one case where nothing was wrong.

`StrictHostKeyChecking=accept-new` is therefore set alongside it, and it changes nothing
about billet's posture: it is the same option `billet.infrastructure.ssh` puts on every ssh
argv billet builds (`_ACCEPT_NEW`). An unknown key is accepted once and recorded; a
**changed** key still fails. That second half is the property that matters, and it is the
existing trust-on-first-use bargain carried onto the Host-to-forge hop rather than a new
concession. The looser `StrictHostKeyChecking=no` is rejected: it also accepts a *changed*
key, which is the exact case the check exists for.

### Failures name their cause; a fetch failure still aborts `start`

Clone and fetch are each wrapped so a non-zero exit first prints a `[billet/source]` line
naming the cause — git ran non-interactively and so could not ask for a credential or a key
passphrase, and an `https://` remote cannot authenticate over the forwarded agent — and then
aborts.

The Decision above is unchanged in substance: **a fetch failure still aborts `start`; only
the *advance* is best-effort.** What changed is the shape of the abort. It is now seconds and
a cause, where before it was a stall with nothing on screen.

### Origin drift is reported, never rewritten (ADR-0005)

Before the fetch, the script reads `git remote get-url origin` and compares it with the
configured `repo_url`, printing a `[billet/source]` warning when the two differ — or when the
checkout has no `origin` at all. It does **not** rewrite the remote, and does not add a
missing one.

A checkout with no remote at all is not an error either: `git fetch --prune` in a repo with
nothing configured is a silent no-op that exits 0, so the run falls through to the "no
upstream" skip and ends at exit 0. The warning is the whole signal — without it, a Workspace
that had quietly stopped tracking anything would look identical to one that was already up
to date.

This is [ADR-0005](adr-0005-instance-lifecycle-ownership.md) applied to a remote. billet owns
the checkout it created, but a remote an operator repointed — at a fork, a mirror, an
internal proxy — is *adopted* state, exactly like the dirty tree the clean-only guard refuses
to arbitrate. billet reports it and leaves it alone. Rewriting `origin` would silently undo a
deliberate operator change and erase the very condition the warning exists to surface; it
would also make billet a writer of Host-side git state, which nothing else in the script is.

The ordering is load-bearing: the warning prints **before** the fetch, not after it. Drift is
usually the cause of the failure that follows, so it has to be on screen by the time the
fetch aborts — printed afterwards it would never be reached, since the fetch's failure exits
the script.

### `ensure_clone` surfaces the Host's own output

`ensure_clone` now runs the ssh command with `check=False` and raises `ProcessError` itself,
built from the captured stdout **and** stderr rather than stderr alone.

The reason is the pty. The script runs under `ssh -t`, which gives the remote a terminal and
merges its stderr onto the same channel as its stdout — so the Host's diagnostics arrive on
the *client's stdout*, while the client's stderr carries only ssh-level noise. The previous
error view (`ProcessError` raised by the runner from `stderr`) therefore rendered an empty
tail for precisely the failures worth reading, including the two cause lines this amendment
adds. Both streams are kept, in the order they were produced.
