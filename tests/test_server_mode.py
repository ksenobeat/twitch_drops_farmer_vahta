"""Private server auth, isolation and desktop compatibility; no live accounts."""
import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from vahta import paths, remote, runtime, update, web
from vahta.settings import Settings


class LocalService(web.Service):
    async def call(self, fn):
        result = fn(self)
        return await result if inspect.isawaitable(result) else result


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secret = Path(self.tmp.name) / 'password'
        self.secret.write_text('a-test-only-password-long-enough')
        self.access = remote.Access('https://vahta.example.com', self.secret)
        self.patches = [patch.object(runtime, 'SERVER_MODE', True),
                        patch.object(web, 'remote_access', self.access)]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.api),
                                       base_url='https://vahta.example.com')

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def login(self):
        return await self.client.post('/auth/login', json={'password': self.secret.read_text()},
                                      headers={'Origin': 'https://vahta.example.com'})

    async def test_closed_before_login_and_bad_hosts(self):
        self.assertEqual((await self.client.get('/')).status_code, 303)
        self.assertEqual((await self.client.get('/api/now')).status_code, 401)
        self.assertEqual((await self.client.get('/', headers={'Host': 'evil.test'})).status_code, 403)
        self.assertEqual((await self.client.get('http://vahta.example.com/')).status_code, 403)
        self.assertEqual((await self.client.get('/auth/login')).status_code, 200)
        with patch.object(web, 'remote_access', None):
            self.assertEqual((await self.client.get('/')).status_code, 503)

    async def test_login_cookie_restart_tamper_logout(self):
        response = await self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['set-cookie']
        for flag in ('HttpOnly', 'Secure', 'SameSite=strict', 'Path=/'):
            self.assertIn(flag, cookie)
        token = self.client.cookies.get(remote.COOKIE)
        self.assertTrue(remote.Access('https://vahta.example.com', self.secret).authenticated(token))
        self.assertFalse(self.access.authenticated(token + 'x'))
        self.assertFalse(self.access.authenticated('oops'))
        with patch.object(remote.time, 'time', return_value=0):
            self.assertFalse(self.access.authenticated(token))
        with patch.object(remote.time, 'time', return_value=9e12):
            self.assertFalse(self.access.authenticated(token))
        response = await self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['x-frame-options'], 'DENY')
        self.assertEqual((await self.client.post('/auth/logout', headers={
            'Origin': 'https://vahta.example.com'})).status_code, 200)
        self.assertEqual((await self.client.get('/api/now')).status_code, 401)

    async def test_cross_site_and_missing_origin_rejected(self):
        await self.login()
        for headers in ({}, {'Origin': 'https://evil.test'}, {'Origin': 'null'},
                        {'Origin': 'https://vahta.example.com:444'},
                        {'Origin': 'https://vahta.example.com', 'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual((await self.client.post('/auth/logout', headers=headers)).status_code, 403)
        self.assertEqual((await self.client.post('/auth/logout', headers={
            'Origin': 'https://vahta.example.com:443'})).status_code, 200)

    async def test_wrong_password_body_limits_and_throttle(self):
        headers = {'Origin': 'https://vahta.example.com'}
        response = await self.client.post('/auth/login', content='x' * 1025, headers=headers)
        self.assertEqual(response.status_code, 413)
        for body in ({'password': 'wrong'}, {'password': 1}, [], None):
            response = await self.client.post('/auth/login', json=body, headers=headers)
            self.assertEqual(response.status_code, 401)
        for _ in range(5):
            await self.client.post('/auth/login', json={}, headers=headers)
        self.assertEqual((await self.login()).status_code, 429)

    async def test_desktop_actions_and_updates_blocked(self):
        await self.login()
        headers = {'Origin': 'https://vahta.example.com'}
        for route in ('/api/update/check', '/api/update/install', '/api/data/open'):
            self.assertEqual((await self.client.post(route, headers=headers)).status_code, 409)
        with patch.object(update, 'fetch', new_callable=AsyncMock) as fetch:
            self.assertIsNone(await update.prepare(None))
            fetch.assert_not_awaited()
        with patch.object(update.subprocess, 'Popen') as popen:
            self.assertFalse(update.install(Path('fake.exe')))
            popen.assert_not_called()

    async def test_password_configuration_and_data_path(self):
        for address in ('http://vahta.example.com', 'https://vahta.example.com/path',
                        'https://user:password@vahta.example.com'):
            with self.assertRaises(ValueError):
                remote.Access(address, self.secret)
        self.secret.write_text('short')
        with self.assertRaises(ValueError):
            remote.Access('https://vahta.example.com', self.secret)
        with patch.dict(os.environ, {'VAHTA_DATA_DIR': self.tmp.name}):
            self.assertEqual(paths.data_dir(), Path(self.tmp.name).resolve())

    async def test_health_local_only_and_server_settings(self):
        service = LocalService()
        service.settings = Settings(Path(self.tmp.name) / 'settings.json')
        with patch.object(web, '_service', service):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.api,
                    client=('127.0.0.1', 4567)), base_url='http://127.0.0.1:8420') as local:
                self.assertEqual((await local.get('/healthz')).status_code, 200)
                self.assertEqual((await local.get('/api/now')).status_code, 403)
                service.stopping = True
                self.assertEqual((await local.get('/healthz')).status_code, 503)
            await self.login()
            with patch.object(web.autostart, 'set_enabled') as autostart:
                response = await self.client.post('/api/settings', json={
                    'autostart': True, 'auto_update': False, 'theme': 'dark'},
                    headers={'Origin': 'https://vahta.example.com'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['server_mode'])
                self.assertEqual(service.settings.theme, 'dark')
                self.assertTrue(service.settings.auto_update)  # server ignores desktop setting
                autostart.assert_not_called()


if __name__ == '__main__':
    unittest.main()
