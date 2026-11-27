"""Regression coverage for the catalog and inventory outages on November 18."""
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vahta import drops, scanner
from vahta.api import LoginRequired, TemporaryTwitchError, Twitch, TwitchError


class CatalogTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tw = Twitch(Path(folder.name))
        self.tw.user_id = "42"
        self.calls = Counter()
        self.campaign = {
            "id": "c", "name": "Campaign", "game": {"id": "g", "displayName": "Game"},
            "self": {"isAccountConnected": True},
            "timeBasedDrops": [{"id": "d", "name": "Drop", "requiredMinutesWatched": 60,
                                "self": {"currentMinutesWatched": 3}}],
        }
        self.dashboard = {"data": {"currentUser": {"dropCampaigns": [self.campaign]}}}
        self.inv = {"data": {"currentUser": {"inventory": {"dropCampaignsInProgress": [self.campaign]}}}}
        self.details = {"data": {"user": {"dropCampaign": self.campaign}}}
        self.timeout = {"errors": [{"message": "service timeout"}] * 113, "data": None}
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.reply))
        self.tw._client = self.http
        self.addAsyncCleanup(self.http.aclose)
        self.sleep = self.enterContext(patch("vahta.api.asyncio.sleep", new_callable=AsyncMock))

    def reply(self, req):
        op = json.loads(req.content)["operationName"]
        self.calls[op] += 1
        response = {"ViewerDropsDashboard": self.dashboard, "Inventory": self.inv,
                    "DropCampaignDetails": self.details}[op]
        if isinstance(response, list):
            response = response.pop(0)
        return httpx.Response(200, json=response)

    async def test_cold_scan_and_watch_use_same_live_inventory_campaign(self):
        self.dashboard = self.timeout
        store = Mock()
        store.watchlist.return_value = [{"game_id": "g", "game_name": "Game", "slug": "game"}]
        with patch.object(scanner.w, "pick_channel", new_callable=AsyncMock, return_value=object()):
            scanned = await scanner.scan(self.tw, store)
        watched = await drops.find_campaigns(self.tw, "Game")
        for camp in (scanned[0].campaigns[0], watched[0]):
            self.assertEqual((camp.id, camp.drops[0].current_minutes), ("c", 3))
        self.assertEqual(self.calls, {"Inventory": 2, "ViewerDropsDashboard": 6, "DropCampaignDetails": 2})
        self.assertIsNone(self.tw.farm_catalog)

    async def test_snapshot_expires_and_does_not_cross_accounts(self):
        self.inv = {"data": {"currentUser": {"inventory": {}}}}
        with patch.object(drops, "monotonic", return_value=100):
            await drops.find_campaigns(self.tw, "Game")
        self.dashboard = self.timeout
        with patch.object(drops, "monotonic", return_value=1899):
            self.assertEqual(len(await drops.find_campaigns(self.tw, "Game")), 1)
            self.tw.user_id = "another"
            with self.assertRaises(TemporaryTwitchError):
                await drops.find_campaigns(self.tw, "Game")
            self.tw.user_id = "42"
        with patch.object(drops, "monotonic", return_value=1900):
            with self.assertRaises(TemporaryTwitchError):
                await drops.find_campaigns(self.tw, "Game")

    async def test_inventory_deadline_is_retried(self):
        self.inv = [{"errors": [{"message": "context deadline exceeded"}]}, self.inv]
        self.assertEqual((await drops.inventory(self.tw)).campaigns[0].id, "c")
        self.sleep.assert_awaited_once_with(2)

    async def test_partial_or_auth_errors_cannot_start_watching(self):
        self.dashboard = {"data": {"currentUser": None}}
        with self.assertRaises(LoginRequired):
            await drops.find_campaigns(self.tw, "Game")
        self.dashboard = {"errors": [{"message": "service timeout"}, {"message": "Forbidden"}]}
        with self.assertRaises(TwitchError) as caught:
            await drops.find_campaigns(self.tw, "Game")
        self.assertNotIsInstance(caught.exception, TemporaryTwitchError)
        self.assertIsNone(self.tw.farm_catalog)
        self.assertEqual(self.calls["DropCampaignDetails"], 0)
        self.sleep.assert_not_awaited()

    async def test_cold_fallback_cannot_turn_unknown_game_into_empty_catalog(self):
        self.dashboard = self.timeout
        with self.assertRaises(TemporaryTwitchError) as caught:
            await drops.find_campaigns(self.tw, "Other game")
        self.assertEqual(str(caught.exception).count("service timeout"), 1)
        self.assertEqual(self.calls["DropCampaignDetails"], 0)

    async def test_vanished_campaign_is_skipped_after_live_detail_check(self):
        self.dashboard = self.timeout
        self.details = {"data": {"user": {"dropCampaign": None}}}
        self.assertEqual(await drops.find_campaigns(self.tw, "Game"), [])
        self.assertEqual(self.calls["DropCampaignDetails"], 1)

    async def test_missing_inventory_is_not_used_as_empty(self):
        self.inv = {"data": {"currentUser": {"inventory": None}}}
        with self.assertRaises(TwitchError):
            await drops.find_campaigns(self.tw, "Game")
        self.assertEqual(self.calls["ViewerDropsDashboard"], 0)


if __name__ == "__main__":
    unittest.main()
