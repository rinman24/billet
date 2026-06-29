"""The ``billet`` command-line entry point.

This slice ships only the composition-root skeleton: a Typer application and a
``version`` command. The Host and Workspace command groups land in later slices.
"""

from importlib.metadata import version as _dist_version

import typer

app = typer.Typer(
    name="billet",
    help="Manage cloud Hosts and the repos' devcontainer Workspaces that run on them.",
    no_args_is_help=True,
    add_completion=False,
)


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
