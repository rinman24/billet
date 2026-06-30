"""The ``billet`` command-line entry point.

The composition root: a Typer application, a ``version`` command, and the ``host`` command
group. The Workspace command group lands in later slices.
"""

from importlib.metadata import version as _dist_version

import typer

from billet.cli import host_commands

app = typer.Typer(
    name="billet",
    help="Manage cloud Hosts and the repos' devcontainer Workspaces that run on them.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(host_commands.app, name="host")


@app.callback()
def root() -> None:
    """Manage cloud Hosts and the repos' devcontainer Workspaces that run on them."""


@app.command()
def version() -> None:
    """Print the installed billet version."""
    typer.echo(_dist_version("billet"))


def main() -> None:
    """Console-script entry point (``billet``)."""
    app()
