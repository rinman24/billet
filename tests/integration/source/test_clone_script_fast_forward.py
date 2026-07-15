"""Exercise the emitted ``_clone_script`` body against throwaway local git repos.

These run the real bash the Host would run (``bash -c <script>``) against local bare/clone
repos on a filesystem-path ``repo_url``, asserting the observable behavior the fix promises:
first clone works, a clean branch behind upstream fast-forwards, an untracked file survives
the advance, and every unsafe checkout (dirty tracked file, diverged branch, detached HEAD)
is skipped non-destructively with a ``[billet/source]`` warning and a zero exit.
"""

import os
from pathlib import Path
import subprocess

from billet.access.source.git_source_access import GitSourceAccess
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host, make_workspace_spec

#: Hermetic git env — no global/system config, an explicit identity, no interactive prompts.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "billet-test",
    "GIT_AUTHOR_EMAIL": "billet@test.invalid",
    "GIT_COMMITTER_NAME": "billet-test",
    "GIT_COMMITTER_EMAIL": "billet@test.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git`` in ``cwd`` with the hermetic env, raising on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    )


def _init_origin(root: Path) -> tuple[Path, Path]:
    """Create a bare ``origin.git`` with one commit on ``main`` plus a seed working clone."""
    bare = root / "origin.git"
    _git(root, "init", "--bare", "-b", "main", str(bare))
    seed = root / "seed"
    _git(root, "clone", str(bare), str(seed))
    (seed / "README.md").write_text("v1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "c1")
    _git(seed, "push", "-u", "origin", "main")
    return bare, seed


def _advance_origin(seed: Path, text: str = "v2\n") -> None:
    """Push a new commit to origin's ``main`` via the seed clone."""
    (seed / "README.md").write_text(text)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "c2")
    _git(seed, "push", "origin", "main")


def _clone_checkout(root: Path, bare: Path, name: str = "checkout") -> Path:
    """Clone ``bare`` into ``root/name`` the way billet's first ``start`` would."""
    _git(root, "clone", str(bare), str(root / name))
    return root / name


def _emitted_script(bare: Path, repo_dir: str) -> str:
    """Return the exact remote bash GitSourceAccess emits (the final ssh argv element)."""
    spec = make_workspace_spec(repo_url=str(bare), repo_dir=repo_dir)
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(spec, make_remote_host())
    return runner.calls[-1][-1]


def _run_script(bare: Path, repo_dir: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the emitted script for ``repo_url=bare`` under ``bash -c`` in ``cwd``."""
    script = _emitted_script(bare, repo_dir)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=_GIT_ENV,
    )


def test_first_use_clones(tmp_path: Path) -> None:
    bare, _seed = _init_origin(tmp_path)
    result = _run_script(bare, "fresh", tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "fresh" / ".git").is_dir()
    assert (tmp_path / "fresh" / "README.md").read_text() == "v1\n"
    assert "cloning" in result.stdout


def test_clean_branch_behind_fast_forwards(tmp_path: Path) -> None:
    bare, seed = _init_origin(tmp_path)
    checkout = _clone_checkout(tmp_path, bare)
    _advance_origin(seed)
    result = _run_script(bare, "checkout", tmp_path)
    assert result.returncode == 0, result.stderr
    assert (checkout / "README.md").read_text() == "v2\n"  # advanced to upstream
    assert "up to date" in result.stdout


def test_untracked_file_survives_fast_forward(tmp_path: Path) -> None:
    bare, seed = _init_origin(tmp_path)
    checkout = _clone_checkout(tmp_path, bare)
    (checkout / ".devcontainer").mkdir()
    (checkout / ".devcontainer" / ".env").write_text("SECRET=keep\n")  # bootstrap-written
    _advance_origin(seed)
    result = _run_script(bare, "checkout", tmp_path)
    assert result.returncode == 0, result.stderr
    assert (checkout / "README.md").read_text() == "v2\n"  # still fast-forwarded
    assert (checkout / ".devcontainer" / ".env").read_text() == "SECRET=keep\n"  # survived


def test_dirty_tracked_file_skips_and_survives(tmp_path: Path) -> None:
    bare, seed = _init_origin(tmp_path)
    checkout = _clone_checkout(tmp_path, bare)
    (checkout / "README.md").write_text("local-edit\n")  # uncommitted tracked change
    _advance_origin(seed)
    result = _run_script(bare, "checkout", tmp_path)
    assert result.returncode == 0, result.stderr
    assert (checkout / "README.md").read_text() == "local-edit\n"  # edit intact, not clobbered
    assert "tracked files dirty" in result.stdout


def test_diverged_branch_skips_and_commit_survives(tmp_path: Path) -> None:
    bare, seed = _init_origin(tmp_path)
    checkout = _clone_checkout(tmp_path, bare)
    # A committed local change that is NOT on origin, plus a different origin commit → diverged.
    (checkout / "README.md").write_text("local-commit\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "local")
    head_before = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    _advance_origin(seed)
    result = _run_script(bare, "checkout", tmp_path)
    assert result.returncode == 0, result.stderr
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head_before  # commit intact
    assert (checkout / "README.md").read_text() == "local-commit\n"
    assert "cannot fast-forward" in result.stdout


def test_detached_head_skips(tmp_path: Path) -> None:
    bare, seed = _init_origin(tmp_path)
    checkout = _clone_checkout(tmp_path, bare)
    _git(checkout, "checkout", "--detach", "HEAD")
    _advance_origin(seed)
    result = _run_script(bare, "checkout", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "HEAD is detached" in result.stdout
