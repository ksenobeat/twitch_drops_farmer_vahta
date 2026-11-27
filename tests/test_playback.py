"""Transport regression tests, no Twitch credentials or network."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vahta.playback import Playback, playlist_urls
from vahta.api import Twitch

BASE = "https://video.ttvnw.net/path/live.m3u8"
MASTER = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nhttps://video.ttvnw.net/high.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://video.ttvnw.net/low.m3u8'
MEDIA = '#EXTM3U\n#EXTINF:2,\nseg1.ts\n#EXTINF:2,\nseg2.ts'


class Parsing(unittest.TestCase):
    def test_only_tagged_media(self):
        self.assertEqual(playlist_urls(MEDIA, BASE), [
            'https://video.ttvnw.net/path/seg1.ts', 'https://video.ttvnw.net/path/seg2.ts'])
        for body in ['{"error":"offline"}', '#EXTM3U\nhttps://video.ttvnw.net/x',
                     '#EXTM3U\n#EXTINF:2,', '#EXTM3U\n#EXTINF:2,\nhttps://localhost/x',
                     '#EXTM3U\n#EXTINF:2,\nhttps://user:secret@video.ttvnw.net/x']:
            self.assertEqual(playlist_urls(body, BASE), [])


class Transport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.tokens = 0
        self.status = 200
        self.block = False

        async def handle(request):
            self.requests.append(request)
            if request.method == 'HEAD':
                if self.block:
                    await asyncio.sleep(60)
                return httpx.Response(self.status)
            if request.url.host == 'usher.ttvnw.net':
                return httpx.Response(200, text=MASTER)
            return httpx.Response(self.status, text=MEDIA)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        owner = self

        class Twitch:
            http = owner.client

            async def gql(self, query):
                owner.tokens += 1
                assert query['variables']['login'] == 'test'
                return {'streamPlaybackAccessToken': {'value': 'private', 'signature': 'private'}}

        self.playback = Playback(Twitch(), 'test')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_head_only_cached_until_broadcast_changes(self):
        self.assertTrue(await self.playback.tick('one'))
        self.assertTrue(await self.playback.tick('one'))
        self.assertEqual(self.tokens, 1)
        self.assertTrue(await self.playback.tick('two'))
        self.assertEqual(self.tokens, 2)
        for request in self.requests:
            if request.url.path.endswith('.ts'):
                self.assertEqual(request.method, 'HEAD')
            self.assertNotIn('authorization', request.headers)
        self.assertTrue(any(r.url.path == '/low.m3u8' for r in self.requests))

    async def test_expired_playlist_gets_new_token(self):
        self.assertTrue(await self.playback.tick('one'))
        self.status = 403
        self.assertFalse(await self.playback.tick('one'))
        self.assertIsNone(self.playback.url)
        self.status = 200
        self.assertTrue(await self.playback.tick('one'))
        self.assertEqual(self.tokens, 2)

    async def test_cancellation_stops_polling(self):
        self.block = True
        task = asyncio.create_task(self.playback.tick('one'))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_twenty_second_polls_keep_minute_progress_interval(self):
        clock = [0.0]
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with patch('vahta.playback.monotonic', lambda: clock[0]), patch('vahta.playback.asyncio.sleep', sleep):
            await self.playback.wait(59, 'one')
        self.assertEqual(sleeps, [20, 20, 19])
        self.assertEqual(sum(r.method == 'HEAD' for r in self.requests), 4)

    async def test_head_content_length_is_not_downloaded_traffic(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            tw = Twitch(Path(folder))
            await tw._measure_in(httpx.Response(200, headers={'Content-Length': '900000'},
                                               request=httpx.Request('HEAD', BASE)))
            self.assertEqual(tw.meter.total, 0)
            await tw._measure_in(httpx.Response(200, headers={'Content-Length': '123'},
                                               request=httpx.Request('GET', BASE)))
            self.assertEqual(tw.meter.total, 123)


if __name__ == '__main__':
    unittest.main()
