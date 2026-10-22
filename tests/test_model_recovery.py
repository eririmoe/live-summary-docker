import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,mock_open
from jsonschema import validate,ValidationError
import core,enrichment,model_output,pipeline,providers


class ModelRecoveryTests(unittest.TestCase):
    def test_wrong_kind_cached_ids_get_repaired_and_reused_without_losing_original(self):
        hotspots=[{'candidate_id':'h0','start_seconds':0,'end_seconds':10,'danmaku_count':2}]
        rewatch=[{'candidate_id':'r0','start_seconds':20,'end_seconds':30,'source':'speech'}]
        wrong={'hotspots':[],'rewatch':[{'candidate_id':'h0','title':'热点','description':'原始说明'}]}
        correct={'hotspots':[{'candidate_id':'h0','title':'热点','description':'弹幕讨论'}],
                 'rewatch':[{'candidate_id':'r0','title':'故事','description':'完整故事'}]}
        schema=enrichment.selection_schema(hotspots,rewatch)
        def checked(value):
            validate(value,schema)
            enrichment.resolve_selection(value,hotspots,rewatch)
            return value
        with tempfile.TemporaryDirectory() as folder,patch('providers.summarize',return_value=correct) as model:
            path=Path(folder)/'candidate.json'
            core.atomic_json(path,wrong)
            before=path.read_bytes()
            result=model_output.validated_candidate(path,{'evidence':'all'},'system',checked)
            self.assertEqual(result,correct)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(model.call_args.args[0]['previous_response'],wrong)
            self.assertIn('enum',model.call_args.args[0]['validation_error'])
            self.assertEqual(model_output.validated_candidate(path,{},'system',checked),correct)
            self.assertEqual(model.call_count,1)

    def test_invalid_repairs_are_saved_and_next_attempt_advances(self):
        schema={'type':'object','required':['ok'],'properties':{'ok':{'const':True}}}
        def checked(value):
            validate(value,schema)
            return value
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'candidate.json'
            core.atomic_json(path,{'ok':False})
            with patch('providers.summarize',return_value={'ok':False}) as model:
                with self.assertRaises(providers.ProviderError):
                    model_output.validated_candidate(path,{},'system',checked)
                self.assertEqual(model.call_count,1)
                self.assertTrue((Path(folder)/'candidate.repair-001.json').exists())
            with patch('providers.summarize',return_value={'ok':True}) as model:
                self.assertEqual(model_output.validated_candidate(path,{},'system',checked),{'ok':True})
                self.assertTrue((Path(folder)/'candidate.repair-002.json').exists())
                self.assertEqual(model.call_count,1)

    def test_empty_and_cross_batch_ids_remain_invalid(self):
        choice={'candidate_id':'r1','title':'标题','description':'说明'}
        schema=enrichment.selection_schema([], [{'candidate_id':'r0'}])
        for value in ({'hotspots':[choice],'rewatch':[]},{'hotspots':[],'rewatch':[choice]}):
            with self.assertRaises(ValidationError):
                validate(value,schema)

    def test_summary_unrequested_metadata_is_ignored_but_original_preserved(self):
        item={'start_seconds':0,'end_seconds':10,'title':'故事','description':'说明','source':'speech'}
        value={'category':'杂谈','overview':'完整概要','topics':['故事'],'timeline':[{**item,'end_seconds_speech':9.9}],
               'highlights':[],'tags':[],'caveats':[]}
        with tempfile.TemporaryDirectory() as folder,patch('providers.summarize') as model:
            path=Path(folder)/'candidate-summary-window-000.json'
            core.atomic_json(path,value)
            before=path.read_bytes()
            result=pipeline.make_summary({},[{'start_seconds':0,'end_seconds':10,'text':'故事'}],{'buckets':[]},10,Path(folder))
            self.assertEqual(result['timeline'],[item])
            self.assertEqual(path.read_bytes(),before)
            model.assert_not_called()
        value['timeline'][0].pop('end_seconds')
        normalized=model_output.declared_fields(value,core.read_json(core.APP/'summary.schema.json'))
        with self.assertRaises(providers.ProviderError):
            pipeline.validate_summary(normalized,10)

    def test_fifth_failed_attempt_is_terminal(self):
        import app
        class StopWorker(Exception):
            pass
        with tempfile.TemporaryDirectory() as folder,patch.object(core,'STATE',Path(folder)):
            core.init_db()
            with core.database() as db:
                db.execute('INSERT INTO jobs(id,created,updated,status,attempts,payload) VALUES(?,?,?,?,?,?)',
                    ('test',0,0,'retry',4,json.dumps({'files':[]})))
            cfg={**core.settings(),'auto_process':True,'scan_interval':60}
            with patch('core.settings',return_value=cfg),patch('app.readiness',return_value=[]),\
                 patch('pipeline.discover'),patch('pipeline.require_offline'),\
                 patch('pipeline.process_job',side_effect=providers.ProviderError('test_error')),\
                 patch('app.time.sleep',side_effect=StopWorker),patch('app.open',mock_open()),patch('app.fcntl.flock'):
                with self.assertRaises(StopWorker):
                    app.worker()
            row=core.job_rows()[0]
            self.assertEqual((row['status'],row['attempts'],row['error']),('failed',5,'test_error'))
