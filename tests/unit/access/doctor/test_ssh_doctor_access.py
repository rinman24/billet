"""Tests for SshDoctorAccess — one probe per Host, its command set, and section parsing.

The process runner is mocked (never a real ``ssh``); parsing runs over recorded probe
output. Two tests also run the generated scripts through a local ``bash -se`` against a
temporary home — the second through the real ``SubprocessRunner.converse`` with a stub
``docker`` on ``PATH`` — proving the markers, the framing and the runtime part on a real
shell without touching any Host.
"""

import os
from pathlib import Path
import re
import stat
import subprocess

import pytest

from billet.access.doctor.ssh_doctor_access import (
    MISSING_MARKER,
    NOT_RUNNING,
    READS_DONE,
    RUNTIME_FAILED,
    SshDoctorAccess,
    parse_probe_output,
    read_facts,
    reads_script,
    runtime_script,
)
from billet.contracts import DevcontainerFacts, RuntimeState, WorkspaceSpec
from billet.infrastructure.process import CompletedProcess, SubprocessRunner
from billet.shared.errors import HostOperationError, ProcessError, ProcessTimeoutError
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host, make_workspace_spec

REMOTE = make_remote_host()
GSWA = make_workspace_spec()
BRAND = make_workspace_spec(key="genshift-brand", repo_dir="genshift-brand")

_DEVCONTAINER_JSON = """\
{
  // JSONC, as consumers write it
  "dockerComposeFile": "docker-compose.yml",
  "service": "%s",
  "workspaceFolder": "/workspaces/%s",
  "remoteUser": "dev",
}
"""

# Recorded from the probe's shape: gswa-backend has every file and a running container,
# genshift-brand lacks sshd.conf, its berth.version has no trailing newline (the extra `echo`
# keeps the next marker whole), and its container is stopped.
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

===facts:gswa-backend===
{_DEVCONTAINER_JSON % ("gswa-backend", "gswa-backend")}
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
===facts:genshift-brand===
{_DEVCONTAINER_JSON % ("genshift-brand", "genshift-brand")}
{READS_DONE}
===runtime:gswa-backend===
running 3f2a9c1d
dev-entrypoint: berth=1
dev-entrypoint: publishing the container environment to /etc/environment for sshd login shells
===runtime:genshift-brand===
{NOT_RUNNING}
"""


def _access(
    stdout: str = _RECORDED, returncode: int = 0
) -> tuple[SshDoctorAccess, FakeProcessRunner]:
    runner = FakeProcessRunner(lambda _argv: completed(stdout=stdout, returncode=returncode))
    return SshDoctorAccess(runner), runner


def test_one_ssh_invocation_covers_every_workspace_on_the_host() -> None:
    access, runner = _access()
    probes = access.probe(REMOTE, [GSWA, BRAND])
    assert len(runner.calls) == 1
    assert [probe.berth.workspace for probe in probes] == ["gswa-backend", "genshift-brand"]


def test_the_probe_is_batch_mode_bash_on_stdin_without_agent_forwarding() -> None:
    access, runner = _access()
    access.probe(REMOTE, [GSWA])
    argv = runner.calls[0]
    assert argv[0] == "ssh"
    assert argv[-1] == "bash -se"
    assert "BatchMode=yes" in argv
    assert "-A" not in argv and "-t" not in argv
    assert "azureuser@20.0.0.5" in argv
    facts = read_facts(_RECORDED, [GSWA])
    assert runner.inputs[0] == reads_script([GSWA]) + runtime_script([GSWA], REMOTE, facts)
    assert runner.timeouts == [30]  # the per-Host deadline over the whole conversation


def test_the_probe_argv_bounds_the_connect_and_a_dead_link() -> None:
    """D-A7-2: keepalives end a dead link as ssh exit 255 in ~15 s, inside the deadline."""
    access, runner = _access()
    access.probe(REMOTE, [GSWA])
    options = {v for k, v in zip(runner.calls[0], runner.calls[0][1:], strict=False) if k == "-o"}
    assert {
        "ConnectTimeout=5",
        "BatchMode=yes",
        "ServerAliveInterval=5",
        "ServerAliveCountMax=3",
    } <= options


def _commands(script: str) -> set[str]:
    """Every command word the script runs (the first word of each simple command)."""
    words: set[str] = set()
    for line in script.splitlines():
        for part in re.split(r";|\|\||&&|\||\$\(|\{ ", line):
            tokens = [
                t for t in part.split() if t not in {"if", "then", "else", "fi", "(", ")", "}"}
            ]
            if tokens and not tokens[0].startswith(("[", "ids=", "id=")):
                words.add(tokens[0])
    return words


def test_the_reads_part_runs_only_cat_and_git_rev_parse() -> None:
    script = reads_script([GSWA, BRAND])
    assert _commands(script) == {"set", "echo", "git", "cat"}
    git_lines = [line for line in script.splitlines() if "git " in line]
    assert len(git_lines) == 2
    assert all("rev-parse --short HEAD" in line for line in git_lines)
    assert script.count("cat ") == 10  # four Berth files + devcontainer.json, x two workspaces
    for word in ("docker", "fetch", "sudo", "rm "):
        assert word not in script


def _facts(service: str) -> DevcontainerFacts:
    return DevcontainerFacts(
        service=service,
        compose_files=(".devcontainer/docker-compose.yml",),
        workspace_folder=f"/workspaces/{service}",
        remote_user="dev",
        post_create_command=None,
    )


def test_the_runtime_part_runs_only_compose_ps_and_docker_logs_never_exec() -> None:
    facts = {"gswa-backend": _facts("gswa-backend"), "genshift-brand": _facts("genshift-brand")}
    script = runtime_script([GSWA, BRAND], REMOTE, facts)
    assert _commands(script) == {"set", "cd", "export", "echo", "exit", "docker", "grep", "true"}
    docker = [m.group(0) for m in re.finditer(r"docker \S+ ?\S*", script)]
    assert docker and all(d.startswith(("docker compose -f", "docker logs ")) for d in docker)
    for word in (" exec", " run ", " stop", " down", " pull", " prune", " volume", " up "):
        assert word not in script
    assert "fetch" not in script and "sudo" not in script


def test_the_ps_names_the_service_under_the_compose_prelude() -> None:
    """D18: Workspaces sharing compose project `devcontainer` stay apart by service."""
    script = runtime_script([GSWA], REMOTE, {"gswa-backend": _facts("gswa-backend")})
    assert "cd gswa-backend\n" in script
    assert "export BILLET_CONTAINER_SSH_PORT=" in script
    assert (
        "docker compose -f .devcontainer/docker-compose.yml ps --status running -q gswa-backend"
        in script
    )
    assert "docker logs \"$id\" 2>&1 </dev/null | { grep '^dev-entrypoint: ' || true; }" in script


def test_a_workspace_whose_facts_do_not_parse_gets_no_runtime_part() -> None:
    script = runtime_script([GSWA], REMOTE, {"gswa-backend": "devcontainer.json missing"})
    assert script == ""


def test_parse_recorded_output_including_missing_files_and_a_non_checkout() -> None:
    gswa, brand = parse_probe_output(_RECORDED, [GSWA, BRAND])
    files = gswa.berth.files
    assert gswa.berth.head == "d223cd5"
    assert files["berth.version"] is not None and files["berth.version"].strip() == "1"
    assert "  set -euo pipefail" in (files["dev-entrypoint.sh"] or "")  # verbatim
    assert brand.berth.head is None
    assert brand.berth.files["berth.version"] == "2"
    assert brand.berth.files["sshd.conf"] is None
    assert brand.berth.files["authorized_keys-stub"] is None


def test_parse_the_runtime_sections() -> None:
    gswa, brand = parse_probe_output(_RECORDED, [GSWA, BRAND])
    assert gswa.runtime.state is RuntimeState.RUNNING
    assert gswa.runtime.log_lines == (
        "dev-entrypoint: berth=1",
        "dev-entrypoint: publishing the container environment to /etc/environment for sshd "
        "login shells",
    )
    assert brand.runtime.state is RuntimeState.NOT_RUNNING
    assert brand.runtime.log_lines == ()


def test_a_failed_runtime_probe_or_missing_devcontainer_json_is_unreadable() -> None:
    failed = _RECORDED.replace(NOT_RUNNING, RUNTIME_FAILED)
    _, brand = parse_probe_output(failed, [GSWA, BRAND])
    assert brand.runtime.state is RuntimeState.UNREADABLE
    assert brand.runtime.reason == "docker compose ps or docker logs failed"
    (read,) = parse_probe_output("", [GSWA])
    assert read.runtime.state is RuntimeState.UNREADABLE
    assert read.runtime.reason == ".devcontainer/devcontainer.json missing"


def test_an_invalid_devcontainer_json_is_unreadable_with_the_parse_error() -> None:
    text = "===facts:gswa-backend===\n{ not json\n"
    assert "invalid devcontainer.json" in str(read_facts(text, [GSWA])["gswa-backend"])


def test_a_section_absent_from_the_output_reads_as_missing() -> None:
    (probe,) = parse_probe_output("", [GSWA])
    assert probe.berth.head is None
    assert set(probe.berth.files.values()) == {None}


def test_ssh_transport_failure_is_host_unreachable() -> None:
    access, _ = _access(stdout="", returncode=255)
    with pytest.raises(HostOperationError, match="could not reach 20.0.0.5"):
        access.probe(REMOTE, [GSWA])


def test_a_probe_past_its_deadline_raises_the_typed_timeout() -> None:
    def _hang(argv: list[str]) -> CompletedProcess:
        raise ProcessTimeoutError(argv, 30)

    access = SshDoctorAccess(FakeProcessRunner(_hang))
    with pytest.raises(ProcessTimeoutError):
        access.probe(REMOTE, [GSWA])


def test_a_probe_failure_on_the_host_is_a_process_error() -> None:
    access, _ = _access(stdout="", returncode=2)
    with pytest.raises(ProcessError):
        access.probe(REMOTE, [GSWA])


def _git_checkout(repo: Path, files: dict[str, str]) -> None:
    (repo / ".devcontainer").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, text in files.items():
        (repo / ".devcontainer" / name).write_text(text)
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "-C", str(repo), *ident, "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), *ident, "commit", "-qm", "x"], check=True)


def test_the_reads_script_runs_under_a_real_bash(tmp_path: Path) -> None:
    _git_checkout(
        tmp_path / "gswa-backend",
        {"berth.version": "1", "sshd.conf": "Port 22\n"},  # berth.version: no trailing newline
    )
    result = subprocess.run(
        ["bash", "-se"],
        input=reads_script([GSWA, BRAND]),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == READS_DONE
    gswa, brand = parse_probe_output(result.stdout, [GSWA, BRAND])
    assert gswa.berth.head is not None and re.fullmatch(r"[0-9a-f]{7,}", gswa.berth.head)
    assert (gswa.berth.files["berth.version"] or "").strip() == "1"
    assert (gswa.berth.files["sshd.conf"] or "").strip() == "Port 22"
    assert gswa.berth.files["dev-entrypoint.sh"] is None
    assert brand.berth.head is None  # repo_dir absent on the Host
    assert set(brand.berth.files.values()) == {None}


# A stand-in `docker`: records each argv, answers `compose ps` per service (two ids for
# gswa-backend, none for billet, a failure for squadra) and `logs` on both streams.
_STUB_DOCKER = """\
#!/usr/bin/env bash
echo "$*" >> "$DOCKER_ARGV_LOG"
case "$*" in
  *" ps "*gswa-backend) printf 'aaa111\\nbbb222\\n' ;;
  *" ps "*billet) ;;
  *" ps "*squadra) echo "no such file" >&2; exit 1 ;;
  "logs aaa111")
    echo "dev-entrypoint: berth=1"
    echo "Server listening on :: port 22."
    echo "dev-entrypoint: repaired /home/dev/.claude (was root:root 755)"
    echo "dev-entrypoint: WARNING: could not write /etc/environment; x" >&2
    ;;
  *) echo "unexpected docker $*" >&2; exit 9 ;;
esac
"""


def test_the_whole_conversation_runs_under_a_real_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(_STUB_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    argv_log = tmp_path / "docker-argv.log"
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("DOCKER_ARGV_LOG", str(argv_log))
    specs: list[WorkspaceSpec] = []
    for key in ("gswa-backend", "billet", "squadra"):
        repo = tmp_path / key
        _git_checkout(repo, {"devcontainer.json": _DEVCONTAINER_JSON % (key, key)})
        specs.append(make_workspace_spec(key=key, repo_dir=str(repo)))

    result = SubprocessRunner().converse(
        ["bash", "-se"],
        opening=reads_script(specs),
        sentinel=READS_DONE,
        reply=lambda out: runtime_script(specs, REMOTE, read_facts(out, specs)),
    )

    assert result.returncode == 0, result.stderr
    gswa, billet, squadra = parse_probe_output(result.stdout, specs)
    assert gswa.runtime.state is RuntimeState.RUNNING
    assert gswa.runtime.log_lines == (
        "dev-entrypoint: berth=1",
        "dev-entrypoint: repaired /home/dev/.claude (was root:root 755)",
        "dev-entrypoint: WARNING: could not write /etc/environment; x",
    )
    assert billet.runtime.state is RuntimeState.NOT_RUNNING
    assert squadra.runtime.state is RuntimeState.UNREADABLE
    calls = argv_log.read_text().splitlines()
    assert calls == [
        "compose -f .devcontainer/docker-compose.yml ps --status running -q gswa-backend",
        "logs aaa111",
        "compose -f .devcontainer/docker-compose.yml ps --status running -q billet",
        "compose -f .devcontainer/docker-compose.yml ps --status running -q squadra",
    ]
