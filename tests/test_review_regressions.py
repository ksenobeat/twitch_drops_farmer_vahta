"""Регрессии адверсариального ревью 0.5.9.3; без аккаунтов и запуска exe."""
import asyncio
import hashlib
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from vahta import service, update, web
from vahta.api import LoginRequired


class LocalService(service.Service):
    async def call(self, fn):
        result = fn(self)
        return await result if inspect.isawaitable(result) else result


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_installer_and_launch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            setup = root / 'updates' / 'Vahta-Setup-99.0.0.exe'
            setup.parent.mkdir()
            body = b'expected installer'
            digest = hashlib.sha256(body).hexdigest()
            s = SimpleNamespace(data_dir=root, latest_version='99.0.0',
                                update_url='https://unused.invalid', update_sha256=digest,
                                update_ready=setup, settings=SimpleNamespace(auto_update=True))
            with patch.object(update, '_tried', set()), patch.object(update, 'fetch', new_callable=AsyncMock) as fetch:
                setup.write_bytes(body)
                self.assertEqual(await update.prepare(s), setup)
                fetch.assert_not_awaited()
                setup.write_bytes(b'corrupted after download')
                fetch.return_value = None
                self.assertIsNone(await update.prepare(s))
                fetch.assert_awaited_once()
                with patch.object(update.subprocess, 'Popen') as launch:
                    self.assertFalse(update.install(setup, digest))
                    launch.assert_not_called()
                    setup.write_bytes(body)
                    self.assertTrue(update.install(setup, digest))
                    launch.assert_called_once()

    async def test_disabled_auto_update_and_manual_install(self):
        with tempfile.TemporaryDirectory() as raw:
            setup = Path(raw) / 'setup.exe'
            setup.write_bytes(b'installer')
            s = LocalService()
            s.settings = SimpleNamespace(auto_update=False)
            s.update_ready = setup
            s.latest_version = '99.0.0'
            s.update_sha256 = hashlib.sha256(setup.read_bytes()).hexdigest()
            with patch.object(update, '_tried', set()), patch.object(web, '_service', s):
                self.assertIsNone(update.take(s))
                self.assertEqual(s.update_ready, setup)
                with patch.object(update.subprocess, 'Popen') as launch:
                    response = await web.update_install()
                    self.assertEqual(response.status_code, 200)
                    launch.assert_called_once()
                    self.assertIsNone(s.update_ready)
                s.update_ready = setup
                s.settings.auto_update = True
                self.assertEqual(update.take(s), setup)

    async def test_local_api_origin_and_native_ipc(self):
        s = LocalService()
        calls = []
        s.quit_ui = lambda: calls.append(True)
        with patch.object(web, '_service', s):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.api),
                                         base_url='http://127.0.0.1:8420') as client:
                for headers in (
                    {'Origin': 'https://foreign.example'},
                    {'Origin': 'null'},
                    {'Origin': 'http://127.0.0.1:9999'},
                    {'Referer': 'https://foreign.example/page'},
                    {'Sec-Fetch-Site': 'cross-site'},
                    {'Host': 'rebound.example:8420'},
                ):
                    response = await client.post('/api/quit', headers=headers)
                    self.assertEqual(response.status_code, 403, headers)
                self.assertEqual(calls, [])
                for headers in ({}, {'Origin': 'http://127.0.0.1:8420', 'Sec-Fetch-Site': 'same-origin'}):
                    response = await client.post('/api/quit', headers=headers)
                    self.assertEqual(response.status_code, 200)
                self.assertEqual(len(calls), 2)
                self.assertEqual((await client.get('/')).status_code, 200)

    async def test_logout_cancels_pending_login(self):
        for stage in ('poll', 'finish'):
            with self.subTest(stage=stage):
                entered = asyncio.Event()
                blocked = asyncio.Event()
                class Twitch:
                    login_name = 'old'
                    def forget(self):
                        self.login_name = None
                    async def poll_device_login(self, device):
                        if stage == 'poll':
                            entered.set()
                            await blocked.wait()
                        return True
                    async def finish_login(self):
                        if stage == 'finish':
                            entered.set()
                            await blocked.wait()
                        self.login_name = 'restored'
                s = LocalService()
                s.tw = Twitch()
                device = {'expires_in': 60, 'interval': 0}
                s.pending_login = device
                task = s.login_job = asyncio.create_task(service.await_login(s, device))
                await asyncio.wait_for(entered.wait(), 2)
                with patch.object(web, '_service', s):
                    await web.twitch_logout()
                self.assertTrue(task.cancelled())
                self.assertIsNone(s.tw.login_name)
                self.assertIsNone(s.pending_login)
                self.assertIsNone(s.login_job)

    async def test_error_notifications_reset_after_recovery(self):
        for failure in (LoginRequired('expired'), httpx.ConnectError('offline')):
            s = service.Service()
            notifications = []
            s.push = lambda *args: notifications.append(args)
            attempts = 0
            async def replan(*args):
                nonlocal attempts
                attempts += 1
                if attempts == 3:
                    return SimpleNamespace(blocks=[])
                raise failure
            s.replan = replan
            async def sleep(_):
                if attempts == 5:
                    raise asyncio.CancelledError
            s.rest = sleep
            with patch.object(update, 'take', return_value=None), patch.object(service.asyncio, 'sleep', sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await service.autopilot(s)
            self.assertEqual(len(notifications), 2)


if __name__ == '__main__':
    unittest.main()
