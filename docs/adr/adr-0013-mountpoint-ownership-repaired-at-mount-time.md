# ADR-0013: Mountpoint ownership is repaired at mount time, not guaranteed at build time

## Status

Accepted (2026-09-14). Part of Berth 1 ([ADR-0012](adr-0012-the-berth.md)). Amends
[ADR-0011](adr-0011-optional-auth-tooling-recipes.md): a recipe has two parts (binary,
volume), not three, and "both halves required" becomes "the image-side mountpoint is
unnecessary". Amends [ADR-0006](adr-0006-claude-token-injection.md): the token injection makes
its own target writable before writing. Records a **first** decision: billet has never had a
mount-time repair (see Context), so nothing here supersedes an earlier billet choice.

## Context

**The Docker behavior.** When a named volume is mounted at a path inside a container, the
directory the process sees takes its ownership from the volume's initialization. Docker
documents that image content at the path is copied into the volume *if the volume is empty*,
and that `nocopy` suppresses this. Docker documents nowhere what uid/gid results when the image
has no directory at that path. Observed on a billet Host on 2026-09-10 with `debian:bookworm-slim`
fixtures:

| Cell | Image has the directory dev-owned? | Volume state | Result inside the container |
|---|---|---|---|
| 1 | no | fresh | `root root 755` — `dev` cannot write `[VERIFIED]` |
| 2 | yes | fresh | `dev dev 700` — the working row every consumer relies on today `[OBSERVED in production; not yet run as a fixture cell]` |
| 3 | yes | previously initialized empty and root-owned by an image without the directory | `dev dev 700` — copy-on-empty re-applies ownership on every start that finds the volume empty `[VERIFIED]` |
| 4 | no | root-owned, **populated** | unchanged — nothing re-owns it `[INFERRED from the copy-on-empty rule]` |
| 6 | no | fresh, then `install -d -o dev -g dev -m 0700` run once inside a container | `dev dev 700` in a **second, fresh** container; `dev` writes `[VERIFIED]` |

Cell 1 is the failure. Cell 6 is the fix. Cell 3 is the migration path for volumes already
broken by cell 1.

**Where the two halves live.** Whether a consumer hits cell 1 or cell 2 is decided by two
files: the image's Dockerfile (does it `install -d -o dev` the path before `USER dev`?) and the
consumer's compose file (does it mount a volume there?). For a repo that builds its own image
both are in one repository and one PR can see both. For a repo whose `.devcontainer/Dockerfile`
is a single digest-pinned `FROM` line against the shared toolchain image, the two halves are
authored in different repositories on different release cadences, and the invariant is enforced
by nothing: no error at build, no error at `compose up`, a `Permission denied` from the tool at
first use. genshift-brand is in that position today, in the working row, held there by a compose
comment that says "the image pre-creates this".

**How billet arrived here.** billet's own `.devcontainer/Dockerfile` has pre-created `.claude`,
`.azure` and `.ssh` dev-owned since the first devcontainer commit (`efaa84e`, 2026-07-02). The
templates inherited the pattern on 2026-07-03 as pure addition. PR #74 moved the `gh` and `az`
lines into opt-in recipes, same layer. The stated reason for build-time placement, repeated in
ADR-0011, the recipe README and both recipe snippets, is that `install -o dev` needs root and so
must precede `USER dev`. That reasoning never barred the entrypoint: `dev-entrypoint.sh` already
runs `sudo install` three times (for `/run/sshd`, the host-key directory and
`/etc/environment`) under the passwordless sudo the Berth requires. The entrypoint route was not rejected; it was never considered, because in billet's
model every adopting repo built its own image and build time was always available. The shared
image is the case where that assumption stops holding.

gswa-backend reached the other answer independently: its entrypoint runs `sudo install -d -m
0755 -o dev -g dev /home/dev/.azure` at start (in its own `.devcontainer/dev-entrypoint.sh`), with a
comment that a freshly created volume "comes up empty (and possibly root-owned), shadowing the
image". The same idiom is universal in this ecosystem: the devcontainer CLI's
`updateRemoteUserUID`, jupyter docker-stacks' `CHOWN_EXTRA`, linuxserver's `PUID`/`PGID`, VS
Code's own recommended `postCreateCommand: sudo chown node node_modules`.

**A second consumer of the invariant inside billet.** ADR-0006 writes
`~/.claude/settings.json` into the running container with `docker compose exec -u dev` on the
line after `docker compose up -d --build`, with no wait between them
(`_compose_up_script` in `compose_container_access.py`). `up -d` returns when PID 1 starts; on a cold start the
entrypoint then spends seconds generating a 4096-bit RSA host key before it reaches anything
else. Today the write succeeds because the image seeded the volume dev-owned before `up`
returned (cell 2). Dropping the explicit `install -d` line does not by itself end that: in both
the shared image and billet's own, the Claude Code installer runs as `dev` and leaves a
populated `/home/dev/.claude` at 0755, so copy-on-empty still seeds a fresh `*_claude_home`
volume dev-owned and the repair correctly skips it as already dev-owned. The mode is the tell —
0755 came from the image, 0700 from the repair. What item 6 defends against is therefore
narrower than "the image stops pre-creating the directory" reads: that seeding is an installer
side effect rather than a guarantee, and against an image that ships no populated `~/.claude` —
the state `~/.azure` and `~/.config/gh` are already in — the exec lands on a root-owned
`~/.claude` while the entrypoint is still in `ssh-keygen`; the program's `if not
claude_dir.exists()` guard skips its own `mkdir`, `tempfile.mkstemp` raises `PermissionError`
outside the `try`, and `billet start` aborts with a traceback. Moving the guarantee into the
entrypoint alone would therefore leave the one write billet itself performs resting on that side
effect.

## Decision

**The Berth entrypoint repairs the ownership of named-volume mount targets under the login
user's home at container start, for targets that are root-owned and empty, and warns about
every other anomaly. Images stop pre-creating Locker directories. The one write billet
performs into a container makes its own target writable first.**

1. **Discovery.** Before generating host keys, the entrypoint reads `/proc/self/mountinfo` and
   selects entries whose mountpoint is under `$HOME/` and whose mount root identifies a Docker
   named volume (`*/volumes/*/_data`). Bind mounts (the workspace checkout, the `authorized_keys`
   file) are skipped by that test. Nothing is configured by the consumer; the compose `volumes:`
   line is the only declaration a Locker has.

2. **Policy, per target.**

   | Target state | Action |
   |---|---|
   | directory, owned by uid 0, **empty** | `sudo -n install -d -o <uid> -g <gid> -m 0700 <target>`; log `repaired <path> (was root:<group> <mode>)` |
   | directory, owned by uid 0, populated | warn `<path> is root-owned and not empty; not repaired` |
   | directory, owned by another uid | warn `<path> owned by uid <n>; not repaired` |
   | directory, owned by the login user | leave alone, mode included |
   | directory that cannot be `stat`ed | warn `cannot stat <path>; not repaired` |
   | directory whose entries cannot be listed | warn `cannot read <path>; not repaired` |
   | missing, where the rule is applied with creation (item 3) | `sudo -n install -d -o <uid> -g <gid> -m 0700 <path>`; log `created <path>` |
   | not a directory, or missing without creation | skip silently |
   | repair or creation fails | warn `repair of <path> failed; continuing` |

   Every line above is emitted with a `dev-entrypoint: ` prefix; the warnings carry a further
   `warning: ` and go to stderr. A root-owned, **0700**, empty directory is reported by the
   `cannot read` row rather than repaired: the entrypoint runs as the login user, so it cannot
   list the directory and so cannot establish that it is empty. That state is unreachable for a
   mountpoint Docker creates — those come up 0755 (cell 1) — so every target this decision
   exists for still takes the repair row; it can only arise if something other than the daemon
   created the path root-owned 0700.

   Never recursive. Never `chown -R`. Never abort: sshd is the operator's recovery path, so the
   entrypoint continues to start it (the posture #62 chose for host-key persistence). Every repair
   actually performed is logged, so an image or consumer relying on the repair is visible in
   `docker compose logs` rather than silently working.

3. **`~/.ssh` is ensured by the same rule** applied to a known path: created dev-owned 0700 if
   missing (logged `created <path>`), repaired if root-owned and empty, otherwise left alone.
   `~/.ssh` is Berth
   infrastructure (sshd's `authorized_keys` bind mount lives under it), not a Locker.

4. **Order.** The repair block runs after `/run/sshd` and the host-key directory are created and
   **before** `ssh-keygen`, so the window in which a Locker is root-owned is the smallest the
   entrypoint can make it.

5. **Images stop pre-creating Locker directories.** genshift-devcontainer removes `.claude` and
   `.config/gh` from its Dockerfile and keeps `.ssh` (item 3) and `.config`. billet's own
   `.devcontainer/Dockerfile` likewise keeps `.ssh` and `.config` and drops `.claude` and
   `.azure`, so billet's own Workspace is the first consumer of the repair rather than the
   last. Neither retained `.config` is a Locker mountpoint: it is the *parent of* one
   (`~/.config/gh`), so the daemon would create it root-owned and the repair in item 1 can
   never see it — a parent of a mountpoint does not appear in `/proc/self/mountinfo`. Widening
   the repair to root-owned empty parents under `$HOME` would change the Berth contract and
   belongs to Berth 2. The base `Dockerfile.snippet` keeps `.ssh` only. The two recipe
   Dockerfile snippets lose their §B mountpoint lines: **a recipe is a binary and a volume**.
   The tests that encoded the build-time model
   (`test_mountpoints_are_created_before_dropping_to_the_non_root_user`,
   `test_a_recipe_pairs_its_volume_with_a_dev_owned_mountpoint`) are retired; the test that the
   base templates carry no CLI-specific tooling stays.

6. **ADR-0006's writer ensures its target.** The token-injection exec runs the item 2 policy on
   `$HOME/.claude` before the Python program, in the same `exec -u dev` session, with the token
   still delivered on stdin. The Python program replaces its `not exists()` guard with an
   existence-and-writability check and, on failure, prints a `[billet]`-prefixed error naming the
   directory's owner and the remedy, instead of a traceback. This is a state write into a
   container billet started, inside the line [ADR-0014](adr-0014-definition-versus-state.md)
   draws. A readiness handshake between `start` and the entrypoint is deliberately not added
   here: a consumer on an older Berth would never signal, so `start` would have to time out and
   proceed, and the writer-ensures-target rule holds on every Berth revision without one.

7. **Verification is billet's, in CI.** A five-cell Docker matrix (cells 1–4 above plus cell 6
   with the shipped entrypoint) runs in billet's GitHub Actions as a path-filtered job on
   changes under `templates/workspace/`, `.devcontainer/` and `tests/integration/`, and nightly,
   behind the existing `BILLET_INTEGRATION` gate. Each cell runs `down -v` on its own
   test-scoped compose project before `up`, because ownership is fixed at volume initialization
   and a reused volume invalidates the cell. The nightly run exists because the behavior under
   test is undocumented by Docker and could change under us. It does not run in
   genshift-devcontainer (private repository, metered minutes) because the invariant is billet's.

## Consequences

- A new tool's persistence now touches exactly one component: the consumer's compose file. The
  N+1th Locker is one `volumes:` line and no image release, in any consumer, on or off the
  shared image.
- An already-broken **empty** volume in the fleet is repaired by the next `billet start` that
  runs a Berth 1 entrypoint (cell 6) or by any image that still pre-creates the path (cell 3).
  No operator runs `docker volume rm`. A **populated** root-owned volume is reported, not
  repaired; the operator decides.
- The shared image's 2.0.0 release, which drops Locker pre-creation, is safe only for consumers
  already on Berth 1. Sequencing is recorded in the plan: genshift-brand's Berth 1 PR merges
  before the image release. Existing volumes are unaffected either way (cell 6 shows repaired
  ownership persists; an initialized dev-owned volume is not re-owned by an image lacking the
  path).
- On genshift-devcontainer 2.0.0 the Claude Locker is protected in practice by
  copy-on-empty, not by the repair: the image still ships a populated dev-owned
  `/home/dev/.claude` — created by the Claude Code install, not by an `install -d` line —
  so a fresh `*_claude_home` volume is cell 2 and the repair correctly skips it as already
  dev-owned. `~/.config/gh` has no counterpart in the image and is the one target actually
  repaired. The repair covers `.claude` unchanged if the image ever stops shipping it.
- ADR-0011's failure table loses its "credential volume only → unwritable" row; the residual
  broken row is "volume without CLI", which costs an empty directory and is caught by
  `verify_cmd` if it matters.
- gswa-backend's `.azure` entrypoint line becomes redundant and is removed in its Berth 1 PR;
  its `az devops configure --defaults` step stays. Its existing volume keeps mode 0755: the
  policy never touches a dev-owned directory.
- billet's CI gains Docker for the first time. The job is path-filtered so a Python change in
  `billet.host` does not pay for it.

## Alternatives considered

- **Grow the image's superset of pre-created mountpoints as declared policy** (the synthesis's
  option 2). Covers known tools only; the N+1th still needs two ordered PRs in two repositories;
  requires a "toolchain and nothing else" image to enumerate directories for tools it does not
  ship. Demoted to unnecessary once the repair exists, and removed rather than kept as an
  optimization because a pre-created mountpoint in the reference implementation tells the next
  reader it is still required.
- **`docker volume create --opt uid=`.** Would be the correct fix; does not exist (moby#45714,
  open).
- **compose `user:` or `tmpfs`.** `user:` changes the process, not the volume's initial
  ownership; tmpfs does not persist.
- **A readiness marker polled by `start` before the injection.** Correct on Berth 1, but every
  consumer on an older entrypoint never writes it, so `start` must time out and proceed, paying
  the timeout on every start of an old Berth. Deferred to ADR-0015's CONFORM step where it is
  reported rather than gated on.
- **Deliver the token over the sshd hop instead of `exec`.** sshd listening implies the
  entrypoint finished, but this changes ADR-0006's mechanism and inherits the hop's
  `ConnectionAttempts=5` retry, which a cold RSA keygen can already outlast.
- **`chown -R` on every mount target.** Rejected: touches contents the consumer owns, and is the
  wrong call for the populated-root-owned case where the honest output is a warning.
