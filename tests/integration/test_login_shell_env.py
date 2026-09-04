"""Live acceptance: the container environment reaches an SSH session (ADR-0003/ADR-0006).

An sshd session is a fresh PAM session that inherits nothing from the entrypoint process,
so Docker ``ENV`` and compose ``environment:`` values used to be invisible to
``billet connect``. ``dev-entrypoint.sh`` now snapshots its environment into
``/etc/environment``, which Debian's ``pam_env.so`` (wired in ``/etc/pam.d/sshd``, with
``UsePAM yes``) replays into every session. This checks that end to end, over real ssh.

Gating matches the rest of this tree (a no-op in CI even though CI collects all of
``tests/``):

- module ``skipif(not BILLET_INTEGRATION)`` — auto-skips whenever the env var is unset;
- ``@pytest.mark.integration`` — documented marker for an explicit ``-m`` selection;
- ``conftest.py`` fixtures skip (never fail) on any missing prerequisite.

Assumptions, all of which **skip** rather than fail: the Workspace is already started and
reachable through the billet-rendered ssh config, and it was started by an entrypoint new
enough to write the block (an older container restarted before this change has no block —
that is a stale environment, not a regression). The probe variable is whatever the
container itself published, so this asserts the mechanism without pinning a name the image
is free to change.
"""

import os
import subprocess

import pytest

from tests.integration.conftest import GSWA, Workspace

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("BILLET_INTEGRATION"),
        reason="live login-shell env acceptance; set BILLET_INTEGRATION=1 to run",
    ),
]

#: Block markers dev-entrypoint.sh writes around the environment it owns.
_BEGIN = "# >>> billet dev-entrypoint: container environment (regenerated on start) >>>"
_END = "# <<< billet dev-entrypoint <<<"

#: Where pam_env reads the login-shell environment from.
_ENV_FILE = "/etc/environment"


def _ssh(alias: str, command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run ``command`` in a non-interactive ssh session on ``alias``."""
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _unquote(value: str) -> str:
    """Strip the one surrounding quote pair pam_env removes, as pam_env would."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


@pytest.fixture
def container_alias(workspaces: dict[str, Workspace]) -> str:
    """The ssh alias of the Workspace container this acceptance probes."""
    return workspaces[GSWA].container_alias


@pytest.fixture
def env_file(container_alias: str) -> str:
    """The container's ``/etc/environment`` (skip if unreachable or not yet published)."""
    result = _ssh(container_alias, f"cat {_ENV_FILE}")
    if result.returncode != 0:
        pytest.skip(f"cannot read {_ENV_FILE} on {container_alias!r}: {result.stderr.strip()}")
    if _BEGIN not in result.stdout:
        pytest.skip(
            f"no billet block in {_ENV_FILE} on {container_alias!r}; restart the Workspace "
            "so the current dev-entrypoint.sh publishes the container environment"
        )
    return result.stdout


@pytest.fixture
def published_env(env_file: str) -> dict[str, str]:
    """The variables the entrypoint published, parsed out of the billet block."""
    lines = env_file.splitlines()
    block = lines[lines.index(_BEGIN) + 1 : lines.index(_END)]
    published: dict[str, str] = {}
    for line in block:
        key, sep, value = line.partition("=")
        if sep:
            published[key] = _unquote(value)
    return published


@pytest.fixture
def probe(published_env: dict[str, str]) -> tuple[str, str]:
    """One published, non-empty variable to assert an ssh session actually receives."""
    for key, value in sorted(published_env.items()):
        if value:
            return key, value
    pytest.skip("the container published no non-empty variable to probe")


def test_published_variable_reaches_a_non_interactive_ssh_session(
    container_alias: str, probe: tuple[str, str]
) -> None:
    key, expected = probe
    result = _ssh(container_alias, f'printf %s "${key}"')
    assert result.returncode == 0, f"ssh to {container_alias!r} failed: {result.stderr.strip()}"
    assert result.stdout == expected, (
        f"{key} is {expected!r} in {_ENV_FILE} but {result.stdout!r} over ssh — pam_env is "
        "not replaying the container environment into the session"
    )


def test_env_file_holds_exactly_one_billet_block(env_file: str) -> None:
    # The entrypoint regenerates between the markers on every start; two blocks would mean
    # it is appending instead, and the file grows without bound across restarts.
    assert env_file.count(_BEGIN) == 1
    assert env_file.count(_END) == 1
