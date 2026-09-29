"""Tests for SshDoctorAccess — one probe per Host, its command set, and section parsing.

The process runner is mocked (never a real ``ssh``); parsing runs over recorded probe
output. One test also runs the generated script through a local ``bash -se`` against a
temporary home, proving the missing-file marker and the no-trailing-newline framing on a
real shell without touching any Host.
"""

from pathlib import Path
import re
import subprocess

import pytest

from billet.access.doctor.ssh_doctor_access import (
    MISSING_MARKER,
    SshDoctorAccess,
    parse_probe_output,
    probe_script,
)
from billet.shared.errors import HostOperationError, ProcessError
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host, make_workspace_spec

REMOTE = make_remote_host()
GSWA = make_workspace_spec()
BRAND = make_workspace_spec(key="genshift-brand", repo_dir="genshift-brand")

# Recorded from the probe's shape: gswa-backend has every file, genshift-brand lacks sshd.conf
# and its berth.version has no trailing newline (the extra `echo` keeps the next marker whole).
_RECORDED = f"""\
===head:gswa-backend===
d223cd5
===file:gswa-backend:berth.version===
1

===file:gswa-backend:dev-entrypoint.sh===
#!/usr/bin/env bash
  set -euo pipefail

===file:gswa-backend:sshd.conf===
Port 22

===file:gswa-backend:authorized_keys-stub===
# stub

===head:genshift-brand===
{MISSING_MARKER}
===file:genshift-brand:berth.version===
2
===file:genshift-brand:dev-entrypoint.sh===
exec sleep infinity

===file:genshift-brand:sshd.conf===
{MISSING_MARKER}
===file:genshift-brand:authorized_keys-stub===
{MISSING_MARKER}
"""


def _access(
    stdout: str = _RECORDED, returncode: int = 0
) -> tuple[SshDoctorAccess, FakeProcessRunner]:
    runner = FakeProcessRunner(lambda _argv: completed(stdout=stdout, returncode=returncode))
    return SshDoctorAccess(runner), runner


def test_one_ssh_invocation_covers_every_workspace_on_the_host() -> None:
    access, runner = _access()
    reads = access.read_berths(REMOTE, [GSWA, BRAND])
    assert len(runner.calls) == 1
    assert [read.workspace for read in reads] == ["gswa-backend", "genshift-brand"]


def test_the_probe_is_batch_mode_bash_on_stdin_without_agent_forwarding() -> None:
    access, runner = _access()
    access.read_berths(REMOTE, [GSWA])
    argv = runner.calls[0]
    assert argv[0] == "ssh"
    assert argv[-1] == "bash -se"
    assert "BatchMode=yes" in argv
    assert "-A" not in argv and "-t" not in argv
    assert "azureuser@20.0.0.5" in argv
    assert runner.inputs[0] == probe_script([GSWA])


def _commands(script: str) -> set[str]:
    """Every command word the script runs (the first word of each simple command)."""
    words: set[str] = set()
    for line in script.splitlines():
        for part in re.split(r";|\|\||&&", line):
            tokens = [t for t in part.split() if t not in {"if", "then", "else", "fi"}]
            if tokens and not tokens[0].startswith("["):
                words.add(tokens[0])
    return words


def test_the_probe_runs_only_cat_and_git_rev_parse() -> None:
    script = probe_script([GSWA, BRAND])
    assert _commands(script) == {"set", "echo", "git", "cat"}
    git_lines = [line for line in script.splitlines() if "git " in line]
    assert len(git_lines) == 2
    assert all("rev-parse --short HEAD" in line for line in git_lines)
    assert script.count("cat ") == 8  # four files x two workspaces
    for word in ("docker", "fetch", "sudo", "rm "):
        assert word not in script


def test_parse_recorded_output_including_missing_files_and_a_non_checkout() -> None:
    gswa, brand = parse_probe_output(_RECORDED, [GSWA, BRAND])
    assert gswa.head == "d223cd5"
    assert gswa.files["berth.version"] is not None and gswa.files["berth.version"].strip() == "1"
    assert "set -euo pipefail" in (gswa.files["dev-entrypoint.sh"] or "")
    assert "  set -euo pipefail" in (gswa.files["dev-entrypoint.sh"] or "")  # verbatim
    assert brand.head is None
    assert brand.files["berth.version"] == "2"
    assert brand.files["sshd.conf"] is None
    assert brand.files["authorized_keys-stub"] is None


def test_a_section_absent_from_the_output_reads_as_missing() -> None:
    (read,) = parse_probe_output("", [GSWA])
    assert read.head is None
    assert set(read.files.values()) == {None}


def test_ssh_transport_failure_is_host_unreachable() -> None:
    access, _ = _access(stdout="", returncode=255)
    with pytest.raises(HostOperationError, match="could not reach 20.0.0.5"):
        access.read_berths(REMOTE, [GSWA])


def test_a_probe_failure_on_the_host_is_a_process_error() -> None:
    access, _ = _access(stdout="", returncode=2)
    with pytest.raises(ProcessError):
        access.read_berths(REMOTE, [GSWA])


def test_the_probe_script_runs_under_a_real_bash(tmp_path: Path) -> None:
    repo = tmp_path / "gswa-backend"
    (repo / ".devcontainer").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".devcontainer" / "berth.version").write_text("1")  # no trailing newline
    (repo / ".devcontainer" / "sshd.conf").write_text("Port 22\n")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        check=True,
    )
    result = subprocess.run(
        ["bash", "-se"],
        input=probe_script([GSWA, BRAND]),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    gswa, brand = parse_probe_output(result.stdout, [GSWA, BRAND])
    assert gswa.head is not None and re.fullmatch(r"[0-9a-f]{7,}", gswa.head)
    assert (gswa.files["berth.version"] or "").strip() == "1"
    assert (gswa.files["sshd.conf"] or "").strip() == "Port 22"
    assert gswa.files["dev-entrypoint.sh"] is None
    assert brand.head is None  # repo_dir absent on the Host
    assert set(brand.files.values()) == {None}
