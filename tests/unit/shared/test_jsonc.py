"""Tests for the dependency-free JSONC parser."""

import pytest

from billet.shared import jsonc


def test_parses_plain_json() -> None:
    assert jsonc.loads('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}


def test_strips_line_comments() -> None:
    text = """
    {
        // the service to drive
        "service": "gswa-backend",
        "port": 2222  // loopback
    }
    """
    assert jsonc.loads(text) == {"service": "gswa-backend", "port": 2222}


def test_strips_block_comments() -> None:
    text = '{ /* lead */ "a": 1, /* mid */ "b": 2 /* tail */ }'
    assert jsonc.loads(text) == {"a": 1, "b": 2}


def test_strips_trailing_commas_in_objects_and_arrays() -> None:
    text = '{ "list": [1, 2, 3,], "obj": {"x": 1,}, }'
    assert jsonc.loads(text) == {"list": [1, 2, 3], "obj": {"x": 1}}


def test_preserves_comment_markers_inside_strings() -> None:
    # A `//` or `,]` inside a string value must not be treated as a comment / trailing comma.
    text = '{"url": "https://example.com", "note": "a,]b"}'
    assert jsonc.loads(text) == {"url": "https://example.com", "note": "a,]b"}


def test_preserves_escaped_quote_inside_string() -> None:
    text = '{"q": "she said \\"hi\\" // not a comment"}'
    assert jsonc.loads(text) == {"q": 'she said "hi" // not a comment'}


def test_rejects_non_object_top_level() -> None:
    with pytest.raises(ValueError, match="top level"):
        jsonc.loads("[1, 2, 3]")


def test_rejects_invalid_json() -> None:
    with pytest.raises(ValueError):
        jsonc.loads("{not valid}")


def test_parses_gswa_style_devcontainer() -> None:
    # Mirrors gswa's real .devcontainer/devcontainer.json shape (JSONC with comments).
    text = """
    {
        "name": "GenShift Development Container",
        "dockerComposeFile": "docker-compose.yml",  // relative to .devcontainer/
        "service": "gswa-backend",                   // NOT "app"
        "workspaceFolder": "/app",
        "postCreateCommand": "bash .devcontainer/postcreate.sh",
        "remoteUser": "dev",
    }
    """
    data = jsonc.loads(text)
    assert data["service"] == "gswa-backend"
    assert data["dockerComposeFile"] == "docker-compose.yml"
    assert data["workspaceFolder"] == "/app"
    assert data["remoteUser"] == "dev"
    assert data["postCreateCommand"] == "bash .devcontainer/postcreate.sh"
