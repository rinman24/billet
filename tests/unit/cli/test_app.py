"""Smoke tests for the billet CLI skeleton."""

from typer.testing import CliRunner

from billet import __version__
from billet.cli.app import app

runner = CliRunner()


def test_version_command_prints_the_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    # no_args_is_help renders usage; Typer maps "no command given" to Click exit code 2.
    assert result.exit_code == 2
    assert "Usage" in result.output


def test_help_lists_the_version_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.stdout
