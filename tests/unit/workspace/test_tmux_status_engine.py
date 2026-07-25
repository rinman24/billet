"""Golden tests for TmuxStatusEngine — the exact identity-publication prelude (ADR-0008)."""

from billet.workspace.engine.tmux_status_engine import TmuxStatusEngine


def test_render_prelude_with_color_exact() -> None:
    prelude = TmuxStatusEngine().render_prelude(workspace="billet", host="devbox", color="#C05CE0")
    assert prelude == (
        "set -g @billet_workspace billet \\; "
        "set -g @billet_host devbox \\; "
        "set -g @billet_color '#C05CE0' \\; "
    )


def test_render_prelude_without_color_omits_the_option_entirely() -> None:
    # Unset is the signal: an absent user option expands to "" in the consumer's format,
    # so there is no sentinel value to publish.
    prelude = TmuxStatusEngine().render_prelude(workspace="billet", host="devbox", color=None)
    assert prelude == "set -g @billet_workspace billet \\; set -g @billet_host devbox \\; "
    assert "@billet_color" not in prelude


def test_render_prelude_writes_no_presentation_option() -> None:
    prelude = TmuxStatusEngine().render_prelude(
        workspace="gswa-backend", host="devbox", color="#C05CE0"
    )
    for option in ("status-style", "status-left", "status-right", "status-format", "window-status"):
        assert option not in prelude


def test_render_prelude_publishes_the_hex_with_its_leading_hash() -> None:
    # A user-option *value* is not format-expanded, so `#` is published verbatim (never
    # doubled): `#[bg=#{@billet_color}]` expands to `#[bg=#C05CE0]`, while `##{...}` would
    # collapse to a literal `#` and never interpolate.
    prelude = TmuxStatusEngine().render_prelude(workspace="x", host="devbox", color="#C05CE0")
    assert "set -g @billet_color '#C05CE0' \\; " in prelude
    assert "##" not in prelude


def test_render_prelude_does_not_escape_a_hash_in_the_workspace_key() -> None:
    prelude = TmuxStatusEngine().render_prelude(workspace="a#b", host="c#d", color=None)
    assert prelude == "set -g @billet_workspace 'a#b' \\; set -g @billet_host 'c#d' \\; "


def test_render_prelude_shell_quotes_values() -> None:
    # The prelude rides through a remote login shell before tmux parses it.
    prelude = TmuxStatusEngine().render_prelude(
        workspace="a b; rm -rf /", host="devbox", color=None
    )
    assert prelude == (
        "set -g @billet_workspace 'a b; rm -rf /' \\; set -g @billet_host devbox \\; "
    )
