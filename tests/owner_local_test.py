import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import owner_local as local


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo=Path(self.tmp.name).resolve()/"workspace"
        self.repo.mkdir()
        root=Path(self.tmp.name)/"state"
        root.mkdir()
        conf={"owner":"OWNER","team":"TEAM","repo":str(self.repo),"origin":"https://example.invalid/repo"}
        (root/"config.json").write_text(json.dumps(conf))
        patched=patch.object(local,"ROOT",root)
        patched.start()
        self.addCleanup(patched.stop)
        self.key=b"fixture"
        self.event={"teamId":"TEAM","channelId":"CHAN","threadTs":"1.000","messageTs":str(time.time()),
                    "senderUserId":"OWNER","text":"<@OWNER> do it","mention":{"type":"user","id":"OWNER"}}

    def task(self,event=None):
        event=event or self.event
        binding=[event[k] for k in ("teamId","channelId","threadTs","messageTs","senderUserId","text")]
        body=base64.urlsafe_b64encode(json.dumps({"binding":binding,"history":[]}).encode()).decode().rstrip("=")
        sig=hmac.new(self.key,("owner-local/1:"+body).encode(),hashlib.sha256).hexdigest()
        return {"ticket":"owner1."+body+"."+sig,"event":event,"output":json.dumps({"action":"shell","command":"echo marker >> local.txt"})}

    def test_replay_runs_once(self):
        task=self.task()
        with patch.object(local.review_evidence,"isolated_turn",return_value='{"action":"done","text":"ok"}'):
            self.assertEqual(local.dispatch("finish",task,self.key),{"reply":"ok"})
            self.assertEqual(local.dispatch("finish",task,self.key),{"reply":"ok"})
        self.assertEqual((self.repo/"local.txt").read_text(),"marker\n")

    def test_unattempted_refusal_does_not_finish_a_local_task(self):
        task=self.task()
        task["output"]='{"action":"done","text":"no tools"}'
        responses=['{"action":"shell","command":"echo checked > proof"}','{"action":"done","text":"verified"}']
        with patch.object(local.review_evidence,"isolated_turn",side_effect=responses):
            self.assertEqual(local.dispatch("finish",task,self.key),{"reply":"verified"})
        self.assertEqual((self.repo/"proof").read_text(),"checked\n")

    def test_foreign_forged_expired_and_retargeted_requests(self):
        task=self.task()
        for bad in [self.task({**self.event,"senderUserId":"OTHER"}),
                    self.task({**self.event,"messageTs":"1.1"}),
                    {**task,"event":{**self.event,"channelId":"DIFFERENT"}},
                    {**task,"ticket":task["ticket"][:-1]+"x"}]:
            self.assertNotIn("prompt",local.dispatch("prepare",bad,self.key))
            local.dispatch("finish",bad,self.key)
        self.assertFalse((self.repo/"local.txt").exists())

    def test_workspace_boundary(self):
        outside=self.repo.parent/"private-data"
        outside.write_text("private")
        (self.repo/".git").mkdir()
        for cmd in [f'cat "{outside}"',f'echo x > "{outside}"','touch .git/hooks-file']:
            self.assertNotEqual(local.shell(cmd,self.repo)["exit"],0)
        self.assertEqual(outside.read_text(),"private")
        self.assertFalse((self.repo/".git/hooks-file").exists())
