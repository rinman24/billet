"""Local gates for the source fast-forward acceptance — bash + git, no Azure/ssh/billet.

The parent ``tests/integration/conftest.py`` gates the *live two-repo* acceptance behind
``az`` / ``billet`` / ``ssh`` autouse fixtures. This subtree instead runs the **emitted**
``_clone_script`` body against throwaway local git repos, so it needs only ``bash`` and
``git`` — both present in dev and CI. We shadow the parent autouse fixtures (fixture
override by name in the nearer conftest) with no-ops, and gate on the tools this subtree
actually needs, skipping (never failing) when they are absent — mirroring the parent's
skip-not-fail convention.
"""

import shutil

import pytest


@pytest.fixture(scope="session", autouse=True)
def require_az_login() -> None:
    """Shadow the live-acceptance Azure gate — this subtree needs no ``az``."""


@pytest.fixture(autouse=True)
def require_tools() -> None:
    """Shadow the live-acceptance ``billet``/``ssh`` gate — this subtree needs only bash + git."""


@pytest.fixture(autouse=True)
def require_bash_and_git() -> None:
    """Skip this subtree unless ``bash`` and ``git`` are on PATH."""
    for tool in ("bash", "git"):
        if shutil.which(tool) is None:
            pytest.skip(f"`{tool}` not on PATH; the source fast-forward acceptance needs it")
