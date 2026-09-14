# ADR-0014: What billet reads and writes — the definition/state boundary

## Status

Proposed (2026-09-14). Restates the boundary that [ADR-0002](adr-0002-workspace-subsystem.md)
§1, [ADR-0005](adr-0005-instance-lifecycle-ownership.md) and
[ADR-0011](adr-0011-optional-auth-tooling-recipes.md) each describe from one side, so that the
writes [ADR-0006](adr-0006-claude-token-injection.md) already performs and the repair
[ADR-0013](adr-0013-mountpoint-ownership-repaired-at-mount-time.md) adds are inside a stated
rule rather than exceptions to an unstated one. Grants **no new read**: reading the consumer's
compose files is proposed in [ADR-0015](adr-0015-billet-doctor.md) and takes effect when
`doctor` lands.

## Context

Three ADRs read as a prohibition on billet touching a repo's container:

- ADR-0002 §1: billet reads five fields of `devcontainer.json` through `_facts_from_json`
  (`compose_container_access.py`) and nothing else; the repo owns its `.devcontainer/`.
- ADR-0005: billet adopts durable infrastructure it does not own; it never grows toward owning
  what the repo or the platform owns.
- ADR-0011: "billet neither installs these CLIs nor reads their credentials"; editing the repo's
  compose file is "rejected outright" (ADR-0003).

And billet writes into the container and the checkout today, deliberately:

- ADR-0006 writes `~/.claude/settings.json` inside the running container
  (`_claude_token_injection` and `build_claude_merge_program` in
  `compose_container_access.py`) and `.claude/settings.local.json` into the repo checkout on
  the Host (the agent-teams block of `_compose_up_script`).
- ADR-0013 repairs the ownership of `~/.claude` before that write, and the Berth entrypoint
  billet publishes repairs every root-owned empty Locker at start.

The stronger reading — "billet never writes anything in a repo's container" — is not the rule
the code obeys, and a reader who holds it will cite ADR-0005 or ADR-0011 against ADR-0006 and
ADR-0013. The research that preceded this cycle did exactly that twice. The rule that actually
holds is narrower and needs to be written down.

A separate confusion was found in the shared image's ADR-0001, which keeps billet's runtime
files in product repos because "billet's contract reads them from there". billet `cat`s one
file. Docker and Compose read the entrypoint, `sshd.conf`, the stub and the compose file. That
sentence is corrected in genshift-devcontainer's ADR-0002; it is mentioned here because it is
the same category error from the other side: conflating what defines a container with what
billet touches.

## Decision

**billet may write runtime *state* into a container it started, and per-tool state files in
the checkout that no build reads. billet never writes a container's *definition*: any file
Docker, Compose or the devcontainer tooling reads to build the image or create the container.**

1. **Definition** means, exhaustively for a billet Workspace: the Dockerfile and anything it
   `COPY`s or `ADD`s, `docker-compose.yml` and every file it includes or interpolates from
   (`.env`), `devcontainer.json`, and the Berth files the repo copied (`dev-entrypoint.sh`,
   `sshd.conf`, `authorized_keys-stub`, `berth.version`). billet reads exactly five fields of
   one of these (ADR-0002 §1) and writes none. Publishing a template a human copies is not
   writing a definition; the copy is the repo's act.

2. **State** means files that tools running *inside* the container consume at runtime and that
   no build step reads: `~/.claude/settings.json`, the ownership bits of a Locker mount target,
   `/etc/environment` as rendered by the entrypoint, sshd host keys on their volume. billet may
   write state into a container it started, via `docker compose exec` as the login user, using
   sudo only for the ownership repair ADR-0013 specifies. The `.claude/settings.local.json`
   write into the checkout is state by this test — Claude Code reads it at runtime, no build
   does — and is the one checkout write billet performs.

3. **Two properties every state write must have.** It is idempotent (re-running `start` yields
   the same file), and it is *about billet*: it configures the tool billet is delivering (the
   token, the flag) or makes that delivery possible (the ownership repair). billet does not write
   a tool's credentials store, a shell profile, or a dotfile; those belong to the operator's
   dotfiles (ADR-0008/0009) or the tool itself.

4. **Reading is separately gated.** This ADR does not widen what billet reads. billet resolves
   the compose file *paths* today (`_facts_from_json` re-roots the `dockerComposeFile` entries
   onto `DevcontainerFacts.compose_files`) and does not open them;
   grep over `src/billet/` finds no compose or Dockerfile read. Opening them is a new capability
   and is proposed in ADR-0015 for `doctor` alone, warn-only, with Dockerfile parsing forbidden.
   Until that ADR's code lands, the grant is not in effect. Granting a read that nothing
   exercises would reintroduce the drift between ADR text and code that this ADR exists to end.

5. **ADR-0011's conclusion holds; its reasoning is restated.** billet still does not validate
   recipe pairing at `start`. The reason is no longer ownership ("billet may not read repo
   files", which item 4 shows is not the line) but sufficiency: after ADR-0013 the pairing
   invariant that mattered is satisfied by construction, the residual case (volume without CLI)
   costs a directory, and where it matters the container answers exactly (`command -v gh`),
   which is what `verify_cmd` exists for. Do not gate; make the gate unnecessary.

## Consequences

- ADR-0006 and ADR-0013 are inside a stated rule. A future reader checking "may billet do X in
  the container?" asks one question: does a build read it? If no, and the write is idempotent and
  about billet's own delivery, yes.
- The `connect` invariant of ADR-0009 (three lookups, no cloud call, nothing written) is
  untouched; state writes happen only in `start`.
- Consumers keep the guarantee ADR-0003 gave them: billet will never edit their compose file, so
  a consumer PR can never be "what did billet change in my repo?".
- The genshift-devcontainer ADR-0001 sentence is corrected in that repo, which is the
  precondition for a later ADR-0016 (Berth baked into the image): once it is clear that Docker,
  not billet, reads the entrypoint from the repo, the entrypoint may live in the image and be
  addressed by an absolute path.

## Alternatives considered

- **Keep the strong reading and treat ADR-0006/0013 as enumerated exceptions.** Rejected: an
  exception list grows silently and is exactly what a reader forgets to check. A line that can be
  applied to a new case is better than a list.
- **Grant the compose read here, now.** Rejected (operator decision Q20): grant it beside the
  code that uses it.
- **Move the token write out of billet into the dotfiles.** Rejected in ADR-0006 already; the
  token is per-operator secret material billet already holds and the dotfiles are public.
