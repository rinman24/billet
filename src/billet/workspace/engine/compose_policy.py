"""ComposePolicy — pure compose-project and published-port checks over one Host (A8).

Input is each Workspace's runtime read, whose ``containers`` came from one
``docker compose ps --format json`` per running Workspace (D-A8-3). Two checks:

- **Compose project** (ADR-0017, D-A8-5). A Workspace whose containers run under a project
  other than its registry key gets a per-Workspace finding; a project that two or more
  running Workspaces on the Host claim gets one Host-level :class:`SharedProject`. A
  Workspace that is not running contributes nothing.
- **Published ports** (ADR-0003, D-A8-4). Every publisher of every container in the project,
  sidecars included, is checked. A publisher with ``PublishedPort`` 0 is exposed, not
  published, and is ignored. One whose ``URL`` parses with :mod:`ipaddress` into
  ``127.0.0.0/8`` or ``::1`` is loopback. Anything else is exposed: ``0.0.0.0``, ``::``, a
  specific non-loopback address, an empty or unparseable ``URL``. Under a shared project every
  Workspace's ``ps`` lists the others' containers too (``--orphans`` defaults to true), so
  publishers are de-duplicated by container ``ID`` across the Host: each is attributed to the
  first Workspace, in registry order, whose ``ps`` listed it.

No I/O: the containers arrive in :class:`WorkspaceRuntimeRead` from the access layer.
"""

from collections.abc import Sequence
import ipaddress

from billet.contracts import (
    ComposeReport,
    ExposedPort,
    PortPublisher,
    RuntimeState,
    SharedProject,
    WorkspaceRuntimeRead,
)


def is_loopback(url: str) -> bool:
    """Return whether a publisher's ``URL`` is a loopback address (``127.0.0.0/8``, ``::1``).

    Parsed with :mod:`ipaddress`, never matched as a string; an empty or unparseable value is
    not loopback.
    """
    try:
        return ipaddress.ip_address(url).is_loopback
    except ValueError:
        return False


def is_exposed(publisher: PortPublisher) -> bool:
    """Return whether ``publisher`` publishes a port on a non-loopback address (D-A8-4)."""
    return publisher.published_port != 0 and not is_loopback(publisher.url)


def assess_host(
    host: str, reads: Sequence[tuple[str, WorkspaceRuntimeRead]]
) -> tuple[dict[str, ComposeReport], tuple[SharedProject, ...]]:
    """Assess one Host's Workspaces, given as ``(key, runtime read)`` in registry order.

    Returns a :class:`ComposeReport` for each running Workspace whose read lists a container,
    keyed by Workspace, and each project two or more of them share, in first-seen order.
    """
    reports: dict[str, ComposeReport] = {}
    claimants: dict[str, list[str]] = {}
    seen: set[str] = set()
    for key, read in reads:
        if read.state is not RuntimeState.RUNNING or not read.containers:
            continue
        projects = tuple(dict.fromkeys(container.project for container in read.containers))
        for project in projects:
            claimants.setdefault(project, []).append(key)
        exposed: list[ExposedPort] = []
        for container in read.containers:
            if container.id in seen:
                continue
            seen.add(container.id)
            exposed.extend(
                ExposedPort(container.project, container.service, container.id, publisher)
                for publisher in container.publishers
                if is_exposed(publisher)
            )
        reports[key] = ComposeReport(
            projects=projects,
            foreign_projects=tuple(project for project in projects if project != key),
            exposed=tuple(exposed),
        )
    shared = tuple(
        SharedProject(host=host, project=project, workspaces=tuple(keys))
        for project, keys in claimants.items()
        if len(keys) > 1
    )
    return reports, shared
