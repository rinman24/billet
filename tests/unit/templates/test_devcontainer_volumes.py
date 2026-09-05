"""Tests for the persisted-state contract the Workspace templates prescribe.

A Workspace container is rebuilt often (``compose up --build``), so anything the operator
authenticated by hand has to live on a named volume or it is lost every rebuild. The
templates encode that as a pair: ``Dockerfile.snippet`` pre-creates the mountpoint
dev-owned (otherwise the daemon creates it ``root:root`` and the non-root ``dev`` user
cannot write it), and ``docker-compose.snippet.yml`` declares and mounts the volume.

These tests hold both halves together — a mount with no declaration, or a declaration with
no dev-owned mountpoint, is the failure mode — and check that billet's own
``.devcontainer/`` still implements what the templates prescribe. billet runs itself as a
Workspace, so it is consumer #1 of its own contract; a template change that is not
mirrored here has not been dogfooded.
"""

from pathlib import Path
import re

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

_TEMPLATE_COMPOSE = _REPO_ROOT / "templates" / "workspace" / "docker-compose.snippet.yml"
_TEMPLATE_DOCKERFILE = _REPO_ROOT / "templates" / "workspace" / "Dockerfile.snippet"
_DEVCONTAINER_COMPOSE = _REPO_ROOT / ".devcontainer" / "docker-compose.yml"
_DEVCONTAINER_DOCKERFILE = _REPO_ROOT / ".devcontainer" / "Dockerfile"

#: The compose service placeholder an adopting repo replaces; billet's own value is
#: ``billet``, which is what makes the template's volume names comparable to billet's.
_SERVICE_PLACEHOLDER = "<service>"
_BILLET_SERVICE = "billet"

#: Where `gh` keeps ``hosts.yml`` — the credential store issue #58 is about.
_GH_CONFIG_PATH = "/home/dev/.config/gh"

#: A compose mount whose source names a volume rather than a path. Docker treats a source
#: containing ``/`` or starting with ``.`` as a bind mount; ``${VAR:-default}``
#: interpolations are bind mounts here too. Angle brackets admit the ``<service>``
#: placeholder the templates carry.
_NAMED_VOLUME_MOUNT = re.compile(
    r"^-\s+(?P<source>[A-Za-z0-9_<][A-Za-z0-9_.<>-]*):(?P<target>/[^:]+)(?::[a-z,]+)?$"
)

#: A top-level volume declaration, e.g. ``  billet_gh_config:``.
_VOLUME_DECLARATION = re.compile(r"^\s+(?P<name>[A-Za-z0-9_<][A-Za-z0-9_.<>-]*):\s*$")


def _named_volume_mounts(compose: Path) -> dict[str, str]:
    """Map each named volume a compose file mounts to the container path it lands on.

    Only service-level ``volumes:`` blocks (which are indented) are read; the top-level
    ``volumes:`` mapping declares names and mounts nothing.

    Parameters
    ----------
    compose
        The compose file to scan.

    Returns
    -------
    dict[str, str]
        Volume name to container path, for named volumes only. Bind mounts are skipped.
    """
    mounts: dict[str, str] = {}
    block_indent: int | None = None
    for raw in compose.read_text().splitlines():
        stripped: str = raw.strip()
        indent: int = len(raw) - len(raw.lstrip())
        if not stripped or stripped.startswith("#"):
            continue
        if block_indent is not None and indent <= block_indent:
            block_indent = None
        if stripped == "volumes:" and indent > 0:
            block_indent = indent
            continue
        if block_indent is None:
            continue
        match = _NAMED_VOLUME_MOUNT.match(stripped)
        if match is not None:
            mounts[match.group("source")] = match.group("target")
    return mounts


def _declared_volumes(compose: Path) -> set[str]:
    """The names in a compose file's top-level ``volumes:`` mapping.

    Parameters
    ----------
    compose
        The compose file to scan.

    Returns
    -------
    set[str]
        Every declared volume name.
    """
    declared: set[str] = set()
    in_block: bool = False
    for raw in compose.read_text().splitlines():
        stripped: str = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw == "volumes:":
            in_block = True
            continue
        if not in_block:
            continue
        if not raw.startswith((" ", "\t")):
            break
        match = _VOLUME_DECLARATION.match(raw)
        if match is not None:
            declared.add(match.group("name"))
    return declared


def _precreates_dev_owned(dockerfile: Path, path: str) -> bool:
    """Whether a Dockerfile creates ``path`` as a 0700 directory owned by ``dev``.

    Parameters
    ----------
    dockerfile
        The Dockerfile (or merge snippet) to scan.
    path
        The absolute container path that must be pre-created.

    Returns
    -------
    bool
        ``True`` when an ``install -d`` for the path carries dev ownership and mode 0700.
    """
    pattern = re.compile(
        rf"install\s+-d\s+-o\s+dev\s+-g\s+dev\s+-m\s+0700\s+{re.escape(path)}(?:\s|\\|$)"
    )
    return pattern.search(dockerfile.read_text()) is not None


@pytest.mark.parametrize(
    ("compose", "volume"),
    [
        (_TEMPLATE_COMPOSE, f"{_SERVICE_PLACEHOLDER}_gh_config"),
        (_DEVCONTAINER_COMPOSE, f"{_BILLET_SERVICE}_gh_config"),
    ],
    ids=["template", "devcontainer"],
)
def test_gh_credentials_are_persisted_on_a_named_volume(compose: Path, volume: str) -> None:
    # The regression (issue #58): hosts.yml on the container filesystem, so every
    # `compose up --build` threw the operator's GitHub auth away.
    assert _named_volume_mounts(compose).get(volume) == _GH_CONFIG_PATH, (
        f"{compose.relative_to(_REPO_ROOT)} must mount the named volume `{volume}` at "
        f"{_GH_CONFIG_PATH} so `gh auth login` survives a container rebuild."
    )


@pytest.mark.parametrize(
    "compose",
    [_TEMPLATE_COMPOSE, _DEVCONTAINER_COMPOSE],
    ids=["template", "devcontainer"],
)
def test_every_mounted_named_volume_is_declared(compose: Path) -> None:
    # Compose errors out at `up` on an undeclared named volume, i.e. only on the VM,
    # long after the review that introduced it.
    mounted: set[str] = set(_named_volume_mounts(compose))
    undeclared: set[str] = mounted - _declared_volumes(compose)
    assert not undeclared, (
        f"{compose.relative_to(_REPO_ROOT)} mounts {sorted(undeclared)} without declaring "
        "them under the top-level `volumes:` mapping."
    )


@pytest.mark.parametrize(
    "dockerfile",
    [_TEMPLATE_DOCKERFILE, _DEVCONTAINER_DOCKERFILE],
    ids=["template", "devcontainer"],
)
def test_the_gh_config_mountpoint_is_pre_created_dev_owned(dockerfile: Path) -> None:
    # Without this the daemon creates the mountpoint root:root on first `up` and `gh`
    # cannot write hosts.yml — the volume would persist an empty, unwritable directory.
    assert _precreates_dev_owned(dockerfile, _GH_CONFIG_PATH), (
        f"{dockerfile.relative_to(_REPO_ROOT)} must `install -d -o dev -g dev -m 0700 "
        f"{_GH_CONFIG_PATH}` so the named volume lands writable by the non-root user."
    )
    # `install -d` gives created parents default ownership and mode, not the flags' — so
    # the parent is created explicitly too, or ~/.config ends up 0755.
    assert _precreates_dev_owned(dockerfile, "/home/dev/.config"), (
        f"{dockerfile.relative_to(_REPO_ROOT)} must pre-create /home/dev/.config itself; "
        "`install -d` does not apply -o/-g/-m to the parents it creates."
    )


@pytest.mark.parametrize(
    "dockerfile",
    [_TEMPLATE_DOCKERFILE, _DEVCONTAINER_DOCKERFILE],
    ids=["template", "devcontainer"],
)
def test_mountpoints_are_created_before_dropping_to_the_non_root_user(dockerfile: Path) -> None:
    # `install -o dev` needs root; after `USER dev` the layer would fail to build.
    text: str = dockerfile.read_text()
    user_line: int = text.find("\nUSER dev")
    creation: int = text.find(f"install -d -o dev -g dev -m 0700 {_GH_CONFIG_PATH}")
    assert user_line != -1, f"{dockerfile.relative_to(_REPO_ROOT)} has no `USER dev` directive"
    assert creation != -1, (
        f"{dockerfile.relative_to(_REPO_ROOT)} never pre-creates {_GH_CONFIG_PATH}"
    )
    assert creation < user_line, (
        f"{dockerfile.relative_to(_REPO_ROOT)} creates {_GH_CONFIG_PATH} after `USER dev`; "
        "setting ownership requires root, so it must come before that directive."
    )


def test_billet_devcontainer_implements_every_volume_the_template_prescribes() -> None:
    # billet runs itself as a Workspace, so it is consumer #1 of these templates: a
    # template volume that is not mirrored here has never actually been exercised.
    template: dict[str, str] = {
        name.replace(_SERVICE_PLACEHOLDER, _BILLET_SERVICE): target
        for name, target in _named_volume_mounts(_TEMPLATE_COMPOSE).items()
    }
    devcontainer: dict[str, str] = _named_volume_mounts(_DEVCONTAINER_COMPOSE)
    missing: dict[str, str] = {
        name: target for name, target in template.items() if devcontainer.get(name) != target
    }
    assert not missing, (
        "billet's own .devcontainer/docker-compose.yml is missing the template's named "
        f"volumes {sorted(missing)} (with `{_SERVICE_PLACEHOLDER}` = `{_BILLET_SERVICE}`). "
        "Port the template change into billet's devcontainer as well."
    )
