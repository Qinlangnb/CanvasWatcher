import json
import tomllib
from pathlib import Path

from app.api.routes import health
from app.main import app


def test_public_release_metadata_and_health_agree():
    root = Path(__file__).resolve().parents[2]
    versions = [tomllib.loads((root / name).read_text(encoding='utf-8'))['project']['version']
                for name in ('backend/pyproject.toml', 'auth-broker/pyproject.toml')]
    versions += [json.loads((root / name).read_text(encoding='utf-8'))['version']
                 for name in ('frontend/package.json', 'frontend/package-lock.json')]
    assert health()['version'] == app.version
    assert all(value == app.version for value in versions)
