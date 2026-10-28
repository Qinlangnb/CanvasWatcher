import os
import stat

from aw_gateway import admin
from aw_gateway.store import IdentityStore

LEGACY = "$6$saltsalt$" + "a" * 86


def run_seed(tmp_path, monkeypatch, capsys, htpasswd_text):
    monkeypatch.setenv("AW_GATEWAY_DATABASE", str(tmp_path / "identity.db"))
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text(htpasswd_text)
    code_file = tmp_path / "owner.code"
    result = admin.main([
        "seed-owner", "--username", "Owner", "--email", "owner@example.com",
        "--htpasswd", str(htpasswd), "--code-file", str(code_file),
    ])
    return result, code_file, capsys.readouterr()


def test_seed_owner_writes_code_only_to_private_file(tmp_path, monkeypatch, capsys):
    result, code_file, output = run_seed(tmp_path, monkeypatch, capsys, f"owner:{LEGACY}\n")
    assert result == 0
    code = code_file.read_text().strip()
    assert len(code) >= 32
    assert code not in output.out + output.err
    assert LEGACY not in output.out + output.err
    if os.name == "posix":
        assert stat.S_IMODE(code_file.stat().st_mode) == 0o600
    user = IdentityStore(tmp_path / "identity.db").find_user("Owner")
    assert user["role"] == "admin" and user["slot"] == 1 and user["must_change_password"] == 1


def test_seed_owner_refuses_second_owner_and_keeps_code_file(tmp_path, monkeypatch, capsys):
    assert run_seed(tmp_path, monkeypatch, capsys, f"owner:{LEGACY}\n")[0] == 0
    (tmp_path / "owner.code").rename(tmp_path / "first.code")
    result, code_file, output = run_seed(tmp_path, monkeypatch, capsys, f"owner:{LEGACY}\n")
    assert result == 1
    assert "Owner already exists" in output.err
    assert not code_file.exists()


def test_seed_owner_rejects_non_sha512_or_multiple_entries(tmp_path, monkeypatch, capsys):
    assert run_seed(tmp_path, monkeypatch, capsys, "owner:$apr1$x$y\n")[0] == 1
    assert run_seed(tmp_path, monkeypatch, capsys, f"a:{LEGACY}\nb:{LEGACY}\n")[0] == 1


def test_gateway_key_mode_relaxed_only_for_systemd_credentials(tmp_path, monkeypatch):
    import pytest

    from aw_gateway.config import load_private_key

    key = tmp_path / "csrf.key"
    key.write_bytes(b"k" * 48)
    key.chmod(0o440)
    if os.name == "posix":
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        with pytest.raises(RuntimeError):
            load_private_key(key)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert load_private_key(key) == b"k" * 48
