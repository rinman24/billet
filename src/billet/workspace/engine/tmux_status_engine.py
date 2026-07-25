"""TmuxStatusEngine — render the tmux *identity publication* prelude for ``billet connect``.

*Publication* means billet writes a Workspace's identity into the tmux session as **data**
and renders nothing (ADR-0008). Three values are published — the Workspace key, the Host
key, and the optional ``status_color`` hex — and the operator's adopted tmux configuration
decides whether, where, and how any of them appear. billet never writes ``status-style``,
``status-left``, ``status-right``, ``status-format``, ``status-*-length`` or
``window-status-*``: those belong to the theme and to the operator who chose it. This engine
is pure text rendering — no side effects — so its exact, byte-for-byte output is
exhaustively unit-testable.

The carrier is tmux's **user-option** namespace, its documented extension point and the
reason no capability probe is needed. Plain ``set`` of an option tmux does not know is an
error; ``set @foo`` always succeeds, on every tmux since 3.0, whether or not anything reads
it. Reads are equally forgiving: an unset user option expands to the empty string with no
error (``X#{@nope}Y`` -> ``XY``), so a consumer that interpolates ``#{@billet_color}`` on a
Workspace with no color simply gets nothing. Publication therefore degrades for free in both
directions — billet writing options nobody reads, and dotfiles reading options billet did
not write.

The rendered *prelude* is the run of ``set -g`` commands that ``connect`` inserts between
``tmux `` and ``new-session ...``. Its position is unchanged from the presentation era and
for the same reason: these are session **globals**, and ``connect`` attaches with
``new-session -A`` (attach-if-exists). When a matching session already exists, ``-A``
short-circuits straight to an attach and never re-applies options that trail it — so the
globals must be set on the same ``tmux`` invocation *ahead* of ``new-session`` to publish on
both the create path and the re-attach path.

Values are published verbatim, with one and only one transformation: ``shlex.quote``, because
the prelude rides through a remote login shell before tmux ever parses it. In particular the
hex color keeps its leading ``#``. A user option's *value* is not a format string — tmux
format-expands the string a consumer writes, not the option it interpolates — so there is no
``#`` -> ``##`` escaping here, and adding it would be a bug: ``set -g @billet_color
'#C05CE0'`` read back as ``#[bg=#{@billet_color}]`` expands to ``#[bg=#C05CE0]`` and paints,
whereas a doubled ``##{@billet_color}`` collapses to a literal ``#`` and the option is never
interpolated at all (verified on tmux 3.7b). Consumers should read ``#{@billet_workspace}``;
``#{E:@billet_workspace}`` re-expands the published value as a format and is deliberately
not part of the contract.
"""

import shlex

_SEP = " \\; "


class TmuxStatusEngine:
    """Renders the tmux identity-publication prelude for one Workspace."""

    def render_prelude(self, *, workspace: str, host: str, color: str | None) -> str:
        r"""Render the ``set -g`` prelude to insert before ``new-session``.

        Parameters
        ----------
        workspace : str
            The Workspace key, published as ``@billet_workspace``. Always emitted.
        host : str
            The Host key the Workspace is placed on, published as ``@billet_host``. Always
            emitted.
        color : str | None
            The optional hex brand color, published verbatim (leading ``#`` included) as
            ``@billet_color``. When ``None`` the ``set -g`` is omitted entirely, so the
            option stays unset and expands to the empty string in a consumer's format —
            which is why no sentinel value is needed for "no color".

        Returns
        -------
        str
            The chained ``set -g`` commands, each separated *and* terminated by `` \; ``
            (space-backslash-semicolon-space) so the caller can concatenate the result
            directly ahead of ``new-session``. The order is ``@billet_workspace``,
            ``@billet_host``, then ``@billet_color`` when it is set.
        """
        commands = [
            f"set -g @billet_workspace {shlex.quote(workspace)}",
            f"set -g @billet_host {shlex.quote(host)}",
        ]
        if color is not None:
            commands.append(f"set -g @billet_color {shlex.quote(color)}")
        return "".join(f"{command}{_SEP}" for command in commands)
