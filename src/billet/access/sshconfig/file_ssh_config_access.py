"""FileSshConfigAccess — write the tool-owned ``billet.conf`` + one ``Include`` line.

Implements ``SshConfigAccess``. billet fully owns ``~/.ssh/config.d/billet.conf`` (it
overwrites it wholesale on each render) and adds — exactly once, idempotently — an
``Include`` line near the top of ``~/.ssh/config`` so the operator's hand-maintained config
is otherwise left untouched. Evolves the lifted ``install-ssh-config.sh`` from an in-place
marker-delimited block to the cleaner Include model (ADR-0002).
"""

from pathlib import Path

_CONF_REL = "config.d/billet.conf"
_INCLUDE_LINE = f"Include {_CONF_REL}"
_SSH_DIR_MODE = 0o700
_SSH_FILE_MODE = 0o600


class FileSshConfigAccess:
    """An ``SshConfigAccess`` backed by the operator's ``~/.ssh`` directory."""

    def __init__(self, ssh_dir: Path | None = None) -> None:
        self._ssh_dir = ssh_dir if ssh_dir is not None else Path.home() / ".ssh"

    def write_conf(self, content: str) -> str:
        """Write ``config.d/billet.conf`` (0600), creating ``~/.ssh/config.d`` (0700)."""
        conf_path = self._ssh_dir / _CONF_REL
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        self._ssh_dir.chmod(_SSH_DIR_MODE)
        conf_path.parent.chmod(_SSH_DIR_MODE)
        conf_path.write_text(content)
        conf_path.chmod(_SSH_FILE_MODE)
        return str(conf_path)

    def ensure_include(self) -> None:
        """Ensure ``~/.ssh/config`` has exactly one ``Include`` line for ``billet.conf``."""
        self._ssh_dir.mkdir(parents=True, exist_ok=True)
        self._ssh_dir.chmod(_SSH_DIR_MODE)
        config_path = self._ssh_dir / "config"
        existing = config_path.read_text() if config_path.is_file() else ""
        if any(line.strip() == _INCLUDE_LINE for line in existing.splitlines()):
            return
        # Prepend so billet's host/container entries win first-match-wins resolution.
        updated = f"{_INCLUDE_LINE}\n\n{existing}" if existing else f"{_INCLUDE_LINE}\n"
        config_path.write_text(updated)
        config_path.chmod(_SSH_FILE_MODE)
