"""Tests for dev-entrypoint.sh's ``render_container_env`` — the /etc/environment snapshot.

Docker ``ENV`` and compose ``environment:`` reach the entrypoint process, but an sshd
session is a fresh PAM session that inherits nothing from it. The entrypoint therefore
snapshots its own environment into ``/etc/environment``, which Debian's ``pam_env.so``
replays into every ``billet connect`` login shell.

The script is sourced with ``BILLET_ENTRYPOINT_SOURCE_ONLY=1`` — its documented test seam,
which defines the function and returns before any container-only work (sudo, ssh-keygen,
sshd). ``BILLET_ENV_FILE`` redirects the "existing file" it merges onto a temp path.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE = _REPO_ROOT / "templates" / "workspace" / "dev-entrypoint.sh"
_DEVCONTAINER = _REPO_ROOT / ".devcontainer" / "dev-entrypoint.sh"

#: The line both copies share as the first line below their differing header comment.
_SPLIT_LINE = b"set -euo pipefail"

#: Block markers, matched by the script as whole lines.
_BEGIN = "# >>> billet dev-entrypoint: container environment (regenerated on start) >>>"
_END = "# <<< billet dev-entrypoint <<<"

#: Per-session names a login shell must own for itself, never pinned globally.
_EXCLUDED = (
    "HOME",
    "PATH",
    "SHELL",
    "USER",
    "LOGNAME",
    "PWD",
    "OLDPWD",
    "HOSTNAME",
    "TERM",
    "SHLVL",
    "_",
)


@dataclass(frozen=True)
class Rendered:
    """One ``render_container_env`` run: its STDOUT, its STDERR, and the parsed block."""

    stdout: str
    stderr: str

    @property
    def lines(self) -> list[str]:
        """Every rendered line, in order."""
        return self.stdout.splitlines()

    @property
    def preamble(self) -> list[str]:
        """The lines above the billet block — everything the script does not own."""
        return self.lines[: self.lines.index(_BEGIN)]

    @property
    def block(self) -> list[str]:
        """The lines strictly between the begin and end markers."""
        return self.lines[self.lines.index(_BEGIN) + 1 : self.lines.index(_END)]

    def value_of(self, key: str) -> str | None:
        """The rendered value of ``key`` inside the block, or ``None`` if it was dropped."""
        for line in self.block:
            name, _, value = line.partition("=")
            if name == key:
                return value
        return None


def _render(tmp_path: Path, env: dict[str, str], existing: str | None = None) -> Rendered:
    """Source the template and run ``render_container_env`` under a controlled environment.

    Parameters
    ----------
    tmp_path
        Directory holding the stand-in for ``/etc/environment``.
    env
        Variables to publish to the function, on top of the seam's own.
    existing
        Content the env-file already has, if any. Absent means no file at all.

    Returns
    -------
    Rendered
        Captured STDOUT/STDERR of the run.
    """
    env_file = tmp_path / "environment"
    if existing is not None:
        env_file.write_text(existing)
    # PATH is needed for awk/env/sort and is itself an excluded name, so it never
    # pollutes the rendered block.
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "BILLET_ENTRYPOINT_SOURCE_ONLY": "1",
        "BILLET_ENV_FILE": str(env_file),
        **env,
    }
    result = subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(_TEMPLATE))}; render_container_env"],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"render_container_env failed: {result.stderr}"
    return Rendered(stdout=result.stdout, stderr=result.stderr)


def _body(path: Path) -> bytes:
    """The script from its first ``set -euo pipefail`` line on, i.e. below the header."""
    raw = path.read_bytes()
    index = raw.find(b"\n" + _SPLIT_LINE + b"\n")
    assert index != -1, f"{path} has no `{_SPLIT_LINE.decode()}` line to split on"
    return raw[index + 1 :]


def test_compose_variables_are_rendered_inside_the_block(tmp_path: Path) -> None:
    # The exact regression: compose `environment:` values invisible to sshd login shells.
    rendered = _render(
        tmp_path,
        {"TYPST_FONT_PATHS": "/workspace/fonts", "DISABLE_AUTOUPDATER": "1"},
    )
    assert 'TYPST_FONT_PATHS="/workspace/fonts"' in rendered.block
    assert 'DISABLE_AUTOUPDATER="1"' in rendered.block


@pytest.mark.parametrize("name", _EXCLUDED)
def test_per_session_names_are_never_published(name: str, tmp_path: Path) -> None:
    # PATH keeps its real value (awk/sort need it); the rest get a sentinel. Either way
    # the name is present in the function's environment and must not reach the block.
    env = {excluded: "sentinel" for excluded in _EXCLUDED if excluded != "PATH"}
    rendered = _render(tmp_path, env)
    assert rendered.value_of(name) is None
    assert not any(line.startswith(f"{name}=") for line in rendered.block)


def test_lines_billet_does_not_own_are_preserved_verbatim(tmp_path: Path) -> None:
    existing = '# image-baked defaults\nLANG="C.UTF-8"\nDEBIAN_FRONTEND=noninteractive\n'
    rendered = _render(tmp_path, {"TYPST_FONT_PATHS": "/workspace/fonts"}, existing=existing)
    assert rendered.preamble == [
        "# image-baked defaults",
        'LANG="C.UTF-8"',
        "DEBIAN_FRONTEND=noninteractive",
    ]
    assert rendered.stdout.startswith(existing)


def test_rendering_is_idempotent_across_restarts(tmp_path: Path) -> None:
    env = {"TYPST_FONT_PATHS": "/workspace/fonts", "DISABLE_AUTOUPDATER": "1"}
    existing = "# image-baked defaults\nLANG=C.UTF-8\n"

    first = _render(tmp_path, env, existing=existing)
    # Feed the render back in as the file the next container start finds.
    second = _render(tmp_path, env, existing=first.stdout)

    assert second.stdout == first.stdout
    assert second.stdout.count(f"{_BEGIN}\n") == 1
    assert second.stdout.count(f"{_END}\n") == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("HAS_QUOTE", 'say "hi"'),
        ("HAS_BACKSLASH", "C:\\fonts"),
        ("HAS_CONTROL_CHAR", "a\tb"),
    ],
)
def test_values_pam_env_cannot_express_are_skipped_with_a_warning(
    key: str, value: str, tmp_path: Path
) -> None:
    # pam_env strips one quote pair, does no backslash unescaping, and joins lines ending
    # in a backslash — so these have no faithful representation and are dropped loudly.
    rendered = _render(tmp_path, {key: value, "KEPT": "fine"})
    assert rendered.value_of(key) is None
    assert f"dev-entrypoint: skipping {key} " in rendered.stderr
    assert rendered.value_of("KEPT") == '"fine"'


def test_names_that_are_not_shell_identifiers_are_dropped_silently(tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"BAD-KEY": "x", "9LEADING_DIGIT": "x", "GOOD_KEY": "x"})
    assert not any("BAD-KEY" in line for line in rendered.block)
    assert not any("9LEADING_DIGIT" in line for line in rendered.block)
    assert rendered.value_of("GOOD_KEY") == '"x"'
    assert "skipping" not in rendered.stderr


def test_an_empty_value_renders_as_empty_quotes(tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"EMPTY_VAR": ""})
    assert 'EMPTY_VAR=""' in rendered.block


def test_block_contents_are_sorted_for_a_stable_diff(tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"ZULU": "1", "ALPHA": "1", "MIKE": "1"})
    assert rendered.block == sorted(rendered.block)
    assert rendered.block.index('ALPHA="1"') < rendered.block.index('MIKE="1"')
    assert rendered.block.index('MIKE="1"') < rendered.block.index('ZULU="1"')


def test_template_and_devcontainer_copy_stay_byte_identical() -> None:
    assert _body(_TEMPLATE) == _body(_DEVCONTAINER), (
        f"{_TEMPLATE.relative_to(_REPO_ROOT)} and {_DEVCONTAINER.relative_to(_REPO_ROOT)} "
        f"must stay byte-identical from `{_SPLIT_LINE.decode()}` onward. The template is "
        "what consumed repos install; billet's own .devcontainer/ is one of those "
        "consumers, so it dogfoods the same script. Only the leading header comment may "
        "differ (it addresses a different reader). Edit one, port the change to the other."
    )


@pytest.mark.parametrize("script", [_TEMPLATE, _DEVCONTAINER], ids=["template", "devcontainer"])
def test_script_parses_as_bash(script: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, f"`bash -n {script}` failed: {result.stderr}"
