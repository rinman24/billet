"""Berth ownership matrix: the Docker behaviour ADR-0013 rests on, and the repair it ships.

When a named volume is mounted at a path inside a container, the directory the process
sees takes its ownership from the volume's initialisation — from the image's directory
when there is one (copy-on-empty), from the daemon (``root:root``) when there is not.
Docker documents the first half and not the second. This matrix pins both, then shows
the shipped ``dev-entrypoint.sh`` repairing the failing cell and the repair surviving a
fresh container:

| Cell | Image pre-creates ``~/.claude``? | Volume | Expected |
| --- | --- | --- | --- |
| 1 | yes | none | ``dev dev 700`` (the image's directory) |
| 2 | yes | fresh | ``dev dev 700`` (copy-on-empty; the row every consumer relied on) |
| 3 | no | none | no directory at all |
| 4 | no | fresh | ``root root 755`` — ``dev`` cannot write. Then the still-empty volume mounted into the pre-creating image is re-owned (F2 cell 3) |
| 5 | no | fresh, **repairing entrypoint** | ``dev dev 700``, ``dev`` writes, ``berth=N`` logged; still ``dev``-owned after ``--force-recreate`` and in a plain container with no entrypoint (F2 cell 6) |

Fixtures are built here from ``debian:bookworm-slim`` (``openssh-server`` + ``sudo`` +
``dev`` at uid/gid 1000 with passwordless sudo + the template ``sshd.conf``), so cell 5
runs the real entrypoint end to end, sshd included. Every test gets its own compose
project and tears it down with ``down -v`` — permitted here and only here, on this
project's own volume (PLAN F11); a reused volume would invalidate a cell, because
ownership is fixed at volume initialisation.

Gating: ``BILLET_INTEGRATION=1`` plus a working ``docker compose``; everything else skips.
The parent ``conftest.py``'s ``az``/``billet``/``ssh`` gates are shadowed here (fixture
override by name, as ``tests/integration/source/conftest.py`` does) because this module
needs none of them. Runs in billet's CI as ``.github/workflows/berth-matrix.yml`` and on
any Host with Docker; the developer Mac has no engine.
"""

from collections.abc import Iterator
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("BILLET_INTEGRATION"),
        reason="Docker ownership matrix; set BILLET_INTEGRATION=1 to run",
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_DIR = _REPO_ROOT / "templates" / "workspace"

#: The Locker this matrix mounts, and the Berth directory the entrypoint ensures.
_LOCKER = "/home/dev/.claude"
_SSH_DIR = "/home/dev/.ssh"

#: ``stat -c '%U %G %a'`` renderings of the two outcomes.
_DEV_OWNED = "dev dev 700"
_ROOT_OWNED = "root root 755"

#: The entrypoint's last line before it execs the CMD; once logged, the repair has run.
_READY = "dev-entrypoint: starting sshd"

#: Both fixture images share this base: a Berth image built the way Dockerfile.snippet
#: says to, minus the pre-created directories, which are what the two variants differ in.
_BASE_DOCKERFILE = """\
FROM debian:bookworm-slim
RUN apt-get update \\
    && apt-get install -y --no-install-recommends openssh-server sudo \\
    && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 1000 dev \\
    && useradd -u 1000 -g 1000 -m -s /bin/bash dev \\
    && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \\
    && chmod 0440 /etc/sudoers.d/dev
COPY sshd.conf /etc/ssh/sshd_config.d/berth.conf
"""
_PLAIN_TAIL = "USER dev\n"
_MOUNTPOINT_TAIL = """\
RUN install -d -o dev -g dev -m 0700 /home/dev/.ssh \\
    && install -d -o dev -g dev -m 0700 /home/dev/.claude
USER dev
"""

#: One compose file per project. Every service that mounts the Locker shares the project's
#: single ``locker`` volume, so a cell can initialise it with one image and read it back
#: with another. ``berth`` runs the shipped template entrypoint from a read-only bind mount
#: of ``templates/workspace/``, so ``$(dirname "$0")/berth.version`` is the real stamp.
_COMPOSE = """\
services:
  plain:
    image: {plain}
    command: sleep infinity
  mountpoint:
    image: {mountpoint}
    command: sleep infinity
  plain-locker:
    image: {plain}
    command: sleep infinity
    volumes:
      - locker:/home/dev/.claude
  mountpoint-locker:
    image: {mountpoint}
    command: sleep infinity
    volumes:
      - locker:/home/dev/.claude
  berth:
    image: {plain}
    entrypoint: ["bash", "/berth/dev-entrypoint.sh"]
    command: sleep infinity
    init: true
    volumes:
      - {template_dir}:/berth:ro
      - locker:/home/dev/.claude

volumes:
  locker:
"""


def _run(
    args: list[str], timeout: int = 120, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a command, failing the test with its full output when ``check`` and it exits non-zero."""
    result: subprocess.CompletedProcess[str] = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        pytest.fail(
            f"`{' '.join(args)}` exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="session", autouse=True)
def require_az_login() -> None:
    """Shadow the live-acceptance Azure gate — the matrix needs no ``az``."""


@pytest.fixture(autouse=True)
def require_tools() -> None:
    """Shadow the live-acceptance ``billet``/``ssh`` gate — the matrix needs only Docker."""


@pytest.fixture(scope="module", autouse=True)
def require_docker() -> None:
    """Skip the module unless a Docker engine and compose v2 answer."""
    if shutil.which("docker") is None:
        pytest.skip("`docker` not on PATH; the Berth matrix needs a Docker engine")
    for probe in (["docker", "info"], ["docker", "compose", "version"]):
        result: subprocess.CompletedProcess[str] = _run(probe, timeout=60, check=False)
        if result.returncode != 0:
            pytest.skip(f"`{' '.join(probe)}` failed: {result.stderr.strip()}")


@dataclass(frozen=True)
class Images:
    """The two fixture image tags: without and with the pre-created ``~/.claude``."""

    plain: str
    mountpoint: str


@pytest.fixture(scope="module")
def images(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Images]:
    """Build both fixture images once per module and remove them afterwards."""
    tag: str = uuid.uuid4().hex[:8]
    build_dir: Path = tmp_path_factory.mktemp("berth-fixtures")
    shutil.copy(_TEMPLATE_DIR / "sshd.conf", build_dir / "sshd.conf")
    built = Images(plain=f"berth-matrix-plain:{tag}", mountpoint=f"berth-matrix-mountpoint:{tag}")
    for name, tail in ((built.plain, _PLAIN_TAIL), (built.mountpoint, _MOUNTPOINT_TAIL)):
        dockerfile: Path = build_dir / f"Dockerfile.{name.partition(':')[0]}"
        dockerfile.write_text(_BASE_DOCKERFILE + tail)
        _run(
            ["docker", "build", "-q", "-t", name, "-f", str(dockerfile), str(build_dir)],
            timeout=600,
        )
    yield built
    _run(["docker", "rmi", "-f", built.plain, built.mountpoint], check=False)


@dataclass(frozen=True)
class Project:
    """One test's compose project: a unique name, its file, and the compose verbs it needs."""

    name: str
    compose_file: Path

    def compose(
        self, *args: str, timeout: int = 180, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run ``docker compose`` scoped to this project."""
        return _run(
            ["docker", "compose", "-p", self.name, "-f", str(self.compose_file), *args],
            timeout=timeout,
            check=check,
        )

    def run(
        self, service: str, *command: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run ``command`` in a fresh, throwaway container of ``service``."""
        return self.compose("run", "--rm", "-T", service, *command, check=check)

    def exec(
        self, service: str, *command: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run ``command`` in the running container of ``service``."""
        return self.compose("exec", "-T", service, *command, check=check)

    def stat(self, service: str, path: str, *, fresh: bool = True) -> str:
        """``owner group mode`` of ``path``, from a fresh container or the running one."""
        runner = self.run if fresh else self.exec
        return runner(service, "stat", "-c", "%U %G %a", path).stdout.strip()

    def logs(self, service: str) -> str:
        """The current container's log for ``service``."""
        return self.compose("logs", "--no-color", "--no-log-prefix", service).stdout

    def wait_ready(self, service: str, timeout: float = 180.0) -> str:
        """Block until ``service``'s entrypoint has started sshd; return its log.

        Fails, with the log, if the container stops first — under ``set -e`` that is the
        entrypoint aborting before it reached sshd, which is the regression PR #62 forbade.
        """
        deadline: float = time.monotonic() + timeout
        logs: str = ""
        while time.monotonic() < deadline:
            logs = self.logs(service)
            if _READY in logs:
                return logs
            container: str = self.compose("ps", "-a", "-q", service).stdout.strip()
            if container:
                status: str = _run(
                    ["docker", "inspect", "-f", "{{.State.Status}}", container]
                ).stdout.strip()
                if status not in {"created", "running"}:
                    pytest.fail(f"{service} is {status} before sshd started; log:\n{logs}")
            time.sleep(1.0)
        pytest.fail(f"{service} did not log {_READY!r} within {timeout:.0f}s; log:\n{logs}")

    def down(self) -> None:
        """Remove this project's containers AND its volume.

        ``down -v`` is permitted here and only here (F11): the volume is this project's,
        created by this test, and a cell reusing it would be measuring a stale
        initialisation.
        """
        self.compose("down", "-v", "--remove-orphans", check=False)


@pytest.fixture
def project(images: Images, tmp_path: Path) -> Iterator[Project]:
    """A compose project with a unique name and a fresh volume, torn down after the test."""
    compose_file: Path = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        _COMPOSE.format(
            plain=images.plain, mountpoint=images.mountpoint, template_dir=_TEMPLATE_DIR
        )
    )
    scoped = Project(name=f"berth-matrix-{uuid.uuid4().hex[:8]}", compose_file=compose_file)
    yield scoped
    scoped.down()


def test_cell_1_pre_created_mountpoint_without_a_volume(project: Project) -> None:
    assert project.stat("mountpoint", _LOCKER) == _DEV_OWNED


def test_cell_2_pre_created_mountpoint_with_a_fresh_volume(project: Project) -> None:
    # Copy-on-empty applies the image directory's ownership to the fresh volume: the
    # working row every consumer relied on before Berth 1.
    assert project.stat("mountpoint-locker", _LOCKER) == _DEV_OWNED
    project.run("mountpoint-locker", "touch", f"{_LOCKER}/probe")


def test_cell_3_no_mountpoint_and_no_volume(project: Project) -> None:
    absent: subprocess.CompletedProcess[str] = project.run(
        "plain", "test", "-e", _LOCKER, check=False
    )
    assert absent.returncode == 1, f"{_LOCKER} unexpectedly exists in the plain image"


def test_cell_4_no_mountpoint_with_a_fresh_volume_lands_root_owned(project: Project) -> None:
    # The failure ADR-0013 exists for: the daemon creates both the mountpoint and the
    # volume's data directory root:root, and dev cannot write.
    assert project.stat("plain-locker", _LOCKER) == _ROOT_OWNED
    denied: subprocess.CompletedProcess[str] = project.run(
        "plain-locker", "touch", f"{_LOCKER}/probe", check=False
    )
    assert denied.returncode != 0, "dev wrote into a root:root 755 directory"
    # F2 cell 3: the same volume, still empty, mounted into an image that pre-creates the
    # directory — copy-on-empty re-fires and re-owns it. This is why an already-broken
    # empty volume is fixed by recreating the container, never by `docker volume rm`.
    assert project.stat("mountpoint-locker", _LOCKER) == _DEV_OWNED


def test_cell_5_repairing_entrypoint_makes_the_locker_writable_and_it_stays_so(
    project: Project,
) -> None:
    berth_version: str = (_TEMPLATE_DIR / "berth.version").read_text().strip()

    project.compose("up", "-d", "berth")
    first: str = project.wait_ready("berth")
    assert f"dev-entrypoint: berth={berth_version}" in first
    assert f"dev-entrypoint: repaired {_LOCKER} (was root:root 755)" in first
    assert f"dev-entrypoint: created {_SSH_DIR}" in first
    assert "warning:" not in first, first
    assert project.stat("berth", _LOCKER, fresh=False) == _DEV_OWNED
    assert project.stat("berth", _SSH_DIR, fresh=False) == _DEV_OWNED
    project.exec("berth", "touch", f"{_LOCKER}/probe")

    # A fresh container on the same volume: the repaired ownership was written to the
    # volume, so the second entrypoint finds a dev-owned directory and leaves it alone.
    project.compose("up", "-d", "--force-recreate", "berth")
    second: str = project.wait_ready("berth")
    assert "repaired" not in second, second
    assert project.stat("berth", _LOCKER, fresh=False) == _DEV_OWNED
    project.exec("berth", "test", "-f", f"{_LOCKER}/probe")

    # F2 cell 6: even a container with no entrypoint and no pre-created directory sees the
    # repaired volume as dev-owned, so the fix does not depend on who mounts it next.
    assert project.stat("plain-locker", _LOCKER) == _DEV_OWNED
    project.run("plain-locker", "test", "-f", f"{_LOCKER}/probe")
