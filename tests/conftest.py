"""Suite-wide fixtures: pin the console environment the CLI renders into.

``billet.cli._ui`` builds its rich ``Console`` with terminal detection left on auto —
correct for a human, wrong for a test, because rich then reads the *invoking shell's*
environment rather than the captured stream pytest actually hands it:

- ``FORCE_COLOR`` (and ``TTY_COMPATIBLE=1``) make ``Console.is_terminal`` return True for
  a non-tty stream, so the CLI emits ANSI styling and, via ``_ui.animate``, animates its
  spinners — the piped-output assertions in ``tests/unit/cli`` then see escape sequences
  and half-drawn spinner frames.
- ``Console.size`` reads ``COLUMNS`` and probes ``os.get_terminal_size`` on the std file
  descriptors *regardless* of ``is_terminal``, so a narrow terminal re-wraps the tables.

Both are properties of the operator's shell, not of billet, and CI only passes because a
GitHub runner exports neither. Pinning belongs here, in the environment: the CLI's real
behaviour for a human must stay exactly as it is, so nothing under ``src/billet/cli`` is
touched. Tests that deliberately exercise colour (``tests/unit/cli/test_ui.py``) build
their own console with an explicit ``force_terminal=``/``width=``, which rich resolves
before it consults the environment — this fixture cannot mask them.
"""

from collections.abc import Iterator

import pytest

#: Variables that talk rich into treating a non-tty stream as a terminal. ``TTY_COMPATIBLE``
#: is checked before ``FORCE_COLOR``, so pinning it to ``"0"`` is on its own enough for the
#: pinned rich; the deletions keep the pin working if that newer knob ever goes away.
_TTY_FORCING_VARS = ("FORCE_COLOR", "CLICOLOR_FORCE", "TTY_COMPATIBLE")

#: The width rich falls back to with no terminal attached — i.e. what CI renders at.
_PINNED_COLUMNS = "80"


@pytest.fixture(scope="session", autouse=True)
def pinned_console_environment() -> Iterator[None]:
    """Render the whole suite as a plain 80-column pipe, whatever the shell exports."""
    with pytest.MonkeyPatch.context() as patch:
        for name in _TTY_FORCING_VARS:
            patch.delenv(name, raising=False)
        patch.setenv("TTY_COMPATIBLE", "0")
        patch.setenv("COLUMNS", _PINNED_COLUMNS)
        yield
