"""Tests for dev-entrypoint.sh's ``render_container_env`` — the /etc/environment snapshot.

Docker ``ENV`` and compose ``environment:`` reach the entrypoint process, but an sshd
session is a fresh PAM session that inherits nothing from it. The entrypoint therefore
snapshots its own environment into ``/etc/environment``, which Debian's ``pam_env.so``
replays into every ``billet connect`` login shell.

The script is sourced with ``BILLET_ENTRYPOINT_SOURCE_ONLY=1`` — its documented test seam,
which defines the function and returns before any container-only work (sudo, ssh-keygen,
sshd). ``BILLET_ENV_FILE`` redirects the "existing file" it merges onto a temp path.

From Berth 2 the snapshot withholds credential-shaped variables, by name or by value, unless
the consumer lists them in ``BILLET_ENV_PUBLISH`` (ADR-0003 amendment 2026-09-30). Every
value in these tests is an obviously fake dummy.
"""

from dataclasses import dataclass
from fnmatch import fnmatchcase
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
    # GOOD_NAME, not the Berth 1 GOOD_KEY: `*_KEY` is a credential glob from Berth 2.
    rendered = _render(tmp_path, {"BAD-KEY": "x", "9LEADING_DIGIT": "x", "GOOD_NAME": "x"})
    assert not any("BAD-KEY" in line for line in rendered.block)
    assert not any("9LEADING_DIGIT" in line for line in rendered.block)
    assert rendered.value_of("GOOD_NAME") == '"x"'
    assert "skipping" not in rendered.stderr


def test_an_empty_value_renders_as_empty_quotes(tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"EMPTY_VAR": ""})
    assert 'EMPTY_VAR=""' in rendered.block


def test_block_contents_are_sorted_for_a_stable_diff(tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"ZULU": "1", "ALPHA": "1", "MIKE": "1"})
    assert rendered.block == sorted(rendered.block)
    assert rendered.block.index('ALPHA="1"') < rendered.block.index('MIKE="1"')
    assert rendered.block.index('MIKE="1"') < rendered.block.index('ZULU="1"')


# --- Berth 2: the credential backstop -------------------------------------------------


def _withholding_line(name: str) -> str:
    """The exact stderr line the entrypoint logs for one withheld name (never the value)."""
    return (
        f"dev-entrypoint: withholding {name} "
        "(looks like a credential; list it in BILLET_ENV_PUBLISH to publish)"
    )


def _withheld(rendered: Rendered) -> set[str]:
    """The names the render logged as withheld."""
    prefix = "dev-entrypoint: withholding "
    return {
        line.removeprefix(prefix).split(" ", 1)[0]
        for line in rendered.stderr.splitlines()
        if line.startswith(prefix)
    }


def _assert_withheld(rendered: Rendered, name: str, value: str) -> None:
    """``name`` is absent from the block, logged exactly once, and its value appears nowhere."""
    assert rendered.value_of(name) is None
    assert rendered.stderr.splitlines().count(_withholding_line(name)) == 1
    if value:  # the empty string is "in" every string
        assert value not in rendered.stderr
        assert value not in "\n".join(rendered.block)


#: One name per D-D17-8 name glob, each matching that glob and no other.
_CREDENTIAL_NAMES = [
    ("*TOKEN*", "GITHUB_TOKEN"),
    ("*SECRET*", "APP_SECRET"),
    ("*PASSWORD*", "DB_PASSWORD"),
    ("*PASSWD*", "DB_PASSWD"),
    ("*_PASS", "SMTP_PASS"),
    ("*PASSPHRASE*", "SIGNING_PASSPHRASE"),
    ("*CREDENTIAL*", "AZURE_CREDENTIALS"),
    ("*API_KEY*", "SERVICE_API_KEY_FILE"),
    ("*ACCESS_KEY*", "AWS_ACCESS_KEY_ID"),
    ("*PRIVATE_KEY*", "SSH_PRIVATE_KEY_PATH"),
    ("*_KEY", "STRIPE_KEY"),
    ("*_PAT", "AZURE_DEVOPS_EXT_PAT"),
]


def test_each_parametrised_name_matches_only_its_own_glob() -> None:
    # Keeps the per-glob test honest: were one glob dropped from the script, its name
    # could not be carried to withholding by a neighbouring glob.
    globs = [glob for glob, _ in _CREDENTIAL_NAMES]
    for glob, name in _CREDENTIAL_NAMES:
        assert [g for g in globs if fnmatchcase(name, g)] == [glob], name


@pytest.mark.parametrize(
    "name", [name for _, name in _CREDENTIAL_NAMES], ids=[glob for glob, _ in _CREDENTIAL_NAMES]
)
def test_each_credential_name_glob_withholds(name: str, tmp_path: Path) -> None:
    value = "dummy-not-a-real-credential"
    rendered = _render(tmp_path, {name: value, "KEPT": "fine"})
    _assert_withheld(rendered, name, value)
    assert rendered.value_of("KEPT") == '"fine"'


@pytest.mark.parametrize("name", ["my_token", "Db_Password", "stripe_key", "ado_pat"])
def test_name_matching_is_case_insensitive(name: str, tmp_path: Path) -> None:
    value = "dummy-not-a-real-credential"
    _assert_withheld(_render(tmp_path, {name: value}), name, value)


def test_gpg_key_is_exempt_and_published(tmp_path: Path) -> None:
    # The python base image's public signing-key id, in every fleet image.
    rendered = _render(tmp_path, {"GPG_KEY": "0123456789ABCDEF0123456789ABCDEF01234567"})
    assert rendered.value_of("GPG_KEY") == '"0123456789ABCDEF0123456789ABCDEF01234567"'
    assert rendered.stderr == ""


def test_the_exemption_is_an_exact_name(tmp_path: Path) -> None:
    value = "dummy-not-a-real-credential"
    _assert_withheld(_render(tmp_path, {"MY_GPG_KEY": value}), "MY_GPG_KEY", value)


def test_the_exemption_covers_the_name_test_only(tmp_path: Path) -> None:
    value = "https://dummy:dummy-pw@keys.example.invalid/"
    _assert_withheld(_render(tmp_path, {"GPG_KEY": value}), "GPG_KEY", value)


@pytest.mark.parametrize(
    "name",
    [
        "AUTHOR",
        "OAUTH_CLIENT_ID",
        "KEYBOARD_LAYOUT",
        "SSH_KEYS_DIR",
        "BYPASS_CACHE",
        "TYPST_FONT_PATHS",
        "COMPASS",
        "PATTERN",
    ],
)
def test_near_miss_names_are_published(name: str, tmp_path: Path) -> None:
    rendered = _render(tmp_path, {name: "plain"})
    assert rendered.value_of(name) == '"plain"'
    assert rendered.stderr == ""


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://u:p@h/db",
        "redis://:p@h:6379",
        "postgresql+psycopg://dummy:dummy-pw@sql:5432/app",
    ],
)
def test_a_url_carrying_a_password_is_withheld_by_value(value: str, tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"SERVICE_URL": value})
    _assert_withheld(rendered, "SERVICE_URL", value)


@pytest.mark.parametrize(
    "value",
    [
        "http://host:8080/p@x",
        "https://example.com",
        "user@host",
        "redis://h:6379/0",
        "https://u@h/x",
    ],
)
def test_urls_without_a_password_are_published(value: str, tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"SERVICE_URL": value})
    assert rendered.value_of("SERVICE_URL") == f'"{value}"'
    assert rendered.stderr == ""


def test_the_credential_check_runs_before_the_representability_skip(tmp_path: Path) -> None:
    # A credential with an unrepresentable value is withheld, not "skipped": the log line
    # tells the operator the one thing BILLET_ENV_PUBLISH can and cannot fix.
    value = 'dummy"pw'
    rendered = _render(tmp_path, {"DB_PASSWORD": value})
    _assert_withheld(rendered, "DB_PASSWORD", value)
    assert "skipping" not in rendered.stderr


def test_a_listed_credential_is_published(tmp_path: Path) -> None:
    rendered = _render(
        tmp_path,
        {
            "DB_PASSWORD": "dummy-db-pw",
            "SERVICE_URL": "redis://:dummy-pw@redis:6379/0",
            "BILLET_ENV_PUBLISH": "DB_PASSWORD SERVICE_URL",
        },
    )
    assert rendered.value_of("DB_PASSWORD") == '"dummy-db-pw"'
    assert rendered.value_of("SERVICE_URL") == '"redis://:dummy-pw@redis:6379/0"'
    assert rendered.stderr == ""


def test_the_publish_list_is_whitespace_separated(tmp_path: Path) -> None:
    rendered = _render(
        tmp_path,
        {"A_TOKEN": "dummy-a", "B_TOKEN": "dummy-b", "BILLET_ENV_PUBLISH": "  A_TOKEN\t B_TOKEN "},
    )
    assert rendered.value_of("A_TOKEN") == '"dummy-a"'
    assert rendered.value_of("B_TOKEN") == '"dummy-b"'


@pytest.mark.parametrize("listed", ["*_TOKEN", "APP", "APP_TOKEN_2", "app_token"])
def test_the_publish_list_holds_exact_names_not_globs(listed: str, tmp_path: Path) -> None:
    value = "dummy-not-a-real-credential"
    rendered = _render(tmp_path, {"APP_TOKEN": value, "BILLET_ENV_PUBLISH": listed})
    _assert_withheld(rendered, "APP_TOKEN", value)


def test_a_listed_excluded_name_stays_excluded_silently(tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"HOME": "/home/dev", "BILLET_ENV_PUBLISH": "HOME PATH"})
    assert rendered.value_of("HOME") is None
    assert rendered.value_of("PATH") is None
    assert rendered.stderr == ""


def test_a_listed_unrepresentable_value_is_still_skipped(tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"DB_PASSWORD": 'dummy"pw', "BILLET_ENV_PUBLISH": "DB_PASSWORD"})
    assert rendered.value_of("DB_PASSWORD") is None
    assert "dev-entrypoint: skipping DB_PASSWORD " in rendered.stderr
    assert "withholding" not in rendered.stderr


@pytest.mark.parametrize("listed", ["", "A_TOKEN", "BILLET_ENV_PUBLISH"])
def test_billet_env_publish_itself_is_never_published(listed: str, tmp_path: Path) -> None:
    rendered = _render(tmp_path, {"A_TOKEN": "dummy-a", "BILLET_ENV_PUBLISH": listed})
    assert rendered.value_of("BILLET_ENV_PUBLISH") is None
    assert "BILLET_ENV_PUBLISH=" not in rendered.stdout


def test_listed_but_unset_and_listed_but_not_credential_names_are_silent(
    tmp_path: Path,
) -> None:
    rendered = _render(
        tmp_path,
        {"TYPST_FONT_PATHS": "/workspace/fonts", "BILLET_ENV_PUBLISH": "NOT_SET TYPST_FONT_PATHS"},
    )
    assert rendered.value_of("TYPST_FONT_PATHS") == '"/workspace/fonts"'
    assert rendered.value_of("NOT_SET") is None
    assert rendered.stderr == ""


# --- The fleet, as it stands on 2026-09-30 (D-D17-8's fleet result) ---------------------

#: Image ``ENV`` common to all four images (PATH omitted: the harness supplies the real one,
#: and it is excluded anyway). Dummy values throughout.
_IMAGE_BASE = {
    "LANG": "C.UTF-8",
    "GPG_KEY": "0123456789ABCDEF0123456789ABCDEF01234567",
    "PYTHON_VERSION": "3.11.0",
    "PYTHON_SHA256": "0" * 64,
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_DEFAULT_TIMEOUT": "100",
    "UV_PYTHON_DOWNLOADS": "never",
}
_CLAUDE = {"CLAUDE_CONFIG_DIR": "/home/dev/.claude"}
_AZ = {"AZURE_CORE_COLLECT_TELEMETRY": "0"}

_FLEET: dict[str, dict[str, str]] = {
    "billet": {**_IMAGE_BASE, **_AZ, **_CLAUDE},
    "genshift-brand": {
        **_IMAGE_BASE,
        "DISABLE_AUTOUPDATER": "1",
        **_CLAUDE,
        "TYPST_FONT_PATHS": "/workspace/fonts",
    },
    "squadra": {**_IMAGE_BASE, **_AZ, "UV_LINK_MODE": "copy", **_CLAUDE},
    "gswa-backend": {
        **_IMAGE_BASE,
        **_AZ,
        "VIRTUAL_ENV": "/workspace/.venv",
        "UV_PROJECT_ENVIRONMENT": "/workspace/.venv",
        **_CLAUDE,
        "REDIS_URL": "redis://:dummy-redis-pw@redis:6379/0",
        "GSWA_DB_DRIVERNAME": "postgresql+psycopg",
        "GSWA_DB_HOST": "sql",
        "GSWA_DB_PORT": "5432",
        "GSWA_DB_USERNAME": "dummy_user",
        "GSWA_DB_DATABASE": "dummy_db",
        "GSWA_DB_PASSWORD": "dummy-db-pw",
        "GSWA_TEST_DB_URL": "postgresql+psycopg://dummy_user:dummy-db-pw@sql:5432/dummy_test",
    },
}

_GSWA_WITHHELD = {"GSWA_DB_PASSWORD", "REDIS_URL", "GSWA_TEST_DB_URL"}


@pytest.mark.parametrize(
    ("workspace", "extra", "withheld"),
    [
        ("billet", {}, set[str]()),
        ("genshift-brand", {}, set[str]()),
        ("squadra", {}, set[str]()),
        # Berth 1 squadra compose still carries the PAT line, empty live.
        ("squadra", {"AZURE_DEVOPS_EXT_PAT": ""}, {"AZURE_DEVOPS_EXT_PAT"}),
        ("gswa-backend", {}, _GSWA_WITHHELD),
        (
            "gswa-backend",
            {"BILLET_ENV_PUBLISH": "REDIS_URL GSWA_DB_PASSWORD GSWA_TEST_DB_URL"},
            set[str](),
        ),
    ],
    ids=[
        "billet",
        "genshift-brand",
        "squadra",
        "squadra-with-pat-line",
        "gswa-backend",
        "gswa-backend-opted-in",
    ],
)
def test_the_fleet_withholds_exactly_the_decided_names(
    workspace: str, extra: dict[str, str], withheld: set[str], tmp_path: Path
) -> None:
    env = {**_FLEET[workspace], **extra}
    rendered = _render(tmp_path, env)

    assert _withheld(rendered) == withheld
    assert len(rendered.stderr.splitlines()) == len(withheld)  # nothing else is logged
    for name, value in env.items():
        if name == "BILLET_ENV_PUBLISH":
            assert rendered.value_of(name) is None
        elif name in withheld:
            _assert_withheld(rendered, name, value)
        else:
            assert rendered.value_of(name) == f'"{value}"', name
    assert rendered.value_of("GPG_KEY") is not None


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
