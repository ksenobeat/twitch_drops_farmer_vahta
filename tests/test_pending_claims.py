"""Completed drops must be collected even when there is nothing left to watch."""
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vahta import drops as dr, service
from vahta.api import TwitchError
from vahta.store import Store


class PendingClaimsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = service.Service()
        self.app.store = Store(Path(self.tmp.name) / 'vahta.db')
        self.app.store.remember_rewards([dr.Reward('older', 'Earlier reward')])
        self.app.tw = object()
        now = dr.now()
        self.drop = dr.Drop('drop', 'Final reward', 30, current_minutes=30,
                            claim_id='instance', benefit_ids=['benefit'])
        self.campaign = dr.Campaign('campaign', 'Campaign', 'Game', 'game',
                                    now-timedelta(days=1), now+timedelta(days=1),
                                    True, [self.drop])
        self.inventory = dr.Inventory(campaigns=[self.campaign])
        self.scan = AsyncMock(return_value=[])
        self.announce = AsyncMock()
        self.patches = [patch.object(service.scanner, 'scan', self.scan),
                        patch.object(service, 'announce_rewards', self.announce)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.app.store.db.close()
        self.tmp.cleanup()

    async def collect(self, tw, drop):
        drop.claimed = True
        return True

    async def test_empty_plan_collects_refreshes_and_does_not_repeat(self):
        refreshed = dr.Inventory(rewards=[dr.Reward('benefit', 'Final reward',
                                                    awarded_at=dr.now())])
        with patch.object(dr, 'inventory', AsyncMock(side_effect=[self.inventory, refreshed, refreshed])), \
                patch.object(dr, 'claim', AsyncMock(side_effect=self.collect)) as claim:
            plan = await self.app.replan()
            self.assertEqual(plan.blocks, [])
            claim.assert_awaited_once_with(self.app.tw, self.drop)
            self.assertEqual(len(self.app.store.claims()), 1)
            self.assertIs(self.app.inv, refreshed)
            self.assertIs(self.scan.await_args.args[3], refreshed)
            self.assertEqual(self.announce.await_args.args[1], ['benefit'])
            await self.app.replan()
            self.assertEqual(claim.await_count, 1)
            self.assertEqual(len(self.app.store.claims()), 1)

    async def test_success_survives_next_claim_failure(self):
        other = dr.Drop('other', 'Other reward', 30, current_minutes=30, claim_id='other-instance')
        self.campaign.drops.append(other)

        async def partly(tw, drop):
            if drop is other:
                raise TwitchError('temporary failure')
            return await self.collect(tw, drop)

        with patch.object(dr, 'inventory', AsyncMock(return_value=self.inventory)), \
                patch.object(dr, 'claim', AsyncMock(side_effect=partly)):
            with self.assertRaises(TwitchError):
                await self.app.replan()
        rows = self.app.store.claims()
        self.assertEqual([row['drop_name'] for row in rows], ['Final reward'])
        self.scan.assert_not_awaited()
        self.assertEqual(self.app.store.db.execute(
            'select claimed from drop_progress where drop_id=?', ('drop',)).fetchone()[0], 1)

    async def test_failed_refresh_keeps_confirmed_claim(self):
        with patch.object(dr, 'inventory', AsyncMock(side_effect=[self.inventory, TwitchError('refresh failed')])), \
                patch.object(dr, 'claim', AsyncMock(side_effect=self.collect)):
            with self.assertRaises(TwitchError):
                await self.app.replan()
        self.assertEqual(len(self.app.store.claims()), 1)

    async def test_rejected_claim_is_not_recorded_and_can_retry(self):
        with patch.object(dr, 'inventory', AsyncMock(return_value=self.inventory)), \
                patch.object(dr, 'claim', AsyncMock(return_value=False)):
            with self.assertRaises(TwitchError):
                await self.app.replan()
        self.assertEqual(self.app.store.claims(), [])
        self.assertFalse(self.drop.claimed)
        self.scan.assert_not_awaited()
        with patch.object(dr, 'inventory', AsyncMock(side_effect=[self.inventory, dr.Inventory()])), \
                patch.object(dr, 'claim', AsyncMock(side_effect=self.collect)):
            await self.app.replan()
        self.assertEqual(len(self.app.store.claims()), 1)

    async def test_no_claim_instance_does_not_invent_collection(self):
        self.drop.claim_id = None
        with patch.object(dr, 'inventory', AsyncMock(return_value=self.inventory)) as inventory, \
                patch.object(dr, 'claim', AsyncMock()) as claim:
            await self.app.replan()
        claim.assert_not_awaited()
        inventory.assert_awaited_once()
        self.assertEqual(self.app.store.claims(), [])


if __name__ == '__main__':
    unittest.main()
