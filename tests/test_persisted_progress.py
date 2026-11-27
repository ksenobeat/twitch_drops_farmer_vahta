"""Keep exact Twitch progress when completed campaigns disappear from Inventory."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vahta import drops as d
from vahta.store import Store

class ProgressTests(unittest.TestCase):
    def test_shared_reward_survives_restart_without_lifetime_count_guess(self):
        now = datetime.now(timezone.utc)
        def campaign():
            return d.Campaign('camp', 'Campaign', 'Game', 'game', now-timedelta(days=1),
                now+timedelta(days=1), True, [
                    d.Drop('one', 'Pack', 60, benefit_ids=['pack']),
                    d.Drop('two', 'Pack', 120, benefit_ids=['pack'])])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'db.sqlite'
            with Store(path) as store:
                camp = campaign()
                camp.drops[0].claimed = True
                store.record_claim(camp, camp.drops[0])
                camp.drops[1].current_minutes = 120
                store.remember_progress(camp)
            with Store(path) as store:
                fresh = campaign()
                store.restore_progress(fresh)
                self.assertTrue(fresh.drops[0].claimed)
                self.assertEqual(fresh.drops[1].current_minutes, 120)
                # No reward evidence: don't silently mark the final drop claimed.
                self.assertFalse(fresh.drops[1].claimed)
                reward = d.Reward(id='pack', name='Pack', awarded_at=now)
                d.mark_granted([fresh], [reward])
                self.assertTrue(fresh.drops[1].granted)
                store.remember_progress(fresh)
                again = campaign()
                store.restore_progress(again)
                self.assertEqual(again.remaining_minutes, 0)
                other = campaign()
                other.id = 'other-campaign'
                store.restore_progress(other)
                self.assertFalse(other.drops[0].claimed)

if __name__ == '__main__':
    unittest.main()
