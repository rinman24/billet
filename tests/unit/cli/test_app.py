"""Smoke tests for the billet CLI skeleton."""

from typer.testing import CliRunner

from billet import __version__
from billet.cli.app import app

runner = CliRunner()


def test_version_command_prints_the_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_bare_invocation_shows_banner_and_command_surface() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    # Non-tty banner degrades to the one-line wordmark; the command surface follows.
    assert f"billet {__version__}" in result.output
    assert "host up" in result.output
    assert "connect <key>" in result.output
    assert "run billet <command> --help for details." in result.output


def test_bare_invocation_pipes_clean() -> None:
    result = runner.invoke(app, [])
    assert "\x1b[" not in result.output  # no ANSI when not a terminal
    assert "██" not in result.output  # the rack is a tty-only treat


def test_help_lists_the_version_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.stdout
