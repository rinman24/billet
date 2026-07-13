"""Tests for RegistryAccess — config.toml parsing, defaults, and validation."""

from pathlib import Path

import pytest

from billet.access.registry.toml_registry_access import RegistryAccess
from billet.shared.errors import ConfigError

_FULL_CONFIG = """
[billet]
subscription_id = "sub-123"
default_host = "devbox"

[hosts.devbox]
resource_group = "gswa-devbox-rg"
vm_name = "gswa-devbox"
location = "westus3"
admin_user = "azureuser"
vm_image = "Canonical:img:latest"
vm_size = "Standard_D4s_v4"
public_ip_sku = "Standard"
os_disk_gb = 64
storage_sku = "Premium_LRS"

[workspaces.gswa-backend]
host = "devbox"
repo_url = "git@github.com:genshift/gswa-backend.git"
repo_dir = "gswa-backend"
host_alias = "gswa-devbox"
container_alias = "gswa-container"
"""

_MINIMAL_HOST = """
[billet]
subscription_id = "s"
[hosts.{key}]
resource_group = "rg"
location = "westus3"
admin_user = "azureuser"
vm_image = "i"
vm_size = "v"
public_ip_sku = "Standard"
os_disk_gb = 64
storage_sku = "Premium_LRS"
"""


# An adopted host: billet manages lifecycle + queries but will never cold-provision it,
# so it carries none of the vm_image / vm_size / … provisioning keys.
_ADOPTED_HOST = """
[billet]
subscription_id = "s"
[hosts.fleet]
resource_group = "rg"
location = "westus3"
admin_user = "azureuser"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_parses_global_config(tmp_path: Path) -> None:
    reg = RegistryAccess(_write(tmp_path, _FULL_CONFIG))
    gc = reg.global_config()
    assert gc.subscription_id == "sub-123"
    assert gc.default_host == "devbox"


def test_parses_explicit_personal_bootstrap_cmd(tmp_path: Path) -> None:
    text = _FULL_CONFIG.replace(
        'default_host = "devbox"',
        'default_host = "devbox"\npersonal_bootstrap_cmd = "bash ~/dotfiles/install.sh"',
    )
    gc = RegistryAccess(_write(tmp_path, text)).global_config()
    assert gc.personal_bootstrap_cmd == "bash ~/dotfiles/install.sh"


def test_personal_bootstrap_cmd_defaults_to_empty_when_absent(tmp_path: Path) -> None:
    gc = RegistryAccess(_write(tmp_path, _FULL_CONFIG)).global_config()
    assert gc.personal_bootstrap_cmd == ""


def test_non_string_personal_bootstrap_cmd_rejected(tmp_path: Path) -> None:
    text = _FULL_CONFIG.replace(
        'default_host = "devbox"', 'default_host = "devbox"\npersonal_bootstrap_cmd = 7'
    )
    reg = RegistryAccess(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="'personal_bootstrap_cmd' must be a string"):
        reg.global_config()


def test_parses_host_with_derived_nsg_and_defaults(tmp_path: Path) -> None:
    host = RegistryAccess(_write(tmp_path, _FULL_CONFIG)).host("devbox")
    assert host.vm_name == "gswa-devbox"
    assert host.nsg_name == "gswa-devboxNSG"
    assert host.ssh_rule_name == "default-allow-ssh"
    assert host.manages_workspaces is True
    assert host.docker_gpg_url.endswith("/gpg")
    assert host.provisioning is not None
    assert host.provisioning.os_disk_gb == 64
    assert host.vm_size == "Standard_D4s_v4"


def test_vm_name_defaults_to_table_key(tmp_path: Path) -> None:
    host = RegistryAccess(_write(tmp_path, _MINIMAL_HOST.format(key="fleet"))).host("fleet")
    assert host.vm_name == "fleet"
    assert host.nsg_name == "fleetNSG"


def test_parses_adopted_host_without_provisioning_keys(tmp_path: Path) -> None:
    host = RegistryAccess(_write(tmp_path, _ADOPTED_HOST)).host("fleet")
    assert host.provisioning is None
    assert host.vm_size is None
    assert host.vm_name == "fleet"


def test_partial_provisioning_keys_raise_at_parse(tmp_path: Path) -> None:
    text = _ADOPTED_HOST + 'vm_size = "Standard_D4s_v5"\n'
    reg = RegistryAccess(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="incomplete provisioning keys.*vm_image"):
        reg.host("fleet")


def test_parses_explicit_manages_workspaces_false(tmp_path: Path) -> None:
    text = _MINIMAL_HOST.format(key="fleet") + "manages_workspaces = false\n"
    host = RegistryAccess(_write(tmp_path, text)).host("fleet")
    assert host.manages_workspaces is False


def test_parses_workspace_with_defaults(tmp_path: Path) -> None:
    ws = RegistryAccess(_write(tmp_path, _FULL_CONFIG)).workspace("gswa-backend")
    assert ws.host == "devbox"
    assert ws.repo_dir == "gswa-backend"
    assert ws.container_ssh_port == 2222
    assert ws.host_alias == "gswa-devbox"
    assert ws.container_alias == "gswa-container"
    assert ws.tmux_session == "main"
    assert ws.host_bootstrap_cmd == ":"
    assert ws.verify_cmd == "make test"


def test_workspace_status_color_defaults_to_none(tmp_path: Path) -> None:
    ws = RegistryAccess(_write(tmp_path, _FULL_CONFIG)).workspace("gswa-backend")
    assert ws.status_color is None


def test_workspace_parses_valid_status_color(tmp_path: Path) -> None:
    text = _FULL_CONFIG + 'status_color = "#C05CE0"\n'
    ws = RegistryAccess(_write(tmp_path, text)).workspace("gswa-backend")
    assert ws.status_color == "#C05CE0"


def test_workspace_invalid_status_color_raises(tmp_path: Path) -> None:
    text = _FULL_CONFIG + 'status_color = "blue"\n'
    reg = RegistryAccess(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="hex color"):
        reg.workspace("gswa-backend")


def test_workspace_non_string_status_color_raises(tmp_path: Path) -> None:
    text = _FULL_CONFIG + "status_color = 123\n"
    reg = RegistryAccess(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="hex color"):
        reg.workspace("gswa-backend")


def test_workspace_rejects_hex_of_wrong_digit_count(tmp_path: Path) -> None:
    # fullmatch pins the length: only exactly 3 or 6 hex digits are a brand color.
    for bad in ("#1234", "#12345", "#1234567"):
        text = _FULL_CONFIG + f'status_color = "{bad}"\n'
        reg = RegistryAccess(_write(tmp_path, text))
        with pytest.raises(ConfigError, match="hex color"):
            reg.workspace("gswa-backend")


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        RegistryAccess(tmp_path / "nope.toml")


def test_invalid_toml_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        RegistryAccess(_write(tmp_path, "this is = = not toml"))


def test_missing_required_host_key_raises(tmp_path: Path) -> None:
    text = '[billet]\nsubscription_id = "s"\n[hosts.devbox]\nlocation = "westus3"\n'
    reg = RegistryAccess(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="missing required key 'resource_group'"):
        reg.host("devbox")


def test_unknown_host_raises(tmp_path: Path) -> None:
    reg = RegistryAccess(_write(tmp_path, _FULL_CONFIG))
    with pytest.raises(ConfigError, match=r"no \[hosts.ghost\]"):
        reg.host("ghost")


def test_bool_typed_int_field_rejected(tmp_path: Path) -> None:
    text = _MINIMAL_HOST.format(key="devbox").replace("os_disk_gb = 64", "os_disk_gb = true")
    reg = RegistryAccess(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="must be an integer"):
        reg.host("devbox")


def test_resolve_host_key_explicit_wins(tmp_path: Path) -> None:
    reg = RegistryAccess(_write(tmp_path, _FULL_CONFIG))
    assert reg.resolve_host_key("other") == "other"


def test_resolve_host_key_uses_default(tmp_path: Path) -> None:
    reg = RegistryAccess(_write(tmp_path, _FULL_CONFIG))
    assert reg.resolve_host_key(None) == "devbox"


def test_resolve_host_key_single_host_when_no_default(tmp_path: Path) -> None:
    reg = RegistryAccess(_write(tmp_path, _MINIMAL_HOST.format(key="solo")))
    assert reg.resolve_host_key(None) == "solo"


def test_resolve_host_key_ambiguous_raises(tmp_path: Path) -> None:
    text = (
        _MINIMAL_HOST.format(key="a")
        + '[hosts.b]\nresource_group = "rg"\nlocation = "l"\nadmin_user = "u"\n'
        + 'vm_image = "i"\nvm_size = "v"\npublic_ip_sku = "Standard"\n'
        + 'os_disk_gb = 64\nstorage_sku = "Premium_LRS"\n'
    )
    reg = RegistryAccess(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="multiple hosts"):
        reg.resolve_host_key(None)
