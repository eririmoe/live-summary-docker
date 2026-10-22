import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = tempfile.TemporaryDirectory()
os.environ['LIVE_SUMMARY_STATE'] = ROOT.name+'/state'
os.environ['LIVE_SUMMARY_RECORDS'] = ROOT.name+'/records'
import core
import providers
import pipeline
import cleanup


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.state_patch = patch.object(core, 'STATE', Path(ROOT.name)/'state')
        self.records_patch = patch.object(core, 'RECORDS', Path(ROOT.name)/'records')
        self.pipeline_state_patch = patch.object(pipeline, 'STATE', Path(ROOT.name)/'state')
        self.pipeline_records_patch = patch.object(pipeline, 'RECORDS', Path(ROOT.name)/'records')
        self.provider_state_patch = patch.object(providers, 'STATE', Path(ROOT.name)/'state')
        self.patches = [self.state_patch,self.records_patch,self.pipeline_state_patch,self.pipeline_records_patch,self.provider_state_patch]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        core.STATE.mkdir(exist_ok=True)
        self.relative = core.settings()['record_dir']+'/录制-123456789-20261015-120053-031-示例午间直播.flv'
        path = core.RECORDS/self.relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'closed-video')
        for name in ('state.sqlite3','state.sqlite3-wal','state.sqlite3-shm'):
            (core.STATE/name).unlink(missing_ok=True)
        core.init_db()

    def test_path_traversal_and_symlinks_are_rejected(self):
        outside = Path(ROOT.name)/'outside.flv'
        outside.write_bytes(b'outside')
        link = (core.RECORDS/self.relative).with_name('link.flv')
        link.unlink(missing_ok=True)
        link.symlink_to(outside)
        for name in ('../outside.flv',link.relative_to(core.RECORDS).as_posix()):
            with self.assertRaises((ValueError,FileNotFoundError)):
                core.source_path(name)

    def test_site_catalogue_keeps_search_data_and_content_addressed_full_notes(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(pipeline,'STATE',Path(folder)):
            entry={'id':'test-live','title':'完整直播','started_at':'2026-10-13T12:00:00+08:00',
                'duration_seconds':60,'danmaku_count':1,'generated_at':'2026-10-17T00:00:00Z',
                'summary':{'overview':'概要','topics':['完整话题'],'timeline':[],
                    'highlights':[],'tags':['杂谈'],'caveats':[],
                    'recap_sections':[{'start_seconds':0,'end_seconds':60,'title':'完整经过','paragraphs':['原文'*1000]}]}}
            core.atomic_json(Path(folder)/'public-summaries/test-live.json',entry)
            archive=pipeline.build_site()
            index=core.read_json(Path(folder)/'site/data/index.json')
            preview=index['entries'][0]
            self.assertEqual(index['schema_version'],2)
            self.assertEqual(preview['summary']['topics'],['完整话题'])
            self.assertNotIn('recap_sections',preview['summary'])
            detail=core.read_json(Path(folder)/'site'/preview['detail_url'].lstrip('/'))
            self.assertEqual(detail,entry)
            self.assertEqual(archive['entries'],[entry])
            self.assertLess(len(json.dumps(index)),len(json.dumps(archive)))
            first_url=preview['detail_url']
            entry['summary']['overview']='更新过的概要'
            core.atomic_json(Path(folder)/'public-summaries/test-live.json',entry)
            pipeline.build_site()
            second=core.read_json(Path(folder)/'site/data/index.json')['entries'][0]
            self.assertNotEqual(second['detail_url'],first_url)
            self.assertTrue((Path(folder)/'site'/first_url.lstrip('/')).exists())

    def test_duplicate_events_and_jobs_are_idempotent(self):
        event = {'EventId':'event-one','EventType':'FileClosed','EventData':{'RoomId':123456789,'SessionId':'session-one','RelativePath':self.relative}}
        self.assertTrue(core.event_store(event))
        self.assertFalse(core.event_store(event))
        first = core.enqueue([self.relative])
        self.assertEqual(first,core.enqueue([self.relative]))
        self.assertEqual(len(core.job_rows()),1)

    def test_other_rooms_are_ignored(self):
        self.assertFalse(core.event_store({'EventId':'foreign','EventType':'StreamEnded','EventData':{'RoomId':123}}))

    def test_modified_recording_invalidates_checkpoint_identity(self):
        source = core.identify(self.relative)
        (core.RECORDS/self.relative).write_bytes(b'changed-video')
        self.assertFalse(core.unchanged(source))

    def test_baidu_exit_zero_api_failure_is_not_upload_success(self):
        with patch('pipeline.baidu_command',return_value='错误 31045 user not exists'):
            with self.assertRaises(providers.ProviderError):
                pipeline.remote_size('/backup.flv')
        with patch('pipeline.baidu_command',return_value='文件大小          4802199527, 4.472397GB'):
            self.assertEqual(pipeline.remote_size('/backup.flv'),4802199527)

    def test_ambiguous_asr_submission_is_not_resubmitted(self):
        checkpoint = core.STATE/'uncertain.json'
        checkpoint.unlink(missing_ok=True)
        with patch('providers.require',return_value='test-key'), patch('providers.temp_upload',return_value='oss://test'), patch('providers.request_json',side_effect=providers.ProviderError('network_error_POST')) as request:
            with self.assertRaises(providers.ProviderError):
                providers.transcribe('/unused.mp3',checkpoint)
            with self.assertRaisesRegex(providers.ProviderError,'submission_uncertain'):
                providers.transcribe('/unused.mp3',checkpoint)
            self.assertEqual(request.call_count,1)

    def test_asr_subtask_failure_is_not_accepted_as_success(self):
        checkpoint = core.STATE/'failed-task.json'
        core.atomic_json(checkpoint,{'task_id':'known-task'})
        result = {'output':{'task_status':'SUCCEEDED','results':[{'subtask_status':'FAILED'}]}}
        with patch('providers.require',return_value='test-key'),patch('providers.request_json',return_value=result):
            with self.assertRaisesRegex(providers.ProviderError,'subtask_failed'):
                providers.transcribe('/unused.mp3',checkpoint)

    def test_summary_rejects_invented_out_of_range_time(self):
        summary = {'overview':'概要','topics':[],'timeline':[{'start_seconds':5,'end_seconds':600,'title':'标题','description':'内容','source':'speech'}],'highlights':[],'tags':[],'caveats':[]}
        with self.assertRaisesRegex(providers.ProviderError,'timeline_out_of_bounds'):
            pipeline.validate_summary(summary,60)

    def test_summary_content_has_no_arbitrary_item_or_character_caps(self):
        item={'start_seconds':1,'end_seconds':2,'title':'标题'*100,'description':'经过'*1000,'source':'speech'}
        value={'overview':'概要'*2000,'topics':['话题'+str(i) for i in range(35)],
            'timeline':[dict(item) for _ in range(100)],'highlights':['看点'*300]*25,
            'tags':['标签'*20]*12,'caveats':['说明'*300]*25,
            'rewatch_segments':[dict(item) for _ in range(8)],
            'danmaku_hotspots':[{k:v for k,v in {**item,'danmaku_count':3}.items() if k!='source'} for _ in range(8)],
            'recap_sections':[{'start_seconds':1,'end_seconds':2,'title':'章节',
                'paragraphs':['完整经过'*1000]*40} for _ in range(130)]}
        self.assertIs(pipeline.validate_summary(value,10),value)

    def test_rejected_summary_is_preserved_and_receives_one_corrective_request(self):
        value={'overview':'概要','topics':[],'timeline':[{'start_seconds':1,'end_seconds':999,
            'title':'标题','description':'说明','source':'speech'}],'highlights':[],'tags':[],'caveats':[]}
        with tempfile.TemporaryDirectory() as folder,patch('providers.summarize',return_value=value) as request:
            directory=Path(folder)
            for _ in range(2):
                with self.assertRaisesRegex(providers.ProviderError,'timeline_out_of_bounds'):
                    pipeline.make_summary({},[{'start_seconds':0,'end_seconds':5,'text':'内容'}],{'buckets':[]},10,directory)
            self.assertEqual(request.call_count,2)
            self.assertEqual(core.read_json(directory/'candidate-summary-window-000.json'),value)
            self.assertEqual(core.read_json(directory/'candidate-summary-window-000.repair-001.json'),value)

    def test_no_words_task_is_empty_and_never_resubmitted(self):
        checkpoint=core.STATE/'no-words-task.json'
        for status in ('FAILED','SUCCEEDED'):
            core.atomic_json(checkpoint,{'task_id':'existing-task'})
            output={'task_status':status,'results':[{'subtask_status':'FAILED','code':'ASR_RESPONSE_HAVE_NO_WORDS'}]}
            with patch('providers.require',return_value='test-key'),\
                    patch('providers.request_json',return_value={'output':output}) as request,\
                    patch('providers.temp_upload',side_effect=AssertionError('must not upload')):
                result=providers.transcribe('/unused.mp3',checkpoint)
                self.assertEqual(result['empty_reason'],'ASR_RESPONSE_HAVE_NO_WORDS')
                self.assertEqual(pipeline.normalized_transcript(result,0),[])
                self.assertEqual(providers.transcribe('/unused.mp3',checkpoint),result)
                self.assertEqual(request.call_count,1)
                self.assertEqual(core.read_json(checkpoint)['task_id'],'existing-task')

    def test_other_failed_task_remains_failure_with_saved_error_code(self):
        checkpoint=core.STATE/'other-failure.json'
        core.atomic_json(checkpoint,{'task_id':'existing-task'})
        with patch('providers.require',return_value='test-key'),\
                patch('providers.request_json',return_value={'output':{'task_status':'FAILED','code':'FILE_DOWNLOAD_FAILED'}}):
            with self.assertRaisesRegex(providers.ProviderError,'asr_task_failed'):
                providers.transcribe('/unused.mp3',checkpoint)
        self.assertEqual(core.read_json(checkpoint)['provider_error_code'],'FILE_DOWNLOAD_FAILED')
        self.assertNotIn('result',core.read_json(checkpoint))

    def test_danmaku_is_bounded_and_only_uses_message_text(self):
        xml = core.STATE/'comments.xml'
        xml.write_text('<i><d p="2,1,1,1,1,0,private-user,1">你好</d><d p="65,1">不在样本内</d></i>')
        value = pipeline.danmaku_digest(xml,10,max_seconds=60)
        self.assertEqual(value['count'],1)
        self.assertNotIn('private-user',json.dumps(value))
        self.assertEqual(value['buckets'][0]['messages'][0]['text'],'你好')

    def test_first_minute_danmaku_overlaps_speech_start_after_zero(self):
        buckets = [{'start_seconds':0,'count':27},{'start_seconds':60,'count':5}]
        self.assertEqual(pipeline.select_danmaku_buckets(buckets,0.2,59.4),[buckets[0]])
        self.assertEqual(pipeline.select_danmaku_buckets(buckets,60,90),[buckets[1]])

    def test_comment_dedup_requires_identical_stable_event_identity(self):
        xml=core.STATE/'overlap-comments.xml'
        seen=set()
        xml.write_text('<i><d p="59,1,1,1,100,0,user,event1">same</d></i>')
        self.assertEqual(pipeline.danmaku_digest(xml,0,seen_messages=seen)['count'],1)
        xml.write_text('<i><d p="1,1,1,1,100,0,user,event1">same</d>'
            '<d p="1,1,1,1,101,0,user,event2">same</d>'
            '<d p="1,1">same</d><d p="1,1">same</d></i>')
        self.assertEqual(pipeline.danmaku_digest(xml,54.5,seen_messages=seen)['count'],3)

    def test_cleanup_requires_completed_verified_outputs(self):
        job_id = core.enqueue([self.relative])
        core.update_job(job_id,status='done')
        self.assertEqual(cleanup.candidates(),[])
        self.assertTrue((core.RECORDS/self.relative).exists())

    def test_retention_accepts_publication_before_backup_but_requires_both(self):
        job_id=core.enqueue([self.relative])
        core.update_job(job_id,status='done')
        directory=core.STATE/'jobs'/job_id
        core.atomic_json(directory/'transcript.json',{})
        core.atomic_json(directory/'summary.json',{})
        source=core.identify(self.relative)
        core.atomic_json(directory/'published.json',{'verified_at':10})
        with patch('cleanup.core.settings',return_value={**core.settings(),'keep_newest_flv':0,'minimum_age_days':0}):
            self.assertEqual(cleanup.candidates(),[])
            receipt={'verified_at':20,'method':'remote_exact_bytes',
                'files':[{'relative':self.relative,'size':source['size']}]}
            core.atomic_json(directory/'backup.json',receipt)
            self.assertEqual(cleanup.candidates(),[source])
            receipt.pop('verified_at')
            core.atomic_json(directory/'backup.json',receipt)
            self.assertEqual(cleanup.candidates(),[])


if __name__=='__main__':
    unittest.main(verbosity=2)
