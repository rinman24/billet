"""Filesystem path resolution for billet.

billet is stateless: the only operator-authored state is ``config.toml``. These helpers
locate it without ever writing it.
"""

import os
from pathlib import Path

_CONFIG_ENV_VAR = "BILLET_CONFIG"
_XDG_CONFIG_HOME = "XDG_CONFIG_HOME"


def default_config_path() -> Path:
    """Return the default config path, honoring ``$XDG_CONFIG_HOME``.

    Returns
    -------
    Path
        ``$XDG_CONFIG_HOME/billet/config.toml`` if set, else
        ``~/.config/billet/config.toml``.
    """
    xdg = os.environ.get(_XDG_CONFIG_HOME)
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "billet" / "config.toml"


def resolve_config_path(explicit: Path | str | None) -> Path:
    """Resolve the config path from the highest-priority source available.

    Order: the ``explicit`` argument (``--config``), then ``$BILLET_CONFIG``, then the
    XDG default.
    """
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(_CONFIG_ENV_VAR)
    if env:
        return Path(env)
    return default_config_path()
