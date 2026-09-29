"""``billet doctor`` contracts: the Berth drift report and the ``DoctorAccess`` Protocol.

``doctor`` compares each Workspace's Host checkout against the Berth the *installed* billet
ships (ADR-0015, ADR-0012). Only the four files a consumer copies whole are compared: three
by directive hash and ``berth.version`` as an integer stamp. The two snippets a consumer
merges into its own files are deliberately not checked (ADR-0015 item 2).

The access side reads raw text (:class:`WorkspaceBerthRead`, one per Workspace, all of a
Host's Workspaces in one SSH session); the pure ``berth_policy`` engine turns that text into
:class:`BerthStatus`. Nothing here performs I/O.
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


@dataclass(frozen=True, slots=True)
class BerthStatus:
    """The Berth drift report for one Workspace, read from its Host checkout.

    ``head`` is the checkout's short ``HEAD`` (``None`` when ``repo_dir`` is not a git
    checkout), so a checkout lagging its remote is visible next to the result.
    """

    workspace: str
    host: str
    repo_dir: str
    head: str | None
    stamp: StampStatus
    files: tuple[BerthFileStatus, ...]


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


class DoctorAccess(Protocol):
    """Reads the copied Berth files of every Workspace on one Host in one SSH session."""

    def read_berths(
        self, remote: RemoteHost, specs: Sequence[WorkspaceSpec]
    ) -> tuple[WorkspaceBerthRead, ...]:
        """Probe ``remote`` once and return one read per spec, in ``specs`` order.

        Raises ``HostOperationError`` when the Host cannot be reached over SSH.
        """
        ...
