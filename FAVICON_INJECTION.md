# Favicon / icon injection — deferred design note

Status: **Deferred** (future work). Companion to the `tmux-status-branding` feature
(PR #23), which shipped the per-Workspace status-bar **color** and **label**.

## Context

`billet connect` now brands a Workspace's tmux status bar with a background color
(`status_color`) and always shows the Workspace key on the left. That solves "which
container am I in?" for the common case using nothing but text and ANSI — it works in
every terminal, needs no capability probing, and keeps billet stateless (the styling
rides in on the `connect` command; nothing is written into the container).

The originally-requested third element — injecting a small **brand icon** into the bar
(the billet mark by default, or a per-Workspace custom icon such as a squadra logo) — was
deliberately left out of that slice. Icons are terminal-capability-dependent in a way that
color and text are not, so they carry a materially larger surface and belong in their own
change. This note records the two viable approaches and a recommendation so the follow-up
can start from a decision rather than a blank page.

## What "icon injection" concretely requires

There is no single mechanism. It reduces to one of two families, and they have very
different cost/fidelity trade-offs:

### Approach A — tmux status-format glyphs (text, not images)

Embed a **glyph** in the status-left format string — a Nerd Font / Unicode Private-Use-Area
codepoint (e.g. a devicon), or a plain emoji. This is "an icon" only in the font sense.

- Pros
  - Pure text. Rides in the existing `set -g status-left` value with zero new mechanism.
  - Works over plain SSH → tmux with no probe, no passthrough, no image protocol, no state.
  - Composes trivially with the current `TmuxStatusEngine`: prepend the glyph to the label
    (mind the existing `#`→`##` format-escaping; a glyph codepoint needs no escaping).
  - Cache-free and deterministic — easy to unit-test with golden strings like today's.
- Cons
  - Requires the **operator's** terminal + font to contain the glyph. A missing glyph
    renders as tofu (`□`) — billet cannot detect this from the far side of the SSH session.
  - Monochrome: the glyph is colored by the bar's foreground, not an arbitrary image.
  - A per-repo "custom favicon" becomes "pick a codepoint," not "supply a PNG" — fine for a
    curated set (billet, squadra, …), awkward for arbitrary brand art.

### Approach B — terminal image-protocol probe (real raster images)

Actually render a small raster image — the billet favicon, or a custom PNG — using a
terminal graphics protocol (Kitty graphics, iTerm2 inline images, or Sixel).

- Pros
  - True arbitrary image; real brand fidelity, including per-Workspace custom logos.
- Cons
  - **Capability probe required.** billet would have to detect, at connect time, whether the
    operator's terminal speaks Kitty/iTerm2/Sixel (a query escape + timed response), and
    fall back cleanly when it does not.
  - **tmux passthrough.** Graphics escapes must survive the pane multiplexer
    (`allow-passthrough`, plus tmux/version constraints); support is uneven and fiddly.
  - **Layout.** A single-row status line is a poor host for an image; images occupy cells or
    rows and positioning them in the status bar (vs. a pane) is not well supported.
  - **Statefulness vs. billet's model.** The image bytes must reach the terminal — either
    shipped to the container (violates "nothing written into the container") or inlined as
    base64 into the `connect` command (bloats it, and still needs passthrough).

## Integration sketch

Either approach extends the same seam the color/label feature uses:

- Config surface (per Workspace, optional), e.g.
  - Approach A: `status_icon = "billet"` (a named built-in glyph) or a raw codepoint.
  - Approach B: `status_icon` naming a bundled image, or `status_icon_path` for a custom one.
- `TmuxStatusEngine.render_prelude(...)` grows an `icon` parameter. For A it prepends the
  glyph to the label inside `status-left`. For B the manager (not the pure engine) would emit
  the passthrough/image sequence, since that is a side-effecting terminal concern, not pure
  rendering — keep the engine pure and push protocol I/O to the boundary.
- Validation stays in `RegistryAccess`, mirroring the `status_color` hex check.

## Recommendation

Start with **Approach A (glyph)**. It matches the pure, stateless, probe-free design that
made the color/label slice clean, composes with the existing engine in a few lines, and
covers the real goal — a recognizable billet mark (and a small curated per-repo set) on the
bar. Treat **Approach B (image protocol)** as a later, opt-in enhancement gated behind a
capability probe, only if glyphs prove insufficient for brand fidelity.

Provide a graceful fallback in both cases: an unknown/unsupported icon simply omits the
glyph and leaves the color + label intact.

## Open questions

- Do we assume a Nerd Font on operator terminals, or restrict to emoji that ship with most
  fonts? Can we surface a one-line "your terminal lacks the glyph" hint?
- What is the curated built-in icon set, and how does a Workspace register a custom glyph?
- For Approach B: which protocol(s) do we target first, and how do we ship image bytes
  without breaking the "stateless / nothing in the container" principle?
