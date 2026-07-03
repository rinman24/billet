"""Golden tests for TmuxStatusEngine — exact status-branding prelude and readable_fg."""

from billet.workspace.engine.tmux_status_engine import TmuxStatusEngine


def test_readable_fg_light_background_gets_black() -> None:
    assert TmuxStatusEngine().readable_fg("#EDE6F2") == "#000000"


def test_readable_fg_dark_background_gets_white() -> None:
    assert TmuxStatusEngine().readable_fg("#141414") == "#ffffff"


def test_readable_fg_expands_three_digit_hex() -> None:
    assert TmuxStatusEngine().readable_fg("#fff") == "#000000"
    assert TmuxStatusEngine().readable_fg("#000") == "#ffffff"


def test_readable_fg_is_case_insensitive() -> None:
    assert TmuxStatusEngine().readable_fg("#c05ce0") == TmuxStatusEngine().readable_fg("#C05CE0")
    assert TmuxStatusEngine().readable_fg("#C05CE0") == "#000000"


def test_render_prelude_with_color_exact() -> None:
    prelude = TmuxStatusEngine().render_prelude(label="billet", color="#C05CE0")
    assert prelude == (
        "set -g status-style 'bg=#C05CE0,fg=#000000' \\; "
        "set -g status-left ' billet ' \\; "
        "set -g status-left-length 8 \\; "
    )


def test_render_prelude_without_color_omits_status_style() -> None:
    prelude = TmuxStatusEngine().render_prelude(label="billet", color=None)
    assert prelude == "set -g status-left ' billet ' \\; set -g status-left-length 8 \\; "
    assert "status-style" not in prelude


def test_render_prelude_length_counts_original_label() -> None:
    prelude = TmuxStatusEngine().render_prelude(label="gswa-backend", color=None)
    assert prelude == ("set -g status-left ' gswa-backend ' \\; set -g status-left-length 14 \\; ")


def test_render_prelude_escapes_hash_in_label_only() -> None:
    # `#` is doubled for the status-left FORMAT string, but the length uses the raw label.
    prelude = TmuxStatusEngine().render_prelude(label="a#b", color=None)
    assert prelude == "set -g status-left ' a##b ' \\; set -g status-left-length 5 \\; "


def test_render_prelude_passes_color_through_verbatim() -> None:
    # The operator's color casing is preserved (not lowercased) in status-style.
    prelude = TmuxStatusEngine().render_prelude(label="x", color="#C05CE0")
    assert "bg=#C05CE0," in prelude
