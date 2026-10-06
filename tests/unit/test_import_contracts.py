"""Guard: the access-module import-linter contracts name every access module on disk.

The two contracts in ``pyproject.toml`` that keep ResourceAccess modules apart list their
modules by name (D-A8-8), because a wildcard would need ``ignore_imports`` for the shared
``compose_script`` leaf, and an ignore rule can hide a real access-to-access import. A
module listed by name is only checked if it is listed, so a new access module left out of
either list would be exempt in silence. This test makes leaving it out fail.
"""

from pathlib import Path
import tomllib
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ACCESS_ROOT = _REPO_ROOT / "src" / "billet" / "access"

#: The one access module other access modules may import; it is in neither list.
_SHARED_LEAF = "billet.access.container.compose_script"


def _contracts() -> list[dict[str, Any]]:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["importlinter"]["contracts"]


def _access_modules_on_disk() -> set[str]:
    """Each access package, or each module of the package holding the shared leaf."""
    found: set[str] = set()
    leaf_package, _, _ = _SHARED_LEAF.rpartition(".")
    for package in sorted(_ACCESS_ROOT.iterdir()):
        if not (package / "__init__.py").is_file():
            continue
        name = f"billet.access.{package.name}"
        if name != leaf_package:
            found.add(name)
            continue
        modules = (f"{name}.{path.stem}" for path in package.glob("*.py"))
        found.update(m for m in modules if m not in {f"{name}.__init__", _SHARED_LEAF})
    return found


def _mismatch(listed: list[str], contract: str, key: str) -> str:
    on_disk = _access_modules_on_disk()
    lines = [
        f"pyproject.toml, contract {contract!r}, `{key}` is out of step with src/billet/access/:"
    ]
    lines += [f"  add {name!r} to `{key}`" for name in sorted(on_disk - set(listed))]
    lines += [f"  remove {name!r} from `{key}`" for name in sorted(set(listed) - on_disk)]
    return "\n".join(lines)


def test_the_independence_contract_lists_every_access_module() -> None:
    (contract,) = [
        c
        for c in _contracts()
        if c["type"] == "independence" and all(m.startswith("billet.access.") for m in c["modules"])
    ]
    assert set(contract["modules"]) == _access_modules_on_disk(), _mismatch(
        contract["modules"], contract["name"], "modules"
    )


def test_the_shared_leaf_contract_forbids_every_other_access_module() -> None:
    (contract,) = [c for c in _contracts() if c.get("source_modules") == [_SHARED_LEAF]]
    assert contract["type"] == "forbidden"
    assert set(contract["forbidden_modules"]) == _access_modules_on_disk(), _mismatch(
        contract["forbidden_modules"], contract["name"], "forbidden_modules"
    )


def test_the_disk_scan_finds_the_access_modules_it_should() -> None:
    # Vacuity guard: a scan that found nothing (or skipped the split package) would let
    # an empty or stale contract list pass.
    found = _access_modules_on_disk()
    assert "billet.access.doctor" in found
    assert "billet.access.container.compose_container_access" in found
    assert _SHARED_LEAF not in found
