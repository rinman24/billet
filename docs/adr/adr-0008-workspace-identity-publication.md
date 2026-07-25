# ADR-0008: billet publishes Workspace identity; the dotfiles own presentation

## Status

Accepted (2026-07-25). Applies [ADR-0002](adr-0002-workspace-subsystem.md) §3's ownership
model — a tool-owned artifact plus one include seam, never in-place mutation — to the tmux
status bar, and extends [ADR-0005](adr-0005-instance-lifecycle-ownership.md)'s "adopt, don't
own" boundary from cloud infrastructure to operator-owned *configuration*. Decision only: no
behavior changes with this ADR; implementation follows in separate PRs.

## Context

`billet connect` brands a Workspace's tmux status bar so an operator can tell otherwise
identical container shells apart. `WorkspaceManager.connect_target` asks `TmuxStatusEngine`
for a prelude of `set -g status-style`, `set -g status-left`, `set -g status-left-length` and
splices it into the same `tmux` invocation *ahead* of `new-session -A`; the per-Workspace
`status_color` is operator intent read (and hex-validated) from the registry.

The prelude's *position* is correct and stays. Live testing against tmux 3.7b confirms the
reasoning already documented in `tmux_status_engine.py`: the config file is fully sourced
before the client's command-line `set -g` commands run — on both the cold-start and the
`new-session -A` re-attach path — and TPM sources plugins synchronously, so there is no race.
billet always wins.

Winning is the problem. The same testing, against real catppuccin checkouts, found exactly
one true collision and it is caused by billet writing an option the theme owns:

- catppuccin v2.3.0 **does** set `status-style` (`catppuccin_tmux.conf:5`) and caches the bar
  background in `@_ctp_status_bg`, then draws its separators as `#[fg=#{@_ctp_status_bg}]█`.
  billet overwriting `status-style` does not update that cached value, so the bar recolors but
  the separators keep painting notches in the theme's old background color.
- catppuccin v2 does **not** set `status-left`/`status-right`; it exposes ~70
  `@catppuccin_status_*` user options the operator composes. The operator's dotfiles
  ([`rinman24/dotfiles`](https://github.com/rinman24/dotfiles)) set `status-left ""` and build
  `status-right` from those modules — so `status-left` is deliberately vacated and billet's
  label there is, today, non-destructive.

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

Five rules define the contract:

1. **Identity is published as tmux user options**, on the same prelude position billet uses
   today (ahead of `new-session -A`, for the reasons already documented): `@billet_workspace`,
   `@billet_host`, `@billet_color`. These are data only — never rendered output. The dotfiles
   interpolate them wherever the operator wants identity to appear.
2. **billet does not write presentation options.** `status-style`, `status-left`,
   `status-right`, `status-format`, `status-*-length` and `window-status-*` are out of scope as
   billet's steady-state contract. Those belong to the theme and the operator who chose it.
3. **A self-disabling paint fallback is retained.** A feature whose entire job is "tell me
   which container I'm in" must not default to no signal, so billet still paints when nothing
   else will — guarded on a dotfiles-set opt-out flag (e.g. `@billet_status_owner`), tested
   with a format-only `if-shell -F` so the guard costs no fork. Dotfiles that consume the
   published options set the flag and billet goes quiet; dotfiles that don't, get today's
   behavior. Silence-by-default was rejected: it would ship a feature that is invisibly dead on
   every container whose dotfiles have not been updated.
4. **Icons and logos are presentation, therefore the dotfiles' business.** billet may publish a
   glyph *name* or brand token; it never renders one. This resolves `FAVICON_INJECTION.md` —
   both its Approach A (glyph) and Approach B (image protocol) — as won't-do. Concretely: a
   billet glyph is constant across every Workspace, so it spends the scarcest row on the screen
   to convey zero information; it is monochrome; a missing glyph renders as tofu that billet
   cannot detect from the far side of the SSH session; and the existing
   `status-left-length` arithmetic counts Python codepoints where tmux counts cells, so a
   glyph silently mis-sizes the field.
5. **`status_color` stays operator intent in the registry**, with its existing hex validation
   and its existing place in the `[workspaces.<key>]` block. Only *who renders it* moves.

## Consequences

- The `adopting-a-repo.md` "pick one" caveat can be deleted. billet and catppuccin stop
  contesting `status-style`, and the separator-notch artifact disappears because the theme's
  cached `@_ctp_status_bg` is never invalidated behind its back.
- Provenance becomes checkable: `tmux show -g @billet_workspace` answers "what does billet
  think this session is?" in one line. Today, "the bar looks wrong" has no such probe.
- `TmuxStatusEngine` stays pure, and its golden-string tests become *meaningful* rather than
  merely exact — nothing else in the session contests a `@billet_`-namespaced option, so the
  asserted bytes are the observable behavior.
- The cost is real: **the consuming half of the contract lives in a second repo**
  (`rinman24/dotfiles`) with no CI, no import-linter, and no tests. Debugging a broken bar now
  spans two repositories, and neither one can validate the other.
- The operator takes on a maintenance burden: a catppuccin module that interpolates
  `#{@billet_*}` into tmux's format DSL, which has famously poor error messages and fails by
  rendering something subtly wrong rather than by erroring.
- A new operator who has not updated their dotfiles gets only the fallback (rule 3) — correct
  and useful, but not the themed integration, and nothing tells them what they are missing.
- Not in scope, noted as a follow-up: `tmux_session` defaults to `"main"` for every Workspace
  (`toml_registry_access.py:184`), discarding a free per-Workspace identity channel that every
  theme already renders as `#S`.

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
- **Silence by default; paint only when asked.** Rejected: the feature exists to answer "which
  container is this?", and a default that answers nothing is worse than a default that
  occasionally over-paints. Hence the opt-out flag in rule 3 rather than an opt-in one.
- **Ship the glyph** (`FAVICON_INJECTION.md` Approach A). Rejected per rule 4: constant across
  Workspaces, monochrome, tofu-risk undetectable remotely, and it breaks the length arithmetic.
- **Terminal image protocols** (Approach B). Rejected for the reasons that note already
  records — capability probing, tmux passthrough, and shipping image bytes into a container
  billet writes nothing into — now compounded by rule 4: rendering is not billet's layer.
