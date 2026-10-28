"""Only disposable fixture secrets; no live keys or data are test inputs."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth.models import CanvasOAuthCredential
from app.auth.provider_sessions import ProviderSession, ProviderSessions
from app.auth.store import CredentialStore
from app.auth.vault import CredentialVault, VaultError
from app.db import AppSetting, Base
from app.services.ai_settings import (
    AIProbeSecretStore,
    ai_probe_secrets,
    get_ai_probe_settings,
    set_ai_credential,
    set_ai_probe_settings,
)


@pytest.fixture
def paths(tmp_path):
    key = tmp_path / 'master.key'
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    return tmp_path / 'credentials/vault.enc', key


def reopen(paths):
    return CredentialVault(*paths)


def test_encrypted_all_stores_restore_and_namespaces_survive(paths):
    vault = reopen(paths)
    sources, providers, ai = CredentialStore(), ProviderSessions(), AIProbeSecretStore()
    for store in (sources, providers, ai):
        store.configure_persistence(vault)
    sources.set_ntlm('site', 'fixture-user', 'fixture-password')
    sources.set_canvas('canvas', 'https://canvas.example', 'fixture-pat')
    sources.set_canvas_session('canvas', 'https://canvas.example', {'session': 'fixture-cookie'})
    sources.set_canvas_oauth('canvas', CanvasOAuthCredential('https://canvas.example', 'fixture-access', 'fixture-refresh', monotonic() + 60))
    sources.select_canvas_mode('canvas', 'pat')
    providers.set('grades', ProviderSession('gradescope', 'https://www.gradescope.com', {'session': 'fixture-grade'}), 0)
    ai.set('fixture-ai', 'https://api.deepseek.com/v1')
    ai.mark_verified(('https://api.deepseek.com/v1', 'model', 'revision', ai.generation))
    data = paths[0].read_bytes()
    for secret in (b'fixture-user', b'fixture-password', b'fixture-pat', b'fixture-cookie', b'fixture-refresh', b'fixture-grade', b'fixture-ai'):
        assert secret not in data
    new_sources, new_providers, new_ai = CredentialStore(), ProviderSessions(), AIProbeSecretStore()
    restored = reopen(paths)
    for store in (new_sources, new_providers, new_ai):
        store.configure_persistence(restored)
    assert new_sources.get_ntlm('site').password == 'fixture-password'
    assert new_sources.get_canvas('canvas').token == 'fixture-pat'
    assert new_sources.canvas_slot('canvas', 'oauth').refresh_token == 'fixture-refresh'
    assert new_sources.canvas_slot('canvas', 'browser_session').cookies == {'session': 'fixture-cookie'}
    assert new_providers.get('grades').cookies == {'session': 'fixture-grade'}
    assert new_ai.get('https://api.deepseek.com/v1') == 'fixture-ai'
    assert new_ai.verified == ('https://api.deepseek.com/v1', 'model', 'revision', new_ai.generation)
    assert not new_sources.recently_verified('canvas', 'pat')
    assert 0 < new_sources.generation('canvas', 'pat') < 2**53
    assert new_sources.generation('canvas', 'pat') != sources.generation('canvas', 'pat')


def test_removal_clear_and_stale_generations_never_resurrect(paths):
    vault = reopen(paths)
    sources = CredentialStore()
    sources.configure_persistence(vault)
    sources.set_canvas('canvas', 'https://canvas.example', 'fixture-pat')
    before = paths[0].read_bytes()
    assert not sources.remove_canvas_slot('canvas', 'pat', 123)
    assert paths[0].read_bytes() == before
    sources.block_canvas('canvas')
    sources.set_ntlm('site', 'fixture-user', 'fixture-password')
    sources.clear('site')
    new = CredentialStore()
    new.configure_persistence(reopen(paths))
    assert new.get_canvas('canvas') is None and new.canvas_blocked('canvas')
    assert new.get_ntlm('site') is None
    providers = ProviderSessions()
    providers.configure_persistence(reopen(paths))
    providers.set('grades', ProviderSession('gradescope', 'https://www.gradescope.com', {'s': 'fixture'}), 0)
    providers.remove('grades')
    new_provider = ProviderSessions()
    new_provider.configure_persistence(reopen(paths))
    assert new_provider.get('grades') is None


def test_oauth_expiry_includes_server_downtime(paths, monkeypatch):
    import app.auth.store as module
    monkeypatch.setattr(module.time, 'time', lambda: 1000)
    monkeypatch.setattr(module, 'monotonic', lambda: 200)
    store = CredentialStore()
    store.configure_persistence(reopen(paths))
    store.set_canvas_oauth('canvas', CanvasOAuthCredential('https://canvas.example', 'a', 'r', 220))
    monkeypatch.setattr(module.time, 'time', lambda: 1030)
    monkeypatch.setattr(module, 'monotonic', lambda: 500)
    restored = CredentialStore()
    restored.configure_persistence(reopen(paths))
    assert restored.canvas_slot('canvas', 'oauth').expires_clock == pytest.approx(490)


def test_ai_ready_survives_restart_but_changes_invalidate(paths):
    vault = reopen(paths)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    try:
        ai_probe_secrets.configure_persistence(vault)
        with Session(engine) as db:
            set_ai_probe_settings(db, '', 'model', 'deepseek')
            set_ai_credential(db, 'fixture-ai', 'deepseek')
            row = db.get(AppSetting, 'ai_probe')
            identity = (row.value_json['base_url'], 'model', row.value_json['revision'], ai_probe_secrets.generation)
            ai_probe_secrets.mark_verified(identity)
            assert get_ai_probe_settings(db)['chat_ready']
            ai_probe_secrets.configure_persistence(reopen(paths))
            assert get_ai_probe_settings(db)['chat_ready']
            set_ai_probe_settings(db, '', 'changed-model', 'deepseek')
            ai_probe_secrets.configure_persistence(reopen(paths))
            assert not get_ai_probe_settings(db)['chat_ready']
            set_ai_probe_settings(db, '', 'model', 'qwen')
            ai_probe_secrets.configure_persistence(reopen(paths))
            assert not get_ai_probe_settings(db)['credential_loaded']
    finally:
        ai_probe_secrets.configure_persistence(None)
        engine.dispose()


def test_write_failure_retains_live_credentials(paths, monkeypatch):
    vault = reopen(paths)
    store = CredentialStore()
    store.configure_persistence(vault)
    store.set_ntlm('site', 'fixture-user', 'old-password')
    old = store.get_ntlm('site')
    def fail(*args):
        raise VaultError()
    monkeypatch.setattr(vault, 'write', fail)
    with pytest.raises(VaultError):
        store.set_ntlm('site', 'fixture-user', 'replacement')
    assert store.get_ntlm('site') is old and old.password == 'old-password'
    with pytest.raises(VaultError):
        store.clear('site')
    assert store.get_ntlm('site') is old


def test_corruption_wrong_missing_key_fail_without_overwrite(paths):
    vault = reopen(paths)
    vault.write('ai_credential', {'key': 'fixture-secret'})
    original = paths[0].read_bytes()
    wrong = paths[1].with_name('wrong.key')
    wrong.write_bytes(Fernet.generate_key())
    wrong.chmod(0o600)
    with pytest.raises(VaultError) as error:
        CredentialVault(paths[0], wrong)
    assert 'fixture-secret' not in str(error.value)
    assert paths[0].read_bytes() == original
    with pytest.raises(VaultError):
        CredentialVault(paths[0], paths[1].with_name('missing'))
    damaged = bytearray(original)
    damaged[-10] ^= 1
    paths[0].write_bytes(damaged)
    with pytest.raises(VaultError):
        reopen(paths)
    assert paths[0].read_bytes() == damaged


def test_concurrent_namespaces_are_not_lost(paths):
    vault = reopen(paths)
    sources, providers, ai = CredentialStore(), ProviderSessions(), AIProbeSecretStore()
    for store in (sources, providers, ai):
        store.configure_persistence(vault)
    with ThreadPoolExecutor() as pool:
        futures = [pool.submit(sources.set_ntlm, 'site', 'user', 'password'),
                   pool.submit(providers.set, 'grades', ProviderSession('gradescope', 'https://www.gradescope.com', {'s': 'cookie'}), 0),
                   pool.submit(ai.set, 'fixture-ai', 'https://api.deepseek.com/v1')]
        for future in futures:
            future.result()
    restored = reopen(paths)
    assert all(restored.read(name) for name in ('source_credentials', 'provider_sessions', 'ai_credential'))


@pytest.mark.skipif(os.name != 'posix', reason='Linux permission boundary')
def test_private_permissions_required(paths):
    paths[1].chmod(0o644)
    with pytest.raises(VaultError):
        reopen(paths)


@pytest.mark.skipif(os.name != 'posix', reason='Linux permission boundary')
def test_systemd_credential_key_accepted_only_inside_credentials_directory(paths, monkeypatch):
    paths[1].chmod(0o440)
    with pytest.raises(VaultError):
        reopen(paths)
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(paths[1].parent))
    assert reopen(paths).read('source_credentials') == {}
    paths[1].chmod(0o444)
    with pytest.raises(VaultError):
        reopen(paths)


@pytest.mark.skipif(os.name != 'posix', reason='Linux root-pinned container ownership')
def test_nonmatching_key_owner_fails_then_root_owned_key_restores(paths):
    assert os.geteuid() == 0
    os.chown(paths[1], 1000, 1000)
    with pytest.raises(VaultError):
        reopen(paths)
    os.chown(paths[1], 0, 0)
    assert reopen(paths).read('source_credentials') == {}


def test_malformed_authenticated_payload_fails_closed(paths):
    vault = reopen(paths)
    vault.write('source_credentials', {'entries': {'site': {'password': 'fixture-secret'}}})
    original = paths[0].read_bytes()
    with pytest.raises(VaultError):
        CredentialStore().configure_persistence(reopen(paths))
    assert paths[0].read_bytes() == original


def test_memory_only_remains_default():
    store = CredentialStore()
    store.set_ntlm('site', 'user', 'password')
    assert not store.persistence_enabled
    store.clear_all()
    assert store.get_ntlm('site') is None


def test_ciphertext_limit_is_enforced_before_replace(paths, monkeypatch):
    import app.auth.vault as module
    vault = reopen(paths)
    vault.write('ai_credential', {'key': 'old'})
    original = paths[0].read_bytes()
    monkeypatch.setattr(module, 'MAX_BYTES', 300)
    with pytest.raises(VaultError):
        vault.write('ai_credential', {'key': 'x' * 180})
    assert paths[0].read_bytes() == original


@pytest.mark.skipif(os.name != 'posix', reason='Linux directory-fsync boundary')
def test_post_replace_fsync_error_prevents_stale_overwrite(paths, monkeypatch):
    import app.auth.vault as module
    vault = reopen(paths)
    calls = 0
    real_fsync = module.os.fsync
    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('fixture fsync failure')
        return real_fsync(fd)
    monkeypatch.setattr(module.os, 'fsync', fsync)
    with pytest.raises(VaultError):
        vault.write('ai_credential', {'key': 'new'})
    with pytest.raises(VaultError):
        vault.write('ai_credential', {'key': 'old'})
    assert reopen(paths).read('ai_credential')['key'] == 'new'


def test_real_process_restarts_restore_then_keep_removals(paths, tmp_path):
    backend = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG'}}
    env.update(DATABASE_URL=f'sqlite:///{(tmp_path / "fixture.db").as_posix()}',
               DOWNLOAD_ROOT=str(tmp_path / 'downloads'),
               COURSES_CONFIG=str(tmp_path / 'no-courses.yaml'),
               FILE_RULES_CONFIG=str(backend.parent / 'config/file_rules.yaml'),
               SCHEDULER_ENABLED='false', CANVAS_BASE_URL='https://canvas.example',
               CREDENTIAL_PERSISTENCE_ENABLED='true', CREDENTIAL_VAULT_PATH=str(paths[0]),
               CREDENTIAL_MASTER_KEY_FILE=str(paths[1]), PYTHONPATH=str(backend))
    for action in ('seed', 'restore', 'removed'):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('persistence_process_fixture.py')), action],
                                env=env, cwd=backend, capture_output=True, text=True, timeout=30)
        # On failure show sanitized status only, never process output containing fixture requests.
        assert result.returncode == 0, f'Fixture process {action} failed with exit {result.returncode}'
        assert 'PROCESS_ACCEPTANCE_PASS' in result.stdout
    database_before = (tmp_path / 'fixture.db').read_bytes()
    vault_before = paths[0].read_bytes()
    paths[1].write_bytes(Fernet.generate_key())
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('persistence_process_fixture.py')), 'removed'],
                            env=env, cwd=backend, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'VaultError' in result.stderr
    assert 'fixture-password' not in result.stderr and 'fixture-ai' not in result.stderr
    assert (tmp_path / 'fixture.db').read_bytes() == database_before
    assert paths[0].read_bytes() == vault_before


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() != 0, reason='root-only provisioning')
def test_key_provisioning_is_idempotent_and_never_replaces_missing_key(tmp_path, monkeypatch):
    import importlib.util
    source = Path(__file__).resolve().parents[2] / 'ops/prepare_credential_key.py'
    spec = importlib.util.spec_from_file_location('fixture_key_setup', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'KEY', tmp_path / 'private-credentials/master.key')
    monkeypatch.setattr(module, 'VAULT', tmp_path / 'vault.enc')
    module.main()
    original = module.KEY.read_bytes()
    Fernet(original)
    assert module.KEY.stat().st_mode & 0o777 == 0o600
    module.main()
    assert module.KEY.read_bytes() == original
    module.VAULT.write_bytes(b'fixture-existing-vault')
    module.KEY.unlink()
    with pytest.raises(RuntimeError, match='original key'):
        module.main()
    assert not module.KEY.exists() and module.VAULT.read_bytes() == b'fixture-existing-vault'
