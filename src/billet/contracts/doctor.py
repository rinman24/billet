"""``billet doctor`` contracts: the Berth drift and runtime reports, and ``DoctorAccess``.

``doctor`` compares each Workspace's Host checkout against the Berth the *installed* billet
ships (ADR-0015, ADR-0012). Only the four files a consumer copies whole are compared: three
by directive hash and ``berth.version`` as an integer stamp. The two snippets a consumer
merges into its own files are deliberately not checked (ADR-0015 item 2).

It then reports each running Workspace's runtime from the entrypoint's own log (D-A4-8):
the Berth the container is running against the checkout's stamp, each path the entrypoint
repaired, and each warning it printed. ``doctor`` never execs into a container.

The access side reads raw text (:class:`WorkspaceProbe`, one per Workspace, all of a Host's
Workspaces in one SSH session); the pure ``berth_policy`` and ``runtime_policy`` engines turn
that text into :class:`BerthStatus` and :class:`RuntimeReport`. Nothing here performs I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from billet.contracts.workspace import RemoteHost, WorkspaceSpec

#: The Berth files compared by directive hash (ADR-0012 item 5), in report order.
BERTH_HASHED_FILES: tuple[str, ...] = ("dev-entrypoint.sh", "sshd.conf", "authorized_keys-stub")

#: The Berth version stamp, compared as an integer (ADR-0012 item 2).
BERTH_VERSION_FILE = "berth.version"

#: Every file ``doctor`` reads from a consumer's ``.devcontainer/``.
BERTH_COPIED_FILES: tuple[str, ...] = (BERTH_VERSION_FILE, *BERTH_HASHED_FILES)

#: The merged snippets ``doctor`` never checks (ADR-0015 item 2, D-A4-2).
BERTH_UNCHECKED_SNIPPETS: tuple[str, ...] = ("Dockerfile.snippet", "docker-compose.snippet.yml")

#: Every line the Berth entrypoint logs starts with this (``templates/workspace/``).
ENTRYPOINT_LOG_PREFIX = "dev-entrypoint: "


@dataclass(frozen=True, slots=True)
class PackagedBerth:
    """The Berth the installed billet ships, read from the package (never a repo path).

    ``files`` maps each of :data:`BERTH_COPIED_FILES` to its shipped text.
    """

    version: int
    files: Mapping[str, str]


class BerthFileState(Enum):
    """How one copied Berth file compares with billet's shipped copy."""

    OK = "ok"
    DRIFT = "drift"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class BerthFileStatus:
    """One directive-hashed Berth file's result for one Workspace.

    ``diff`` is the unified diff of the *normalized* lines (billet's copy ``-``, the
    Workspace's ``+``), already capped; ``diff_more`` counts the lines the cap withheld.
    ``changed_lines`` is the number of added plus removed normalized lines.
    """

    file: str
    state: BerthFileState
    changed_lines: int = 0
    diff: tuple[str, ...] = ()
    diff_more: int = 0


class StampState(Enum):
    """How a Workspace's ``berth.version`` compares with the Berth billet ships."""

    OK = "ok"
    BEHIND = "behind"
    AHEAD = "ahead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StampStatus:
    """The ``berth.version`` comparison: ``found`` is ``None`` when missing or unparseable."""

    state: StampState
    shipped: int
    found: int | None

    @property
    def behind_by(self) -> int:
        """How many Berth versions the Workspace lags the installed billet (0 unless behind)."""
        if self.state is not StampState.BEHIND or self.found is None:
            return 0
        return self.shipped - self.found


class RuntimeState(Enum):
    """Whether ``doctor`` could read a Workspace's running container."""

    RUNNING = "running"
    NOT_RUNNING = "not running"
    UNREADABLE = "unreadable"


class RunningBerthState(Enum):
    """How the Berth the container logged at start compares with the checkout's stamp."""

    MATCH = "match"
    DIFFERS = "differs"
    NOT_LOGGED = "not logged"


@dataclass(frozen=True, slots=True)
class RuntimeReport:
    """One Workspace's runtime, from its entrypoint's log of the current run (D-A4-8).

    ``running_berth`` is the text the log's ``berth=`` line carries (``"1"``, or
    ``"unknown"`` when the entrypoint had no ``berth.version``); ``None`` when no such line
    was logged. ``repaired`` holds the text after ``repaired`` (``<path> (was <owner> <mode>)``),
    ``warnings`` the text after ``warning:`` / ``WARNING:``, and ``notes`` every other
    entrypoint line (``created …``, ``skipping …``), which is informational. ``reason`` says
    why the state is ``UNREADABLE``.
    """

    state: RuntimeState
    checkout_stamp: int | None = None
    running_berth: str | None = None
    berth_state: RunningBerthState | None = None
    repaired: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class BerthStatus:
    """The ``doctor`` report for one Workspace: Berth drift, then its runtime.

    ``head`` is the checkout's short ``HEAD`` (``None`` when ``repo_dir`` is not a git
    checkout), so a checkout lagging its remote is visible next to the result. ``runtime`` is
    ``None`` only where no runtime was assessed.
    """

    workspace: str
    host: str
    repo_dir: str
    head: str | None
    stamp: StampStatus
    files: tuple[BerthFileStatus, ...]
    runtime: RuntimeReport | None = None


@dataclass(frozen=True, slots=True)
class DoctorSkip:
    """A Host ``doctor`` could not read — reported and stepped over, never started."""

    host: str
    reason: str
    workspaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DoctorFilters:
    """``--host`` / ``--workspace``: narrow the report; ``None`` means every one."""

    host: str | None = None
    workspace: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """The whole ``doctor`` run: the Berth compared against, each Workspace, each skip."""

    berth_version: int
    statuses: tuple[BerthStatus, ...]
    skipped: tuple[DoctorSkip, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceBerthRead:
    """The raw probe result for one Workspace: short HEAD and each copied file's text.

    ``files`` holds every name in :data:`BERTH_COPIED_FILES`; a value of ``None`` means the
    file is absent from the checkout's ``.devcontainer/``.
    """

    workspace: str
    head: str | None
    files: Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimeRead:
    """The raw runtime read for one Workspace.

    ``log_lines`` are the entrypoint's lines (each starting :data:`ENTRYPOINT_LOG_PREFIX`)
    from ``docker logs`` of the service's running container, verbatim and in order; empty
    unless ``RUNNING``. ``reason`` says why the state is ``UNREADABLE``.
    """

    state: RuntimeState
    log_lines: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceProbe:
    """Everything one probe read for one Workspace: its Berth files and its runtime."""

    berth: WorkspaceBerthRead
    runtime: WorkspaceRuntimeRead


class DoctorAccess(Protocol):
    """Reads every Workspace on one Host in one SSH session: Berth files and runtime."""

    def probe(
        self, remote: RemoteHost, specs: Sequence[WorkspaceSpec]
    ) -> tuple[WorkspaceProbe, ...]:
        """Probe ``remote`` once and return one probe per spec, in ``specs`` order.

        Raises ``HostOperationError`` when the Host cannot be reached over SSH, and
        ``ProcessTimeoutError`` when the probe outlives its deadline.
        """
        ...
