"""Durable review grants, scoped to one verified Slack thread and owner."""
import base64
from contextlib import closing
import hashlib
import hmac
import json
import sqlite3


class ReviewTasks:
    def __init__(self, path, claim, ticket, key):
        self.path, self.claim, self.ticket, self.key = path, claim, ticket, key
        self.scope = json.dumps(claim['binding'][:3] + [claim['binding'][5]])
        self.ts = float(claim['binding'][3])
        self.event = hashlib.sha256(ticket.encode()).hexdigest()
        with self.connect() as db:
            db.executescript('''
                create table if not exists tasks(scope text,target text,id text,origin text,
                  status text,updated real,receipt text,primary key(scope,target));
                create table if not exists turns(event text primary key,plan text);
            ''')
            db.commit()

    def connect(self):
        return closing(sqlite3.connect(self.path,timeout=15))

    def rows(self):
        with self.connect() as db:
            rows=db.execute('select target,id,origin,status,updated,receipt from tasks where scope=?',(self.scope,)).fetchall()
        return {r[0]:dict(zip(('id','origin','status','updated','receipt'),r[1:])) for r in rows}

    def summary(self):
        return [{'target':target,'status':row['status'],'receipt':json.loads(row['receipt']) if row['receipt'] else None}
                for target,row in self.rows().items()]

    def origin_claim(self, ticket, target):
        tag,body,signature=ticket.split('.')
        expected=hmac.new(self.key,('intent/1:'+body).encode(),hashlib.sha256).hexdigest()
        if tag!='intent1' or not hmac.compare_digest(expected,signature):
            raise ValueError('Invalid original review authorization.')
        claim=json.loads(base64.urlsafe_b64decode(body+'==='))
        if (json.dumps(claim['binding'][:3]+[claim['binding'][5]])!=self.scope or
                not claim.get('review') or target not in claim['targets']):
            raise ValueError('Original grant belongs to another thread, owner or target.')
        return claim

    def revoke_cancelled(self):
        for target,row in self.rows().items():
            if row['status']!='pending': continue
            origin=self.origin_claim(row['origin'],target)
            for event in self.claim.get('cancellations',[]):
                timestamp=float(event['ts'])
                if (event['author']==self.claim['binding'][5] and
                        float(origin['binding'][3])<timestamp<=self.ts):
                    with self.connect() as db:
                        db.execute("update tasks set status='cancelled',updated=? where scope=? and target=? and id=?",
                                   (max(timestamp,row['updated']),self.scope,target,row['id']))
                        db.commit()
                    break

    def prepare(self, classify):
        self.revoke_cancelled()
        with self.connect() as db:
            saved=db.execute('select plan from turns where event=?',(self.event,)).fetchone()
        if saved: return json.loads(saved[0])
        rows=self.rows()
        candidates=set(self.claim['targets']) | {t for t,r in rows.items() if r['status']=='pending'}
        plan={}
        if self.claim.get('review') and candidates:
            proposal=classify(sorted(candidates),self.summary())
            actions=proposal.get('actions')
            if not isinstance(actions,list): raise ValueError('Invalid review continuation plan.')
            seen=set()
            with self.connect() as db:
                for entry in actions:
                    target,action=entry.get('target'),entry.get('action')
                    if target not in candidates or target in seen or action not in ('new','continue','inspect','cancel','none'):
                        raise ValueError('Invalid review task action.')
                    seen.add(target)
                    row=rows.get(target)
                    if row and self.ts < row['updated']:
                        continue
                    if action=='new':
                        # A follow-up must not replace the identity of an unfinished write.
                        if row and row['status']=='pending':
                            action='continue'
                        else:
                            if target not in self.claim['targets']: raise ValueError('New target is outside signed context.')
                            self.origin_claim(self.ticket,target)
                            identity=hashlib.sha256((self.ticket+':review-task:'+target).encode()).hexdigest()
                            db.execute('insert or replace into tasks values(?,?,?,?,?,?,NULL)',
                                       (self.scope,target,identity,self.ticket,'pending',self.ts))
                            plan[target]=identity
                    if action=='continue' and row and row['status']=='pending':
                        self.origin_claim(row['origin'],target)
                        plan[target]=row['id']
                        db.execute('update tasks set updated=? where scope=? and target=?',(self.ts,self.scope,target))
                    if action=='cancel' and row and row['status']=='pending':
                        db.execute("update tasks set status='cancelled',updated=? where scope=? and target=?",(self.ts,self.scope,target))
                db.execute('insert into turns values(?,?)',(self.event,json.dumps(plan)))
                db.commit()
        else:
            with self.connect() as db:
                db.execute('insert into turns values(?,?)',(self.event,'{}'))
                db.commit()
        return plan

    def require(self,target,plan):
        if not self.claim.get('review') or target not in plan:
            raise ValueError('This turn does not continue an authorized review.')
        row=self.rows().get(target)
        if not row or row['id']!=plan[target] or row['status']!='pending' or row['updated']>self.ts:
            raise ValueError('Review authorization is completed, cancelled or superseded.')
        self.origin_claim(row['origin'],target)
        return row['id']

    def import_legacy(self,target,directory):
        """Migrate only a signed request with matching host-side formal-review records."""
        origin=self.origin_claim(self.ticket,target)
        repo,number=target.rsplit('/pull/',1)
        thread=json.dumps(origin['binding'][:3]+[repo.lower(),int(number)])
        with closing(sqlite3.connect((directory/'intent.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
            saved=db.execute('select plan from plans where job=?',(self.event,)).fetchone()
        if not saved: raise ValueError('No verified legacy dispatch record.')
        plan=json.loads(saved[0])
        if plan.get('route')!='review' or target not in plan.get('targets',[plan.get('target')]):
            raise ValueError('Legacy request did not authorize this review.')
        identity=hashlib.sha256((self.ticket+(target if 'targets' in plan else '')).encode()).hexdigest()
        with closing(sqlite3.connect((directory/'reviews.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute("select ts,report from review_context where thread=? and report not like '%只读复核，未投票%' order by ts desc limit 1",(thread,)).fetchone()
            job=db.execute('select id from jobs where id=?',(identity,)).fetchone()
        if not job or not row or row[0]!=float(origin['binding'][3]) or not (
                row[1].startswith('未确认 GitHub 审批') or ' INCOMPLETE\n' in row[1]):
            raise ValueError('No matching unfinished formal review exists.')
        with self.connect() as db:
            db.execute('insert or ignore into tasks values(?,?,?,?,?,?,NULL)',
                       (self.scope,target,identity,self.ticket,'pending',row[0]))
            db.commit()

    def complete(self,target,identity,receipt):
        with self.connect() as db:
            db.execute("update tasks set status='completed',receipt=? where scope=? and target=? and id=?",
                       (json.dumps(receipt),self.scope,target,identity))
            db.commit()
