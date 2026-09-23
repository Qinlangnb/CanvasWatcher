from types import SimpleNamespace

import httpx
import pytest

from app.services.source_health import classify_canvas_errors, source_health_view
from app.sources.canvas import CanvasAdapter
from app.sources.canvas_client import CanvasClient


@pytest.mark.parametrize('errors,successful,state', [
    ({}, True, 'HEALTHY'),
    ({'file': 'permission_denied', 'page': 'resource_not_found'}, True, 'DEGRADED'),
    ({'page': 'invalid_response'}, True, 'DEGRADED'),
    ({'assignment': 'permission_denied'}, True, 'ERROR'),
    ({'page': 'invalid_response'}, False, 'ERROR'),
    ({'file': 'auth_required'}, True, 'AUTH_REQUIRED'),
])
def test_resource_health(errors, successful, state):
    assert classify_canvas_errors(errors, successful) == state


def test_legacy_status_is_unknown_not_invented():
    source = SimpleNamespace(metadata_json={}, state='DEGRADED',
        last_error='file:permission_denied; page:invalid_response')
    view = source_health_view(source)
    assert 'remains usable' in view['health_summary']
    assert len(view['health_diagnostics']) == 2
    assert all(row['historical'] and row['http_status'] is None for row in view['health_diagnostics'])


def test_legacy_page_file_diagnostic_keeps_file_id():
    source = SimpleNamespace(metadata_json={}, state='DEGRADED',
        last_error='page_file:123:permission_denied')
    assert source_health_view(source)['health_diagnostics'] == [{
        'resource': 'page_file:123', 'code': 'permission_denied',
        'http_status': None, 'historical': True,
    }]


def test_current_diagnostics_preserve_http_status():
    details = [{'resource': 'page', 'code': 'resource_not_found', 'http_status': 404}]
    source = SimpleNamespace(metadata_json={'resource_errors': details}, state='DEGRADED', last_error=None)
    assert source_health_view(source)['health_diagnostics'] == details


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,code', [(403, '{}', 'permission_denied'),
    (404, '{}', 'resource_not_found'), (200, 'not-json', 'invalid_response'),
    (200, '{}', 'invalid_response')])
async def test_optional_resource_diagnostics_keep_http_evidence(status, body, code):
    client = CanvasClient('https://canvas.example', 'fake', transport=httpx.MockTransport(
        lambda request: httpx.Response(status, content=body, request=request)))
    adapter = CanvasAdapter('https://canvas.example', 'fake', client=client)
    assert await adapter._collection('page', '/api/v1/courses/1/pages') == []
    assert adapter.error_details['page'] == {'resource': 'page', 'code': code, 'http_status': status}
