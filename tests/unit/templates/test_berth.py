"""Tests for the Berth stamp and the mount-time ownership repair in ``dev-entrypoint.sh``.

The Berth (ADR-0012) is the Workspace runtime contract billet publishes under
``templates/workspace/``; ``berth.version`` is its one-integer version and the entrypoint
prints it on every start. ADR-0013 adds the entrypoint's one new job: repairing the
ownership of named-volume mount targets under the login user's home that came up
root-owned and empty, and ensuring ``~/.ssh`` — before ``ssh-keygen``, which is the slow
cold-start step and what billet's token injection races.

These are contract tests on the shipped text plus a real run of the mountinfo parser
through the script's documented source seam (``BILLET_ENTRYPOINT_SOURCE_ONLY=1``). The
Docker behaviour itself is exercised by ``tests/integration/test_berth_ownership_matrix.py``.
Both copies of the entrypoint are checked: the template consumers copy and billet's own
``.devcontainer/`` copy, which dogfoods it.
"""

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_DIR = _REPO_ROOT / "templates" / "workspace"
_DEVCONTAINER_DIR = _REPO_ROOT / ".devcontainer"

_ENTRYPOINTS = [_TEMPLATE_DIR / "dev-entrypoint.sh", _DEVCONTAINER_DIR / "dev-entrypoint.sh"]
_VERSIONS = [_TEMPLATE_DIR / "berth.version", _DEVCONTAINER_DIR / "berth.version"]
_COPY_IDS = ["template", "devcontainer"]

#: The statements the Berth adds to the entrypoint, as shipped.
_SOURCE_ONLY_RETURN = '[ -z "${BILLET_ENTRYPOINT_SOURCE_ONLY:-}" ] || return 0'
_BERTH_PRINT = 'echo "dev-entrypoint: berth=$(cat "$(dirname "$0")/berth.version" 2>/dev/null || echo unknown)"'
_SSH_ENSURE = 'berth_ensure_dir "${HOME}/.ssh" create'
_MOUNT_LOOP = "berth_mount_targets /proc/self/mountinfo"
_FIRST_RUNTIME_SUDO = "sudo install -d -m 0755 /run/sshd"
_HOST_KEYGEN = "sudo ssh-keygen"


def _runtime_section(script: Path) -> str:
    """The part of the script that only runs in a container: after the source-only seam."""
    text: str = script.read_text()
    index: int = text.find(_SOURCE_ONLY_RETURN)
    assert index != -1, f"{script} lost its BILLET_ENTRYPOINT_SOURCE_ONLY seam"
    return text[index + len(_SOURCE_ONLY_RETURN) :]


def _source_and_run(function_call: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Source the template through its seam and run one Berth function under ``env``."""
    template: Path = _ENTRYPOINTS[0]
    child_env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "BILLET_ENTRYPOINT_SOURCE_ONLY": "1",
        **env,
    }
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(template))}; {function_call}"],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("version", _VERSIONS, ids=_COPY_IDS)
def test_berth_version_is_a_single_positive_integer(version: Path) -> None:
    # One integer and a newline, nothing else: the entrypoint interpolates `cat` of this
    # file into a log line, and doctor (ADR-0015) will parse it.
    text: str = version.read_text()
    assert text.endswith("\n"), f"{version.relative_to(_REPO_ROOT)} must end with a newline"
    assert text.strip().isdigit(), f"{version.relative_to(_REPO_ROOT)} is not an integer"
    assert int(text) > 0, f"{version.relative_to(_REPO_ROOT)} must be a positive integer"
    assert text == f"{int(text)}\n", f"{version.relative_to(_REPO_ROOT)} carries extra text"


def test_berth_version_is_identical_in_both_locations() -> None:
    # billet's own .devcontainer/ is a consumer of the templates; a stamp that disagrees
    # means one copy was bumped without the other, which is the drift the stamp exists to
    # make visible.
    template, devcontainer = (path.read_text() for path in _VERSIONS)
    assert template == devcontainer, (
        f"{_VERSIONS[0].relative_to(_REPO_ROOT)} says {template.strip()!r} but "
        f"{_VERSIONS[1].relative_to(_REPO_ROOT)} says {devcontainer.strip()!r}; copy the "
        "template's berth.version into .devcontainer/ alongside the entrypoint."
    )


@pytest.mark.parametrize("script", _ENTRYPOINTS, ids=_COPY_IDS)
def test_berth_version_is_the_first_runtime_log_line(script: Path) -> None:
    # Read from the sibling file rather than hardcoded, so a re-copied entrypoint cannot
    # misreport and a copy made without berth.version says `unknown`. Printed before any
    # other runtime statement so it is the first line in `docker compose logs`.
    runtime: str = _runtime_section(script)
    assert _BERTH_PRINT in runtime, f"{script.relative_to(_REPO_ROOT)} does not print berth="
    assert runtime.index(_BERTH_PRINT) < runtime.index(_FIRST_RUNTIME_SUDO), (
        f"{script.relative_to(_REPO_ROOT)} must print the Berth version before doing anything "
        "else at runtime"
    )


@pytest.mark.parametrize("script", _ENTRYPOINTS, ids=_COPY_IDS)
def test_ssh_is_ensured_then_mount_targets_repaired_before_host_key_generation(
    script: Path,
) -> None:
    # Order is the contract (ADR-0013 item 4): ~/.ssh first (sshd's authorized_keys mount
    # lives under it), then every named-volume target, and all of it before ssh-keygen —
    # the slow cold-start step — so the root-owned window is as short as the script can
    # make it and billet's token injection (ADR-0006) finds ~/.claude writable.
    runtime: str = _runtime_section(script)
    for statement in (_SSH_ENSURE, _MOUNT_LOOP, _HOST_KEYGEN):
        assert statement in runtime, f"{script.relative_to(_REPO_ROOT)} lacks `{statement}`"
    assert runtime.index(_SSH_ENSURE) < runtime.index(_MOUNT_LOOP) < runtime.index(_HOST_KEYGEN), (
        f"{script.relative_to(_REPO_ROOT)} must ensure ~/.ssh, then repair mount targets, "
        "then generate host keys — in that order"
    )


@pytest.mark.parametrize("script", _ENTRYPOINTS, ids=_COPY_IDS)
def test_repair_is_never_recursive(script: Path) -> None:
    # `install -d` on the target itself, never `chown -R`: a populated root-owned volume
    # holds someone's files and gets a warning, not a re-own. Comment lines are dropped
    # first so the prose that states this rule cannot trip it.
    text: str = "\n".join(
        line for line in script.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert re.search(r"\bchown\b", text) is None, (
        f"{script.relative_to(_REPO_ROOT)} calls chown; the repair policy is `install -d` on "
        "the empty target only"
    )
    assert "sudo -n install -d" in text


def test_mount_targets_are_named_volumes_under_home_only(tmp_path: Path) -> None:
    # A mountinfo(5) with the shapes a Workspace actually has: the overlay root, a Locker
    # under $HOME, the sshd-keys volume outside it, the authorized_keys and workspace bind
    # mounts, a volume path with an escaped space, and a volume under a *different* user's
    # home whose name merely starts with $HOME.
    mountinfo: Path = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 0:20 / / rw,relatime - overlay overlay rw\n"
        "100 22 8:1 /var/lib/docker/volumes/billet_claude_home/_data /home/dev/.claude "
        "rw,relatime - ext4 /dev/sda1 rw\n"
        "101 22 8:1 /var/lib/docker/volumes/billet-sshd-keys/_data /etc/ssh/host_keys "
        "rw,relatime - ext4 /dev/sda1 rw\n"
        "102 22 8:1 /home/azureuser/.ssh/authorized_keys /home/dev/.ssh/authorized_keys "
        "ro,relatime - ext4 /dev/sda1 rw\n"
        "103 22 8:1 /home/azureuser/billet /workspace rw,relatime - ext4 /dev/sda1 rw\n"
        "104 22 8:1 /mnt/docker/volumes/with\\040space/_data /home/dev/.config/my\\040tool "
        "rw,relatime - ext4 /dev/sda1 rw\n"
        "105 22 8:1 /var/lib/docker/volumes/other/_data /home/devon/.claude "
        "rw,relatime - ext4 /dev/sda1 rw\n"
    )
    result = _source_and_run(
        f"berth_mount_targets {shlex.quote(str(mountinfo))}", {"HOME": "/home/dev"}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["/home/dev/.claude", "/home/dev/.config/my tool"]


def test_mount_targets_tolerates_a_missing_mountinfo(tmp_path: Path) -> None:
    # `set -e` is on: an unreadable mountinfo must not abort the entrypoint before sshd.
    result = _source_and_run(
        f"berth_mount_targets {shlex.quote(str(tmp_path / 'absent'))}; echo rc=$?",
        {"HOME": "/home/dev"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rc=0\n"


def test_ensure_dir_skips_a_missing_mount_target_and_a_non_directory(tmp_path: Path) -> None:
    # `skip` mode is for mountinfo targets: a path that is gone or is not a directory is
    # not ours. `create` mode never replaces a file either. Neither path reaches sudo.
    regular_file: Path = tmp_path / "file"
    regular_file.write_text("")
    result = _source_and_run(
        f"berth_ensure_dir {shlex.quote(str(tmp_path / 'absent'))} skip; "
        f"berth_ensure_dir {shlex.quote(str(regular_file))} create; echo rc=$?",
        {"HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rc=0\n"
    assert result.stderr == ""
    assert regular_file.is_file()


@pytest.mark.skipif(sys.platform != "linux", reason="GNU stat -c; the entrypoint targets Debian")
def test_ensure_dir_leaves_a_directory_the_login_user_owns_alone(tmp_path: Path) -> None:
    # Already dev-owned: nothing to do, mode included — a 0755 ~/.azure an operator relies
    # on stays 0755.
    owned: Path = tmp_path / "owned"
    owned.mkdir(mode=0o755)
    result = _source_and_run(
        f"berth_ensure_dir {shlex.quote(str(owned))} skip; echo rc=$?", {"HOME": str(tmp_path)}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "rc=0\n"
    assert result.stderr == ""
    assert (owned.stat().st_mode & 0o777) == 0o755
