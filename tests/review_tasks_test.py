import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from review_tasks import ReviewTasks

TARGET='team/repo/pull/745'
KEY=b'test-key'

class ReviewTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'tasks.sqlite'

    def task(self,ts='1',text='review PR',channel='C',sender='KEVEN',review=True,cancellations=None):
        claim={'binding':['T',channel,'root',ts,sender,'OWNER',text],
               'review':review,'targets':[TARGET],'history':[],'cancellations':cancellations or []}
        body=base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip('=')
        sig=hmac.new(KEY,('intent/1:'+body).encode(),hashlib.sha256).hexdigest()
        return ReviewTasks(self.path,claim,'intent1.'+body+'.'+sig,KEY)

    def plan(self,task,action):
        return task.prepare(lambda *_:{'actions':[{'target':TARGET,'action':action}]})

    def test_interruption_and_other_authorized_sender_preserve_original_grant(self):
        first=self.task();original=self.plan(first,'new')
        follow=self.task('2','为什么总是暂缓审批',sender='MINGHO')
        resumed=self.plan(follow,'continue')
        self.assertEqual(resumed,original)
        self.assertEqual(follow.require(TARGET,resumed),original[TARGET])
        self.assertEqual(follow.rows()[TARGET]['origin'],first.ticket)
        with self.assertRaises(ValueError): first.require(TARGET,original)

    def test_history_alone_and_other_thread_cannot_grant_authority(self):
        self.plan(self.task(),'new')
        other=self.task('2',channel='D')
        self.assertEqual(self.plan(other,'continue'),{})
        with self.assertRaises(ValueError):other.require(TARGET,{})

    def test_revoked_capability_cannot_resume(self):
        self.plan(self.task(),'new')
        current=self.task('2',review=False)
        callback=Mock()
        self.assertEqual(current.prepare(callback),{})
        callback.assert_not_called()

    def test_explicit_inspection_does_not_vote_or_erase_pending_task(self):
        self.plan(self.task(),'new')
        current=self.task('2','只查状态，不投票')
        self.assertEqual(self.plan(current,'inspect'),{})
        self.assertEqual(current.rows()[TARGET]['status'],'pending')
        with self.assertRaises(ValueError):current.require(TARGET,{})
        later=self.task('3','继续吧')
        self.assertIn(TARGET,self.plan(later,'continue'))

    def test_completed_task_requires_a_new_review_request(self):
        current=self.task();plan=self.plan(current,'new')
        current.complete(TARGET,plan[TARGET],{'review':12})
        later=self.task('2','怎么样了')
        self.assertEqual(self.plan(later,'continue'),{})
        again=self.task('3','更新好了，再 review')
        self.assertNotEqual(self.plan(again,'new'),plan)

    def test_cancel_prevents_later_continuation_and_stale_replay(self):
        first=self.task();plan=self.plan(first,'new')
        self.plan(self.task('3','取消这次审查'),'cancel')
        self.assertEqual(self.plan(self.task('4'),'continue'),{})
        self.assertEqual(self.plan(self.task('2'),'new'),{})
        with self.assertRaises(ValueError):first.require(TARGET,plan)

    def test_ingress_owner_cancel_also_revokes_persisted_grant(self):
        self.plan(self.task(),'new')
        follow=self.task('3',cancellations=[{'author':'OWNER','ts':'2'}])
        self.assertEqual(self.plan(follow,'continue'),{})
        self.assertEqual(follow.rows()[TARGET]['status'],'cancelled')

    def test_quoted_or_unapproved_author_does_not_revoke(self):
        self.plan(self.task(),'new')
        follow=self.task('3',cancellations=[{'author':'OTHER','ts':'2'}])
        self.assertIn(TARGET,self.plan(follow,'continue'))

    def test_legacy_migration_requires_matching_formal_dispatch_and_unfinished_record(self):
        import sqlite3
        directory=Path(self.temp.name)/'legacy';directory.mkdir()
        task=self.task()
        identity=hashlib.sha256(task.ticket.encode()).hexdigest()
        db=sqlite3.connect(directory/'intent.sqlite')
        db.execute('create table plans(job text,plan text)')
        db.execute('insert into plans values(?,?)',(identity,json.dumps({'route':'review','target':TARGET})))
        db.commit();db.close()
        db=sqlite3.connect(directory/'reviews.sqlite')
        db.execute('create table jobs(id text)')
        db.execute('insert into jobs values(?)',(identity,))
        db.execute('create table review_context(thread text,ts real,report text)')
        db.execute('insert into review_context values(?,?,?)',(json.dumps(['T','C','root','team/repo',745]),1,'abc INCOMPLETE\nmissing schema'))
        db.commit();db.close()
        task.import_legacy(TARGET,directory)
        self.assertEqual(task.rows()[TARGET]['id'],identity)
        unrelated=self.task('2',channel='D')
        with self.assertRaises(ValueError): unrelated.import_legacy(TARGET,directory)

    def test_request_replay_does_not_reclassify_or_replace_origin(self):
        first=self.task();plan=self.plan(first,'new')
        classifier=Mock(side_effect=AssertionError('must not reclassify'))
        self.assertEqual(first.prepare(classifier),plan)
        follow=self.task('2');self.assertEqual(self.plan(follow,'new'),plan)

    def test_unknown_targets_and_corrupt_original_grants_fail_closed(self):
        first=self.task();self.plan(first,'new')
        with first.connect() as db:
            db.execute("update tasks set origin='intent1.bad.bad'");db.commit()
        with self.assertRaises(ValueError):self.plan(self.task('2'),'continue')
        with self.assertRaises(ValueError):
            self.task('3').prepare(lambda *_:{'actions':[{'target':'team/repo/pull/2','action':'new'}]})

if __name__=='__main__':unittest.main()
