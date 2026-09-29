"""The Berth the installed billet ships, resolved through ``importlib.resources``.

``templates/workspace/`` is force-included into the wheel as ``billet/_berth``
(``pyproject.toml``), so ``doctor`` compares a Workspace against the templates of the billet
actually installed and never against a repo path (ADR-0015, D-A4-1).

hatchling's editable install (what ``uv run`` uses in a checkout) puts ``src/`` on the path
and does not expose the force-included directory to ``import billet``: there the resource is
absent and :func:`read_packaged_berth` raises a :class:`ConfigError` naming the fix. The
packaging tests prove the resolver against a built wheel.
"""

from importlib.resources import files
from importlib.resources.abc import Traversable

from billet.contracts import BERTH_COPIED_FILES, BERTH_VERSION_FILE, PackagedBerth
from billet.shared.errors import ConfigError

_PACKAGE = "billet"
#: Where ``pyproject.toml`` force-includes ``templates/workspace/`` inside the package.
BERTH_RESOURCE_DIR = "_berth"


def berth_resource_root() -> Traversable:
    """Return the packaged Berth directory (it may not exist under an editable install)."""
    return files(_PACKAGE).joinpath(BERTH_RESOURCE_DIR)


def read_packaged_berth() -> PackagedBerth:
    """Read the four copied Berth files and the shipped version from the installed package."""
    root = berth_resource_root()
    missing = [name for name in BERTH_COPIED_FILES if not root.joinpath(name).is_file()]
    if missing:
        raise ConfigError(
            f"this billet install carries no packaged Berth ({_PACKAGE}/{BERTH_RESOURCE_DIR}: "
            f"missing {', '.join(missing)}).\n"
            "an editable checkout (`uv run`) does not ship the templates; run doctor from a "
            "built wheel: `uv build`, then `uvx --from dist/<wheel> billet doctor`"
        )
    texts = {name: root.joinpath(name).read_text(encoding="utf-8") for name in BERTH_COPIED_FILES}
    return PackagedBerth(version=_shipped_version(texts[BERTH_VERSION_FILE]), files=texts)


def _shipped_version(text: str) -> int:
    stripped = text.strip()
    if not stripped.isdigit() or int(stripped) < 1:
        raise ConfigError(
            f"the packaged {BERTH_VERSION_FILE} is not a positive integer: {stripped!r}"
        )
    return int(stripped)
