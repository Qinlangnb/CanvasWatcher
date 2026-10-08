from pathlib import Path
import tempfile
import unittest
import httpx
from academic_watcher_auth_broker.config import Config
from academic_watcher_auth_broker.server import create_app


ORIGIN = 'https://192.168.1.10:8080'


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, handler, *, origin=ORIGIN):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Config(frontend_origins=(ORIGIN,), profiles=Path(directory)),
                             transport=httpx.MockTransport(handler))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8765') as client:
                return await client.get('/health', headers={'Origin': origin})

    async def test_callback_ready(self):
        def handler(request):
            self.assertEqual(str(request.url), 'http://127.0.0.1:8000/api/health')
            self.assertEqual(request.method, 'GET')
            return httpx.Response(200, json={'status': 'ok'})
        response = await self.request(handler)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['backend_ready'])
        self.assertEqual(response.json()['protocol_version'], 1)

    async def test_callback_missing(self):
        def handler(request):
            raise httpx.ConnectError('redacted', request=request)
        self.assertFalse((await self.request(handler)).json()['backend_ready'])

    async def test_invalid_callback_payload_or_status(self):
        for value in (httpx.Response(500, json={'status': 'ok'}), httpx.Response(200, json='bad'),
                      httpx.Response(200, text='invalid'), httpx.Response(200, json={'status': 'failed'})):
            self.assertFalse((await self.request(lambda request: value)).json()['backend_ready'])

    async def test_wrong_origin_remains_denied(self):
        def handler(request):
            self.fail('Denied origin must not trigger callback request')
        self.assertEqual((await self.request(handler, origin='https://evil.example')).status_code, 403)


if __name__ == '__main__':
    unittest.main()
