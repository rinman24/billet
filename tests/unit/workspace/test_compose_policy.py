"""Tests for the pure compose policy: project and published-port checks over one Host (A8).

The hit shapes (``0.0.0.0``, ``::``, a specific address, an empty ``URL``) were not
observable on the fleet (FA8-10, D-A8-14), so they are built here from the recorded
publisher shape; the non-hits include the fleet's own loopback and exposed-only publishers.
"""

import pytest

from billet.contracts import (
    ComposeContainer,
    ComposeReport,
    ExposedPort,
    PortPublisher,
    RuntimeState,
    SharedProject,
    WorkspaceRuntimeRead,
)
from billet.workspace.engine.compose_policy import assess_host, is_exposed, is_loopback
from tests.unit._fakes import make_container, make_runtime_read


def _publisher(url: str, published: int = 8080, target: int = 80) -> PortPublisher:
    return PortPublisher(url=url, target_port=target, published_port=published, protocol="tcp")


@pytest.mark.parametrize("url", ["0.0.0.0", "::", "10.0.0.4", ""])
def test_a_non_loopback_publish_is_exposed(url: str) -> None:
    """S2-3 hits: any address, IPv6 any, a specific address, an empty URL with a port."""
    assert is_exposed(_publisher(url))


@pytest.mark.parametrize("url", ["127.0.0.1", "127.0.0.2", "::1"])
def test_a_loopback_publish_is_not_exposed(url: str) -> None:
    assert is_loopback(url)
    assert not is_exposed(_publisher(url))


@pytest.mark.parametrize("url", ["0.0.0.0", "", "localhost", "127.0.0.1.example", "not-an-ip"])
def test_loopback_is_parsed_never_string_matched(url: str) -> None:
    assert not is_loopback(url)


def test_an_exposed_only_port_is_not_a_publish() -> None:
    """PublishedPort 0 (redis on the fleet) is exposed to the network, not published."""
    assert not is_exposed(_publisher("", published=0, target=6379))
    assert not is_exposed(_publisher("0.0.0.0", published=0))


def test_a_workspace_on_its_own_project_with_loopback_ports_has_no_findings() -> None:
    """The fleet's shape (FA8-10): sshd and sql on loopback, redis exposed, a worker bare."""
    sql = make_container("gswa-backend", "sql", _publisher("127.0.0.1", 5432, 5432))
    redis = make_container("gswa-backend", "redis", _publisher("", 0, 6379))
    worker = ComposeContainer("w", "gswa-backend", "gswa-outbox-worker", ())  # publishes []
    reads = [("gswa-backend", make_runtime_read(containers=[make_container(), sql, redis, worker]))]
    reports, shared = assess_host("devbox", reads)
    assert reports == {"gswa-backend": ComposeReport(projects=("gswa-backend",))}
    assert shared == ()


def test_a_sidecar_publishing_on_a_non_loopback_address_is_named() -> None:
    """S2-4: every service in the project is checked, not just the Workspace's own."""
    sql = make_container("gswa-backend", "sql", _publisher("0.0.0.0", 5432, 5432))
    reads = [("gswa-backend", make_runtime_read(containers=[make_container(), sql]))]
    reports, _ = assess_host("devbox", reads)
    assert reports["gswa-backend"].exposed == (
        ExposedPort("gswa-backend", "sql", sql.id, _publisher("0.0.0.0", 5432, 5432)),
    )


def test_a_workspace_running_under_another_project_is_a_foreign_project() -> None:
    reads = [("billet", make_runtime_read(project="devcontainer"))]
    reports, shared = assess_host("devbox", reads)
    assert reports["billet"].foreign_projects == ("devcontainer",)
    assert shared == ()  # one Workspace alone shares nothing


def test_two_workspaces_on_one_project_are_one_host_level_finding() -> None:
    """S2-5: one shared project names both Workspaces; each gets its own foreign finding."""
    billet = make_container("devcontainer", "billet")
    brand = make_container("devcontainer", "genshift-brand")
    reads = [
        ("billet", make_runtime_read(containers=[billet, brand])),
        ("genshift-brand", make_runtime_read(containers=[billet, brand])),
        ("squadra", make_runtime_read(project="squadra")),
    ]
    reports, shared = assess_host("devbox", reads)
    assert shared == (
        SharedProject(
            host="devbox", project="devcontainer", workspaces=("billet", "genshift-brand")
        ),
    )
    assert reports["billet"].foreign_projects == ("devcontainer",)
    assert reports["genshift-brand"].foreign_projects == ("devcontainer",)
    assert reports["squadra"].foreign_projects == ()


def test_a_workspace_that_is_not_running_contributes_nothing() -> None:
    reads = [
        ("billet", make_runtime_read(project="devcontainer")),
        ("genshift-brand", WorkspaceRuntimeRead(RuntimeState.NOT_RUNNING)),
        ("squadra", WorkspaceRuntimeRead(RuntimeState.UNREADABLE, reason="x")),
    ]
    reports, shared = assess_host("devbox", reads)
    assert set(reports) == {"billet"}
    assert shared == ()


def test_under_a_shared_project_a_publisher_is_reported_once_by_container_id() -> None:
    """S2-6: both Workspaces' `ps` list the sidecar; the first in registry order owns it."""
    sql = make_container("devcontainer", "sql", _publisher("0.0.0.0", 5432, 5432))
    billet = make_container("devcontainer", "billet")
    brand = make_container("devcontainer", "genshift-brand", _publisher("::", 3000, 3000))
    reads = [
        ("billet", make_runtime_read(containers=[billet, sql, brand])),
        ("genshift-brand", make_runtime_read(containers=[brand, billet, sql])),
    ]
    reports, _ = assess_host("devbox", reads)
    assert [e.service for e in reports["billet"].exposed] == ["sql", "genshift-brand"]
    assert reports["genshift-brand"].exposed == ()
