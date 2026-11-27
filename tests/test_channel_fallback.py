"""Unknown channel eligibility and silent-stall notification regressions."""
import asyncio, unittest, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from vahta import watcher as w, telegram as t
from vahta.api import TwitchError
class Tests(unittest.IsolatedAsyncioTestCase):
 async def test_null_fallback_and_empty_rejection(self):
  ch=w.Channel('1','test','Test')
  camp=NS(id='c',game='Game',allowed_channels=[],ends_at=None)
  tw=NS(gql=AsyncMock())
  with patch.object(w,'live_channels',AsyncMock(return_value=[ch])):
   tw.gql.return_value={'channel':{'viewerDropCampaigns':None}}
   self.assertEqual(await w.pick_channel(tw,'game',[camp]),(ch,camp))
   tw.gql.return_value={'channel':{'viewerDropCampaigns':[]}}
   self.assertIsNone(await w.pick_channel(tw,'game',[camp]))
   tw.gql.return_value={'channel':None}
   with self.assertRaises(TwitchError): await w.pick_channel(tw,'game',[camp])
 async def test_timer_real_progress_recovery_and_retry(self):
  bot=NS(farm_error=AsyncMock(side_effect=[False,True,True]))
  with patch.object(t,'monotonic',return_value=0):
   alert=t.FarmProgressAlert();alert.observe('drop',20)
  with patch.object(t,'monotonic',return_value=899): await alert.check(bot)
  self.assertEqual(bot.farm_error.await_count,0)
  with patch.object(t,'monotonic',return_value=900):
   alert.observe('drop',20);await alert.check(bot);await alert.check(bot)
  self.assertEqual(bot.farm_error.await_count,1)
  with patch.object(t,'monotonic',return_value=1020): await alert.check(bot)
  with patch.object(t,'monotonic',return_value=1100):
   await alert.check(bot);alert.observe('drop',21)
  with patch.object(t,'monotonic',return_value=2000): await alert.check(bot)
  self.assertEqual(bot.farm_error.await_count,3)
  with patch.object(t,'monotonic',return_value=3000):
   alert.reset();await alert.check(bot)
  self.assertEqual(bot.farm_error.await_count,3)
if __name__=='__main__': unittest.main()
