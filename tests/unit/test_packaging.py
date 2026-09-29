"""Packaging guards: the one version literal, and the Berth shipped inside the wheel.

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

The second half proves ``billet doctor``'s side of the Berth comparison comes from the
installed package: a wheel built from this checkout carries ``templates/workspace/`` as
``billet/_berth``, and the resolver reads it from there with the repo off the path.
"""

from collections.abc import Iterator
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from typing import Any
import zipfile

import pytest

from billet import __version__
from billet.access.doctor.packaged_berth import (
    BERTH_RESOURCE_DIR,
    berth_resource_root,
    read_packaged_berth,
)
from billet.contracts import BERTH_COPIED_FILES
from billet.shared.errors import ConfigError

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


# --- the packaged Berth (ADR-0015, D-A4-1, FA-3) -----------------------------------------
#
# FA-3, settled empirically: hatchling's editable install (what `uv run` uses) writes a
# `.pth` that puts `src/` on sys.path, so `import billet` resolves to `src/billet` — which has
# no `_berth`. hatchling does copy the force-included files into site-packages/billet/_berth,
# but that directory is only a namespace-package portion and loses to the regular package.
# The resource is therefore absent under `uv run`, and these tests prove the resolver against
# a wheel built from this checkout instead.

_TEMPLATE_DIR = _REPO_ROOT / "templates" / "workspace"


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Build this checkout's wheel once (via sdist, as `uv build` does by default)."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH; the wheel cannot be built")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [uv, "build", "--quiet", "--out-dir", str(out), str(_REPO_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = out.glob("billet-*.whl")
    yield wheel


def test_the_wheel_carries_the_four_copied_berth_files(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        for name in BERTH_COPIED_FILES:
            member = f"billet/{BERTH_RESOURCE_DIR}/{name}"
            assert archive.read(member) == (_TEMPLATE_DIR / name).read_bytes(), member


def test_doctor_resolves_the_berth_from_the_installed_package_not_the_repo(
    built_wheel: Path, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(site)
    probe = (
        "import json, billet\n"
        "from billet.access.doctor.packaged_berth import berth_resource_root, "
        "read_packaged_berth\n"
        "berth = read_packaged_berth()\n"
        "print(json.dumps({'package': billet.__file__, 'root': str(berth_resource_root()), "
        "'version': berth.version, 'files': dict(berth.files)}))\n"
    )
    # -I: isolated (no user site, no PYTHONPATH, no cwd on sys.path); -S: no site-packages,
    # so the editable `.pth` pointing at this checkout's src/ is never processed.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, {str(site)!r})\n{probe}",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert Path(payload["package"]).is_relative_to(site)
    assert Path(payload["root"]).is_relative_to(site)
    assert not Path(payload["root"]).is_relative_to(_REPO_ROOT)
    assert payload["version"] == int((_TEMPLATE_DIR / "berth.version").read_text())
    for name in BERTH_COPIED_FILES:
        assert payload["files"][name] == (_TEMPLATE_DIR / name).read_text()


def test_pyproject_force_includes_the_templates_into_the_package() -> None:
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["force-include"] == {"templates/workspace": f"billet/{BERTH_RESOURCE_DIR}"}
    sdist = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "templates/workspace" in sdist["include"]  # a wheel built from the sdist needs it


def test_without_a_packaged_berth_the_resolver_names_the_fix() -> None:
    # Under the editable install (FA-3) the resource is absent; the resolver must say so
    # rather than fall back to a repo path (D-A4-1).
    if berth_resource_root().is_dir():
        pytest.skip("this interpreter has a packaged Berth (a wheel install)")
    with pytest.raises(ConfigError, match="uv build"):
        read_packaged_berth()
