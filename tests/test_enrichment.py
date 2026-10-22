import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import ValidationError
import core
import enrichment
import pipeline
import providers


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.transcript={'metadata':{'title':'测试'},'duration_seconds':250,
            'segments':[{'start_seconds':12.4,'end_seconds':29.7,'text':'开场测试'},
                        {'start_seconds':75.2,'end_seconds':89.1,'text':'一个有趣的故事'}]}
        self.summary={'overview':'概要','topics':[],'timeline':[{'start_seconds':70,'end_seconds':90,
            'title':'故事','description':'讲故事','source':'speech'}],'highlights':[],'tags':[],'caveats':[]}
        self.danmaku={'count':14,'missing':False,'buckets':[
            {'start_seconds':0,'count':2,'messages':[{'text':'听得见','count':2}]},
            {'start_seconds':60,'count':5,'messages':[{'text':'哈哈','count':5}]},
            {'start_seconds':240,'count':7,'messages':[{'text':'再见','count':7}]}]}

    def test_hotspot_counts_are_measured_and_end_is_bounded(self):
        result=enrichment.hotspot_candidates(self.danmaku,self.transcript['segments'],250)
        self.assertTrue(result)
        for candidate in result:
            expected=sum(b['count'] for b in self.danmaku['buckets'] if candidate['start_seconds']<=b['start_seconds']<candidate['end_seconds'])
            self.assertEqual(candidate['danmaku_count'],expected)
            self.assertLessEqual(candidate['end_seconds'],250)

    def test_rewatch_snaps_to_existing_speech_and_rejects_danmaku_only(self):
        self.summary['timeline'].append({**self.summary['timeline'][0],'source':'danmaku'})
        result=enrichment.rewatch_candidates(self.summary,self.transcript['segments'],250)
        self.assertEqual(len(result),1)
        self.assertEqual((result[0]['start_seconds'],result[0]['end_seconds']),(75,90))

    def test_model_cannot_invent_times_ids_or_counts(self):
        hotspots=enrichment.hotspot_candidates(self.danmaku,self.transcript['segments'],250)
        rewatch=enrichment.rewatch_candidates(self.summary,self.transcript['segments'],250)
        choice={'candidate_id':'unknown','title':'标题','description':'说明'}
        with self.assertRaises(providers.ProviderError):
            enrichment.resolve_selection({'hotspots':[choice],'rewatch':[]},hotspots,rewatch)
        choice['candidate_id']=hotspots[0]['candidate_id']
        with self.assertRaises(providers.ProviderError):
            enrichment.resolve_selection({'hotspots':[choice,choice],'rewatch':[]},hotspots,rewatch)
        choice['start_seconds']=999
        with self.assertRaises(ValidationError):
            enrichment.resolve_selection({'hotspots':[choice],'rewatch':[]},hotspots,rewatch)

    def test_cached_enrichment_does_not_repeat_model_request(self):
        selection={'hotspots':[{'candidate_id':'h0','title':'热点','description':'弹幕讨论'}],
                   'rewatch':[{'candidate_id':'r0','title':'故事','description':'完整故事'}]}
        with tempfile.TemporaryDirectory() as folder, patch('providers.summarize',return_value=selection) as summarize:
            first=enrichment.enrich(self.summary,self.transcript,self.danmaku,Path(folder))
            second=enrichment.enrich({**self.summary,**first},self.transcript,self.danmaku,Path(folder))
            self.assertEqual(first,second)
            self.assertEqual(summarize.call_count,1)

    def test_new_time_ranges_use_same_duration_guard(self):
        for key in ('danmaku_hotspots','rewatch_segments'):
            item={'start_seconds':200,'end_seconds':999,'title':'标题','description':'说明'}
            item.update({'danmaku_count':3} if key=='danmaku_hotspots' else {'source':'speech'})
            with self.assertRaises(providers.ProviderError):
                pipeline.validate_summary({**self.summary,key:[item]},250)

    def test_all_distinct_hotspots_and_full_speech_are_available(self):
        segments=[{'start_seconds':0,'end_seconds':2000,'text':'完整内容'*1000}]
        danmaku={'buckets':[{'start_seconds':i*240,'count':i+1,'messages':[]} for i in range(8)]}
        result=enrichment.hotspot_candidates(danmaku,segments,2000)
        self.assertEqual(len(result),8)
        self.assertTrue(all(c['speech']==segments[0]['text'] for c in result))
        summary={'timeline':[{'start_seconds':0,'end_seconds':1000,'source':'speech'}]}
        clips=enrichment.rewatch_candidates(summary,segments,2000)
        self.assertEqual(len(clips),1)
        self.assertEqual(clips[0]['end_seconds'],2000)

    def test_more_than_six_rewatch_choices_and_long_descriptions_are_valid(self):
        candidates=[{'candidate_id':'r'+str(i),'start_seconds':i,'end_seconds':i+1,'source':'speech'} for i in range(9)]
        selection={'hotspots':[],'rewatch':[{'candidate_id':c['candidate_id'],'title':'标题','description':'说明'*500} for c in candidates]}
        self.assertEqual(len(enrichment.resolve_selection(selection,[],candidates)['rewatch_segments']),9)

    def test_large_candidate_evidence_is_batched_without_truncation(self):
        candidates=[{'candidate_id':'r'+str(i),'start_seconds':i,'end_seconds':i+1,
            'source':'speech','speech':'完整内容'*6000} for i in range(4)]
        def select(evidence,system):
            return {'hotspots':[],'rewatch':[{'candidate_id':c['candidate_id'],'title':'标题','description':'说明'}
                for c in evidence['rewatch_candidates']]}
        with tempfile.TemporaryDirectory() as folder,patch('enrichment.hotspot_candidates',return_value=[]),\
                patch('enrichment.rewatch_candidates',return_value=candidates),patch('providers.summarize',side_effect=select) as model:
            first=enrichment.enrich(self.summary,self.transcript,self.danmaku,Path(folder))
            self.assertEqual(len(first['rewatch_segments']),4)
            self.assertGreater(model.call_count,1)
            sent=[c for call in model.call_args_list for c in call.args[0]['rewatch_candidates']]
            self.assertEqual(sent,candidates)
            count=model.call_count
            enrichment.enrich(self.summary,self.transcript,self.danmaku,Path(folder))
            self.assertEqual(model.call_count,count)

    def test_backfill_preserves_evidence_and_never_transcribes_again(self):
        selection={'hotspots':[],'rewatch':[{'candidate_id':'r0','title':'故事','description':'值得回看'}]}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            directory=root/'jobs'/'test-job'
            for name,value in [('summary',self.summary),('transcript',self.transcript),('danmaku',self.danmaku),('result',{'summary':self.summary})]:
                core.atomic_json(directory/(name+'.json'),value)
            before=(directory/'transcript.json').read_bytes()
            with patch.object(pipeline,'STATE',root),patch('core.job_rows',return_value=[{'id':'test-job','status':'done'}]),patch('pipeline.build_site'),patch('recap.generate',return_value={'recap_sections':[]}),patch('providers.summarize',return_value=selection),patch('providers.transcribe') as transcribe:
                self.assertEqual(pipeline.enrich_completed(),['test-job'])
                self.assertEqual(pipeline.enrich_completed(),[])
                transcribe.assert_not_called()
            self.assertEqual(before,(directory/'transcript.json').read_bytes())
            self.assertTrue((directory/'summary.before-enrichment.json').exists())
            self.assertTrue(core.read_json(root/'public-summaries/test-job.json')['summary']['rewatch_segments'])
