"""Device login client identity and failure regressions, without live tokens."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vahta.api import Twitch, TwitchError
from vahta import constants as c, web, drops

class DeviceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_catalog_is_not_empty_catalog(self):
        tw = type('TW', (), {'gql':AsyncMock(return_value={'currentUser':{'dropCampaigns':None}})})()
        with self.assertRaises(TwitchError):
            await drops.list_campaigns(tw)
        tw.gql.return_value={'currentUser':{'dropCampaigns':[]}}
        self.assertEqual(await drops.list_campaigns(tw),[])

    async def test_new_token_client_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            tw = Twitch(Path(folder))
            def reply(req):
                if req.url.path.endswith('/device'):
                    self.assertIn(c.DEVICE_CLIENT_ID, req.content.decode())
                    self.assertEqual(req.headers['Client-Id'], c.DEVICE_CLIENT_ID)
                    return httpx.Response(200, json={'device_code':'test-device'})
                if req.url.path.endswith('/token'):
                    self.assertIn(c.DEVICE_CLIENT_ID, req.content.decode())
                    return httpx.Response(200, json={'access_token':'test-only-token'})
                return httpx.Response(200, json={'client_id':c.DEVICE_CLIENT_ID,'user_id':'test-user','login':'test'})
            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                tw._client = client
                device = await tw.begin_device_login()
                self.assertTrue(await tw.poll_device_login(device))
                await tw.finish_login()
            reloaded = Twitch(Path(folder))
            reloaded._load_auth()
            self.assertEqual(reloaded._headers()['Client-Id'], c.DEVICE_CLIENT_ID)

    async def test_legacy_identity_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'auth.json').write_text(json.dumps({'access_token':'legacy-test-token'}))
            tw = Twitch(Path(folder))
            tw._load_auth()
            self.assertEqual(tw.client_id, c.CLIENT_ID)

    async def test_device_failure_exposed_to_ui(self):
        class Service:
            pending_login = None
            login_error = ''
            tw = type('TW', (), {'login_name':None,'user_id':None,
                                'begin_device_login':AsyncMock(side_effect=TwitchError('Twitch не выдал код'))})()
            async def call(self, fn):
                return await fn(self)
        with patch.object(web, '_service', Service()):
            response = await web.twitch_login()
            self.assertIn('не выдал код', json.loads(response.body)['error'])

    async def test_provider_response_not_leaked(self):
        with tempfile.TemporaryDirectory() as folder:
            tw = Twitch(Path(folder))
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req:httpx.Response(400,json={'message':'sensitive-provider-detail'}))) as client:
                tw._client=client
                with self.assertRaises(TwitchError) as failure:
                    await tw.begin_device_login()
                self.assertIn('HTTP 400',str(failure.exception))
                self.assertNotIn('sensitive',str(failure.exception))

if __name__ == '__main__':
    unittest.main()
