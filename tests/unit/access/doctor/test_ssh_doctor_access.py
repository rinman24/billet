"""Tests for SshDoctorAccess — one probe per Host, its command set, and section parsing.

The process runner is mocked (never a real ``ssh``); parsing runs over recorded probe
output, including the real gswa-backend ``docker compose ps --format json`` captured
read-only from the Host on 2026-10-05 (``fixtures/``). Two tests also run the generated
scripts through a local ``bash -se`` against a temporary home — the second through the real
``SubprocessRunner.converse`` with a stub ``docker`` on ``PATH`` — proving the markers, the
framing and the runtime part on a real shell without touching any Host.
"""

from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import re
import stat
import subprocess

import pytest

from billet.access.doctor.compose_ps import ComposePsError, parse_compose_ps
from billet.access.doctor.ssh_doctor_access import (
    ProbeMarkers,
    SshDoctorAccess,
    parse_probe_output,
    read_facts,
    reads_script,
    runtime_script,
)
from billet.contracts import (
    BerthFileState,
    ComposeContainer,
    DevcontainerFacts,
    PortPublisher,
    RuntimeState,
    WorkspaceSpec,
)
from billet.infrastructure.process import CompletedProcess, SubprocessRunner
from billet.shared.errors import HostOperationError, ProcessError, ProcessTimeoutError
from billet.workspace.engine import berth_policy
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host, make_workspace_spec

REMOTE = make_remote_host()
GSWA = make_workspace_spec()
BRAND = make_workspace_spec(key="genshift-brand", repo_dir="genshift-brand")

# Recorded-output tests use one fixed nonce (D-A7-5); a real probe draws a fresh one per run.
_M = ProbeMarkers("0123456789abcdef")
_N = _M.nonce

_DEVCONTAINER_JSON = """\
{
  // JSONC, as consumers write it
  "dockerComposeFile": "docker-compose.yml",
  "service": "%s",
  "workspaceFolder": "/workspaces/%s",
  "remoteUser": "dev",
}
"""

# `docker compose -f .devcontainer/docker-compose.yml ps --format json` in gswa-backend's
# checkout on the devbox Host, captured read-only 2026-10-05 (Compose v5.1.4, JSON lines; four
# services, all project gswa-backend; FA8-10). Verbatim: it carries no environment values.
_FIXTURES = Path(__file__).parent / "fixtures"
_GSWA_PS = (_FIXTURES / "gswa-backend-compose-ps-2026-10-05.jsonl").read_text()

# Recorded from the probe's shape: gswa-backend has every file and a running container,
# genshift-brand lacks sshd.conf, its berth.version has no trailing newline (the extra `echo`
# keeps the next marker whole), and its container is stopped.
_RECORDED = f"""\
===head:gswa-backend@{_N}===
d223cd5
===file:gswa-backend:berth.version@{_N}===
1

===file:gswa-backend:dev-entrypoint.sh@{_N}===
#!/usr/bin/env bash
  set -euo pipefail

===file:gswa-backend:sshd.conf@{_N}===
Port 22

===file:gswa-backend:authorized_keys-stub@{_N}===
# stub

===facts:gswa-backend@{_N}===
{_DEVCONTAINER_JSON % ("gswa-backend", "gswa-backend")}
===head:genshift-brand@{_N}===
{_M.missing}
===file:genshift-brand:berth.version@{_N}===
2
===file:genshift-brand:dev-entrypoint.sh@{_N}===
exec sleep infinity

===file:genshift-brand:sshd.conf@{_N}===
{_M.missing}
===file:genshift-brand:authorized_keys-stub@{_N}===
{_M.missing}
===facts:genshift-brand@{_N}===
{_DEVCONTAINER_JSON % ("genshift-brand", "genshift-brand")}
{_M.reads_done}
===runtime:gswa-backend@{_N}===
{_M.running}3f2a9c1d
dev-entrypoint: berth=1
dev-entrypoint: publishing the container environment to /etc/environment for sshd login shells
===compose:gswa-backend@{_N}===
{_GSWA_PS.rstrip()}
===runtime:genshift-brand@{_N}===
{_M.not_running}
"""


def _access(
    stdout: str = _RECORDED, returncode: int = 0
) -> tuple[SshDoctorAccess, FakeProcessRunner]:
    runner = FakeProcessRunner(lambda _argv: completed(stdout=stdout, returncode=returncode))
    return SshDoctorAccess(runner, new_markers=lambda: _M), runner


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
    facts = read_facts(_RECORDED, [GSWA], _M)
    assert runner.inputs[0] == reads_script([GSWA], _M) + runtime_script([GSWA], REMOTE, facts, _M)
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
    script = reads_script([GSWA, BRAND], _M)
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
    script = runtime_script([GSWA, BRAND], REMOTE, facts, _M)
    assert _commands(script) == {"set", "cd", "export", "echo", "exit", "docker", "grep", "true"}
    docker = [m.group(0) for m in re.finditer(r"docker \S+ ?\S*", script)]
    assert docker and all(d.startswith(("docker compose -f", "docker logs ")) for d in docker)
    for word in (" exec", " run ", " stop", " down", " pull", " prune", " volume", " up "):
        assert word not in script
    assert "fetch" not in script and "sudo" not in script


def _docker_commands(script: str) -> list[str]:
    """Each docker invocation in ``script``, from ``docker`` up to its redirect or pipe."""
    return [m.group(0).strip() for m in re.finditer(r"docker [^<|)]*", script)]


def test_the_host_commands_are_a7s_plus_one_project_ps_per_workspace() -> None:
    """S2-1: A7's ps and logs, plus `ps --format json` (no service filter) per Workspace."""
    facts = {"gswa-backend": _facts("gswa-backend"), "genshift-brand": _facts("genshift-brand")}
    script = runtime_script([GSWA, BRAND], REMOTE, facts, _M)
    flags = "-f .devcontainer/docker-compose.yml"
    assert _docker_commands(script) == [
        f"docker compose {flags} ps --status running -q gswa-backend",
        'docker logs "$id" 2>&1',
        f"docker compose {flags} ps --format json",
        f"docker compose {flags} ps --status running -q genshift-brand",
        'docker logs "$id" 2>&1',
        f"docker compose {flags} ps --format json",
    ]
    reads = reads_script([GSWA, BRAND], _M)
    assert "docker-compose" not in reads and "compose.y" not in reads  # no compose file read
    for word in (" inspect", " port", " exec", "config"):
        assert word not in script + reads
    assert script.count("ps --format json </dev/null") == 2  # stdin never the script


def test_the_project_ps_runs_after_the_service_is_found_running_in_its_own_section() -> None:
    script = runtime_script([GSWA], REMOTE, {"gswa-backend": _facts("gswa-backend")}, _M)
    lines = script.splitlines()
    not_running = next(i for i, line in enumerate(lines) if _M.not_running in line)
    header = lines.index(f"echo ===compose:gswa-backend@{_N}===")
    assert not_running < header
    assert lines[header + 1].endswith("ps --format json </dev/null")


def test_the_ps_names_the_service_under_the_compose_prelude() -> None:
    """D18: Workspaces sharing compose project `devcontainer` stay apart by service."""
    script = runtime_script([GSWA], REMOTE, {"gswa-backend": _facts("gswa-backend")}, _M)
    assert "cd gswa-backend\n" in script
    assert "export BILLET_CONTAINER_SSH_PORT=" in script
    assert (
        "docker compose -f .devcontainer/docker-compose.yml ps --status running -q gswa-backend"
        in script
    )
    assert "docker logs \"$id\" 2>&1 </dev/null | { grep '^dev-entrypoint: ' || true; }" in script


def test_a_workspace_whose_facts_do_not_parse_gets_no_runtime_part() -> None:
    script = runtime_script([GSWA], REMOTE, {"gswa-backend": "devcontainer.json missing"}, _M)
    assert script == ""


def test_parse_recorded_output_including_missing_files_and_a_non_checkout() -> None:
    gswa, brand = parse_probe_output(_RECORDED, [GSWA, BRAND], _M)
    files = gswa.berth.files
    assert gswa.berth.head == "d223cd5"
    assert files["berth.version"] is not None and files["berth.version"].strip() == "1"
    assert "  set -euo pipefail" in (files["dev-entrypoint.sh"] or "")  # verbatim
    assert brand.berth.head is None
    assert brand.berth.files["berth.version"] == "2"
    assert brand.berth.files["sshd.conf"] is None
    assert brand.berth.files["authorized_keys-stub"] is None


def test_parse_the_runtime_sections() -> None:
    gswa, brand = parse_probe_output(_RECORDED, [GSWA, BRAND], _M)
    assert gswa.runtime.state is RuntimeState.RUNNING
    assert [c.service for c in gswa.runtime.containers] == [
        "gswa-backend",
        "gswa-outbox-worker",
        "redis",
        "sql",
    ]
    assert brand.runtime.containers == ()
    assert gswa.runtime.log_lines == (
        "dev-entrypoint: berth=1",
        "dev-entrypoint: publishing the container environment to /etc/environment for sshd "
        "login shells",
    )
    assert brand.runtime.state is RuntimeState.NOT_RUNNING
    assert brand.runtime.log_lines == ()


def test_a_failed_runtime_probe_or_missing_devcontainer_json_is_unreadable() -> None:
    failed = _RECORDED.replace(_M.not_running, _M.runtime_failed)
    _, brand = parse_probe_output(failed, [GSWA, BRAND], _M)
    assert brand.runtime.state is RuntimeState.UNREADABLE
    assert brand.runtime.reason == "docker compose ps or docker logs failed"
    (read,) = parse_probe_output("", [GSWA], _M)
    assert read.runtime.state is RuntimeState.UNREADABLE
    assert read.runtime.reason == ".devcontainer/devcontainer.json missing"


def test_a_running_workspace_whose_project_ps_fails_or_will_not_parse_is_unreadable() -> None:
    """D-A8-13: the runtime is skipped as unreadable; the Berth read still stands."""
    compose = f"===compose:gswa-backend@{_N}===\n{_GSWA_PS.rstrip()}\n"
    cases = {
        "docker compose ps --format json failed": f"{compose}{_M.runtime_failed}\n",
        "docker compose ps --format json: invalid JSON": compose.replace("{", "<", 1),
        "docker compose ps --format json listed no container": compose.split("\n", 1)[0] + "\n",
        "no compose ps output": "",
    }
    for reason, replacement in cases.items():
        gswa, _ = parse_probe_output(_RECORDED.replace(compose, replacement), [GSWA, BRAND], _M)
        assert gswa.runtime.state is RuntimeState.UNREADABLE, reason
        assert (gswa.runtime.reason or "").startswith(reason), gswa.runtime.reason
        assert gswa.berth.head == "d223cd5"  # the Berth results still print


def test_an_invalid_devcontainer_json_is_unreadable_with_the_parse_error() -> None:
    text = f"===facts:gswa-backend@{_N}===\n{{ not json\n"
    assert "invalid devcontainer.json" in str(read_facts(text, [GSWA], _M)["gswa-backend"])


def test_a_section_absent_from_the_output_reads_as_missing() -> None:
    (probe,) = parse_probe_output("", [GSWA], _M)
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
        input=reads_script([GSWA, BRAND], _M),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == _M.reads_done
    gswa, brand = parse_probe_output(result.stdout, [GSWA, BRAND], _M)
    assert gswa.berth.head is not None and re.fullmatch(r"[0-9a-f]{7,}", gswa.berth.head)
    assert (gswa.berth.files["berth.version"] or "").strip() == "1"
    assert (gswa.berth.files["sshd.conf"] or "").strip() == "Port 22"
    assert gswa.berth.files["dev-entrypoint.sh"] is None
    assert brand.berth.head is None  # repo_dir absent on the Host
    assert set(brand.berth.files.values()) == {None}


# A stand-in `docker`: records each argv, answers `compose ps` per service (two ids for
# gswa-backend, none for billet, a failure for squadra), the project `ps` as JSON lines,
# and `logs` on both streams.
_STUB_DOCKER = """\
#!/usr/bin/env bash
echo "$*" >> "$DOCKER_ARGV_LOG"
case "$*" in
  *" ps --format json")
    if [ -n "${DOCKER_PS_JSON_FAILS:-}" ]; then echo "ps failed" >&2; exit 1; fi
    echo '{"ID":"aaa111","Project":"gswa-backend","Service":"gswa-backend","Publishers":[]}'
    ;;
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


_EXPECTED_DOCKER_CALLS = [
    "compose -f .devcontainer/docker-compose.yml ps --status running -q gswa-backend",
    "logs aaa111",
    "compose -f .devcontainer/docker-compose.yml ps --format json",
    "compose -f .devcontainer/docker-compose.yml ps --status running -q billet",
    "compose -f .devcontainer/docker-compose.yml ps --status running -q squadra",
]


def _stub_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, dict[str, str]] | None = None,
) -> tuple[list[WorkspaceSpec], Path]:
    """Put the stub ``docker`` on PATH and three checkouts under ``tmp_path``; return both.

    ``extra`` maps a Workspace key to more ``.devcontainer/`` files for its checkout.
    """
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
        files = {"devcontainer.json": _DEVCONTAINER_JSON % (key, key), **(extra or {}).get(key, {})}
        _git_checkout(repo, files)
        specs.append(make_workspace_spec(key=key, repo_dir=str(repo)))
    return specs, argv_log


def test_the_whole_conversation_runs_under_a_real_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs, argv_log = _stub_host(tmp_path, monkeypatch)

    result = SubprocessRunner().converse(
        ["bash", "-se"],
        opening=reads_script(specs, _M),
        sentinel=_M.reads_done,
        reply=lambda out: runtime_script(specs, REMOTE, read_facts(out, specs, _M), _M),
    )

    assert result.returncode == 0, result.stderr
    gswa, billet, squadra = parse_probe_output(result.stdout, specs, _M)
    assert gswa.runtime.state is RuntimeState.RUNNING
    assert gswa.runtime.log_lines == (
        "dev-entrypoint: berth=1",
        "dev-entrypoint: repaired /home/dev/.claude (was root:root 755)",
        "dev-entrypoint: WARNING: could not write /etc/environment; x",
    )
    assert gswa.runtime.containers == (
        ComposeContainer("aaa111", "gswa-backend", "gswa-backend", ()),
    )
    assert billet.runtime.state is RuntimeState.NOT_RUNNING
    assert squadra.runtime.state is RuntimeState.UNREADABLE
    assert argv_log.read_text().splitlines() == _EXPECTED_DOCKER_CALLS


def test_a_failing_project_ps_under_a_real_bash_is_unreadable_and_ends_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure marker lands in the compose section; later Workspaces still run."""
    specs, argv_log = _stub_host(tmp_path, monkeypatch)
    monkeypatch.setenv("DOCKER_PS_JSON_FAILS", "1")

    gswa, billet, squadra = SshDoctorAccess(_LocalBash()).probe(REMOTE, specs)

    assert gswa.runtime.state is RuntimeState.UNREADABLE
    assert gswa.runtime.reason == "docker compose ps --format json failed"
    assert gswa.berth.head is not None
    assert billet.runtime.state is RuntimeState.NOT_RUNNING
    assert squadra.runtime.state is RuntimeState.UNREADABLE
    assert argv_log.read_text().splitlines() == _EXPECTED_DOCKER_CALLS


# --- the nonce: a consumer line shaped like a marker stays file content (D-A7-5) ---------


def test_each_probe_run_draws_a_fresh_random_nonce() -> None:
    runner = FakeProcessRunner(lambda _argv: completed())
    access = SshDoctorAccess(runner)  # the default: a fresh nonce per run
    access.probe(REMOTE, [GSWA])
    access.probe(REMOTE, [GSWA])
    nonces = [re.findall(r"@([0-9a-f]{16})===", text or "") for text in runner.inputs]
    assert all(found and len(set(found)) == 1 for found in nonces)  # one nonce per run
    assert nonces[0][0] != nonces[1][0]


class _LocalBash:
    """A ConversationRunner that runs the probe on a local ``bash -se`` instead of ssh."""

    def converse(
        self,
        argv: Sequence[str],
        *,
        opening: str,
        sentinel: str,
        reply: Callable[[str], str],
        timeout: float | None = None,
    ) -> CompletedProcess:
        return SubprocessRunner().converse(
            ["bash", "-se"], opening=opening, sentinel=sentinel, reply=reply, timeout=timeout
        )


# Every line after the shebang looks like a marker of the pre-nonce probe: a section header,
# the old reads sentinel (which fired the runtime part early), the old missing marker, and a
# later Workspace's runtime header with its "not running" line.
_SPOOFING_ENTRYPOINT = """#!/usr/bin/env bash
===head:x===
===doctor:reads-done===
===missing===
===runtime:billet===
not running
exec sleep infinity
"""


def test_marker_shaped_consumer_lines_are_file_content_and_spoof_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs, argv_log = _stub_host(
        tmp_path, monkeypatch, extra={"gswa-backend": {"dev-entrypoint.sh": _SPOOFING_ENTRYPOINT}}
    )

    gswa, billet, squadra = SshDoctorAccess(_LocalBash()).probe(REMOTE, specs)

    read = gswa.berth.files["dev-entrypoint.sh"]
    assert read is not None and read.rstrip("\n") == _SPOOFING_ENTRYPOINT.rstrip("\n")
    status = berth_policy.compare_file("dev-entrypoint.sh", _SPOOFING_ENTRYPOINT, read)
    assert status.state is BerthFileState.OK  # no false drift
    assert gswa.berth.head is not None and re.fullmatch(r"[0-9a-f]{7,}", gswa.berth.head)
    # The reply fired on the real sentinel, so every Workspace after gswa-backend has its
    # runtime part, and billet's comes from docker, not from the spoofed "not running".
    assert gswa.runtime.state is RuntimeState.RUNNING
    assert billet.runtime.state is RuntimeState.NOT_RUNNING
    assert squadra.runtime.state is RuntimeState.UNREADABLE
    assert argv_log.read_text().splitlines() == _EXPECTED_DOCKER_CALLS


# --- `docker compose ps --format json` (A8, D-A8-13) -------------------------------------


def test_parse_the_recorded_gswa_backend_ps_json_lines() -> None:
    """FA8-10: four services, one project, sshd and sql on loopback, redis only exposed."""
    containers = {c.service: c for c in parse_compose_ps(_GSWA_PS)}
    assert set(containers) == {"gswa-backend", "gswa-outbox-worker", "redis", "sql"}
    assert {c.project for c in containers.values()} == {"gswa-backend"}
    assert containers["gswa-backend"].publishers == (PortPublisher("127.0.0.1", 22, 2222, "tcp"),)
    assert containers["sql"].publishers == (PortPublisher("127.0.0.1", 5432, 5432, "tcp"),)
    assert containers["redis"].publishers == (PortPublisher("", 6379, 0, "tcp"),)
    assert containers["gswa-outbox-worker"].publishers == ()
    assert containers["gswa-backend"].id == "bc584ed6d28d"


def test_parse_the_json_array_older_compose_prints() -> None:
    lines = [json.loads(line) for line in _GSWA_PS.splitlines()]
    assert parse_compose_ps(json.dumps(lines)) == parse_compose_ps(_GSWA_PS)
    assert parse_compose_ps("[]") == ()


def test_null_or_absent_publishers_read_as_none() -> None:
    text = (
        '{"ID":"a","Project":"p","Service":"s","Publishers":null}\n'
        '{"ID":"b","Project":"p","Service":"t"}\n'
    )
    assert [c.publishers for c in parse_compose_ps(text)] == [(), ()]


def test_blank_ps_output_is_no_containers() -> None:
    assert parse_compose_ps("") == ()
    assert parse_compose_ps("\n\n") == ()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("not json", "invalid JSON"),
        ('{"ID":"a","Project":"p"', "invalid JSON"),
        ('"a string"', "not a JSON object"),
        ('{"a":1}', "ID missing or not a string"),
        ('{"ID":"a","Project":"p","Service":""}', "Service missing or not a string"),
        ('{"ID":"a","Project":"p","Service":"s","Publishers":{}}', "Publishers is not a list"),
        ('{"ID":"a","Project":"p","Service":"s","Publishers":[1]}', "not a JSON object"),
        (
            '{"ID":"a","Project":"p","Service":"s","Publishers":[{"URL":"","TargetPort":"80",'
            '"PublishedPort":0,"Protocol":"tcp"}]}',
            "TargetPort missing or not an integer",
        ),
        ('[{"ID":"a","Project":"p","Service":"s"}, 3]', "not a JSON object"),
    ],
)
def test_malformed_ps_output_raises_with_a_short_reason(text: str, reason: str) -> None:
    with pytest.raises(ComposePsError, match=reason):
        parse_compose_ps(text)
