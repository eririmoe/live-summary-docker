import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import core,providers,asr_context


class ASRContextTests(unittest.TestCase):
    def test_synthetic_profile_fits_provider_context(self):
        cfg={**core.settings(),'asr_context_enabled':True}
        options=asr_context.enhancement(cfg)
        self.assertEqual(options['profile']['term_count'],5)
        self.assertLessEqual(options['profile']['context_characters'],400)
        for word in ('示例主播','语音识别','示例术语','示例爸爸','示例妈妈'):
            self.assertIn(word,options['parameters']['vocabulary'])
        profile=core.read_json(core.APP/'asr-vocabulary.json')
        self.assertTrue(all(t.get('confirmed_by_user') for t in profile['terms']))
        self.assertEqual(options['profile']['sha256'],asr_context.enhancement(cfg)['profile']['sha256'])

    def test_new_submission_sends_context_and_vocabulary_and_freezes_profile(self):
        cfg={**core.settings(),'asr_context_enabled':True}
        expected=asr_context.enhancement(cfg)
        responses=[{'output':{'task_id':'new'}},
            {'output':{'task_status':'SUCCEEDED','results':[{'subtask_status':'SUCCEEDED','transcription_url':'https://result.test'}]}},
            {'transcripts':[{'text':'示例术语'}]}]
        with tempfile.TemporaryDirectory() as folder,patch('providers.settings',return_value=cfg),\
            patch('providers.require',return_value='test'),patch('providers.temp_upload',return_value='oss://test'),\
            patch('providers.request_json',side_effect=responses) as request:
            checkpoint=Path(folder)/'asr.json'
            result=providers.transcribe('/unused.mp3',checkpoint)
            payload=request.call_args_list[0].kwargs['json']
            self.assertEqual(payload['input'],{'file_urls':['oss://test'],**expected['input']})
            self.assertEqual(payload['parameters']['vocabulary'],expected['parameters']['vocabulary'])
            self.assertEqual(core.read_json(checkpoint)['recognition_profile'],expected['profile'])
            with patch('asr_context.enhancement',side_effect=AssertionError('must not modify submitted task')):
                self.assertEqual(providers.transcribe('/gone.mp3',checkpoint),result)
            self.assertEqual(request.call_count,3)

    def test_existing_task_is_polled_without_adding_or_changing_context(self):
        responses=[{'output':{'task_status':'SUCCEEDED','results':[{'subtask_status':'SUCCEEDED','transcription_url':'https://result.test'}]}},
                   {'transcripts':[{'text':'原始内容'}]}]
        with tempfile.TemporaryDirectory() as folder,patch('providers.require',return_value='test'),\
            patch('asr_context.enhancement',side_effect=AssertionError('already submitted')),\
            patch('providers.temp_upload',side_effect=AssertionError('no reupload')),\
            patch('providers.request_json',side_effect=responses) as request:
            checkpoint=Path(folder)/'asr.json'
            core.atomic_json(checkpoint,{'task_id':'existing','recognition_profile':{'version':'old'}})
            providers.transcribe('/gone.mp3',checkpoint)
            self.assertTrue(all(call.args[0]=='GET' for call in request.call_args_list))
            self.assertEqual(core.read_json(checkpoint)['recognition_profile'],{'version':'old'})

    def test_invalid_profile_is_rejected_before_audio_upload(self):
        cfg={**core.settings(),'asr_context_enabled':True}
        with tempfile.TemporaryDirectory() as folder,patch('providers.settings',return_value=cfg),\
            patch('providers.require',return_value='test'),patch('asr_context.enhancement',side_effect=ValueError('context_exceeds_provider_limit')),\
            patch('providers.temp_upload') as upload,patch('providers.request_json') as request:
            with self.assertRaisesRegex(providers.ProviderError,'context_exceeds_provider_limit'):
                providers.transcribe('/unused.mp3',Path(folder)/'asr.json')
            upload.assert_not_called();request.assert_not_called()

    def test_no_silent_truncation_or_duplicate_terms(self):
        cfg={**core.settings(),'asr_context_enabled':True}
        profile=core.read_json(core.APP/'asr-vocabulary.json')
        for mutation in ('long_context','duplicate','bad_weight'):
            value=copy.deepcopy(profile)
            if mutation=='long_context':
                value['context_prefix']='过长'*300
            elif mutation=='duplicate':
                value['terms'].append(value['terms'][0])
            else:
                value['terms'][0]['weight']=50
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'profile.json';core.atomic_json(path,value)
                with self.assertRaises(ValueError):
                    asr_context.enhancement(cfg,path)

    def test_disabled_profile_keeps_original_request_options(self):
        options=asr_context.enhancement({**core.settings(),'asr_context_enabled':False})
        self.assertEqual(options['input'],{})
        self.assertEqual(options['parameters'],{})
