"""Tests for the ``BILLET_`` naming contract the Workspace templates carry.

Every environment variable billet owns is namespaced ``BILLET_*`` — ``BILLET_AUTHORIZED_KEYS``,
``BILLET_CONTAINER_SSH_PORT``. The name a variable is given is a published interface: the
compose file a repo merged months ago interpolates it, and billet's remote-script prelude
exports it; nothing re-reads either at review time. So a variable that drifts to another
prefix (issue #60's ``DEVBOX_*``) fails the way compose interpolation always fails —
silently. The mount falls back to its default, the container's sshd trusts the empty
``authorized_keys`` stub, and the operator discovers it as a refused ``billet connect`` on
the VM, long after the PR that caused it.

These tests pin the naming rule as a general scan rather than a spot check on one variable:
every ``${NAME}`` a compose file interpolates and every ``NAME=`` a compose
``environment:`` list assigns must carry the prefix, so the *next* mis-prefixed variable is
caught by the same assertion. ``_DEPRECATED_ALIASES`` is the escape hatch for a rename in
flight; it is empty, and a name added to it is a promise to delete it again. A variable a
tool owns (``CLAUDE_CONFIG_DIR`` is Claude Code's) is set in mapping form and is not
billet's to name; the Locker tests cover it.

They also pin the two ends of the contract against each other: every variable the template
interpolates is one billet's prelude actually exports, and the mount resolves from the named
variable first with the tracked stub as the default — which is what keeps a build away from
the VM from hard-failing. And billet runs itself as a Workspace, making it consumer #1 of
these templates: the mount is checked in both ``templates/workspace/`` and billet's own
``.devcontainer/``, because a template change that is not mirrored there has never actually
been dogfooded.
"""

from dataclasses import dataclass
from pathlib import Path
import re

import pytest

from billet.access.container.compose_container_access import ComposeContainerAccess
from tests.unit._fakes import (
    FakeProcessRunner,
    completed,
    make_devcontainer_facts,
    make_remote_host,
    make_workspace_spec,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_TEMPLATE_COMPOSE = _REPO_ROOT / "templates" / "workspace" / "docker-compose.snippet.yml"
_DEVCONTAINER_COMPOSE = _REPO_ROOT / ".devcontainer" / "docker-compose.yml"

#: The namespace every billet-owned environment variable lives in.
_BILLET_PREFIX = "BILLET_"

#: Pre-rename names still honoured as compose fallbacks while a rename is in flight. Empty:
#: the ``DEVBOX_AUTHORIZED_KEYS`` fallback was removed in 0.2.0 once every Workspace had
#: adopted the new name. Each entry added here is a promise to delete it again.
_DEPRECATED_ALIASES: frozenset[str] = frozenset()

_AUTHORIZED_KEYS_VAR = "BILLET_AUTHORIZED_KEYS"

#: The pre-rename name. No longer resolved by any compose file; still asserted against so a
#: copy-paste from an old repo cannot quietly bring it back.
_DEPRECATED_AUTHORIZED_KEYS_VAR = "DEVBOX_AUTHORIZED_KEYS"

#: The tracked empty stub the mount falls back to off-VM, and where it lands in-container.
_AUTHORIZED_KEYS_STUB = "./authorized_keys-stub"
_AUTHORIZED_KEYS_TARGET = "/home/dev/.ssh/authorized_keys"

#: A compose ``${NAME...}`` interpolation, matched at its opening brace so the outer and the
#: nested name of ``${A:-${B:-default}}`` are both found. Requiring ``${`` is what keeps the
#: ``<service>`` / ``<port>`` / ``<workspaceFolder>`` placeholders the template carries from
#: being read as variables — they are substituted by the adopting repo, not by compose.
_INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)")

#: A ``NAME=value`` assignment, as a compose ``environment:`` list entry or a shell
#: ``export NAME=`` line in the prelude billet runs on the Host.
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
        The compose file to read.

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
    interpolations (nested ones included) and ``NAME=`` assignments in a compose
    ``environment:`` list.

    Parameters
    ----------
    path
        The compose file to scan.

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


def _prelude_exports() -> set[str]:
    """Every ``export NAME=`` the remote-script prelude emits ahead of a compose call.

    Captured by driving a real :class:`ComposeContainerAccess` against a fake runner and
    scanning the script it fed to ``bash -se`` — the same scan the templates get, so the two
    ends of the contract are read by one rule.

    Returns
    -------
    set[str]
        The variable names exported.
    """
    runner: FakeProcessRunner = FakeProcessRunner(lambda _argv: completed())
    access: ComposeContainerAccess = ComposeContainerAccess(runner)
    access.compose_up(make_workspace_spec(), make_remote_host(), make_devcontainer_facts())
    script: str | None = runner.inputs[-1]
    assert script is not None
    names: set[str] = set()
    for line in script.splitlines():
        exported: re.Match[str] | None = _ASSIGNMENT.match(line.strip())
        if exported is not None and line.strip().startswith("export "):
            names.add(exported.group("name"))
    return names


@pytest.mark.parametrize(
    "path",
    [_TEMPLATE_COMPOSE, _DEVCONTAINER_COMPOSE],
    ids=["template-compose", "devcontainer-compose"],
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
        f"with `{_BILLET_PREFIX}`. Every variable billet owns is namespaced — a consumer's "
        "compose file interpolates these and billet's prelude exports them, and nothing "
        "re-reads either at review time. Rename it, or, if it is a pre-rename alias kept as "
        "a compose fallback, add it to _DEPRECATED_ALIASES with the release that deletes it."
    )


@pytest.mark.parametrize(
    "compose",
    [_TEMPLATE_COMPOSE, _DEVCONTAINER_COMPOSE],
    ids=["template", "devcontainer"],
)
def test_billet_exports_every_variable_the_compose_interpolates(compose: Path) -> None:
    # The other end of the contract. Before 0.4.0 BILLET_AUTHORIZED_KEYS came from an .env
    # an operator copied by hand, and the templates could name a variable nothing set. Now
    # every `${BILLET_*}` a compose file interpolates must be an `export` in the prelude
    # billet runs before compose — or the mount falls back to the stub and sshd trusts no
    # keys, silently, on the VM.
    interpolated: set[str] = {
        name for name in _referenced_variables(compose) if name.startswith(_BILLET_PREFIX)
    }
    assert interpolated, f"{compose.relative_to(_REPO_ROOT)} interpolates no BILLET_* variable"
    exported: set[str] = _prelude_exports()
    assert exported, "the compose prelude exported nothing — the scan no longer matches it"
    unset: set[str] = interpolated - exported
    assert not unset, (
        f"{compose.relative_to(_REPO_ROOT)} interpolates {sorted(unset)}, which billet's "
        "remote-script prelude never exports (compose_container_access._prelude). Compose "
        "would silently take the default. Export it there, or drop it from the template."
    )


@pytest.mark.parametrize(
    "compose",
    [_TEMPLATE_COMPOSE, _DEVCONTAINER_COMPOSE],
    ids=["template", "devcontainer"],
)
def test_the_authorized_keys_mount_resolves_from_the_billet_variable_alone(compose: Path) -> None:
    # Parsed rather than substring-matched so a reintroduced fallback layer is caught too:
    # 0.2.0 removed the DEVBOX_AUTHORIZED_KEYS default, and a stale name silently winning
    # again is exactly the drift this module exists to stop.
    chain: _Interpolation = _interpolation_chain(_mount_source(_authorized_keys_mount(compose)))
    expected: tuple[str, ...] = (_AUTHORIZED_KEYS_VAR,)
    assert chain.names == expected, (
        f"{compose.relative_to(_REPO_ROOT)} resolves the {_AUTHORIZED_KEYS_TARGET} mount from "
        f"{list(chain.names)}, not {list(expected)}. The interpolation must read "
        f"`${{{_AUTHORIZED_KEYS_VAR}:-{_AUTHORIZED_KEYS_STUB}}}` — a single level, with no "
        "pre-rename fallback behind it."
    )
    assert chain.default == _AUTHORIZED_KEYS_STUB, (
        f"{compose.relative_to(_REPO_ROOT)} falls back to `{chain.default}` rather than "
        f"`{_AUTHORIZED_KEYS_STUB}` when neither variable is set. The tracked empty stub is "
        "what keeps a build off the VM from hard-failing on a missing bind-mount source."
    )


def test_the_prelude_exports_the_new_variable_name_only() -> None:
    # The prelude replaced the .env.example an operator used to copy (0.4.0). Since 0.2.0 the
    # compose has no fallback, so a prelude exporting the old name would set a value nothing
    # reads and the stub would win silently.
    exported: set[str] = _prelude_exports()
    assert _AUTHORIZED_KEYS_VAR in exported, (
        f"billet's compose prelude must `export {_AUTHORIZED_KEYS_VAR}=` so the container's "
        "sshd trusts the Host admin user's authorized_keys with no file to copy."
    )
    assert _DEPRECATED_AUTHORIZED_KEYS_VAR not in exported, (
        f"billet's compose prelude still exports `{_DEPRECATED_AUTHORIZED_KEYS_VAR}`, which "
        f"no compose file reads any more — the fallback was removed in 0.2.0."
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
