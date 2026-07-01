"""Tests for FileSshConfigAccess — Include-file write + idempotent single Include line."""

from pathlib import Path
import stat

from billet.access.sshconfig.file_ssh_config_access import FileSshConfigAccess


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_conf_creates_config_d_and_writes_content(tmp_path: Path) -> None:
    access = FileSshConfigAccess(ssh_dir=tmp_path)
    returned = access.write_conf("# billet conf\n")
    conf = tmp_path / "config.d" / "billet.conf"
    assert conf.read_text() == "# billet conf\n"
    assert returned == str(conf)
    assert _mode(conf) == 0o600
    assert _mode(tmp_path / "config.d") == 0o700


def test_write_conf_overwrites_wholesale(tmp_path: Path) -> None:
    access = FileSshConfigAccess(ssh_dir=tmp_path)
    access.write_conf("old\n")
    access.write_conf("new\n")
    assert (tmp_path / "config.d" / "billet.conf").read_text() == "new\n"


def test_ensure_include_creates_config_with_include_line(tmp_path: Path) -> None:
    access = FileSshConfigAccess(ssh_dir=tmp_path)
    access.ensure_include()
    config = tmp_path / "config"
    assert config.read_text() == "Include config.d/billet.conf\n"
    assert _mode(config) == 0o600


def test_ensure_include_prepends_and_preserves_existing(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text("Host myhost\n    HostName 1.2.3.4\n")
    FileSshConfigAccess(ssh_dir=tmp_path).ensure_include()
    text = config.read_text()
    assert text.startswith("Include config.d/billet.conf\n")
    assert "Host myhost" in text


def test_ensure_include_is_idempotent(tmp_path: Path) -> None:
    access = FileSshConfigAccess(ssh_dir=tmp_path)
    access.ensure_include()
    access.ensure_include()
    text = (tmp_path / "config").read_text()
    assert text.count("Include config.d/billet.conf") == 1
