"""Campaign details must resolve the account by ID, not by its login name."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vahta import drops
from vahta.api import LoginRequired, Twitch, TwitchError


class CampaignDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_identity_gets_details_when_login_returns_null(self):
        def reply(request):
            variables = json.loads(request.content)["variables"]
            self.assertEqual(variables["dropID"], "campaign-1")
            user = None
            if variables["channelLogin"] == "42":
                user = {"dropCampaign": {
                    "id": "campaign-1", "name": "Live campaign",
                    "game": {"id": "7", "displayName": "Game"},
                    "self": {"isAccountConnected": True},
                    "timeBasedDrops": [{"id": "drop-1", "name": "Reward",
                                        "requiredMinutesWatched": 60}],
                }}
            return httpx.Response(200, json={"data": {"user": user}})

        with tempfile.TemporaryDirectory() as folder:
            tw = Twitch(Path(folder))
            tw.user_id, tw.login_name = "42", "test-account"
            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                tw._client = client
                campaign = await drops.campaign_details(tw, "campaign-1")
            self.assertEqual(campaign.name, "Live campaign")
            self.assertEqual(campaign.drops[0].required_minutes, 60)

    async def test_missing_identity_requires_login_without_request(self):
        tw = type("Fake", (), {"user_id": None, "gql": AsyncMock()})()
        with self.assertRaises(LoginRequired):
            await drops.campaign_details(tw, "campaign-1")
        tw.gql.assert_not_awaited()

    async def test_null_user_is_provider_failure_not_missing_campaign(self):
        tw = type("Fake", (), {"user_id": "42", "gql": AsyncMock(
            return_value={"user": None})})()
        with self.assertRaises(TwitchError):
            await drops.campaign_details(tw, "campaign-1")

    async def test_missing_campaign_still_reported(self):
        tw = type("Fake", (), {"user_id": "42", "gql": AsyncMock(
            return_value={"user": {"dropCampaign": None}})})()
        with self.assertRaises(LookupError):
            await drops.campaign_details(tw, "campaign-1")


if __name__ == "__main__":
    unittest.main()
