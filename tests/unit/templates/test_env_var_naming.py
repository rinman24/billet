"""Tests for the ``BILLET_`` naming contract the Workspace templates carry.

Every environment variable billet owns is namespaced ``BILLET_*`` — ``BILLET_AUTHORIZED_KEYS``,
``BILLET_CONTAINER_SSH_PORT``. The name a variable is given is a published interface: it is
typed into a gitignored ``.devcontainer/.env`` on a VM, and nothing re-reads that file at
review time. So a variable that drifts to another prefix (issue #60's ``DEVBOX_*``) fails
the way compose interpolation always fails — silently. The mount falls back to its default,
the container's sshd trusts the empty ``authorized_keys`` stub, and the operator discovers
it as a refused ``billet connect`` on the VM, long after the PR that caused it.

These tests pin the naming rule as a general scan rather than a spot check on one variable:
every ``${NAME}`` a compose file interpolates and every ``NAME=`` an ``.env`` example assigns
must carry the prefix, so the *next* mis-prefixed variable is caught by the same assertion.
The single exception is the pre-rename ``DEVBOX_AUTHORIZED_KEYS`` alias, allow-listed below
and honoured only as a nested fallback so an ``.env`` already sitting on a VM keeps working.

They also pin the shape of that fallback. The precedence — new name, then deprecated alias,
then the tracked stub — is the whole point of the nested interpolation: a stale
``DEVBOX_AUTHORIZED_KEYS`` must not beat a ``BILLET_AUTHORIZED_KEYS`` the operator has since
set. Order is invisible to a substring match, so it is parsed here. And billet runs itself
as a Workspace, making it consumer #1 of these templates: the mount is checked in both
``templates/workspace/`` and billet's own ``.devcontainer/``, because a template change that
is not mirrored there has never actually been dogfooded.
"""

from dataclasses import dataclass
from pathlib import Path
import re

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

_TEMPLATE_COMPOSE = _REPO_ROOT / "templates" / "workspace" / "docker-compose.snippet.yml"
_TEMPLATE_ENV_EXAMPLE = _REPO_ROOT / "templates" / "workspace" / "env.example"
_DEVCONTAINER_COMPOSE = _REPO_ROOT / ".devcontainer" / "docker-compose.yml"
_DEVCONTAINER_ENV_EXAMPLE = _REPO_ROOT / ".devcontainer" / ".env.example"

#: The namespace every billet-owned environment variable lives in.
_BILLET_PREFIX = "BILLET_"

#: Pre-rename names still honoured as fallbacks, so an ``.env`` written before the rename
#: keeps working on a VM nobody has revisited. Each entry is a promise to delete: the
#: compose fallbacks go away in billet 0.2.0 and this allow-list empties out with them.
_DEPRECATED_ALIASES = frozenset({"DEVBOX_AUTHORIZED_KEYS"})

_AUTHORIZED_KEYS_VAR = "BILLET_AUTHORIZED_KEYS"
_DEPRECATED_AUTHORIZED_KEYS_VAR = "DEVBOX_AUTHORIZED_KEYS"

#: The tracked empty stub the mount falls back to off-VM, and where it lands in-container.
_AUTHORIZED_KEYS_STUB = "./authorized_keys-stub"
_AUTHORIZED_KEYS_TARGET = "/home/dev/.ssh/authorized_keys"

#: A compose ``${NAME...}`` interpolation, matched at its opening brace so the outer and the
#: nested name of ``${A:-${B:-default}}`` are both found. Requiring ``${`` is what keeps the
#: ``<service>`` / ``<port>`` / ``<workspaceFolder>`` placeholders the template carries from
#: being read as variables — they are substituted by the adopting repo, not by compose.
_INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)")

#: A ``NAME=value`` assignment, as an ``.env`` line or a compose ``environment:`` list entry.
_ASSIGNMENT = re.compile(r"^(?:-\s+)?(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=")

#: One ``${NAME:-default}`` layer, unwrapped one at a time by :func:`_interpolation_chain`.
_FALLBACK = re.compile(r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*):-(?P<default>.*)\}$")

#: The service-level bind mount that hands the operator's authorized_keys to sshd.
_AUTHORIZED_KEYS_MOUNT = re.compile(
    rf"^-\s+(?P<source>.+?):{re.escape(_AUTHORIZED_KEYS_TARGET)}(?::[a-z,]+)?$"
)


@dataclass(frozen=True)
class _Interpolation:
    """A parsed ``${A:-${B:-literal}}`` chain, in the order compose resolves it."""

    names: tuple[str, ...]
    default: str


def _uncommented_lines(path: Path) -> list[str]:
    """Every meaningful line of a template file, stripped of its indentation.

    Blank lines and whole-line comments are dropped. The templates explain the deprecated
    alias in prose, so a scan that read comments would flag the very text documenting the
    deprecation it is meant to permit.

    Parameters
    ----------
    path
        The compose file or ``.env`` example to read.

    Returns
    -------
    list[str]
        The stripped lines, in file order.
    """
    lines: list[str] = []
    for raw in path.read_text().splitlines():
        stripped: str = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def _referenced_variables(path: Path) -> set[str]:
    """Every environment variable name a template file interpolates or assigns.

    One pass covers both shapes billet-owned names appear in: compose ``${NAME}``
    interpolations (nested ones included) and ``NAME=`` assignments, whether they sit in an
    ``.env`` example or in a compose ``environment:`` list.

    Parameters
    ----------
    path
        The compose file or ``.env`` example to scan.

    Returns
    -------
    set[str]
        Every variable name referenced, deprecated aliases included.
    """
    names: set[str] = set()
    for line in _uncommented_lines(path):
        for interpolation in _INTERPOLATION.finditer(line):
            names.add(interpolation.group("name"))
        assignment: re.Match[str] | None = _ASSIGNMENT.match(line)
        if assignment is not None:
            names.add(assignment.group("name"))
    return names


def _authorized_keys_mount(compose: Path) -> str:
    """The stripped mount line that binds the operator's authorized_keys into a Workspace.

    Parameters
    ----------
    compose
        The compose file to scan.

    Returns
    -------
    str
        The mount line, indentation removed.

    Raises
    ------
    AssertionError
        If the compose file mounts nothing at the container's authorized_keys path.
    """
    for line in _uncommented_lines(compose):
        if _AUTHORIZED_KEYS_MOUNT.match(line) is not None:
            return line
    raise AssertionError(
        f"{compose.relative_to(_REPO_ROOT)} mounts nothing at {_AUTHORIZED_KEYS_TARGET}; "
        "the container's sshd would trust whatever the image happens to ship. Restore the "
        f"`${{{_AUTHORIZED_KEYS_VAR}:-...}}` bind mount."
    )


def _mount_source(mount_line: str) -> str:
    """The left-hand side of a mount line — the host path or interpolation being mounted.

    Parameters
    ----------
    mount_line
        A stripped compose mount line, as returned by :func:`_authorized_keys_mount`.

    Returns
    -------
    str
        The mount's source expression.

    Raises
    ------
    AssertionError
        If the line is not a mount of the container's authorized_keys path.
    """
    match: re.Match[str] | None = _AUTHORIZED_KEYS_MOUNT.match(mount_line)
    if match is None:
        raise AssertionError(f"`{mount_line}` is not an {_AUTHORIZED_KEYS_TARGET} mount")
    return match.group("source")


def _interpolation_chain(expression: str) -> _Interpolation:
    """Unwrap a nested ``${A:-${B:-literal}}`` expression into its resolution order.

    Each layer is peeled in turn, so the names come back in the order compose consults
    them and whatever is left once no layer matches is the literal default.

    Parameters
    ----------
    expression
        A compose interpolation expression, e.g. ``${A:-${B:-./stub}}``.

    Returns
    -------
    _Interpolation
        The variable names in resolution order, and the literal that applies when every
        one of them is unset. An expression with no ``:-`` default yields no names and
        itself as the default.
    """
    names: list[str] = []
    remainder: str = expression
    while True:
        layer: re.Match[str] | None = _FALLBACK.match(remainder)
        if layer is None:
            return _Interpolation(names=tuple(names), default=remainder)
        names.append(layer.group("name"))
        remainder = layer.group("default")


@pytest.mark.parametrize(
    "path",
    [
        _TEMPLATE_COMPOSE,
        _TEMPLATE_ENV_EXAMPLE,
        _DEVCONTAINER_COMPOSE,
        _DEVCONTAINER_ENV_EXAMPLE,
    ],
    ids=["template-compose", "template-env", "devcontainer-compose", "devcontainer-env"],
)
def test_every_billet_owned_variable_uses_the_billet_prefix(path: Path) -> None:
    # The regression (issue #60): DEVBOX_AUTHORIZED_KEYS beside BILLET_CONTAINER_SSH_PORT.
    # A general scan, not a spot check — the bug is a *prefix* drifting, so the next
    # variable to drift has to fail here too.
    referenced: set[str] = _referenced_variables(path)
    assert referenced, (
        f"{path.relative_to(_REPO_ROOT)} referenced no environment variables at all. Either "
        "the file stopped configuring the Workspace or this scan's regexes no longer match "
        "it — a naming test that reads nothing passes vacuously forever."
    )
    offenders: set[str] = {
        name
        for name in referenced
        if not name.startswith(_BILLET_PREFIX) and name not in _DEPRECATED_ALIASES
    }
    assert not offenders, (
        f"{path.relative_to(_REPO_ROOT)} references {sorted(offenders)}, which do not start "
        f"with `{_BILLET_PREFIX}`. Every variable billet owns is namespaced — an operator "
        "types these into a gitignored .devcontainer/.env by hand and nothing re-reads that "
        "file at review time. Rename it, or, if it is a pre-rename alias kept as a compose "
        "fallback, add it to _DEPRECATED_ALIASES with the release that deletes it."
    )


@pytest.mark.parametrize(
    "compose",
    [_TEMPLATE_COMPOSE, _DEVCONTAINER_COMPOSE],
    ids=["template", "devcontainer"],
)
def test_the_authorized_keys_mount_prefers_billet_over_the_deprecated_alias(compose: Path) -> None:
    # Order is the contract, so it is parsed rather than substring-matched: an .env already
    # on a VM must keep working, *and* a BILLET_AUTHORIZED_KEYS the operator has since set
    # must win over the stale DEVBOX_AUTHORIZED_KEYS sitting next to it in the same file.
    chain: _Interpolation = _interpolation_chain(_mount_source(_authorized_keys_mount(compose)))
    expected: tuple[str, ...] = (_AUTHORIZED_KEYS_VAR, _DEPRECATED_AUTHORIZED_KEYS_VAR)
    assert chain.names == expected, (
        f"{compose.relative_to(_REPO_ROOT)} resolves the {_AUTHORIZED_KEYS_TARGET} mount from "
        f"{list(chain.names)}, not {list(expected)}. The nested interpolation must read "
        f"`${{{_AUTHORIZED_KEYS_VAR}:-${{{_DEPRECATED_AUTHORIZED_KEYS_VAR}:-"
        f"{_AUTHORIZED_KEYS_STUB}}}}}` so the new name wins and the deprecated one is only a "
        "fallback (removed in billet 0.2.0)."
    )
    assert chain.default == _AUTHORIZED_KEYS_STUB, (
        f"{compose.relative_to(_REPO_ROOT)} falls back to `{chain.default}` rather than "
        f"`{_AUTHORIZED_KEYS_STUB}` when neither variable is set. The tracked empty stub is "
        "what keeps a build off the VM from hard-failing on a missing bind-mount source."
    )


@pytest.mark.parametrize(
    "env_example",
    [_TEMPLATE_ENV_EXAMPLE, _DEVCONTAINER_ENV_EXAMPLE],
    ids=["template", "devcontainer"],
)
def test_the_env_example_assigns_only_the_new_variable_name(env_example: Path) -> None:
    # The .env.example is the file an operator copies (billet's host_bootstrap_cmd copies it
    # verbatim), so it is the one place the deprecated alias must not survive — teaching it
    # would mint fresh .env files that the 0.2.0 fallback removal breaks.
    assigned: set[str] = _referenced_variables(env_example)
    assert _AUTHORIZED_KEYS_VAR in assigned, (
        f"{env_example.relative_to(_REPO_ROOT)} must set `{_AUTHORIZED_KEYS_VAR}=` so the "
        "copy an operator makes on the VM points sshd at the VM's authorized_keys."
    )
    assert _DEPRECATED_AUTHORIZED_KEYS_VAR not in assigned, (
        f"{env_example.relative_to(_REPO_ROOT)} still assigns "
        f"`{_DEPRECATED_AUTHORIZED_KEYS_VAR}`. The alias is honoured as a compose fallback "
        f"for .env files that predate the rename, but the example teaches only "
        f"`{_AUTHORIZED_KEYS_VAR}`."
    )


def test_the_template_and_billet_devcontainer_mount_authorized_keys_identically() -> None:
    # billet runs itself as a Workspace, so it is consumer #1 of its own templates. This
    # mount carries no <service> placeholder, so the two lines compare byte-for-byte; any
    # difference means a template change was shipped without ever being dogfooded.
    template: str = _authorized_keys_mount(_TEMPLATE_COMPOSE)
    devcontainer: str = _authorized_keys_mount(_DEVCONTAINER_COMPOSE)
    assert template == devcontainer, (
        "The authorized_keys mount has drifted between "
        f"{_TEMPLATE_COMPOSE.relative_to(_REPO_ROOT)} and "
        f"{_DEVCONTAINER_COMPOSE.relative_to(_REPO_ROOT)}:\n"
        f"  template:     {template}\n"
        f"  devcontainer: {devcontainer}\n"
        "This line has no `<service>` placeholder, so port the change to both files."
    )
