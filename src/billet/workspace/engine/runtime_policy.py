"""RuntimePolicy — pure parser from the entrypoint's log to a :class:`RuntimeReport`.

``doctor`` learns what a running container knows from the Berth entrypoint's own log, never
from ``docker exec`` (D-A4-8, ADR-0015 item 4). Every line the entrypoint prints starts
``dev-entrypoint: `` (``templates/workspace/dev-entrypoint.sh``); after that prefix:

- ``berth=<N or unknown>`` opens a run. A restarted container keeps its log, so only the
  lines from the *last* ``berth=`` line on describe the current run;
- ``repaired <path> (was <owner>:<group> <mode>)`` is a mountpoint the entrypoint fixed:
  ``ok (repaired at start)``;
- ``warning: …`` and the uppercase ``WARNING: …`` are ``warn``;
- anything else (``created <path>``, ``skipping <key> …``, progress lines) is informational.

The running Berth is compared with the checkout's ``berth.version`` stamp: equal is ok, and a
difference, an ``unknown`` Berth or no ``berth=`` line at all is a warning.

Accepted limitation: ownership that changes after start is not seen (nothing does that).

No I/O: the log lines arrive in a :class:`WorkspaceRuntimeRead` from the access layer.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from billet.contracts import (
    ENTRYPOINT_LOG_PREFIX,
    RunningBerthState,
    RuntimeReport,
    RuntimeState,
    WorkspaceRuntimeRead,
)

_BERTH = "berth="
_REPAIRED = "repaired "
_WARNINGS = ("warning: ", "WARNING: ")


@dataclass(frozen=True, slots=True)
class EntrypointLog:
    """The current run of an entrypoint log, classified line by line."""

    berth: str | None
    repaired: tuple[str, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]


def parse_log(lines: Sequence[str]) -> EntrypointLog:
    """Classify the entrypoint lines of the current run (from the last ``berth=`` line on).

    Lines without the entrypoint prefix are ignored; with no ``berth=`` line, every line is
    taken as the current run and ``berth`` is ``None``.
    """
    messages = [
        line.rstrip("\r\n")[len(ENTRYPOINT_LOG_PREFIX) :]
        for line in lines
        if line.startswith(ENTRYPOINT_LOG_PREFIX)
    ]
    starts = [i for i, message in enumerate(messages) if message.startswith(_BERTH)]
    berth: str | None = None
    if starts:
        run = messages[starts[-1] :]
        berth = run[0][len(_BERTH) :].strip()
        run = run[1:]
    else:
        run = messages
    repaired: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    for message in run:
        if message.startswith(_REPAIRED):
            repaired.append(message[len(_REPAIRED) :])
        elif message.startswith(_WARNINGS):
            warnings.append(message.split(": ", 1)[1])
        else:
            notes.append(message)
    return EntrypointLog(berth, tuple(repaired), tuple(warnings), tuple(notes))


def compare_running_berth(running: str | None, checkout_stamp: int | None) -> RunningBerthState:
    """Compare the ``berth=`` value the container logged with the checkout's stamp."""
    if running is None:
        return RunningBerthState.NOT_LOGGED
    if checkout_stamp is not None and running.isdigit() and int(running) == checkout_stamp:
        return RunningBerthState.MATCH
    return RunningBerthState.DIFFERS


def assess_runtime(read: WorkspaceRuntimeRead, checkout_stamp: int | None) -> RuntimeReport:
    """Build one Workspace's :class:`RuntimeReport` from its raw runtime read."""
    if read.state is not RuntimeState.RUNNING:
        return RuntimeReport(state=read.state, checkout_stamp=checkout_stamp, reason=read.reason)
    log = parse_log(read.log_lines)
    return RuntimeReport(
        state=RuntimeState.RUNNING,
        checkout_stamp=checkout_stamp,
        running_berth=log.berth,
        berth_state=compare_running_berth(log.berth, checkout_stamp),
        repaired=log.repaired,
        warnings=log.warnings,
        notes=log.notes,
    )
