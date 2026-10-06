"""Pure parser for ``docker compose ps --format json`` (A8, D-A8-3, D-A8-13).

Compose v2.21 and later (v5.1.4 on the Host) print one JSON object per line; older Compose
prints a single JSON array. Both are accepted. Only the four keys ``doctor`` checks are read
from each object (``ID``, ``Project``, ``Service``, ``Publishers``); everything else ``ps``
prints (labels, mounts, command) is ignored and never kept.

A malformed document raises :class:`ComposePsError`, whose message is the short reason the
probe reports as ``skipped: runtime unreadable (<reason>)``.
"""

import json
from typing import cast

from billet.contracts import ComposeContainer, PortPublisher


class ComposePsError(ValueError):
    """``docker compose ps --format json`` output that does not have the expected shape."""


def parse_compose_ps(text: str) -> tuple[ComposeContainer, ...]:
    """Parse ``ps --format json`` output, JSON lines or one JSON array, into containers.

    Blank output parses to no containers.

    Raises
    ------
    ComposePsError
        When the text is not JSON, or an object lacks a key ``doctor`` needs or carries it
        with the wrong type.
    """
    stripped = text.strip()
    if not stripped:
        return ()
    try:
        if stripped.startswith("["):  # valid JSON starting `[` is an array
            items = cast(list[object], json.loads(stripped))
        else:
            items = [json.loads(line) for line in stripped.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise ComposePsError(f"invalid JSON: {exc.msg}") from exc
    return tuple(_container(item) for item in items)


def _container(item: object) -> ComposeContainer:
    if not isinstance(item, dict):
        raise ComposePsError("a container entry is not a JSON object")
    data = cast(dict[str, object], item)
    publishers = data.get("Publishers")
    if publishers is None:
        publishers = []
    if not isinstance(publishers, list):
        raise ComposePsError("Publishers is not a list")
    return ComposeContainer(
        id=_string(data, "ID"),
        project=_string(data, "Project"),
        service=_string(data, "Service"),
        publishers=tuple(_publisher(entry) for entry in cast(list[object], publishers)),
    )


def _publisher(entry: object) -> PortPublisher:
    if not isinstance(entry, dict):
        raise ComposePsError("a Publishers entry is not a JSON object")
    data = cast(dict[str, object], entry)
    return PortPublisher(
        url=_string(data, "URL", allow_empty=True),
        target_port=_int(data, "TargetPort"),
        published_port=_int(data, "PublishedPort"),
        protocol=_string(data, "Protocol", allow_empty=True),
    )


def _string(data: dict[str, object], key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ComposePsError(f"{key} missing or not a string")
    return value


def _int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ComposePsError(f"{key} missing or not an integer")
    return value
