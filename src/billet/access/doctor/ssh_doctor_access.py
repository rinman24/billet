"""SshDoctorAccess — probe every Workspace on a Host in one SSH session.

Implements ``DoctorAccess``. One agent-less, batch-mode SSH invocation per Host runs a
sectioned probe script on stdin (``bash -se``, the ``host specs`` precedent in
``SshMetricsAccess``); the output is parsed by the pure helpers below. The script arrives in
two parts over the same session (:class:`~billet.infrastructure.process.ConversationRunner`):

1. **reads**: per Workspace, ``git -C <repo_dir> rev-parse --short HEAD``, ``cat`` of the four
   files billet copies whole into ``.devcontainer/``, and ``cat`` of ``devcontainer.json``,
   then the :data:`READS_DONE` line;
2. **runtime**: built on the Mac from the ``devcontainer.json`` just read, with billet's own
   parser, so the container lookup names the same service and compose files ``start`` drives.
   Per Workspace, under the usual compose prelude (``cd <repo_dir>``, billet's exports):
   ``docker compose -f … ps --status running -q <service>`` (scoped by service, D18), then
   ``docker logs <id> 2>&1 | grep '^dev-entrypoint: '``.

The probe never fetches, never forwards the agent, and never execs into a container
(ADR-0015 items 1 and 4). Every docker command reads ``/dev/null`` as stdin so none can
swallow the rest of the script.

Each value is framed by an ``===<section>===`` line; a file that could not be read is the
single line ``===missing===``. ``cat`` output is followed by one newline so a file without a
trailing newline cannot swallow the next marker; the extra blank line is harmless because
the directive hash drops blank lines and JSONC ignores whitespace. A runtime section holds
``running <id>`` and the entrypoint lines, or :data:`NOT_RUNNING`, or ends with
:data:`RUNTIME_FAILED`.
"""

from collections.abc import Mapping, Sequence
import posixpath
import shlex

from billet.access.container.compose_script import (
    DEVCONTAINER_JSON,
    compose_prelude,
    facts_from_json,
    running_ps_command,
)
from billet.contracts import (
    BERTH_COPIED_FILES,
    ENTRYPOINT_LOG_PREFIX,
    DevcontainerFacts,
    RemoteHost,
    RuntimeState,
    WorkspaceBerthRead,
    WorkspaceProbe,
    WorkspaceRuntimeRead,
    WorkspaceSpec,
)
from billet.infrastructure import ssh
from billet.infrastructure.process import ConversationRunner
from billet.shared.errors import BilletError, HostOperationError, ProcessError

#: The consumer directory the Berth files are copied into (ADR-0012).
DEVCONTAINER_DIR = ".devcontainer"

#: The line that stands in for a value the probe could not read.
MISSING_MARKER = "===missing==="

#: The line that ends the reads; the runtime part is sent once it arrives.
READS_DONE = "===doctor:reads-done==="

#: A runtime section's only line when the service has no running container.
NOT_RUNNING = "not running"

#: The line a runtime section ends with when ``docker compose ps`` or ``docker logs`` failed.
RUNTIME_FAILED = "runtime probe failed"

_RUNNING = "running "

# Bound connection establishment only (never command runtime): a deallocated Azure host
# drops inbound packets, so an untimed probe would hang (the `billet ls` precedent).
_SSH_CONNECT_TIMEOUT = 5

# ssh(1) reserves exit 255 for its own failures (connect/auth); the probe never produces it.
_SSH_TRANSPORT_RC = 255


class SshDoctorAccess:
    """A ``DoctorAccess`` over one sectioned, two-part probe script per Host, run via SSH."""

    def __init__(self, runner: ConversationRunner) -> None:
        self._runner = runner

    def probe(
        self, remote: RemoteHost, specs: Sequence[WorkspaceSpec]
    ) -> tuple[WorkspaceProbe, ...]:
        """Run the probe for ``specs`` on ``remote`` in one session; one probe per spec."""
        argv = ssh.ssh_argv(
            remote.admin_user,
            remote.ip,
            "bash -se",
            connect_timeout=_SSH_CONNECT_TIMEOUT,
            batch_mode=True,
        )

        def reply(reads_output: str) -> str:
            return runtime_script(specs, remote, read_facts(reads_output, specs))

        result = self._runner.converse(
            argv, opening=reads_script(specs), sentinel=READS_DONE, reply=reply
        )
        if result.returncode == _SSH_TRANSPORT_RC:
            raise HostOperationError(
                f"could not reach {remote.ip} over SSH — is the Host up? "
                "doctor never starts a Host; `billet host up` does."
            )
        if result.returncode != 0:
            raise ProcessError(result.argv, result.returncode, result.stderr)
        return parse_probe_output(result.stdout, specs)


# --- the probe script (pure) ----------------------------------------------------------


def head_section(key: str) -> str:
    """Return the section name carrying ``key``'s short ``HEAD``."""
    return f"head:{key}"


def file_section(key: str, name: str) -> str:
    """Return the section name carrying ``key``'s copy of Berth file ``name``."""
    return f"file:{key}:{name}"


def facts_section(key: str) -> str:
    """Return the section name carrying ``key``'s ``devcontainer.json``."""
    return f"facts:{key}"


def runtime_section(key: str) -> str:
    """Return the section name carrying ``key``'s container state and entrypoint log."""
    return f"runtime:{key}"


def _cat_or_missing(path: str) -> str:
    quoted = shlex.quote(path)
    missing = shlex.quote(MISSING_MARKER)
    return f"if [ -f {quoted} ]; then cat {quoted}; echo; else echo {missing}; fi"


def _marker(section: str) -> str:
    return f"echo {shlex.quote(f'==={section}===')}"


def reads_script(specs: Sequence[WorkspaceSpec]) -> str:
    """Build the reads part: each spec's HEAD, copied Berth files and ``devcontainer.json``."""
    missing = shlex.quote(MISSING_MARKER)
    lines = ["set -u"]
    for spec in specs:
        repo = shlex.quote(spec.repo_dir)
        lines.append(_marker(head_section(spec.key)))
        lines.append(f"git -C {repo} rev-parse --short HEAD 2>/dev/null || echo {missing}")
        for name in BERTH_COPIED_FILES:
            lines.append(_marker(file_section(spec.key, name)))
            lines.append(_cat_or_missing(posixpath.join(spec.repo_dir, DEVCONTAINER_DIR, name)))
        lines.append(_marker(facts_section(spec.key)))
        lines.append(_cat_or_missing(posixpath.join(spec.repo_dir, DEVCONTAINER_JSON)))
    lines.append(f"echo {shlex.quote(READS_DONE)}")
    return "\n".join(lines) + "\n"


def runtime_script(
    specs: Sequence[WorkspaceSpec],
    remote: RemoteHost,
    facts: Mapping[str, DevcontainerFacts | str],
) -> str:
    """Build the runtime part: per spec whose facts parsed, find its container and log.

    Each Workspace runs in a subshell under the compose prelude (fail-fast there), with the
    outer ``errexit`` suspended around it, so one Workspace's docker failure is reported in
    its own section and never ends the probe.
    """
    lines: list[str] = []
    for spec in specs:
        spec_facts = facts.get(spec.key)
        if not isinstance(spec_facts, DevcontainerFacts):
            continue
        prefix = shlex.quote(f"^{ENTRYPOINT_LOG_PREFIX}")
        lines += [
            _marker(runtime_section(spec.key)),
            "set +e",
            "(",
            compose_prelude(spec, remote).rstrip("\n"),
            f"ids=$({running_ps_command(spec_facts)} </dev/null)",
            f'if [ -z "$ids" ]; then echo {shlex.quote(NOT_RUNNING)}; exit 0; fi',
            "id=${ids%%$'\\n'*}",
            f'echo "{_RUNNING}$id"',
            f'docker logs "$id" 2>&1 </dev/null | {{ grep {prefix} || true; }}',
            ")",
            f'[ "$?" -eq 0 ] || echo {shlex.quote(RUNTIME_FAILED)}',
            "set -e",
        ]
    return "\n".join(lines) + "\n" if lines else ""


# --- parsing the sectioned output (pure) ----------------------------------------------


def read_facts(text: str, specs: Sequence[WorkspaceSpec]) -> dict[str, DevcontainerFacts | str]:
    """Parse each spec's ``devcontainer.json`` from probe output; a ``str`` is why it failed."""
    sections = split_sections(text)
    facts: dict[str, DevcontainerFacts | str] = {}
    for spec in specs:
        path = posixpath.join(spec.repo_dir, DEVCONTAINER_JSON)
        body = sections.get(facts_section(spec.key))
        if body is None:
            facts[spec.key] = f"{DEVCONTAINER_JSON} missing"
            continue
        try:
            facts[spec.key] = facts_from_json(body, path)
        except BilletError as exc:
            facts[spec.key] = str(exc).splitlines()[0]
    return facts


def parse_runtime(
    section: str | None, facts: DevcontainerFacts | str | None
) -> WorkspaceRuntimeRead:
    """Turn one runtime section (and why it may be absent) into a :class:`WorkspaceRuntimeRead`."""
    if isinstance(facts, str):
        return WorkspaceRuntimeRead(RuntimeState.UNREADABLE, reason=facts)
    lines = section.splitlines() if section is not None else []
    if RUNTIME_FAILED in lines:
        return WorkspaceRuntimeRead(
            RuntimeState.UNREADABLE, reason="docker compose ps or docker logs failed"
        )
    if lines[:1] == [NOT_RUNNING]:
        return WorkspaceRuntimeRead(RuntimeState.NOT_RUNNING)
    if lines and lines[0].startswith(_RUNNING):
        return WorkspaceRuntimeRead(
            RuntimeState.RUNNING,
            log_lines=tuple(line for line in lines[1:] if line.startswith(ENTRYPOINT_LOG_PREFIX)),
        )
    return WorkspaceRuntimeRead(RuntimeState.UNREADABLE, reason="no runtime output")


def parse_probe_output(text: str, specs: Sequence[WorkspaceSpec]) -> tuple[WorkspaceProbe, ...]:
    """Turn the probe's sectioned stdout into one :class:`WorkspaceProbe` per spec.

    A file section that is absent from the output, or that holds the missing marker, reads as
    ``None`` (file absent / not a git checkout).
    """
    sections = split_sections(text)
    facts = read_facts(text, specs)
    probes: list[WorkspaceProbe] = []
    for spec in specs:
        head = sections.get(head_section(spec.key))
        berth = WorkspaceBerthRead(
            workspace=spec.key,
            head=(head.strip() or None) if head is not None else None,
            files={name: sections.get(file_section(spec.key, name)) for name in BERTH_COPIED_FILES},
        )
        runtime = parse_runtime(sections.get(runtime_section(spec.key)), facts.get(spec.key))
        probes.append(WorkspaceProbe(berth=berth, runtime=runtime))
    return tuple(probes)


def split_sections(text: str) -> dict[str, str | None]:
    """Group raw output lines under their ``===name===`` markers, keeping content verbatim.

    Unlike the metrics splitter this keeps blank and indented lines: they are file content.
    """
    raw: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line == MISSING_MARKER:
            if current is not None:
                current.append(line)
            continue
        if line.startswith("===") and line.endswith("===") and len(line) > len("======"):
            current = raw.setdefault(line[3:-3], [])
        elif current is not None:
            current.append(line)
    sections: dict[str, str | None] = {}
    for name, body in raw.items():
        sections[name] = None if body == [MISSING_MARKER] else "\n".join(body)
    return sections
