import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from review_materials import ReviewMaterials,blob_digest,PAGE


def archive(entries):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in entries.items(): z.writestr(name,data)
    return out.getvalue()


class MaterialTests(unittest.TestCase):
    def setup_review(self, raw, status='added', text='review', old=None):
        target='team/repo/pull/1'
        data={'base':old if old is not None else raw,'head':raw}
        def api(path):
            if '/contents/' in path:
                value=data[path.split('ref=')[1]]
                return {'type':'file','sha':blob_digest(value),'size':len(value)}
            digest=path.rsplit('/',1)[1]
            value=next(v for v in data.values() if blob_digest(v)==digest)
            return {'encoding':'base64','content':base64.b64encode(value).decode(),'size':len(value)}
        reader=ReviewMaterials(api)
        pr={'base':{'sha':'base'},'head':{'sha':'head'}}
        packet={'full_diff':text,'file_manifest':[{'filename':'flow.tfl','status':status,'artifact_required':True}]}
        reader.register(target,pr,packet)
        return reader,target,pr,packet

    def test_nested_spreadsheet_is_read_before_approval_evidence_is_complete(self):
        xlsx=archive({'xl/sharedStrings.xml':'<sst>2025 goals</sst>','xl/worksheets/sheet1.xml':'<v>42</v>'})
        reader,t,_,_=self.setup_review(archive({'flow':'{"steps":[]}','Data/goal.xlsx':xlsx}))
        top=reader.inspect(t,'flow.tfl')
        self.assertEqual(top['kind'],'archive')
        self.assertEqual(len(reader.missing(t)),2)
        reader.inspect(t,'flow.tfl',members=['flow'])
        reader.inspect(t,'flow.tfl',members=['Data/goal.xlsx'])
        self.assertEqual(len(reader.missing(t)),2)
        reader.inspect(t,'flow.tfl',members=['Data/goal.xlsx','xl/sharedStrings.xml'])
        reader.inspect(t,'flow.tfl',members=['Data/goal.xlsx','xl/worksheets/sheet1.xml'])
        self.assertEqual(reader.missing(t),[])

    def test_large_material_and_member_can_be_paged_without_skipping_evidence(self):
        reader,t,_,_=self.setup_review(archive({'flow':'a'*(PAGE*3)}),text='diff'*(PAGE))
        self.assertTrue(any(x.get('material') for x in reader.missing(t)))
        page=reader.page(t,0)
        while page['next_offset'] is not None:page=reader.page(t,page['next_offset'])
        reader.inspect(t,'flow.tfl')
        reader.inspect(t,'flow.tfl',members=['flow'],offset=PAGE)
        self.assertTrue(reader.missing(t))
        page=reader.inspect(t,'flow.tfl',members=['flow'])
        while page['next_offset'] is not None:
            page=reader.inspect(t,'flow.tfl',members=['flow'],offset=page['next_offset'])
        self.assertEqual(reader.missing(t),[])

    def test_modified_archive_requires_both_pinned_versions_and_new_head_resets_reads(self):
        reader,t,pr,packet=self.setup_review(b'new',status='modified',old=b'old')
        reader.inspect(t,'flow.tfl')
        self.assertEqual(reader.missing(t)[0]['side'],'base')
        self.assertEqual(reader.inspect(t,'flow.tfl',side='base')['text'],'old')
        self.assertEqual(reader.missing(t),[])
        reader.register(t,pr,packet)
        self.assertEqual(reader.missing(t),[])
        reader.register(t,{**pr,'head':{'sha':'new-revision'}},packet)
        self.assertEqual(len(reader.missing(t)),2)

    def test_blob_mismatch_and_unknown_paths_are_rejected(self):
        reader,t,_,_=self.setup_review(b'content')
        with self.assertRaises(ValueError):reader.inspect(t,'outside.txt')
        original=reader.api
        def tamper(path):
            data=original(path)
            if '/git/blobs/' in path:data['content']=base64.b64encode(b'incorrect').decode()
            return data
        reader.api=tamper
        with self.assertRaisesRegex(ValueError,'pinned Git object'):reader.inspect(t,'flow.tfl')

    def test_unreadable_binary_stays_explicit_without_executing_it(self):
        reader,t,_,_=self.setup_review(b'\x00\xff\x80')
        result=reader.inspect(t,'flow.tfl')
        self.assertFalse(result['ok'])
        self.assertEqual(reader.missing(t)[0]['reason'],'unsupported')

    def test_archive_png_is_delivered_as_image_evidence(self):
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2ioAAAAASUVORK5CYII=')
        reader,t,_,_=self.setup_review(archive({'flowGraphImage.png':png,'flow':'{}'}))
        reader.inspect(t,'flow.tfl')
        reader.inspect(t,'flow.tfl',members=['flow'])
        image=reader.inspect(t,'flow.tfl',members=['flowGraphImage.png'])
        self.assertEqual(image['kind'],'image')
        self.assertEqual(base64.b64decode(image['image']['data']),png)
        self.assertEqual(reader.missing(t),[])

    def test_archive_capacity_and_duplicate_names_do_not_create_false_evidence(self):
        reader,t,_,_=self.setup_review(archive({'flow':'data'}))
        with patch('review_materials.MAX_EXPANDED',2):
            with self.assertRaisesRegex(ValueError,'capacity'):reader.inspect(t,'flow.tfl')
        self.assertTrue(reader.missing(t))

    def test_native_collect_keeps_binary_manifest_but_legacy_cannot_silently_approve(self):
        spec=importlib.util.spec_from_file_location('review_binary',Path(__file__).resolve().parents[1]/'scripts/github-review.py')
        review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)
        pr={'changed_files':1,'additions':0,'deletions':0,'title':'Archive','body':'','state':'open',
            'base':{'sha':'base'},'head':{'sha':'head'}}
        diff='diff --git a/flow.tfl b/flow.tfl\nBinary files a/flow.tfl and b/flow.tfl differ\n'
        def api(path,**kwargs):
            if kwargs.get('diff'):return diff
            if '/files?' in path:return [{'filename':'flow.tfl','sha':'blob','status':'added'}]
            return []
        with patch.object(review,'api',side_effect=api):
            with self.assertRaises(review.Stop):review.collect('repos/team/repo/pulls/1',pr)
            packet=review.collect('repos/team/repo/pulls/1',pr,native=True)
        self.assertTrue(packet['file_manifest'][0]['artifact_required'])

    def test_missing_or_truncated_diff_requires_all_pinned_files(self):
        spec=importlib.util.spec_from_file_location('review_fallback',Path(__file__).resolve().parents[1]/'scripts/github-review.py')
        review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)
        pr={'changed_files':1,'additions':1,'deletions':1,'title':'Update','body':'','state':'open',
            'base':{'sha':'base'},'head':{'sha':'head'}}
        files=[{'filename':'query.sql','status':'modified','patch':'@@ -1 +1 @@\n-old\n+new'}]
        for response in (review.Stop('GitHub timeout'),'', 'diff --git a/query.sql b/query.sql\n@@ -1 +1 @@\n+new'):
            def api(path,**kwargs):
                if kwargs.get('diff'):
                    if isinstance(response,Exception):raise response
                    return response
                return files if '/files?' in path else []
            with self.subTest(response=str(response)),patch.object(review,'api',side_effect=api):
                packet=review.collect('repos/team/repo/pulls/1',pr,native=True)
                self.assertIsNone(packet['full_diff'])
                self.assertTrue(packet['file_manifest'][0]['artifact_required'])
                self.assertEqual(packet['diff_retrieval']['status'],'read_changed_blobs')

    def test_removed_and_renamed_files_use_existing_pinned_paths(self):
        reader,t,pr,packet=self.setup_review(b'old',status='removed')
        reader.inspect(t,'flow.tfl',side='base')
        self.assertEqual(reader.missing(t),[])
        with self.assertRaises(ValueError):reader.inspect(t,'flow.tfl')
        packet['file_manifest'][0].update(status='renamed',previous_filename='old flow.tfl')
        reader.register(t,pr,packet)
        original=reader.api
        paths=[]
        def api(path):
            paths.append(path)
            return original(path)
        reader.api=api
        reader.inspect(t,'flow.tfl',side='base')
        reader.inspect(t,'flow.tfl')
        self.assertTrue(any('/contents/old%20flow.tfl?ref=base' in path for path in paths))
        self.assertEqual(reader.missing(t),[])


if __name__=='__main__':unittest.main()
