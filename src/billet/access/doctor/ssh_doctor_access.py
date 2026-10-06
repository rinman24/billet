"""SshDoctorAccess — probe every Workspace on a Host in one SSH session.

Implements ``DoctorAccess``. One agent-less, batch-mode SSH invocation per Host runs a
sectioned probe script on stdin (``bash -se``, the ``host specs`` precedent in
``SshMetricsAccess``); the output is parsed by the pure helpers below. The script arrives in
two parts over the same session (:class:`~billet.infrastructure.process.ConversationRunner`):

1. **reads**: per Workspace, ``git -C <repo_dir> rev-parse --short HEAD``, ``cat`` of the four
   files billet copies whole into ``.devcontainer/``, and ``cat`` of ``devcontainer.json``,
   then the :attr:`~ProbeMarkers.reads_done` line;
2. **runtime**: built on the Mac from the ``devcontainer.json`` just read, with billet's own
   parser, so the container lookup names the same service and compose files ``start`` drives.
   Per Workspace, under the usual compose prelude (``cd <repo_dir>``, billet's exports):
   ``docker compose -f … ps --status running -q <service>`` (scoped by service, D18), then
   ``docker logs <id> 2>&1 | grep '^dev-entrypoint: '``, then, in its own section,
   ``docker compose -f … ps --format json`` with no service filter (A8, D-A8-3): every
   running container of the Workspace's compose project, for its project and published
   ports. The last two run only when the service's container is running. No compose file is
   opened: compose reads the files it is named, as it does for ``start``.

The probe never fetches, never forwards the agent, and never execs into a container
(ADR-0015 items 1 and 4). Every docker command reads ``/dev/null`` as stdin so none can
swallow the rest of the script.

Each value is framed by an ``===<section>@<nonce>===`` line; a file that could not be read is
the single line ``===missing@<nonce>===``. ``cat`` output is followed by one newline so a
file without a trailing newline cannot swallow the next marker; the extra blank line is
harmless because the directive hash drops blank lines and JSONC ignores whitespace. A runtime
section holds ``running@<nonce> <id>`` and the entrypoint lines, or
``not running@<nonce>``; the compose section after it holds the ``ps`` JSON. Whichever of the
two was open when a docker command failed ends with ``runtime probe failed@<nonce>``.

The nonce is random per probe run (D-A7-5), so no line of a consumer's file can be taken for
a marker: without it, a copied file holding ``===doctor:reads-done===`` would fire the
runtime part early and a line ``===head:x===`` would split the file. Every marker is built
by :class:`ProbeMarkers`, which the pure script builders and parsers take as a parameter.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import posixpath
import secrets
import shlex

from billet.access.container.compose_script import (
    DEVCONTAINER_JSON,
    compose_file_flags,
    compose_prelude,
    facts_from_json,
    running_ps_command,
)
from billet.access.doctor.compose_ps import ComposePsError, parse_compose_ps
from billet.contracts import (
    BERTH_COPIED_FILES,
    ENTRYPOINT_LOG_PREFIX,
    ComposeContainer,
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

# Random bytes per probe run: 64 bits, so a consumer file cannot carry the nonce by chance.
_NONCE_BYTES = 8


@dataclass(frozen=True, slots=True)
class ProbeMarkers:
    """Every in-band marker of one probe run, each carrying the run's ``nonce`` (D-A7-5).

    Section headers are ``===<section>@<nonce>===``; the value markers put the nonce after
    their word. Tests build one from a fixed nonce; :meth:`fresh` draws a random one.
    """

    nonce: str

    @classmethod
    def fresh(cls) -> "ProbeMarkers":
        """Return the markers of a new run, with a random hex nonce."""
        return cls(secrets.token_hex(_NONCE_BYTES))

    def header(self, section: str) -> str:
        """Return the line that opens ``section``."""
        return f"==={section}{self._suffix}"

    @property
    def missing(self) -> str:
        """The line that stands in for a value the probe could not read."""
        return f"===missing@{self.nonce}==="

    @property
    def reads_done(self) -> str:
        """The line that ends the reads; the runtime part is sent once it arrives."""
        return self.header("doctor:reads-done")

    @property
    def not_running(self) -> str:
        """A runtime section's only line when the service has no running container."""
        return f"not running@{self.nonce}"

    @property
    def runtime_failed(self) -> str:
        """The line a runtime section ends with when ``docker compose ps`` or ``docker logs`` failed."""
        return f"runtime probe failed@{self.nonce}"

    @property
    def running(self) -> str:
        """The prefix of a runtime section's first line, followed by the container id."""
        return f"running@{self.nonce} "

    def section_of(self, line: str) -> str | None:
        """Return the section ``line`` opens, or ``None`` when it is not this run's header."""
        suffix = self._suffix
        if line.startswith("===") and line.endswith(suffix) and len(line) > 3 + len(suffix):
            return line[3 : -len(suffix)]
        return None

    @property
    def _suffix(self) -> str:
        return f"@{self.nonce}==="


# Bound connection establishment: a deallocated Azure host drops inbound packets, so an
# untimed probe would hang (the `billet ls` precedent).
_SSH_CONNECT_TIMEOUT = 5

# Keepalives (D-A7-2): a link that dies mid-probe ends as ssh exit 255, reported unreachable
# like a failed connect, once 3 probes 5 s apart go unanswered, about 15-20 s after it died.
# That beats the deadline only for a link that dies early in the probe: one that dies later
# than about 10 s in is reported `probe timed out after 30s` instead (D-A8-7).
_SSH_SERVER_ALIVE_INTERVAL = 5
_SSH_SERVER_ALIVE_COUNT_MAX = 3

# The wall-clock bound on one Host's whole probe conversation, opening through exit
# (D-A7-2), every read and write included (D-A8-6). A live link on which the probe stalls (a
# hung `docker`, say) is what the keepalives cannot end; on expiry ssh's process group is
# killed and the Host is reported `probe timed out` (D-A7-3). The measured run is about 2 s
# for four Workspaces.
_PROBE_DEADLINE = 30.0

# ssh(1) reserves exit 255 for its own failures (connect/auth); the probe never produces it.
_SSH_TRANSPORT_RC = 255


class SshDoctorAccess:
    """A ``DoctorAccess`` over one sectioned, two-part probe script per Host, run via SSH.

    ``deadline`` (seconds) bounds each Host's probe. It and ``new_markers`` (one fresh nonce
    per probe run) are parameters only so tests can shorten the one and fix the other: the
    composition root takes the defaults, and there is no CLI flag.
    """

    def __init__(
        self,
        runner: ConversationRunner,
        *,
        deadline: float = _PROBE_DEADLINE,
        new_markers: Callable[[], ProbeMarkers] = ProbeMarkers.fresh,
    ) -> None:
        self._runner = runner
        self._deadline = deadline
        self._new_markers = new_markers

    def probe(
        self, remote: RemoteHost, specs: Sequence[WorkspaceSpec]
    ) -> tuple[WorkspaceProbe, ...]:
        """Run the probe for ``specs`` on ``remote`` in one session; one probe per spec.

        Raises
        ------
        HostOperationError
            When ssh itself fails (exit 255): the Host cannot be reached, or the link died.
        ProcessTimeoutError
            When the conversation outlives the deadline; the ssh child has been killed.
        ProcessError
            When the probe script exits non-zero on the Host.
        """
        argv = ssh.ssh_argv(
            remote.admin_user,
            remote.ip,
            "bash -se",
            connect_timeout=_SSH_CONNECT_TIMEOUT,
            server_alive_interval=_SSH_SERVER_ALIVE_INTERVAL,
            server_alive_count_max=_SSH_SERVER_ALIVE_COUNT_MAX,
            batch_mode=True,
        )

        markers = self._new_markers()

        def reply(reads_output: str) -> str:
            facts = read_facts(reads_output, specs, markers)
            return runtime_script(specs, remote, facts, markers)

        result = self._runner.converse(
            argv,
            opening=reads_script(specs, markers),
            sentinel=markers.reads_done,
            reply=reply,
            timeout=self._deadline,
        )
        if result.returncode == _SSH_TRANSPORT_RC:
            raise HostOperationError(
                f"could not reach {remote.ip} over SSH — is the Host up? "
                "doctor never starts a Host; `billet host up` does."
            )
        if result.returncode != 0:
            raise ProcessError(result.argv, result.returncode, result.stderr)
        return parse_probe_output(result.stdout, specs, markers)


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


def compose_section(key: str) -> str:
    """Return the section name carrying ``key``'s ``docker compose ps --format json``."""
    return f"compose:{key}"


def project_ps_command(facts: DevcontainerFacts) -> str:
    """Build the ``ps`` that lists every running container of the project as JSON (D-A8-3).

    No service filter: sidecars publish ports too (D-A8-4). Same ``-f`` files as
    :func:`~billet.access.container.compose_script.running_ps_command`, so it resolves the
    same project.
    """
    return f"docker compose {compose_file_flags(facts)} ps --format json"


def _cat_or_missing(path: str, markers: ProbeMarkers) -> str:
    quoted = shlex.quote(path)
    missing = shlex.quote(markers.missing)
    return f"if [ -f {quoted} ]; then cat {quoted}; echo; else echo {missing}; fi"


def _echo(line: str) -> str:
    return f"echo {shlex.quote(line)}"


def reads_script(specs: Sequence[WorkspaceSpec], markers: ProbeMarkers) -> str:
    """Build the reads part: each spec's HEAD, copied Berth files and ``devcontainer.json``."""
    missing = shlex.quote(markers.missing)
    lines = ["set -u"]
    for spec in specs:
        repo = shlex.quote(spec.repo_dir)
        lines.append(_echo(markers.header(head_section(spec.key))))
        lines.append(f"git -C {repo} rev-parse --short HEAD 2>/dev/null || echo {missing}")
        for name in BERTH_COPIED_FILES:
            lines.append(_echo(markers.header(file_section(spec.key, name))))
            path = posixpath.join(spec.repo_dir, DEVCONTAINER_DIR, name)
            lines.append(_cat_or_missing(path, markers))
        lines.append(_echo(markers.header(facts_section(spec.key))))
        lines.append(_cat_or_missing(posixpath.join(spec.repo_dir, DEVCONTAINER_JSON), markers))
    lines.append(_echo(markers.reads_done))
    return "\n".join(lines) + "\n"


def runtime_script(
    specs: Sequence[WorkspaceSpec],
    remote: RemoteHost,
    facts: Mapping[str, DevcontainerFacts | str],
    markers: ProbeMarkers,
) -> str:
    """Build the runtime part: per spec whose facts parsed, its container, log and project.

    Each Workspace runs in a subshell under the compose prelude (fail-fast there), with the
    outer ``errexit`` suspended around it, so one Workspace's docker failure is reported in
    its own section and never ends the probe. Once the service is found running, its log is
    read and the project's ``ps --format json`` follows in the compose section.
    """
    lines: list[str] = []
    for spec in specs:
        spec_facts = facts.get(spec.key)
        if not isinstance(spec_facts, DevcontainerFacts):
            continue
        prefix = shlex.quote(f"^{ENTRYPOINT_LOG_PREFIX}")
        lines += [
            _echo(markers.header(runtime_section(spec.key))),
            "set +e",
            "(",
            compose_prelude(spec, remote).rstrip("\n"),
            f"ids=$({running_ps_command(spec_facts)} </dev/null)",
            f'if [ -z "$ids" ]; then {_echo(markers.not_running)}; exit 0; fi',
            "id=${ids%%$'\\n'*}",
            f'echo {shlex.quote(markers.running)}"$id"',
            f'docker logs "$id" 2>&1 </dev/null | {{ grep {prefix} || true; }}',
            _echo(markers.header(compose_section(spec.key))),
            f"{project_ps_command(spec_facts)} </dev/null",
            ")",
            f'[ "$?" -eq 0 ] || {_echo(markers.runtime_failed)}',
            "set -e",
        ]
    return "\n".join(lines) + "\n" if lines else ""


# --- parsing the sectioned output (pure) ----------------------------------------------


def read_facts(
    text: str, specs: Sequence[WorkspaceSpec], markers: ProbeMarkers
) -> dict[str, DevcontainerFacts | str]:
    """Parse each spec's ``devcontainer.json`` from probe output; a ``str`` is why it failed."""
    sections = split_sections(text, markers)
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


def parse_compose_section(
    section: str | None, markers: ProbeMarkers
) -> tuple[ComposeContainer, ...] | str:
    """Parse a running Workspace's compose section; a ``str`` is why it is unreadable.

    The section is absent when the probe never reached the ``ps``; it ends with the failure
    marker when ``ps`` failed. Output that will not parse, or lists no container although the
    service was just found running, is unreadable too (D-A8-13).
    """
    if section is None:
        return "no compose ps output"
    lines = section.splitlines()
    if markers.runtime_failed in lines:
        return "docker compose ps --format json failed"
    try:
        containers = parse_compose_ps(section)
    except ComposePsError as exc:
        return f"docker compose ps --format json: {exc}"
    return containers or "docker compose ps --format json listed no container"


def parse_runtime(
    section: str | None,
    compose: str | None,
    facts: DevcontainerFacts | str | None,
    markers: ProbeMarkers,
) -> WorkspaceRuntimeRead:
    """Turn one Workspace's runtime and compose sections into a :class:`WorkspaceRuntimeRead`.

    ``facts`` says why the sections may be absent. A running service whose project ``ps``
    failed, will not parse, or lists no container is ``UNREADABLE`` (D-A8-13).
    """
    if isinstance(facts, str):
        return WorkspaceRuntimeRead(RuntimeState.UNREADABLE, reason=facts)
    lines = section.splitlines() if section is not None else []
    if markers.runtime_failed in lines:
        return WorkspaceRuntimeRead(
            RuntimeState.UNREADABLE, reason="docker compose ps or docker logs failed"
        )
    if lines[:1] == [markers.not_running]:
        return WorkspaceRuntimeRead(RuntimeState.NOT_RUNNING)
    if not (lines and lines[0].startswith(markers.running)):
        return WorkspaceRuntimeRead(RuntimeState.UNREADABLE, reason="no runtime output")
    containers = parse_compose_section(compose, markers)
    if isinstance(containers, str):
        return WorkspaceRuntimeRead(RuntimeState.UNREADABLE, reason=containers)
    return WorkspaceRuntimeRead(
        RuntimeState.RUNNING,
        log_lines=tuple(line for line in lines[1:] if line.startswith(ENTRYPOINT_LOG_PREFIX)),
        containers=containers,
    )


def parse_probe_output(
    text: str, specs: Sequence[WorkspaceSpec], markers: ProbeMarkers
) -> tuple[WorkspaceProbe, ...]:
    """Turn the probe's sectioned stdout into one :class:`WorkspaceProbe` per spec.

    A file section that is absent from the output, or that holds the missing marker, reads as
    ``None`` (file absent / not a git checkout).
    """
    sections = split_sections(text, markers)
    facts = read_facts(text, specs, markers)
    probes: list[WorkspaceProbe] = []
    for spec in specs:
        head = sections.get(head_section(spec.key))
        berth = WorkspaceBerthRead(
            workspace=spec.key,
            head=(head.strip() or None) if head is not None else None,
            files={name: sections.get(file_section(spec.key, name)) for name in BERTH_COPIED_FILES},
        )
        runtime = parse_runtime(
            sections.get(runtime_section(spec.key)),
            sections.get(compose_section(spec.key)),
            facts.get(spec.key),
            markers,
        )
        probes.append(WorkspaceProbe(berth=berth, runtime=runtime))
    return tuple(probes)


def split_sections(text: str, markers: ProbeMarkers) -> dict[str, str | None]:
    """Group raw output lines under this run's section headers, keeping content verbatim.

    Unlike the metrics splitter this keeps blank and indented lines: they are file content.
    Only a header carrying ``markers``' nonce opens a section, so a consumer line shaped like
    one (``===head:x===``) stays in the file it belongs to.
    """
    raw: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line == markers.missing:
            if current is not None:
                current.append(line)
            continue
        section = markers.section_of(line)
        if section is not None:
            current = raw.setdefault(section, [])
        elif current is not None:
            current.append(line)
    sections: dict[str, str | None] = {}
    for name, body in raw.items():
        sections[name] = None if body == [markers.missing] else "\n".join(body)
    return sections
