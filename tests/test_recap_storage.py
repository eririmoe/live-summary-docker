import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import core
import recap
import storage
import providers


class RecapTests(unittest.TestCase):
    def test_longer_recap_preserves_all_paragraphs(self):
        transcript={'metadata':{},'duration_seconds':20,'segments':[{'start_seconds':1,'end_seconds':10,'text':'故事'}]}
        paragraphs=['细节'+str(n)+'经过'*1600 for n in range(40)]
        with tempfile.TemporaryDirectory() as folder,patch('providers.summarize',return_value={'title':'详细内容','paragraphs':paragraphs}):
            result=recap.generate(transcript,{'buckets':[]},Path(folder))
            self.assertEqual(result['recap_sections'][0]['paragraphs'],paragraphs)

    def test_completed_malformed_json_is_repaired_once(self):
        response=lambda content:{'choices':[{'finish_reason':'stop','message':{'content':content}}]}
        with patch('providers.require',return_value='test'),patch('providers.request_json',side_effect=[response('{"title":"broken" "paragraphs":[]}'),response('{"title":"fixed","paragraphs":["content"]}')]) as request:
            self.assertEqual(providers.summarize({},'system')['title'],'fixed')
            self.assertEqual(request.call_count,2)

    def test_fenced_json_with_unescaped_newline_is_normalised(self):
        value={'choices':[{'finish_reason':'stop','message':{'content':'```json\n{"text":"line1\nline2"}\n```'}}]}
        with patch('providers.require',return_value='test'),patch('providers.request_json',return_value=value) as request:
            self.assertEqual(providers.summarize({},'system')['text'],'line1\nline2')
            self.assertEqual(request.call_count,1)

    def test_all_sentences_covered_and_cached_without_repeating_calls(self):
        transcript={'metadata':{},'duration_seconds':1820,'segments':[
            {'start_seconds':start,'end_seconds':start+5,'text':'sentence-'+str(start)} for start in (1,901,1811)]}
        with tempfile.TemporaryDirectory() as folder,patch('providers.summarize',return_value={'title':'详细章节','paragraphs':['具体事件经过。']}) as model:
            first=recap.generate(transcript,{'buckets':[]},Path(folder))
            second=recap.generate(transcript,{'buckets':[]},Path(folder))
            self.assertEqual(first,second)
            self.assertEqual(model.call_count,3)
            covered=[s['text'] for call in model.call_args_list for s in call.args[0]['speech']]
            self.assertEqual(set(covered),{s['text'] for s in transcript['segments']})
            self.assertTrue(all(0<=s['start_seconds']<=s['end_seconds']<=1820 for s in first['recap_sections']))

    def test_model_cannot_inject_arbitrary_chapter_times(self):
        transcript={'metadata':{},'duration_seconds':20,'segments':[{'start_seconds':1,'end_seconds':10,'text':'故事'}]}
        with tempfile.TemporaryDirectory() as folder,patch('providers.summarize',return_value={'title':'故事','paragraphs':['细节'],'start_seconds':999}):
            result=recap.generate(transcript,{'buckets':[]},Path(folder))
            self.assertEqual(result['recap_sections'][0]['start_seconds'],1)
            self.assertEqual(result['recap_sections'][0]['end_seconds'],10)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patch=patch.object(core,'STATE',self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.cfg={'room_id':123456789,'transcript_archive':{'backend':'r2','account_id':'a'*32,'bucket':'private-transcripts','prefix':'transcripts'}}
        core.atomic_json(self.root/'r2-secrets.json',{'R2_ACCESS_KEY_ID':'test-access','R2_SECRET_ACCESS_KEY':'test-secret'})
        self.directory=self.root/'job'
        self.directory.mkdir()
        (self.directory/'transcript.json').write_text('{"segments":[]}',encoding='utf-8')
        (self.directory/'transcript.txt').write_text('私有转写',encoding='utf-8')

    def test_upload_verifies_full_readback_and_second_call_skips_put(self):
        objects={}
        operations=[]
        def request(method,key,data=b'',content_type=None):
            operations.append(method)
            if method=='PUT':
                objects[key]=data
                return b''
            return objects[key]
        with patch('core.settings',return_value=self.cfg),patch('storage.signed_request',side_effect=request):
            receipt=storage.archive(self.directory,'b'*24,{'started_at':'2026-10-15T12:00:00+08:00'})
            self.assertEqual(len(receipt['objects']),3)
            self.assertEqual(operations.count('PUT'),3)
            storage.archive(self.directory,'b'*24,{'started_at':'2026-10-15T12:00:00+08:00'})
            self.assertEqual(operations.count('PUT'),3)
            self.assertEqual(operations.count('GET'),6)
            self.assertNotIn('test-secret',json.dumps(receipt))

    def test_corrupt_readback_never_creates_success_receipt(self):
        with patch('core.settings',return_value=self.cfg),patch('storage.signed_request',return_value=b'corrupt'):
            with self.assertRaisesRegex(providers.ProviderError,'checksum_mismatch'):
                storage.archive(self.directory,'b'*24,{'started_at':'2026-10-15T12:00:00+08:00'})
        self.assertFalse((self.directory/'transcript-archive.json').exists())

    def test_sigv4_uses_only_cloudflare_https_and_no_redirects(self):
        with patch('core.settings',return_value=self.cfg),patch('storage.requests.request',return_value=SimpleNamespace(status_code=200,content=b'ok')) as request:
            self.assertEqual(storage.signed_request('GET','transcripts/中文.json'),b'ok')
            args,kwargs=request.call_args
            self.assertTrue(args[1].startswith('https://'+'a'*32+'.r2.cloudflarestorage.com/private-transcripts/'))
            self.assertIn('%E4%B8%AD',args[1])
            self.assertIn('AWS4-HMAC-SHA256',kwargs['headers']['Authorization'])
            self.assertNotIn('test-secret',kwargs['headers']['Authorization'])
            self.assertFalse(kwargs['allow_redirects'])

    def test_private_download_url_has_short_expiry_and_never_contains_secret(self):
        from urllib.parse import urlparse,parse_qs
        key='transcripts/123456789/2026/10/13/'+'b'*24+'/transcript.txt'
        with patch('core.settings',return_value=self.cfg):
            url=storage.presigned_read_url(key)
            head=storage.presigned_read_url(key,method='HEAD')
        parsed=urlparse(url);query=parse_qs(parsed.query)
        self.assertEqual(parsed.scheme,'https')
        self.assertEqual(parsed.hostname,'a'*32+'.r2.cloudflarestorage.com')
        self.assertEqual(query['X-Amz-Expires'],['300'])
        self.assertEqual(query['X-Amz-SignedHeaders'],['host'])
        self.assertNotIn('test-secret',url)
        self.assertNotEqual(parse_qs(urlparse(head).query)['X-Amz-Signature'],query['X-Amz-Signature'])

    def test_private_download_rejects_other_objects_write_methods_and_long_expiry(self):
        key='transcripts/123456789/2026/10/13/'+'b'*24+'/transcript.json'
        with patch('core.settings',return_value=self.cfg):
            for kwargs in ({'key':'status/auth/secret'},{'key':key,'method':'PUT'},
                           {'key':key,'expires':3600},{'key':key.replace('/123456789/','/123/')},
                           {'key':key.replace('transcript.json','manifest.json')}):
                with self.assertRaises(providers.ProviderError):
                    storage.presigned_read_url(**kwargs)
