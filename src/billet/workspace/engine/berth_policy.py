"""BerthPolicy — pure Berth drift policy: normalization, directive hash, stamp, diff cap.

ADR-0012 item 5 makes the *directive hash* the unit of drift: two copies of a Berth file are
the same revision when they agree after normalization, whatever their comments or
whitespace. The normalization (D-A4-3, ADR-0012 item 5's 2026-09-29 clarification) runs in
this order:

1. fold backslash line continuations;
2. strip leading and trailing whitespace from every line;
3. drop blank lines;
4. drop lines whose first character is ``#`` (the shebang included).

A trailing ``# …`` after code is kept: splitting it off would mean parsing shell. The hash
is SHA-256 of the normalized lines joined with newlines.

No I/O: the templates arrive as a :class:`PackagedBerth` and the Workspace's files as a
:class:`WorkspaceBerthRead`, both read by the access layer.
"""

from collections.abc import Sequence
import difflib
import hashlib

from billet.contracts import (
    BERTH_HASHED_FILES,
    BERTH_VERSION_FILE,
    BerthFileState,
    BerthFileStatus,
    BerthStatus,
    PackagedBerth,
    StampState,
    StampStatus,
    WorkspaceBerthRead,
)

#: At most this many changed (``+``/``-``) lines are reported per drifted file (D-A4-11).
#: The one diff unit is the changed line (D-A7-8): the ``(N lines)`` count, this cap and the
#: ``… (M more)`` remainder all count only ``+``/``-`` lines, so shown + M = N always.
DIFF_CAP = 20

_CONTINUATION = "\\"
_COMMENT = "#"
_HUNK_MARKER = "@@"


def normalize(text: str) -> tuple[str, ...]:
    """Return ``text``'s directive lines, per the four ordered D-A4-3 rules.

    A continuation is folded by joining the next physical line onto the current one with a
    single space, so re-indenting a continued line is a whitespace-only change too.
    """
    folded: list[str] = []
    pending: str | None = None
    for raw in text.splitlines():
        line = raw if pending is None else f"{pending} {raw.lstrip()}"
        if line.endswith(_CONTINUATION):
            pending = line[: -len(_CONTINUATION)].rstrip()
            continue
        pending = None
        folded.append(line)
    if pending is not None:
        folded.append(pending)
    stripped = (line.strip() for line in folded)
    return tuple(line for line in stripped if line and not line.startswith(_COMMENT))


def directive_hash(text: str) -> str:
    """Return the SHA-256 hex digest of ``text``'s normalized lines joined with newlines."""
    return hashlib.sha256("\n".join(normalize(text)).encode("utf-8")).hexdigest()


def compare_stamp(shipped: int, consumer: str | None) -> StampStatus:
    """Compare a Workspace's ``berth.version`` text with the version billet ships.

    ``None`` (the file is missing) or text that is not one positive integer is ``unknown``.
    """
    found = None if consumer is None else _stamp_value(consumer)
    if found is None:
        return StampStatus(StampState.UNKNOWN, shipped, None)
    if found < shipped:
        return StampStatus(StampState.BEHIND, shipped, found)
    if found > shipped:
        return StampStatus(StampState.AHEAD, shipped, found)
    return StampStatus(StampState.OK, shipped, found)


def compare_file(name: str, shipped: str, consumer: str | None) -> BerthFileStatus:
    """Compare one Berth file by directive hash; on drift, carry the capped normalized diff."""
    if consumer is None:
        return BerthFileStatus(name, BerthFileState.MISSING)
    if directive_hash(shipped) == directive_hash(consumer):
        return BerthFileStatus(name, BerthFileState.OK)
    body = _normalized_diff(normalize(shipped), normalize(consumer))
    changed = sum(1 for line in body if line != _HUNK_MARKER)
    shown, more = cap_diff(body)  # shown's changed lines + more == changed
    return BerthFileStatus(
        name, BerthFileState.DRIFT, changed_lines=changed, diff=shown, diff_more=more
    )


def cap_diff(lines: Sequence[str], cap: int = DIFF_CAP) -> tuple[tuple[str, ...], int]:
    """Return ``lines`` through the ``cap``-th changed line, and how many changed lines follow.

    Only ``+``/``-`` lines count (D-A7-8). A bare ``@@`` separator is kept between hunks
    inside the shown part and never counted, and none is left dangling after the last shown
    line, so the shown changed lines plus the withheld count equal the diff's changed lines.
    """
    shown: list[str] = []
    kept = 0
    for line in lines:
        if line == _HUNK_MARKER:
            if kept < cap:
                shown.append(line)
            continue
        if kept == cap:
            break
        shown.append(line)
        kept += 1
    if shown and shown[-1] == _HUNK_MARKER:
        shown.pop()
    changed = sum(1 for line in lines if line != _HUNK_MARKER)
    return tuple(shown), changed - kept


def assess(
    read: WorkspaceBerthRead, berth: PackagedBerth, *, host: str, repo_dir: str
) -> BerthStatus:
    """Build one Workspace's :class:`BerthStatus` from its raw read and the shipped Berth."""
    return BerthStatus(
        workspace=read.workspace,
        host=host,
        repo_dir=repo_dir,
        head=read.head,
        stamp=compare_stamp(berth.version, read.files.get(BERTH_VERSION_FILE)),
        files=tuple(
            compare_file(name, berth.files[name], read.files.get(name))
            for name in BERTH_HASHED_FILES
        ),
    )


def _stamp_value(text: str) -> int | None:
    stripped = text.strip()
    if not stripped.isdigit():
        return None
    value = int(stripped)
    return value if value > 0 else None


def _normalized_diff(shipped: Sequence[str], consumer: Sequence[str]) -> list[str]:
    """Unified diff (no context) of normalized lines, without file or first-hunk headers.

    Hunk headers are reduced to a bare ``@@`` separator between hunks: their line numbers
    count *normalized* lines, which name no line in either real file.
    """
    body: list[str] = []
    for line in difflib.unified_diff(shipped, consumer, lineterm="", n=0):
        if line.startswith(_HUNK_MARKER):
            if body:
                body.append(_HUNK_MARKER)
        elif body or not line.startswith(("---", "+++")):
            body.append(line)
    return body
