"""Tests for the pure runtime policy: entrypoint log lines to a RuntimeReport (D-A4-8).

The fixtures are the entrypoint's own lines: every ``echo "dev-entrypoint: …"`` statement in
``templates/workspace/dev-entrypoint.sh`` is extracted and run through ``bash`` with sample
values, so a reworded log line changes the fixture and the parser is tested against it.
"""

from pathlib import Path
import re
import subprocess

import pytest

from billet.contracts import (
    ENTRYPOINT_LOG_PREFIX,
    RunningBerthState,
    RuntimeState,
    WorkspaceRuntimeRead,
)
from billet.workspace.engine.runtime_policy import (
    assess_runtime,
    compare_running_berth,
    parse_log,
)

_TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "workspace" / "dev-entrypoint.sh"

_ECHO = re.compile(r'^\s*echo "dev-entrypoint: ')

# Sample values for the variables the echo statements interpolate.
_SAMPLE_ENV = """\
path=/home/dev/.claude
owner_uid=1001
owner_names=root:root
perms=755
key=AZURE_CLIENT_SECRET
ENV_FILE=/etc/environment
exec 2>&1
"""


def _echo_statements() -> list[str]:
    """Every ``echo "dev-entrypoint: …"`` statement in the template, continuations joined."""
    lines = _TEMPLATE.read_text().splitlines()
    statements: list[str] = []
    for i, line in enumerate(lines):
        if not _ECHO.match(line):
            continue
        statement = line.strip()
        j = i
        while statement.endswith("\\"):
            j += 1
            statement = statement[:-1] + " " + lines[j].strip()
        statements.append(statement)
    return statements


@pytest.fixture(scope="module")
def template_lines(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    """The lines the template's echo statements print, in file order."""
    home = tmp_path_factory.mktemp("berth")
    (home / "berth.version").write_text("1\n")
    script = _SAMPLE_ENV + "\n".join(_echo_statements()) + "\n"
    result = subprocess.run(
        ["bash", "-c", script, str(home / "dev-entrypoint.sh")],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _kind(message: str) -> str:
    return message.split(" ", 1)[0].split("=", 1)[0].rstrip(":")


def test_the_template_fixtures_cover_every_fa9_string(template_lines: list[str]) -> None:
    """Vacuity guard: a reworded or removed log line fails here, not silently."""
    assert all(line.startswith(ENTRYPOINT_LOG_PREFIX) for line in template_lines)
    kinds = [_kind(line[len(ENTRYPOINT_LOG_PREFIX) :]) for line in template_lines]
    for kind in ("berth", "created", "repaired", "warning", "WARNING", "skipping"):
        assert kind in kinds, kind
    assert kinds.count("warning") == 6  # :130 :136 :142 :146 :150 :156
    assert "dev-entrypoint: berth=1" in template_lines


def test_every_template_line_is_classified(template_lines: list[str]) -> None:
    berth = [line for line in template_lines if "berth=" in line]
    others = [line for line in template_lines if "berth=" not in line]
    log = parse_log([*berth, *others])  # berth= opens the run, as at runtime
    assert log.berth == "1"
    assert log.repaired == ("/home/dev/.claude (was root:root 755)",)
    assert len(log.warnings) == 7
    assert "/home/dev/.claude owned by uid 1001; not repaired" in log.warnings
    assert "/home/dev/.claude is root-owned and not empty; not repaired" in log.warnings
    assert "cannot stat /home/dev/.claude; not repaired" in log.warnings
    assert "cannot read /home/dev/.claude; not repaired" in log.warnings
    assert "repair of /home/dev/.claude failed; continuing" in log.warnings
    assert (
        "could not write /etc/environment; image and compose variables will NOT reach sshd "
        "login shells"
    ) in log.warnings  # the uppercase WARNING: line
    assert "created /home/dev/.claude" in log.notes
    assert len(log.repaired) + len(log.warnings) + len(log.notes) == len(others)


def test_skipping_is_informational_not_a_warning(template_lines: list[str]) -> None:
    (skipping,) = [line for line in template_lines if "skipping" in line]
    # gswa-backend's local ENV_SECRET_EXCLUDE variant logs the same verb.
    local = "dev-entrypoint: skipping AZURE_CLIENT_SECRET (credential; never published to …)"
    log = parse_log(["dev-entrypoint: berth=1", skipping, local])
    assert log.warnings == ()
    assert log.notes == (
        "skipping AZURE_CLIENT_SECRET (value cannot be expressed in /etc/environment)",
        "skipping AZURE_CLIENT_SECRET (credential; never published to …)",
    )


def test_only_the_last_run_counts() -> None:
    log = parse_log(
        [
            "dev-entrypoint: berth=1",
            "dev-entrypoint: warning: /home/dev/.x owned by uid 1001; not repaired",
            "dev-entrypoint: berth=2",
            "dev-entrypoint: repaired /home/dev/.x (was root:root 755)",
        ]
    )
    assert log.berth == "2"
    assert log.warnings == ()
    assert log.repaired == ("/home/dev/.x (was root:root 755)",)


def test_lines_without_the_prefix_are_ignored_and_a_missing_berth_line_is_none() -> None:
    log = parse_log(["Server listening on :: port 22.", "dev-entrypoint: created /home/dev/.ssh"])
    assert log.berth is None
    assert log.notes == ("created /home/dev/.ssh",)


@pytest.mark.parametrize(
    ("running", "stamp", "expected"),
    [
        ("1", 1, RunningBerthState.MATCH),
        ("2", 1, RunningBerthState.DIFFERS),
        ("1", 2, RunningBerthState.DIFFERS),
        ("unknown", 1, RunningBerthState.DIFFERS),
        ("1", None, RunningBerthState.DIFFERS),
        (None, 1, RunningBerthState.NOT_LOGGED),
    ],
)
def test_running_berth_against_the_checkout_stamp(
    running: str | None, stamp: int | None, expected: RunningBerthState
) -> None:
    assert compare_running_berth(running, stamp) is expected


def test_assess_a_running_container() -> None:
    read = WorkspaceRuntimeRead(
        RuntimeState.RUNNING,
        log_lines=(
            "dev-entrypoint: berth=2",
            "dev-entrypoint: repaired /home/dev/.claude (was root:root 755)",
            "dev-entrypoint: warning: /home/dev/.cache is root-owned and not empty; not repaired",
        ),
    )
    report = assess_runtime(read, checkout_stamp=1)
    assert report.state is RuntimeState.RUNNING
    assert report.running_berth == "2"
    assert report.checkout_stamp == 1
    assert report.berth_state is RunningBerthState.DIFFERS
    assert report.repaired == ("/home/dev/.claude (was root:root 755)",)
    assert report.warnings == ("/home/dev/.cache is root-owned and not empty; not repaired",)


def test_a_stopped_or_unreadable_container_carries_no_verdict() -> None:
    stopped = assess_runtime(WorkspaceRuntimeRead(RuntimeState.NOT_RUNNING), checkout_stamp=1)
    assert stopped.state is RuntimeState.NOT_RUNNING
    assert stopped.berth_state is None
    unreadable = assess_runtime(
        WorkspaceRuntimeRead(RuntimeState.UNREADABLE, reason="boom"), checkout_stamp=1
    )
    assert unreadable.state is RuntimeState.UNREADABLE
    assert unreadable.reason == "boom"
