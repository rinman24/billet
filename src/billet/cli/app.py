"""The ``billet`` command-line entry point.

The composition root: a Typer application, a ``version`` command, the ``host`` command
group, and the top-level Workspace commands (add / ls / start / stop / connect /
ssh-config / rm).
"""

from importlib.metadata import version as _dist_version
from typing import Annotated

import typer

from billet.cli import _ui, host_commands, workspace_commands

app = typer.Typer(
    name="billet",
    help="Manage cloud Hosts and the repos' devcontainer Workspaces that run on them.",
    add_completion=False,
)
app.add_typer(host_commands.app, name="host", rich_help_panel="host · vm lifecycle")
workspace_commands.register(app)


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="stream raw az/ssh/docker output.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="print only outcomes and errors.")
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="disable color output.")] = False,
    ascii_only: Annotated[
        bool, typer.Option("--ascii", help="ascii glyphs only (no box/braille characters).")
    ] = False,
) -> None:
    """Manage cloud Hosts and the repos' devcontainer Workspaces that run on them."""
    _ui.configure(
        _ui.UIState(quiet=quiet, verbose=verbose, no_color=no_color, ascii_only=ascii_only)
    )
    if ctx.invoked_subcommand is None:
        # Bare `billet` is the signature moment: the berth-rack banner + command surface
        # (`billet version` stays a bare version string — scripts parse it).
        _ui.banner(_dist_version("billet"))
        _ui.command_surface()


@app.command()
def version() -> None:
    """Print the installed billet version."""
    typer.echo(_dist_version("billet"))


def main() -> None:
    """Console-script entry point (``billet``)."""
    app()
