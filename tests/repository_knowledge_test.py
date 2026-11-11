import base64
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from repository_knowledge import RepositoryKnowledge
from review_materials import blob_digest


class KnowledgeTests(unittest.TestCase):
    def fixture(self,broken=False,truncated=False):
        docs={'AGENTS.md':'Read CLAUDE.md and docs/joins.md.', 'CLAUDE.md':'Use the pinned commit.',
              'docs/joins.md':'Join at invoice grain.', 'models/AGENTS.md':'Old rule.',
              'models/AGENTS.override.md':'Current model rule.'}
        blobs={blob_digest(text.encode()):text.encode() for text in docs.values()}
        calls=[]
        def api(path):
            calls.append(path)
            if path=='repos/team/repo':return {'default_branch':'develop'}
            if path.endswith('/commits/develop'):return {'sha':'a'*40}
            if '/git/trees/' in path:
                return {'truncated':truncated,'tree':[{'path':p,'type':'blob','sha':blob_digest(t.encode()),'size':len(t.encode())} for p,t in docs.items()]}
            if '/git/blobs/' in path:
                raw=blobs[path.rsplit('/',1)[1]]
                if broken and raw==b'Use the pinned commit.':raise RuntimeError('HTTP 403')
                return {'encoding':'base64','content':base64.b64encode(raw).decode()}
            raise AssertionError(path)
        return RepositoryKnowledge(api),calls

    def test_technical_question_resolves_default_branch_and_reads_referenced_document(self):
        reader,calls=self.fixture()
        first=reader.read('team/repo')
        self.assertEqual(first['commit'],'a'*40)
        self.assertIn('docs/joins.md',first['knowledge_paths'])
        self.assertEqual({d['path'] for d in first['documents']},{'AGENTS.md','CLAUDE.md'})
        next_page=reader.read('team/repo',first['commit'],['docs/joins.md'])
        self.assertIn('Join at invoice grain.',[d['text'] for d in next_page['documents']])
        self.assertEqual(sum('/git/trees/' in c for c in calls),1)

    def test_review_uses_exact_head_and_nested_override(self):
        reader,calls=self.fixture()
        result=reader.read('team/repo','b'*40,changed_paths=['models/sales/model.sql'])
        self.assertFalse(any('/commits/' in c for c in calls))
        paths={d['path'] for d in result['documents']}
        self.assertIn('models/AGENTS.override.md',paths)
        self.assertNotIn('models/AGENTS.md',paths)
        self.assertTrue(all('/'+'b'*40+'/' in d['url'] for d in result['documents']))

    def test_missing_or_failed_documents_remain_explicit_without_losing_other_evidence(self):
        reader,_=self.fixture(broken=True,truncated=True)
        result=reader.read('team/repo','a'*40,['docs/missing.md'])
        self.assertTrue(result['index_truncated'])
        self.assertIn('AGENTS.md',[d['path'] for d in result['documents']])
        self.assertEqual({d['path'] for d in result['errors']},{'CLAUDE.md','docs/missing.md'})

    def test_traversal_and_external_paths_are_rejected(self):
        reader,_=self.fixture()
        for path in ['../secret','/etc/passwd','docs/../../secret']:
            with self.assertRaises(ValueError):reader.read('team/repo','a'*40,[path])
