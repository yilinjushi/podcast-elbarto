import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from podcast_login_portal import COOKIE, create_app


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root/'novnc'
        (self.assets/'core').mkdir(parents=True)
        (self.assets/'core/rfb.js').write_text('export default {};')
        self.save = self.root/'save-request'
        self.status = self.root/'status.json'
        self.origin = {'origin': 'https://private.example'}
        self.token = 'secret-token-that-must-never-be-rendered'
        self.clients = []
        self.client = await self.new_client()

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()

    async def new_client(self, expiry=None):
        app = create_app(self.token, expiry or time.time()+120, self.save, self.status, self.assets, self.origin)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        return client

    async def authenticate(self, client=None):
        client = client or self.client
        response = await client.post('/session', json={'token': self.token}, headers={'Origin': self.origin['origin']})
        self.assertEqual(response.status, 200)
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie['secure'])
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Strict')
        self.assertNotEqual(cookie.value, self.token)
        # TestServer is deliberately HTTP loopback; browsers receive HTTPS tunnel.
        return {'Cookie': COOKIE+'='+cookie.value, 'Origin': self.origin['origin']}

    async def test_landing_no_secret_nonce_and_protected_desktop(self):
        first = await self.client.get('/')
        html = await first.text()
        self.assertNotIn(self.token, html)
        self.assertIn('history.replaceState', html)
        self.assertIn("'nonce-", first.headers['Content-Security-Policy'])
        second = await self.client.get('/')
        self.assertNotEqual(first.headers['Content-Security-Policy'], second.headers['Content-Security-Policy'])
        self.assertEqual((await self.client.get('/desktop')).status, 401)
        headers = await self.authenticate()
        response = await self.client.get('/desktop', headers=headers)
        self.assertEqual(response.status, 200)
        self.assertIn("'/novnc/core/rfb.js'", await response.text())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_invalid_auth_origin_and_body_limit(self):
        for origin in ('https://attacker.example', 'null', ''):
            response = await self.client.post('/session', json={'token': self.token}, headers={'Origin': origin})
            self.assertEqual(response.status, 403)
        response = await self.client.post('/session', json={'token': 'incorrect'}, headers={'Origin': self.origin['origin']})
        self.assertEqual(response.status, 401)
        response = await self.client.post('/session', json={'token': 'x'*9000}, headers={'Origin': self.origin['origin']})
        self.assertEqual(response.status, 413)
        self.origin['origin'] = ''
        response = await self.client.post('/session', json={'token': self.token}, headers={'Origin': 'https://private.example'})
        self.assertEqual(response.status, 403)

    async def test_save_and_fixed_status(self):
        headers = await self.authenticate()
        response = await self.client.post('/save', json={}, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertTrue(self.save.exists())
        self.assertEqual(await (await self.client.get('/status', headers=headers)).json(), {'status':'waiting'})
        for value in ('saved', 'needs_login', 'waiting', 'error', 'secret cookie body'):
            self.status.write_text(json.dumps({'status': value}))
            actual = await (await self.client.get('/status', headers=headers)).json()
            self.assertEqual(actual, {'status': value if value != 'secret cookie body' else 'error'})
        self.save.unlink()
        headers['Origin'] = 'https://attacker.example'
        self.assertEqual((await self.client.post('/save', json={}, headers=headers)).status, 403)
        self.assertFalse(self.save.exists())

    async def test_static_traversal_extensions_and_symlink(self):
        headers = await self.authenticate()
        self.assertEqual((await self.client.get('/novnc/core/rfb.js', headers=headers)).status, 200)
        (self.assets/'secret.json').write_text('{"secret":"must not escape"}')
        for path in ('/novnc/secret.json', '/novnc/%2e%2e/secret.json', '/novnc/core%5c..%5csecret.json', '/novnc/%2Fsecret.json'):
            response = await self.client.get(path, headers=headers)
            self.assertNotEqual(response.status, 200)
            self.assertNotIn('must not escape', await response.text())
        outside = self.root/'outside.js'
        outside.write_text('secret outside')
        link = self.assets/'link.js'
        try:
            link.symlink_to(outside)
        except OSError:
            return  # Windows without symlink privilege; traversal checks still run.
        self.assertEqual((await self.client.get('/novnc/link.js', headers=headers)).status, 404)

    async def test_expiry_rejects_all_requests(self):
        expired = await self.new_client(time.time()-1)
        self.assertEqual((await expired.get('/')).status, 410)
        self.assertEqual((await expired.post('/session', json={'token': self.token}, headers={'Origin':self.origin['origin']})).status, 410)

    async def test_websocket_auth_and_origin_before_tcp(self):
        with patch('podcast_login_portal.asyncio.open_connection') as connect:
            with self.assertRaises(WSServerHandshakeError) as failure:
                await self.client.ws_connect('/websockify', headers={'Origin':self.origin['origin']})
            self.assertEqual(failure.exception.status, 401)
            headers = await self.authenticate()
            headers['Origin'] = 'https://attacker.example'
            with self.assertRaises(WSServerHandshakeError) as failure:
                await self.client.ws_connect('/websockify', headers=headers)
            self.assertEqual(failure.exception.status, 403)
            connect.assert_not_called()

    async def test_websocket_binary_bridge_and_expiry(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(echo, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        real_connect = asyncio.open_connection
        async def connection(host, target_port):
            self.assertEqual((host, target_port), ('127.0.0.1', 5900))
            return await real_connect(host, port)
        client = await self.new_client(time.time()+1)
        headers = await self.authenticate(client)
        with patch('podcast_login_portal.asyncio.open_connection', side_effect=connection):
            ws = await client.ws_connect('/websockify', headers=headers)
            await ws.send_bytes(b'RFB 003.008\n')
            self.assertEqual((await ws.receive(timeout=2)).data, b'RFB 003.008\n')
            await ws.receive(timeout=3)
            self.assertTrue(ws.closed)


if __name__ == '__main__':
    unittest.main()
