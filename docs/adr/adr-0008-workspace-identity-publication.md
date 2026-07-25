# ADR-0008: billet publishes Workspace identity; the dotfiles own presentation

## Status

Accepted (2026-07-25). Applies [ADR-0002](adr-0002-workspace-subsystem.md) §3's ownership
model — a tool-owned artifact plus one include seam, never in-place mutation — to the tmux
status bar, and extends [ADR-0005](adr-0005-instance-lifecycle-ownership.md)'s "adopt, don't
own" boundary from cloud infrastructure to operator-owned *configuration*. Decision only: no
behavior changes with this ADR; implementation follows in separate PRs.

Amended (2026-07-25): the operator's dotfiles no longer load catppuccin/tmux or any plugin
manager — the status bar is a hand-rolled config
([`rinman24/dotfiles`](https://github.com/rinman24/dotfiles) PR #14). The decision and all six
rules are unaffected; the ownership boundary is theme-agnostic by construction, which is
precisely what this amendment demonstrates. The catppuccin specifics in Context and
Alternatives below are retained as the historical motivation — that collision is why the
boundary was drawn, and the record is less useful without it.

## Context

`billet connect` brands a Workspace's tmux status bar so an operator can tell otherwise
identical container shells apart. `WorkspaceManager.connect_target` asks `TmuxStatusEngine`
for a prelude of `set -g status-style`, `set -g status-left`, `set -g status-left-length` and
splices it into the same `tmux` invocation *ahead* of `new-session -A`; the per-Workspace
`status_color` is operator intent read (and hex-validated) from the registry.

The prelude's *position* is correct and stays. Live testing against tmux 3.7b confirms the
reasoning already documented in `tmux_status_engine.py`: the config file is fully sourced
before the client's command-line `set -g` commands run — on both the cold-start and the
`new-session -A` re-attach path — and any plugin manager the config invokes sources
synchronously, so there is no race.
billet always wins.

Winning is the problem. The same testing, against real catppuccin checkouts, found exactly
one true collision and it is caused by billet writing an option the theme owns:

- catppuccin v2.3.0 **does** set `status-style` (`catppuccin_tmux.conf:5`) and caches the bar
  background in `@_ctp_status_bg`, then draws its separators as `#[fg=#{@_ctp_status_bg}]█`.
  billet overwriting `status-style` does not update that cached value, so the bar recolors but
  the separators keep painting notches in the theme's old background color.
- catppuccin v2 does **not** set `status-left`/`status-right`; it exposes ~70
  `@catppuccin_status_*` user options the operator composes. The operator's dotfiles
  ([`rinman24/dotfiles`](https://github.com/rinman24/dotfiles)) at the time set `status-left ""`
  and built `status-right` from those modules — so `status-left` was deliberately vacated and
  billet's label there was non-destructive.

This is not hypothetical. `docs/adopting-a-repo.md` currently tells operators: *"Pick one —
either drop `status_color` from the Workspace block, or don't load catppuccin/tmux."* A
documented mutual exclusion between the tool and the user's own config is the design defect
this ADR resolves.

**billet already made this decision once, and made it the other way.** The lifted
`install-ssh-config.sh` wrote a marker-delimited block *into* `~/.ssh/config`. ADR-0002 §3
deliberately moved away from that: billet now fully owns a separate generated file
(`~/.ssh/config.d/billet.conf`) that it may clobber wholesale, adds exactly one idempotent
`Include` line to the operator's hand-maintained config, and *consumes* operator-chosen alias
names rather than inventing its own. The tmux status feature is the un-migrated form of the
pattern the project already rejected by ADR: in-place mutation of operator-declared state, no
owned namespace, no include seam, no provenance.

ADR-0005 supplies the second half. The operator's chezmoi-managed tmux config is durable,
shared across every Workspace on every Host, and provisioned declaratively elsewhere — the
configuration analogue of NSG policy or a landing-zone VNet. billet merely *adopts* it. The
doctrine that stops `create` from growing into a landing zone should stop `connect` from
growing into a theme engine.

tmux supplies the seam. `@`-prefixed **user options** are its documented extension point:
plain `set` of an unknown option errors, `set @foo` always succeeds, and the value reads back
in any format as `#{@foo}`. Unset user options expand to the empty string with no error
(`X#{@nope}Y` → `XY`), so a published-data contract needs no capability probe and degrades for
free. User options exist since tmux 3.0; the containers here run ≥ 3.3a.

## Decision

**billet publishes Workspace identity into the tmux session as data. The operator's adopted
dotfiles own all presentation.**

Six rules define the contract:

1. **Identity is published as tmux user options**, on the same prelude position billet uses
   today (ahead of `new-session -A`, for the reasons already documented): `@billet_workspace`,
   `@billet_host`, `@billet_color`. These are data only — never rendered output. The dotfiles
   interpolate them wherever the operator wants identity to appear.
2. **billet does not write presentation options — ever, not merely by default.**
   `status-style`, `status-left`, `status-right`, `status-format`, `status-*-length` and
   `window-status-*` are out of scope, unconditionally. Those belong to the theme and to the
   operator who chose it. `TmuxStatusEngine` loses `readable_fg` along with them: picking a
   legible foreground is a rendering decision, and billet no longer renders.
3. **billet renders nothing; silence by default is accepted.** There is no paint fallback and
   no opt-out flag. A Workspace whose dotfiles do not consume the published options gets no
   billet-supplied status branding, and that is the intended behavior — a clean ownership
   boundary is worth more than a guaranteed signal. Crucially, "silence" is not the same as
   "no identity": the options remain queryable (`tmux show -g @billet_workspace`), and once
   `tmux_session` carries the Workspace key (rule 6), *stock* tmux already renders it — its
   default `status-left` is `[#{session_name}] `, verified on tmux 3.7b — so an unconfigured
   container still shows the Workspace name without billet writing a single presentation
   option.
4. **Icons and logos are presentation, therefore the dotfiles' business.** billet may publish a
   glyph *name* or brand token; it never renders one. This resolves `FAVICON_INJECTION.md` —
   both its Approach A (glyph) and Approach B (image protocol) — as won't-do. Concretely: a
   billet glyph is constant across every Workspace, so it spends the scarcest row on the screen
   to convey zero information; it is monochrome; a missing glyph renders as tofu that billet
   cannot detect from the far side of the SSH session; and sizing the field correctly requires
   cell-width measurement rather than a codepoint count (a double-width emoji is one codepoint
   in two cells, a ZWJ sequence three codepoints in two), which is the renderer's problem to
   solve and therefore belongs where the font is also known.
5. **`status_color` stays operator intent in the registry**, with its existing hex validation
   and its existing place in the `[workspaces.<key>]` block. Only *who renders it* moves.
6. **`tmux_session` defaults to the Workspace key.** This is what makes rule 3 safe, so it
   belongs to this contract rather than being an incidental follow-up: it moves identity onto a
   channel billet already owns (the `new-session -s` argument) and that every theme — including
   no theme at all — renders for free as `#S`.

## Consequences

- The `adopting-a-repo.md` "pick one" caveat can be deleted. billet contests no presentation
  option with any theme, so no theme's cached state can be invalidated behind its back and the
  separator-notch class of artifact cannot recur.
- Provenance becomes checkable: `tmux show -g @billet_workspace` answers "what does billet
  think this session is?" in one line. Today, "the bar looks wrong" has no such probe.
- `TmuxStatusEngine` stays pure, and its golden-string tests become *meaningful* rather than
  merely exact — nothing else in the session contests a `@billet_`-namespaced option, so the
  asserted bytes are the observable behavior.
- The cost is real: **the consuming half of the contract lives in a second repo**
  (`rinman24/dotfiles`) with no CI, no import-linter, and no tests. Debugging a broken bar now
  spans two repositories, and neither one can validate the other.
- The operator takes on a maintenance burden: a status-bar segment that interpolates
  `#{@billet_*}` into tmux's format DSL, which has famously poor error messages and fails by
  rendering something subtly wrong rather than by erroring.
- A new operator who has not updated their dotfiles gets the session name and nothing more.
  This is the accepted cost of rule 3, and it is a real regression against today's behavior for
  that operator: no brand color, no styled segment, and nothing that tells them a richer
  integration exists. The mitigation is documentation, not code — `docs/adopting-a-repo.md`
  gains the consuming snippet in place of its current "pick one" caveat.
- Deleting the paint path means billet permanently gives up the ability to signal identity on a
  container it does not control the dotfiles of. If a future Workspace must be branded without
  operator cooperation, this ADR has to be revisited rather than extended — rule 2 is
  unconditional by design, and a `--paint` escape hatch would reintroduce exactly the collision
  and the dual rendering paths this ADR removes.

## Alternatives considered

- **Keep writing `status-style` / `status-left` (status quo).** Rejected: it is in-place
  mutation of operator-declared state — the pattern ADR-0002 §3 already retired for
  `~/.ssh/config` — and it is the documented source of the "pick one" mutual exclusion and the
  separator artifact.
- **Generate a billet-owned `tmux.conf` fragment and have the dotfiles `source-file` it** (the
  literal ADR-0002 Include model). Rejected: it would require writing files into the container,
  which the connect path deliberately does not do. The tmux user-option namespace *is* the
  include seam, evaluated at runtime, with nothing left behind.
- **Probe for the theme and adapt** (detect catppuccin, update `@_ctp_status_bg` too).
  Rejected: billet would encode a specific theme's private cache variable and inherit its
  refactors. Unset user options expanding to empty means the published contract needs no probe
  at all.
- **Retain a self-disabling paint fallback**, guarded on a dotfiles-set opt-out flag (e.g.
  `@billet_status_owner`) tested with a format-only `if-shell -F`. This was the initially
  drafted rule 3, on the reasoning that a feature answering "which container is this?" must not
  default to no signal. Rejected: it keeps billet writing theme-owned options on precisely the
  containers whose configuration billet knows least about, so the `status-style` collision
  survives as the *default* path rather than being removed; it leaves two rendering paths to
  specify, test, and keep in sync forever; and it makes the ownership boundary conditional,
  which is the property this ADR exists to eliminate. The argument that motivated it does not
  survive rule 6: with `tmux_session` carrying the Workspace key, an unconfigured container is
  not silent — stock tmux renders `[#{session_name}] ` in its default `status-left`.
- **Ship the glyph** (`FAVICON_INJECTION.md` Approach A). Rejected per rule 4: constant across
  Workspaces, monochrome, tofu-risk undetectable remotely, and it breaks the length arithmetic.
- **Terminal image protocols** (Approach B). Rejected for the reasons that note already
  records — capability probing, tmux passthrough, and shipping image bytes into a container
  billet writes nothing into — now compounded by rule 4: rendering is not billet's layer.
