"""The ``billet`` command-line entry point.

The composition root: a Typer application, a ``version`` command, the ``host`` command
group, and the top-level Workspace commands (add / ls / start / stop / connect /
ssh-config / rm).
"""

from importlib.metadata import version as _dist_version

import typer

from billet.cli import host_commands, workspace_commands

app = typer.Typer(
    name="billet",
    help="Manage cloud Hosts and the repos' devcontainer Workspaces that run on them.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(host_commands.app, name="host")
workspace_commands.register(app)


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
