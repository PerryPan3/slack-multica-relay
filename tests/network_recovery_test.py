import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import network_recovery as recovery


class NetworkRecoveryTests(unittest.TestCase):
    def test_silent_disconnection_releases_slot_but_tool_call_can_settle(self):
        watch=recovery.ProgressWatch(0)
        with self.assertRaises(recovery.NetworkWait):watch.check(61,lambda:False)
        watch=recovery.ProgressWatch(0)
        watch.event({'type':'item.started','item':{'type':'mcp_tool_call','id':'write'}},1)
        watch.check(61,lambda:False)
        watch.event({'type':'item.completed','item':{'type':'mcp_tool_call','id':'write'}},80)
        with self.assertRaises(recovery.NetworkWait):watch.check(141,lambda:False)

    def test_errors_do_not_refresh_progress_and_healthy_stall_is_bounded(self):
        watch=recovery.ProgressWatch(0)
        watch.event({'type':'error','message':'Reconnecting'},290)
        with self.assertRaises(recovery.NetworkWait) as caught:watch.check(301,lambda:True)
        self.assertEqual(caught.exception.code,'model_stalled_wait')

    def test_real_progress_allows_long_review_without_fixed_five_minute_limit(self):
        watch=recovery.ProgressWatch(0)
        for now in range(20,1800,20):
            watch.event({'type':'item.completed','item':{'type':'agent_message'}},now)
            watch.check(now+10,lambda:True)

    def test_offline_checks_do_not_consume_resumption_budget(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(recovery.review_evidence,'PRIVATE',Path(tmp)):
            for _ in range(20):self.assertTrue(recovery.defer('id',{'ticket':'signed'},'model_network_wait'))
            self.assertEqual(recovery.read('id')['resumes'],0)
            with self.assertRaises(recovery.NetworkWait):recovery.consume('id')
            for i in range(3):
                data=recovery.read('id');data['ready']=True;recovery.save('id',data);recovery.consume('id')
                self.assertEqual(recovery.defer('id',{'ticket':'signed'},'model_stalled_wait'),i<2)

    def test_watcher_offline_no_model_no_dispatch_then_two_healthy_checks_and_one_rerun(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(recovery.review_evidence,'PRIVATE',Path(tmp)):
            root=Path(tmp);(root/'review').mkdir();(root/'review/key').write_text('key')
            recovery.save('id',{'args':{},'resumes':0,'created':0,'not_before':0,'healthy_since':None,'ready':False})
            config={'enabled':True,'multica':'multica','profile':'private','server_url':'https://tasks.test','workspace_id':'workspace'}
            with patch.object(recovery,'configuration',return_value=config),patch.object(recovery,'reachable',return_value=False) as health, \
                 patch.object(recovery,'recovery_target',return_value={'action':'resume','runId':'run'}) as target, \
                 patch.object(recovery.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')) as run, \
                 patch.object(recovery.time,'time',return_value=100) as clock:
                recovery.tick();self.assertEqual(recovery.read('id')['resumes'],0);target.assert_not_called();run.assert_not_called()
                health.return_value=True
                recovery.tick();run.assert_not_called()
                clock.return_value=131;recovery.tick();self.assertEqual(run.call_count,1)
                recovery.tick();self.assertEqual(run.call_count,1)
                self.assertTrue(recovery.read('id')['ready'])

    def test_cancelled_wait_is_removed_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(recovery.review_evidence,'PRIVATE',Path(tmp)):
            root=Path(tmp);(root/'review').mkdir();(root/'review/key').write_text('key')
            recovery.save('id',{'args':{},'not_before':0,'healthy_since':1,'ready':False})
            with patch.object(recovery,'configuration',return_value={'enabled':True}),patch.object(recovery,'reachable',return_value=True), \
                 patch.object(recovery,'recovery_target',return_value={'action':'cancelled'}),patch.object(recovery.subprocess,'run') as run:
                recovery.tick();run.assert_not_called();self.assertIsNone(recovery.read('id'))

    def test_unknown_dispatch_is_not_repeated_and_owner_is_notified(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(recovery.review_evidence,'PRIVATE',Path(tmp)):
            root=Path(tmp);(root/'review').mkdir();(root/'review/key').write_text('key')
            recovery.save('id',{'args':{},'not_before':0,'healthy_since':1,'ready':True,'source_run':'run','dispatch_at':1})
            with patch.object(recovery,'configuration',return_value={'enabled':True}),patch.object(recovery,'reachable',return_value=True), \
                 patch.object(recovery,'recovery_target',side_effect=[{'action':'resume','runId':'run'},{'action':'notified'}]) as target, \
                 patch.object(recovery.subprocess,'run') as run:
                recovery.tick();run.assert_not_called();self.assertTrue(recovery.read('id')['blocked'])
                self.assertEqual(target.call_args.kwargs,{'dispatch_unknown':'run'})

if __name__=='__main__':unittest.main()
