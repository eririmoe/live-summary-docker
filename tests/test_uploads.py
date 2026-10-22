import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from email import policy
from email.parser import BytesParser
import requests
import providers


class UploadTests(unittest.TestCase):
    def test_multipart_stream_has_exact_length_policy_fields_and_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'audio.mp3';content=b'full-audio'*20000;path.write_bytes(content)
            body=providers.MultipartAudio(path,[('policy',(None,'signed-policy')),('key',(None,'tmp/private-object'))])
            request=requests.Request('POST','https://example.invalid',data=body,
                headers={'Content-Type':'multipart/form-data; boundary='+body.boundary}).prepare()
            chunks=list(body)
            self.assertTrue(all(len(chunk)<=64*1024 for chunk in chunks))
            encoded=b''.join(chunks)
            self.assertEqual(int(request.headers['Content-Length']),len(encoded))
            self.assertNotIn('Transfer-Encoding',request.headers)
            message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+request.headers['Content-Type']+'\r\n\r\n').encode()+encoded)
            parts={p.get_param('name',header='content-disposition'):p for p in message.iter_parts()}
            self.assertEqual(parts['policy'].get_payload(decode=True),b'signed-policy')
            self.assertEqual(parts['key'].get_payload(decode=True),b'tmp/private-object')
            self.assertEqual(parts['file'].get_payload(decode=True),content)

    def test_missing_assets_are_sent_once_in_bounded_batches(self):
        assets={str(i):{'key':str(i),'value':'a'*(400000 if i!=3 else 1500000),'base64':True} for i in range(7)}
        missing=['0','2','3','4','6']
        with patch('providers.request_json',return_value={'success':True}) as request:
            providers.upload_page_assets(assets,missing,'private-test-key')
        groups=[call.kwargs['json'] for call in request.call_args_list]
        self.assertEqual([item['key'] for group in groups for item in group],missing)
        for group in groups:
            self.assertTrue(len(group)==1 or sum(len(json.dumps(a).encode()) for a in group)<=1024**2)
        self.assertTrue(all(call.kwargs['timeout']==(120,180) for call in request.call_args_list))

    def test_audio_upload_extends_send_timeout_without_submitting_asr(self):
        policy={'upload_dir':'temporary','oss_access_key_id':'test','signature':'test','policy':'test',
            'x_oss_object_acl':'private','x_oss_forbid_overwrite':'true','upload_host':'https://example.invalid'}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'audio.mp3';path.write_bytes(b'audio')
            with patch('providers.require',return_value='test'),\
                 patch('providers.request_json',return_value={'data':policy}) as api,\
                 patch('providers.requests.post',return_value=SimpleNamespace(status_code=200)) as upload:
                self.assertTrue(providers.temp_upload(path).startswith('oss://temporary/'))
            self.assertEqual(upload.call_args.kwargs['timeout'],(120,300))
            self.assertEqual(api.call_count,1)
            self.assertEqual(api.call_args.args[0],'GET')
