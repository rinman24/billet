"""Repo-integrity gate: this repo must declare the Canon rules plugin.

A ready-made implementation of the CI declaration-check described in the README
("Enforcing that the rules are installed"). The SessionStart hook can only *warn*;
this test, run in required build validation + branch protection, makes a repo that
drops the canon declaration un-mergeable. Offline and deterministic.

Adopted from canon's ``templates/ci/test_canon_adoption.py`` (commit ``5cc14c1``)
with the knobs below set for billet:
  * MARKETPLACE_REPO  — the GitHub repo backing the canon marketplace
  * REQUIRED_MODULES  — the modules THIS repo requires in .claude/canon.txt
"""

import json
from pathlib import Path
import re
from typing import Any, cast

# --- knobs ----------------------------------------------------------------------
MARKETPLACE_NAME: str = "canon"
MARKETPLACE_REPO: str = "rinman24/canon"
PLUGIN: str = "canon-core@canon"
REQUIRED_MODULES: frozenset[str] = frozenset(
    {
        "architecture-closed",
        "delivery-vertical-slice",
        "dev-hygiene",
        "git-semilinear",
        "testing-conventions",
        "worktree-isolation",
        "typing-python",
    }
)
# -------------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SETTINGS: Path = REPO_ROOT / ".claude" / "settings.json"
CANON_TXT: Path = REPO_ROOT / ".claude" / "canon.txt"


def _settings() -> dict[str, Any]:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def _marketplace() -> dict[str, Any]:
    raw: Any = _settings().get("extraKnownMarketplaces", {}).get(MARKETPLACE_NAME)
    assert isinstance(raw, dict), (
        f"'{MARKETPLACE_NAME}' marketplace missing from .claude/settings.json"
    )
    return cast(dict[str, Any], raw)


def test_canon_marketplace_declared() -> None:
    source: Any = _marketplace().get("source", {})
    assert source.get("source") == "github"
    assert source.get("repo") == MARKETPLACE_REPO


def test_canon_ref_is_pinned_semver_tag() -> None:
    ref: Any = _marketplace()["source"].get("ref")
    assert isinstance(ref, str) and re.fullmatch(r"v\d+\.\d+\.\d+", ref), (
        f"canon ref must be a pinned semver tag (vX.Y.Z), got {ref!r}"
    )


def test_canon_plugin_enabled() -> None:
    assert _settings().get("enabledPlugins", {}).get(PLUGIN) is True


def test_canon_autoupdate_disabled() -> None:
    assert _marketplace().get("autoUpdate") is False, (
        "canon autoUpdate must be false so the pinned ref is not silently rolled"
    )


def _declared_modules() -> set[str]:
    lines: list[str] = CANON_TXT.read_text(encoding="utf-8").splitlines()
    return {
        stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#")
    }


def test_canon_required_modules_declared() -> None:
    missing: frozenset[str] = REQUIRED_MODULES - _declared_modules()
    assert not missing, f".claude/canon.txt must require: {sorted(missing)}"
