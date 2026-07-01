"""Dependency-free JSONC parsing.

``devcontainer.json`` is *JSONC*: standard JSON plus ``//`` / ``/* */`` comments and
trailing commas, which :func:`json.loads` rejects. Rather than pull in a dependency
(billet ships with only ``typer``), we strip both — string-aware, so a ``//`` or ``,]``
*inside* a string value is preserved — then hand the result to the stdlib parser. If this
ever proves too fragile for real-world files, swap the body of :func:`loads` for ``pyjson5``
behind the same signature; nothing else changes.
"""

import json
from typing import Any, cast

# Only double-quoted strings exist in JSONC (unlike JSON5), so a single quote char suffices.
_QUOTE = '"'


def _strip_comments(text: str) -> str:
    """Remove ``//`` line and ``/* */`` block comments, ignoring those inside strings."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < n:  # escape: copy the escaped char verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if char == _QUOTE:
                in_string = False
            i += 1
            continue
        if char == _QUOTE:
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Drop a comma that is followed (past whitespace) by ``}`` or ``]``, string-aware."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if char == _QUOTE:
                in_string = False
            i += 1
            continue
        if char == _QUOTE:
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # skip the trailing comma
                continue
        out.append(char)
        i += 1
    return "".join(out)


def loads(text: str) -> dict[str, Any]:
    """Parse a JSONC document into a dict.

    Raises
    ------
    ValueError
        If the text is not valid JSON once comments and trailing commas are stripped, or
        the top-level value is not an object.
    """
    cleaned = _strip_trailing_commas(_strip_comments(text))
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object at the top level")
    return cast("dict[str, Any]", data)
