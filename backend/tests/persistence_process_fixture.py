"""Disposable acceptance fixture: real app lifespans in independent processes."""
import sys
from time import monotonic

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import CanvasOAuthCredential
from app.auth.provider_sessions import ProviderSession, provider_sessions
from app.auth.store import credential_store
from app.db import AppSetting, CredentialProfile, SessionLocal
from app.main import app
from app.services.ai_settings import ai_probe_secrets, set_ai_credential, set_ai_probe_settings

action = sys.argv[1]
with TestClient(app) as client:
    assert client.get('/api/settings/credential-storage').json()['mode'] == 'server_encrypted'
    if action == 'seed':
        credential_store.set_ntlm('site', 'fixture-user', 'fixture-password')
        credential_store.set_canvas('canvas', 'https://canvas.example', 'fixture-pat')
        credential_store.set_canvas_session('canvas', 'https://canvas.example', {'session': 'fixture-cookie'})
        credential_store.set_canvas_oauth('canvas', CanvasOAuthCredential('https://canvas.example', 'fixture-access', 'fixture-refresh', monotonic() + 600))
        credential_store.select_canvas_mode('canvas', 'pat')
        provider_sessions.set('grades', ProviderSession('gradescope', 'https://www.gradescope.com', {'s': 'fixture-grade'}), 0)
        provider_sessions.set('prairie', ProviderSession('prairielearn', 'https://us.prairielearn.com', {'s': 'fixture-pl'}), 0)
        with SessionLocal() as db:
            for key, kind in [('site', 'ntlm'), ('grades', 'provider_session'), ('prairie', 'provider_session')]:
                db.add(CredentialProfile(credential_id=key, auth_type=kind, probe_url='https://example.test', state='ACTIVE'))
            set_ai_probe_settings(db, '', 'model', 'deepseek')
            set_ai_credential(db, 'fixture-ai', 'deepseek')
            row = db.get(AppSetting, 'ai_probe')
            ai_probe_secrets.mark_verified((row.value_json['base_url'], 'model', row.value_json['revision'], ai_probe_secrets.generation))
            db.commit()
    elif action == 'restore':
        assert credential_store.get_ntlm('site').password == 'fixture-password'
        assert credential_store.get_canvas('canvas').token == 'fixture-pat'
        assert credential_store.canvas_slot('canvas', 'oauth').refresh_token == 'fixture-refresh'
        assert credential_store.canvas_slot('canvas', 'browser_session').cookies == {'session': 'fixture-cookie'}
        assert provider_sessions.get('grades').cookies == {'s': 'fixture-grade'}
        assert provider_sessions.get('prairie').cookies == {'s': 'fixture-pl'}
        assert client.get('/api/settings/ai').json()['chat_ready']
        canvas = client.get('/api/auth/canvas/status').json()
        assert canvas['credential_storage'] == 'server_encrypted'
        assert not canvas['verified'] and canvas['credentials']['pat']['state'] == 'RESTORED'
        with SessionLocal() as db:
            assert all(profile.state == 'ACTIVE' for profile in db.scalars(select(CredentialProfile)))
        credential_store.block_canvas('canvas')
        credential_store.clear('site')
        provider_sessions.remove('grades')
        provider_sessions.remove('prairie')
        ai_probe_secrets.clear()
    elif action == 'removed':
        assert credential_store.get_canvas('canvas') is None
        assert credential_store.canvas_blocked('canvas')
        assert credential_store.get_ntlm('site') is None
        assert provider_sessions.get('grades') is None
        assert provider_sessions.get('prairie') is None
        assert not client.get('/api/settings/ai').json()['chat_ready']
        assert not client.get('/api/settings/ai').json()['credential_loaded']
    else:
        raise AssertionError('Unknown fixture action')
print('PROCESS_ACCEPTANCE_PASS')
