"""Guards on the wiring that keeps ``billet.__version__`` the only version literal.

``src/billet/__init__.py`` holds the one version literal. Everything else must be derived
from it: the CLI prints the constant directly (``billet.cli.app``), and the built
distribution's metadata is generated from the same file by hatchling via pyproject's
``[tool.hatch.version]``.

This is the layer the stale-version defect really belonged to. Installed distribution
metadata is a build-time *snapshot* of the constant, and an editable venv keeps serving
the old snapshot after a bump — ``uv sync --frozen`` does not refresh it, because a
dynamic version is not recorded in ``uv.lock`` for uv to compare against. Reading that
snapshot at runtime is what made ``billet version`` print a stale answer, so the CLI no
longer does. These tests stop a second, independently-editable version from creeping back
into the build wiring, which is the only derived copy left.
"""

from pathlib import Path
import tomllib
from typing import Any

from billet import __version__

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: The single source of truth, as pyproject must spell it (repo-relative, posix).
_VERSION_FILE = "src/billet/__init__.py"


def _pyproject() -> dict[str, Any]:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_pyproject_derives_the_distribution_version_from_the_source_constant() -> None:
    config = _pyproject()
    project: dict[str, Any] = config["project"]
    # A static `version` key would be a second literal for a bump to forget.
    assert "version" not in project
    assert "version" in project["dynamic"]
    assert config["tool"]["hatch"]["version"]["path"] == _VERSION_FILE


def test_the_version_constant_is_a_plain_literal() -> None:
    # hatchling reads this file textually, so the constant must stay a literal assignment.
    # Computing it (e.g. from `importlib.metadata`) would break the build *and* reopen the
    # stale-version defect by making the package read its own installed snapshot.
    source = (_REPO_ROOT / _VERSION_FILE).read_text(encoding="utf-8")
    assert f'__version__ = "{__version__}"' in source
