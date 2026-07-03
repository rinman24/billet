"""TmuxStatusEngine — render the tmux *status branding* prelude for ``billet connect``.

*Status branding* is the color-plus-label billet stamps onto a Workspace's tmux status bar
so an operator can tell otherwise-identical container shells apart at a glance: a **label**
(the Workspace key, always shown on the left) and an optional **brand color** (the
status-bar background). This engine is pure text rendering — no side effects — so its exact,
byte-for-byte output is exhaustively unit-testable.

The rendered *prelude* is the run of ``set -g`` commands that ``connect`` inserts between
``tmux `` and ``new-session ...``. It is emitted *before* ``new-session`` on purpose:
``status-style`` / ``status-left`` are session **globals**, and ``connect`` attaches with
``new-session -A`` (attach-if-exists). When a matching session already exists, ``-A``
short-circuits straight to an attach and never re-applies options that trail it — so the
globals must be set on the same ``tmux`` invocation *ahead* of ``new-session`` to brand both
the create path and the re-attach path.

``status-left`` is a tmux **FORMAT** string: tmux interprets ``#`` sequences (``#H``,
``#{...}``) inside it, so a literal ``#`` in the label must be doubled to ``##`` or tmux
would swallow it. ``status-style`` is **not** a format string — it is a style spec whose
``#`` introduces a hex color (``bg=#rrggbb``) — so its ``#`` must be left untouched. Every
option *value* is additionally ``shlex.quote``-d, because the prelude rides through a remote
login shell before tmux ever parses it.
"""

import shlex

_LUMINANCE_THRESHOLD = 128
_SHORT_HEX_DIGITS = 3
_SEP = " \\; "


class TmuxStatusEngine:
    """Renders the tmux status-branding prelude for one Workspace."""

    def render_prelude(self, *, label: str, color: str | None) -> str:
        r"""Render the ``set -g`` prelude to insert before ``new-session``.

        Parameters
        ----------
        label : str
            The Workspace identity shown on the status bar's left. Always emitted, so the
            container stays identifiable even when no brand color is set.
        color : str | None
            The optional hex brand color used as the status-bar background, or ``None`` to
            leave the bar at tmux's default style.

        Returns
        -------
        str
            The chained ``set -g`` commands, each separated *and* terminated by `` \; ``
            (space-backslash-semicolon-space) so the caller can concatenate the result
            directly ahead of ``new-session``. When ``color`` is set the order is
            ``status-style``, ``status-left``, ``status-left-length``; otherwise
            ``status-style`` is omitted.
        """
        commands: list[str] = []
        if color is not None:
            style = f"bg={color},fg={self.readable_fg(color)}"
            commands.append(f"set -g status-style {shlex.quote(style)}")
        escaped_label = label.replace("#", "##")
        commands.append(f"set -g status-left {shlex.quote(f' {escaped_label} ')}")
        commands.append(f"set -g status-left-length {shlex.quote(str(len(label) + 2))}")
        return "".join(f"{command}{_SEP}" for command in commands)

    def readable_fg(self, color: str) -> str:
        """Return the legible foreground (``#000000``/``#ffffff``) for a hex background.

        Uses integer Rec.601 perceived luminance,
        ``lum = (r*299 + g*587 + b*114) // 1000``: black text on a light background
        (``lum >= 128``), white text on a dark one.

        Parameters
        ----------
        color : str
            A hex color, ``#rgb`` or ``#rrggbb`` in any case. The three-digit form is
            expanded by doubling each nibble (``#abc`` -> ``#aabbcc``).

        Returns
        -------
        str
            ``"#000000"`` when the background is light, ``"#ffffff"`` when it is dark.
        """
        digits = color.removeprefix("#")
        if len(digits) == _SHORT_HEX_DIGITS:
            digits = "".join(nibble * 2 for nibble in digits)
        red = int(digits[0:2], 16)
        green = int(digits[2:4], 16)
        blue = int(digits[4:6], 16)
        lum = (red * 299 + green * 587 + blue * 114) // 1000
        return "#000000" if lum >= _LUMINANCE_THRESHOLD else "#ffffff"
