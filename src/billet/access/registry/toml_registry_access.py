"""RegistryAccess — the sole reader of ``config.toml``, parsed into typed specs.

Reads operator intent (``[billet]`` / ``[hosts.*]`` / ``[workspaces.*]``) and validates it
into :class:`GlobalConfig` / :class:`HostSpec` / :class:`WorkspaceSpec`, raising
:class:`ConfigError` with an operator-actionable message on any problem.
"""

from pathlib import Path
import tomllib
from typing import Any, cast

from billet.contracts import GlobalConfig, HostSpec, WorkspaceSpec
from billet.shared.errors import ConfigError
from billet.shared.paths import resolve_config_path

_DEFAULT_DOCKER_GPG_URL = "https://download.docker.com/linux/ubuntu/gpg"
_DEFAULT_DOCKER_APT_URL = "https://download.docker.com/linux/ubuntu"
_DEFAULT_SSH_RULE_NAME = "default-allow-ssh"
_DEFAULT_CONTAINER_SSH_PORT = 2222


class RegistryAccess:
    """Reads operator intent from ``config.toml`` and parses it into typed specs."""

    def __init__(self, config_path: Path) -> None:
        self._path = config_path
        self._data = self._load(config_path)

    @classmethod
    def resolve(cls, explicit: Path | str | None = None) -> "RegistryAccess":
        """Construct from the resolved config path (``--config`` / ``$BILLET_CONFIG`` / XDG)."""
        return cls(resolve_config_path(explicit))

    # --- file loading --------------------------------------------------------------

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    # --- typed scalar extraction ---------------------------------------------------

    @staticmethod
    def _as_table(value: Any, ctx: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ConfigError(f"{ctx}: expected a table")
        return cast("dict[str, Any]", value)

    def _table(self, name: str) -> dict[str, Any]:
        if name not in self._data:
            return {}
        return self._as_table(self._data[name], f"[{name}]")

    def _str(self, table: dict[str, Any], key: str, ctx: str, default: str | None = None) -> str:
        if key not in table:
            if default is not None:
                return default
            raise ConfigError(f"{ctx}: missing required key '{key}'")
        value = table[key]
        if not isinstance(value, str):
            raise ConfigError(f"{ctx}: key '{key}' must be a string")
        return value

    def _int(self, table: dict[str, Any], key: str, ctx: str, default: int | None = None) -> int:
        if key not in table:
            if default is not None:
                return default
            raise ConfigError(f"{ctx}: missing required key '{key}'")
        value = table[key]
        # bool is a subclass of int — reject it explicitly so `true` is not read as 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{ctx}: key '{key}' must be an integer")
        return value

    def _bool(self, table: dict[str, Any], key: str, ctx: str, *, default: bool) -> bool:
        if key not in table:
            return default
        value = table[key]
        if not isinstance(value, bool):
            raise ConfigError(f"{ctx}: key '{key}' must be a boolean")
        return value

    # --- public API ----------------------------------------------------------------

    def global_config(self) -> GlobalConfig:
        """Parse the ``[billet]`` table."""
        table = self._table("billet")
        ctx = "[billet]"
        subscription_id = self._str(table, "subscription_id", ctx)
        default_host = table.get("default_host")
        if default_host is not None and not isinstance(default_host, str):
            raise ConfigError(f"{ctx}: key 'default_host' must be a string")
        return GlobalConfig(subscription_id=subscription_id, default_host=default_host)

    def host_keys(self) -> list[str]:
        """Return the sorted logical keys of every ``[hosts.*]`` table."""
        return sorted(self._table("hosts").keys())

    def host(self, key: str) -> HostSpec:
        """Parse the ``[hosts.<key>]`` table into a :class:`HostSpec`."""
        hosts = self._table("hosts")
        if key not in hosts:
            raise ConfigError(f"no [hosts.{key}] table in {self._path}")
        ctx = f"[hosts.{key}]"
        table = self._as_table(hosts[key], ctx)
        vm_name = self._str(table, "vm_name", ctx, default=key)
        return HostSpec(
            key=key,
            resource_group=self._str(table, "resource_group", ctx),
            vm_name=vm_name,
            location=self._str(table, "location", ctx),
            admin_user=self._str(table, "admin_user", ctx),
            vm_image=self._str(table, "vm_image", ctx),
            vm_size=self._str(table, "vm_size", ctx),
            public_ip_sku=self._str(table, "public_ip_sku", ctx),
            os_disk_gb=self._int(table, "os_disk_gb", ctx),
            storage_sku=self._str(table, "storage_sku", ctx),
            nsg_name=self._str(table, "nsg_name", ctx, default=f"{vm_name}NSG"),
            ssh_rule_name=self._str(table, "ssh_rule_name", ctx, default=_DEFAULT_SSH_RULE_NAME),
            manages_workspaces=self._bool(table, "manages_workspaces", ctx, default=True),
            docker_gpg_url=self._str(table, "docker_gpg_url", ctx, default=_DEFAULT_DOCKER_GPG_URL),
            docker_apt_url=self._str(table, "docker_apt_url", ctx, default=_DEFAULT_DOCKER_APT_URL),
        )

    def workspace_keys(self) -> list[str]:
        """Return the sorted logical keys of every ``[workspaces.*]`` table."""
        return sorted(self._table("workspaces").keys())

    def workspace(self, key: str) -> WorkspaceSpec:
        """Parse the ``[workspaces.<key>]`` table into a :class:`WorkspaceSpec`."""
        workspaces = self._table("workspaces")
        if key not in workspaces:
            raise ConfigError(f"no [workspaces.{key}] table in {self._path}")
        ctx = f"[workspaces.{key}]"
        table = self._as_table(workspaces[key], ctx)
        return WorkspaceSpec(
            key=key,
            host=self._str(table, "host", ctx),
            repo_url=self._str(table, "repo_url", ctx),
            repo_dir=self._str(table, "repo_dir", ctx),
            container_ssh_port=self._int(
                table, "container_ssh_port", ctx, default=_DEFAULT_CONTAINER_SSH_PORT
            ),
            host_alias=self._str(table, "host_alias", ctx),
            container_alias=self._str(table, "container_alias", ctx),
            tmux_session=self._str(table, "tmux_session", ctx, default="main"),
            agent_teams_flag=self._str(table, "agent_teams_flag", ctx, default=""),
            host_bootstrap_cmd=self._str(table, "host_bootstrap_cmd", ctx, default=":"),
            verify_cmd=self._str(table, "verify_cmd", ctx, default="make test"),
        )

    def resolve_host_key(self, requested: str | None) -> str:
        """Resolve which host a command targets.

        Order: the explicit ``requested`` key, then ``[billet].default_host``, then the sole
        host if exactly one is defined. Raises :class:`ConfigError` if ambiguous or empty.
        """
        if requested is not None:
            return requested
        default_host = self.global_config().default_host
        if default_host is not None:
            return default_host
        keys = self.host_keys()
        if len(keys) == 1:
            return keys[0]
        if not keys:
            raise ConfigError(f"no [hosts.*] defined in {self._path}")
        joined = ", ".join(keys)
        raise ConfigError(
            f"multiple hosts defined ({joined}); pass --host or set [billet].default_host"
        )
