"""Telegram error delivery and autopilot recovery without production accounts."""
import asyncio
import errno
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vahta import service, telegram, update
from vahta.api import LoginRequired, TwitchError


class ErrorAlertsTests(unittest.IsolatedAsyncioTestCase):
    def test_full_database_is_reported_as_local_disk_failure(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE payload (data BLOB)')
            pages = db.execute('PRAGMA page_count').fetchone()[0]
            db.execute(f'PRAGMA max_page_count={pages}')
            try:
                db.execute('INSERT INTO payload VALUES (zeroblob(100000))')
            except sqlite3.OperationalError as exc:
                self.assertEqual(exc.sqlite_errorcode, sqlite3.SQLITE_FULL)
                self.assertEqual(service._note(exc), 'на диске ВАХТЫ закончилось место')
            else:
                self.fail('Expected a real SQLITE_FULL failure')
        self.assertEqual(service._note(OSError(errno.ENOSPC, 'private path')),
                         'на диске ВАХТЫ закончилось место')
        self.assertEqual(service._note(sqlite3.OperationalError('private path')),
                         'не удалось записать или прочитать данные ВАХТЫ')
        self.assertEqual(service._note(ValueError('private detail')),
                         'внутренняя ошибка ВАХТЫ')

    async def test_empty_plan_wait_does_not_count_towards_stall(self):
        bot = SimpleNamespace(farm_error=AsyncMock(return_value=True))
        with patch.object(telegram, 'monotonic', return_value=0) as clock:
            alert = telegram.FarmProgressAlert()
            alert.start_watch()
            clock.return_value = 60
            alert.reset()  # last scan found no work
            clock.return_value += 660  # idle sleep plus the next scan
            alert.start_watch()
            for elapsed in (240, 899):
                clock.return_value = 720 + elapsed
                await alert.check(bot)
                bot.farm_error.assert_not_awaited()
            clock.return_value += 1
            await alert.check(bot)
            await alert.check(bot)
            bot.farm_error.assert_awaited_once()

    async def test_silent_watch_notifies_without_waiting_for_return(self):
        app = service.Service()
        app.bot = SimpleNamespace(farm_error=AsyncMock(return_value=True))
        app.store = SimpleNamespace(box_art=lambda _: '')
        app.quiet_games = lambda: set()
        head = SimpleNamespace(game_id='1', game='Game', campaign='Campaign')
        app.replan = AsyncMock(return_value=SimpleNamespace(
            blocks=[SimpleNamespace(task=head)]))
        clock = [0]
        calls = 0

        async def watching(*args, **kwargs):
            await asyncio.Event().wait()

        async def wait(tasks, timeout):
            nonlocal calls
            calls += 1
            clock[0] += 901
            if calls == 2:
                app.stopping = True
                raise asyncio.CancelledError
            return set(), tasks

        with patch.object(update, 'take', return_value=None), \
             patch.object(service.w, 'watch', watching), \
             patch.object(service.asyncio, 'wait', wait), \
             patch.object(telegram, 'monotonic', side_effect=lambda: clock[0]):
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await service.autopilot(app)
            finally:
                if app.current:
                    app.current.cancel()
                    await asyncio.gather(app.current, return_exceptions=True)
        self.assertEqual(app.bot.farm_error.await_count, 1)
        self.assertIn('15 минут', app.bot.farm_error.await_args.args[0])

    async def test_rescan_between_slices_does_not_count_as_viewing(self):
        bot = SimpleNamespace(farm_error=AsyncMock(return_value=True))
        with patch.object(telegram, 'monotonic', return_value=0) as clock:
            alert = telegram.FarmProgressAlert()
            alert.start_watch()
            clock.return_value = 600
            alert.end_watch()
            clock.return_value += 1800
            await alert.check(bot)
            alert.start_watch()
            clock.return_value += 299
            await alert.check(bot)
            bot.farm_error.assert_not_awaited()
            clock.return_value += 1
            await alert.check(bot)
            bot.farm_error.assert_awaited_once()

    async def test_offline_campaign_wait_then_stalled_live_watch(self):
        app = service.Service()
        clock = 0
        alerts = []
        first_live_attempt = None
        offline_attempts = 0
        live_slices = 0
        tarkov = SimpleNamespace(game_id='a', game='Tarkov', campaign='Day 6')
        finals = SimpleNamespace(game_id='b', game='Overwatch', campaign='Finals')
        app.store = SimpleNamespace(box_art=lambda _: '')
        app.bot = SimpleNamespace(farm_error=AsyncMock(
            side_effect=lambda _: alerts.append(clock) or True))

        async def replan(_):
            head = tarkov if clock == 0 else finals
            return SimpleNamespace(blocks=[SimpleNamespace(task=head)])

        async def rest(seconds):
            nonlocal clock
            clock += seconds

        async def watch(_, game, **kwargs):
            nonlocal clock, first_live_attempt, offline_attempts, live_slices
            if game == 'Tarkov':
                clock += 60
                return service.w.NOTHING  # final drop is ready in the inventory
            if clock < 1800:
                offline_attempts += 1
                return service.w.NO_CHANNEL
            if first_live_attempt is None:
                first_live_attempt = clock
            live_slices += 1
            if live_slices > 3:
                app.stopping = True
                raise asyncio.CancelledError
            clock += (899, 1, 60)[live_slices - 1]
            return service.w.SLICE

        app.replan, app.rest = replan, rest
        with patch.object(update, 'take', return_value=None), \
             patch.object(service.w, 'watch', watch), \
             patch.object(service, 'monotonic', side_effect=lambda: clock), \
             patch.object(telegram, 'monotonic', side_effect=lambda: clock):
            with self.assertRaises(asyncio.CancelledError):
                await service.autopilot(app)
        self.assertGreater(offline_attempts, 1)
        self.assertEqual(alerts, [first_live_attempt + 900])

    async def test_autopilot_alerts_once_then_rearms_after_recovery(self):
        for failure, note in ((LoginRequired('private detail'), 'нужен вход в Twitch'),
                              (TwitchError('private detail'), 'Twitch отвечает ошибкой'),
                              (httpx.ConnectError('private detail'), 'нет связи с Twitch'),
                              (sqlite3.OperationalError('database or disk is full'),
                               'на диске ВАХТЫ закончилось место')):
            with self.subTest(note=note):
                app = service.Service()
                app.bot = SimpleNamespace(farm_error=AsyncMock(return_value=True))
                app.push = lambda *args: None
                attempts = 0

                async def replan(*args):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 3:
                        return SimpleNamespace(blocks=[])
                    raise failure

                async def sleep(_):
                    if attempts == 5:
                        raise asyncio.CancelledError

                app.replan, app.rest = replan, sleep
                with patch.object(update, 'take', return_value=None), \
                     patch.object(service.asyncio, 'sleep', sleep):
                    with self.assertRaises(asyncio.CancelledError):
                        await service.autopilot(app)
                self.assertEqual([x.args for x in app.bot.farm_error.await_args_list],
                                 [(note,), (note,)])

    async def test_failed_delivery_retries_with_delay_then_stops(self):
        alert = telegram.FarmErrorAlert()
        bot = SimpleNamespace(farm_error=AsyncMock(side_effect=[False, True]))
        with patch.object(telegram, 'monotonic', return_value=100):
            await alert.notify(bot, 'failure')
            await alert.notify(bot, 'failure')
            self.assertEqual(bot.farm_error.await_count, 1)
        with patch.object(telegram, 'monotonic', return_value=220):
            await alert.notify(bot, 'failure')
            await alert.notify(bot, 'failure')
            self.assertEqual(bot.farm_error.await_count, 2)

    async def test_transport_failure_does_not_escape_or_log_token(self):
        alert = telegram.FarmErrorAlert()
        bot = SimpleNamespace(farm_error=AsyncMock(side_effect=RuntimeError('secret-token')))
        with self.assertLogs('vahta', level='WARNING') as logs:
            await alert.notify(bot, 'failure')
        self.assertFalse(alert.sent)
        self.assertNotIn('secret-token', str(logs.output))
        self.assertIn('RuntimeError', str(logs.output))

    async def test_no_bot_does_not_consume_alert_and_new_reason_notifies(self):
        alert = telegram.FarmErrorAlert()
        bot = SimpleNamespace(farm_error=AsyncMock(return_value=True))
        await alert.notify(None, 'failure')
        await alert.notify(bot, 'failure')
        await alert.notify(bot, 'нужен вход в Twitch')
        self.assertEqual(bot.farm_error.await_count, 2)

    async def test_shutdown_cancellation_propagates(self):
        bot = SimpleNamespace(farm_error=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await telegram.FarmErrorAlert().notify(bot, 'failure')

    async def test_telegram_payload_escaping_and_delivery_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            config = telegram.Config(Path(folder)/'telegram.json')
            config.token, config.chat_id = 'test-token', 42
            bot = telegram.Bot(None, config)
            await bot.close()
            bodies = []
            accepted = False

            def reply(request):
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={'ok': accepted, 'result': {'message_id': 1}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                bot.http = client
                self.assertFalse(await bot.farm_error('Twitch <error> & failure'))
                accepted = True
                self.assertTrue(await bot.farm_error('нужен вход в Twitch'))
                self.assertTrue(await bot.farm_error('Проверка', test=True))
                config.chat_id = None
                self.assertFalse(await bot.farm_error('failure'))
            self.assertEqual(len(bodies), 3)
            self.assertEqual(bodies[0]['chat_id'], 42)
            self.assertIn('&lt;error&gt; &amp;', bodies[0]['text'])
            self.assertIn('войди в Twitch заново', bodies[1]['text'])
            self.assertIn('Это тест. Фарм продолжает работать.', bodies[2]['text'])


if __name__ == '__main__':
    unittest.main()
