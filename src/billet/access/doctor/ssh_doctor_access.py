"""SshDoctorAccess — read every Workspace's copied Berth files on a Host in one SSH session.

Implements ``DoctorAccess``. One agent-less, batch-mode SSH round trip per Host runs a
sectioned probe script on stdin (``bash -se``, the ``host specs`` precedent in
``SshMetricsAccess``); the output is parsed by the pure helpers below. Per Workspace the
script runs only ``git -C <repo_dir> rev-parse --short HEAD`` and ``cat`` of the four files
billet copies whole into ``.devcontainer/``. It never fetches, never forwards the agent, and
never touches a container (ADR-0015 items 1 and 4).

Each value is framed by an ``===<section>===`` line; a value that could not be read is the
single line ``===missing===``. ``cat`` output is followed by one newline so a file without a
trailing newline cannot swallow the next marker; the extra blank line is harmless because
the directive hash drops blank lines.
"""

from collections.abc import Sequence
import posixpath
import shlex

from billet.contracts import BERTH_COPIED_FILES, RemoteHost, WorkspaceBerthRead, WorkspaceSpec
from billet.infrastructure import ssh
from billet.infrastructure.process import ProcessRunner
from billet.shared.errors import HostOperationError, ProcessError

#: The consumer directory the Berth files are copied into (ADR-0012).
DEVCONTAINER_DIR = ".devcontainer"

#: The line that stands in for a value the probe could not read.
MISSING_MARKER = "===missing==="

# Bound connection establishment only (never command runtime): a deallocated Azure host
# drops inbound packets, so an untimed probe would hang (the `billet ls` precedent).
_SSH_CONNECT_TIMEOUT = 5

# ssh(1) reserves exit 255 for its own failures (connect/auth); the probe never produces it.
_SSH_TRANSPORT_RC = 255


class SshDoctorAccess:
    """A ``DoctorAccess`` over one sectioned probe script per Host, run via SSH."""

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    def read_berths(
        self, remote: RemoteHost, specs: Sequence[WorkspaceSpec]
    ) -> tuple[WorkspaceBerthRead, ...]:
        """Run the probe for ``specs`` on ``remote`` once and parse one read per spec."""
        argv = ssh.ssh_argv(
            remote.admin_user,
            remote.ip,
            "bash -se",
            connect_timeout=_SSH_CONNECT_TIMEOUT,
            batch_mode=True,
        )
        result = self._runner.run(argv, input_text=probe_script(specs), check=False)
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


def probe_script(specs: Sequence[WorkspaceSpec]) -> str:
    """Build the one probe script that reads every spec's HEAD and copied Berth files."""
    missing = shlex.quote(MISSING_MARKER)
    lines = ["set -u"]
    for spec in specs:
        repo = shlex.quote(spec.repo_dir)
        lines.append(f"echo {shlex.quote(f'==={head_section(spec.key)}===')}")
        lines.append(f"git -C {repo} rev-parse --short HEAD 2>/dev/null || echo {missing}")
        for name in BERTH_COPIED_FILES:
            path = shlex.quote(posixpath.join(spec.repo_dir, DEVCONTAINER_DIR, name))
            lines.append(f"echo {shlex.quote(f'==={file_section(spec.key, name)}===')}")
            lines.append(f"if [ -f {path} ]; then cat {path}; echo; else echo {missing}; fi")
    return "\n".join(lines) + "\n"


# --- parsing the sectioned output (pure) ----------------------------------------------


def parse_probe_output(text: str, specs: Sequence[WorkspaceSpec]) -> tuple[WorkspaceBerthRead, ...]:
    """Turn the probe's sectioned stdout into one :class:`WorkspaceBerthRead` per spec.

    A section that is absent from the output, or that holds the missing marker, reads as
    ``None`` (file absent / not a git checkout).
    """
    sections = split_sections(text)
    reads: list[WorkspaceBerthRead] = []
    for spec in specs:
        head = sections.get(head_section(spec.key))
        reads.append(
            WorkspaceBerthRead(
                workspace=spec.key,
                head=(head.strip() or None) if head is not None else None,
                files={
                    name: sections.get(file_section(spec.key, name)) for name in BERTH_COPIED_FILES
                },
            )
        )
    return tuple(reads)


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
