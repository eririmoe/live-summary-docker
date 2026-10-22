import importlib.util
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import core
import merging
import pipeline
from providers import ProviderError

spec=importlib.util.spec_from_file_location('consolidation',core.APP/'tools/23-consolidate-recordings.py')
consolidation=importlib.util.module_from_spec(spec)
spec.loader.exec_module(consolidation)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)/'state'
        self.records=Path(self.temp.name)/'records'
        self.state.mkdir()
        self.cfg={**core.settings(),'auto_process':True}
        for module in (core,pipeline):
            for key,value in [('STATE',self.state),('RECORDS',self.records)]:
                item=patch.object(module,key,value);item.start();self.addCleanup(item.stop)
        for key,value in [('STATE',self.state),('RECORDS',self.records)]:
            item=patch.object(consolidation,key,value);item.start();self.addCleanup(item.stop)
        core.atomic_json(self.state/'config.json',self.cfg)
        core.init_db()
        disk=patch('merging.shutil.disk_usage',return_value=SimpleNamespace(free=10*1024**3))
        disk.start();self.addCleanup(disk.stop)

    def file(self,date,clock,content=b'FLV'):
        relative=self.cfg['record_dir']+'/录制-123456789-'+date+'-'+clock+'-000-断流测试.flv'
        path=self.records/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(content)
        os.utime(path,(time.time()-3600,time.time()-3600))
        return relative

    def offline_ready(self):
        core.atomic_json(self.state/'offline-state.json',{'offline_since':time.time()-1000,'checked_at':time.time()})

    def test_reconnects_cross_midnight_but_separate_same_day_streams(self):
        paths=[self.file('20261015','235900'),self.file('20261016','000200'),
               self.file('20261016','120000'),self.file('20261016','120300')]
        with patch('merging.media_info',return_value={'duration':120}):
            result=merging.groups(paths,{paths[0]:'old',paths[1]:'new'})
        self.assertEqual([g['paths'] for g in result],[paths[:2],paths[2:]])
        self.assertEqual(result[0]['session_ids'],['old','new'])

    def test_live_and_grace_prevent_early_enqueue_then_group_once(self):
        a=self.file('20261016','100000');b=self.file('20261016','100300')
        with patch('pipeline.recorder_room',return_value={'streaming':True}):
            self.assertEqual(pipeline.discover(),[])
        with patch('pipeline.recorder_room',return_value={}),patch('merging.media_info',return_value={'duration':120}):
            self.assertEqual(pipeline.discover(),[])
            self.offline_ready()
            pipeline.discover();pipeline.discover()
        rows=core.job_rows()
        self.assertEqual(len(rows),1)
        self.assertEqual(len(json.loads(rows[0]['payload'])['files']),2)

    def test_changed_or_pending_layout_is_never_claimed(self):
        a=self.file('20261016','100000')
        self.offline_ready()
        marker=self.state/'jobs'/'a'/'consolidation.pending.json'
        core.atomic_json(marker,{})
        with patch('pipeline.recorder_room',return_value={}),patch('merging.media_info') as probe:
            self.assertEqual(pipeline.discover(),[])
            probe.assert_not_called()
        marker.unlink()
        os.utime(self.records/a,None)
        with patch('pipeline.recorder_room',return_value={}):
            self.assertEqual(pipeline.discover(),[])

    def test_ten_minute_offline_boundary_and_file_stability(self):
        self.cfg['reconnect_grace_seconds']=600
        core.atomic_json(self.state/'config.json',self.cfg)
        relative=self.file('20261016','100000')
        now=time.time()
        os.utime(self.records/relative,(now-600,now-600))
        with patch('pipeline.time.time',return_value=now),patch('pipeline.recorder_room',return_value={}),\
                patch('merging.media_info',return_value={'duration':120}):
            core.atomic_json(self.state/'offline-state.json',{'offline_since':now-599,'checked_at':now})
            self.assertEqual(pipeline.discover(),[])
            core.atomic_json(self.state/'offline-state.json',{'offline_since':now-600,'checked_at':now})
            self.assertEqual(len(pipeline.discover()),1)

    def test_backup_failure_preserves_publication_and_retry_skips_paid_work(self):
        relative=self.file('20261016','100000')
        job_id=core.enqueue([relative])
        payload=json.loads(core.job_rows()[0]['payload'])
        directory=self.state/'jobs'/job_id
        public={'id':job_id,'duration_seconds':60,'summary':{'overview':'概要','topics':[],
            'timeline':[],'highlights':[],'tags':[],'caveats':[]}}
        prepared={'path':self.records/relative,'merged':False}
        def publish(*args,**kwargs):
            self.assertFalse(kwargs['mark_complete'])
            self.assertFalse((directory/'backup.json').exists())
            core.atomic_json(directory/'result.json',public)
            core.atomic_json(directory/'published.json',{'verified_at':time.time()})
            return public
        def backup_failed(*args):
            self.assertTrue((directory/'published.json').exists())
            raise ProviderError('baidu_upload_process_failed')
        with patch('pipeline.require_offline'),patch('merging.prepare',return_value=prepared),\
                patch('pipeline.probe',return_value=60),patch('pipeline.audio_parts',return_value=[]),\
                patch('pipeline.finish_evidence',side_effect=publish) as finish,\
                patch('pipeline.backup_job',side_effect=backup_failed):
            with self.assertRaisesRegex(ProviderError,'baidu_upload_process_failed'):
                pipeline.process_job(job_id,payload)
            finish.assert_called_once()
        self.assertEqual(core.read_json(directory/'stage.json')['stage'],'uploading')
        self.assertTrue((self.records/relative).exists())
        with patch('pipeline.require_offline'),patch('merging.prepare',return_value=prepared),\
                patch('pipeline.audio_parts',side_effect=AssertionError('must not re-extract')),\
                patch('pipeline.finish_evidence',side_effect=AssertionError('must not republish')),\
                patch('providers.transcribe',side_effect=AssertionError('must not resubmit ASR')),\
                patch('pipeline.backup_job') as backup:
            self.assertEqual(pipeline.process_job(job_id,payload),public)
            backup.assert_called_once()
        self.assertEqual(core.read_json(directory/'stage.json')['stage'],'complete')

    def test_publication_failure_does_not_begin_baidu_backup(self):
        relative=self.file('20261016','100000')
        job_id=core.enqueue([relative])
        payload=json.loads(core.job_rows()[0]['payload'])
        with patch('pipeline.require_offline'),\
                patch('merging.prepare',return_value={'path':self.records/relative,'merged':False}),\
                patch('pipeline.probe',return_value=60),patch('pipeline.audio_parts',return_value=[]),\
                patch('pipeline.finish_evidence',side_effect=ProviderError('publish_failed')),\
                patch('pipeline.backup_job') as backup:
            with self.assertRaisesRegex(ProviderError,'publish_failed'):
                pipeline.process_job(job_id,payload)
            backup.assert_not_called()

    def test_header_rule_rejects_geometry_and_audio_changes(self):
        base={'streams':[{'codec_type':'video','codec_name':'h264','width':160,'height':90,'extradata_hash':'a'},
                         {'codec_type':'audio','codec_name':'aac','sample_rate':'48000','extradata_hash':'a'}]}
        headers=json.loads(json.dumps(base));headers['streams'][0]['extradata_hash']='b'
        self.assertTrue(merging.h264_header_change_only([base,headers]))
        geometry=json.loads(json.dumps(headers));geometry['streams'][0]['width']=320
        self.assertFalse(merging.h264_header_change_only([base,geometry]))
        audio=json.loads(json.dumps(headers));audio['streams'][1]['extradata_hash']='b'
        self.assertFalse(merging.h264_header_change_only([base,audio]))

    def test_cross_mount_copy_is_verified_and_conflicts_are_not_overwritten(self):
        source=self.state/'complete.flv';source.write_bytes(b'verified-recording')
        target=self.records/'complete.flv';target.parent.mkdir(parents=True,exist_ok=True)
        original_link=os.link
        def link(a,b):
            if a==str(source):
                raise OSError(errno.EXDEV,'cross mount')
            return original_link(a,b)
        with patch.object(consolidation.os,'link',side_effect=link):
            self.assertEqual(consolidation.install_verified(source,target,'a'*24),'verified_copy')
        self.assertEqual(source.read_bytes(),target.read_bytes())
        self.assertEqual(consolidation.install_verified(source,target,'a'*24),'existing_verified')
        target.write_bytes(b'conflicting-file')
        with self.assertRaises(AssertionError):
            consolidation.install_verified(source,target,'a'*24)
        self.assertEqual(target.read_bytes(),b'conflicting-file')

    def test_cross_mount_copy_checks_free_space_and_preserves_source(self):
        source=self.state/'complete.flv';source.write_bytes(b'verified-recording')
        target=self.records/'complete.flv';target.parent.mkdir(parents=True,exist_ok=True)
        with patch.object(consolidation.os,'link',side_effect=OSError(errno.EXDEV,'cross mount')),\
                patch.object(consolidation.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaises(AssertionError):
                consolidation.install_verified(source,target,'a'*24)
        self.assertTrue(source.exists())
        self.assertFalse(target.exists())

    @unittest.skipUnless(shutil.which('ffmpeg'),'requires real FFmpeg')
    def test_real_h264_header_switch_preserves_coded_content(self):
        files=[self.file('20261016','100000'),self.file('20261016','100003')]
        for index,f in enumerate(files):
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                'color=c='+('red' if index==0 else 'blue')+':s=160x90:r=10',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','2',
                '-c:v','libx264','-preset','veryfast','-profile:v','baseline' if index==0 else 'high',
                '-pix_fmt','yuv420p','-c:a','aac',str(self.records/f)],check=True,timeout=60)
            os.utime(self.records/f,(time.time()-3600,time.time()-3600))
        infos=[merging.media_info(self.records/f) for f in files]
        self.assertNotEqual(infos[0]['streams'],infos[1]['streams'])
        self.assertTrue(merging.h264_header_change_only(infos))
        directory=self.state/'header-job';directory.mkdir()
        result=merging.prepare({'files':[core.identify(f) for f in files]},directory)
        self.assertEqual(result['merge_method'],'ffmpeg_stream_copy')
        self.assertEqual(result['validation'],'duration_and_boundary_decode')
        self.assertEqual(merging.h264_content_evidence([result['path']]),
                         merging.h264_content_evidence([self.records/f for f in files]))
        self.assertTrue(all((self.records/f).exists() for f in files))

    @unittest.skipUnless(shutil.which('ffmpeg'),'requires real FFmpeg')
    def test_audio_timestamp_gap_keeps_the_recording_timeline(self):
        relative=self.file('20261016','100000')
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=s=160x90:r=1',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','12','-c:v','libx264',
            '-preset','ultrafast','-af',r'aselect=not(between(t\,2\,9))','-c:a','aac',
            str(self.records/relative)],check=True,timeout=60)
        length=pipeline.probe(self.records/relative)
        directory=self.state/'gap-audio';directory.mkdir()
        parts=pipeline.audio_parts(self.records/relative,directory,length)
        self.assertEqual(len(parts),1)
        self.assertAlmostEqual(pipeline.probe(parts[0]['path']),length,delta=0.2)
        trimmed=self.state/'trimmed-gap-audio';trimmed.mkdir()
        result=pipeline.audio_parts(self.records/relative,trimmed,length-2,source_offset=2)
        self.assertAlmostEqual(pipeline.probe(result[0]['path']),length-2,delta=0.2)

    def test_existing_paid_asr_is_not_reused_after_audio_offset_changes(self):
        relative=self.file('20261016','100000')
        directory=self.state/'offset-asr';directory.mkdir()
        (directory/'audio-000.mp3').write_bytes(b'old')
        core.atomic_json(directory/'audio-000.json',{'bytes':3,'duration':10,'source_offset':0})
        checkpoint={'task_id':'existing-paid-task'}
        core.atomic_json(directory/'asr-000.json',checkpoint)
        with patch('pipeline.run_media',side_effect=AssertionError('must not replace paid evidence')):
            with self.assertRaisesRegex(ProviderError,'offset_change_requires_asr_review'):
                pipeline.audio_parts(self.records/relative,directory,9,source_offset=1)
        self.assertEqual(core.read_json(directory/'asr-000.json'),checkpoint)

    def test_layout_difference_attempts_copy_instead_of_manual_rejection(self):
        files=[self.file('20261016','100000'),self.file('20261016','100300')]
        directory=self.state/'job';directory.mkdir()
        payload={'files':[core.identify(p) for p in files]}
        with patch('merging.media_info',side_effect=[{'duration':120,'streams':['h264']},{'duration':120,'streams':['hevc']}]):
            with self.assertRaisesRegex(ProviderError,'merge_ffmpeg_failed'):
                merging.prepare(payload,directory)
        self.assertFalse((directory/'merge.json').exists())
        self.assertTrue(all((self.records/p).exists() for p in files))

    @unittest.skipUnless(shutil.which('ffmpeg'),'requires real FFmpeg')
    def test_real_aac_configuration_header_change_is_merged_without_transcoding(self):
        files=[self.file('20261016','100000'),self.file('20261016','100003')]
        for index,f in enumerate(files):
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                'color=c='+('red' if index==0 else 'blue')+':s=160x90:r=10',
                '-f','lavfi','-i','sine=frequency='+str(440+index*220)+':sample_rate=48000',
                '-t','2','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',
                '-c:a','aac','-ac','2','-f','flv',str(self.records/f)],check=True,timeout=30)
        # AAC configuration padding differs, while coded audio stays identical.
        path=self.records/files[1];original=path.read_bytes();out=bytearray(original[:13]);pos=13;changed=False
        while pos+11<=len(original):
            size=int.from_bytes(original[pos+1:pos+4],'big');end=pos+11+size
            header=bytearray(original[pos:pos+11]);body=original[pos+11:end]
            if header[0]==8 and len(body)>2 and body[0]>>4==10 and body[1]==0:
                body+=b'\0\0';header[1:4]=len(body).to_bytes(3,'big');changed=True
            out.extend(header);out.extend(body);out.extend((11+len(body)).to_bytes(4,'big'));pos=end+4
        self.assertTrue(changed);path.write_bytes(out)
        infos=[merging.media_info(self.records/f) for f in files]
        self.assertNotEqual(infos[0]['streams'][1]['extradata_hash'],infos[1]['streams'][1]['extradata_hash'])
        directory=self.state/'aac-header-job';directory.mkdir()
        with patch('merging.packet_evidence',side_effect=AssertionError('no whole-broadcast packet scan')):
            result=merging.prepare({'files':[core.identify(f) for f in files]},directory)
        self.assertEqual(result['merge_method'],'ffmpeg_stream_copy')
        self.assertAlmostEqual(result['duration'],sum(i['duration'] for i in infos),delta=.2)
        self.assertEqual(merging.h264_content_evidence([result['path']]),
            merging.h264_content_evidence([self.records/f for f in files]))
        self.assertTrue(all((self.records/f).exists() for f in files))

    def test_low_disk_stops_before_writing_or_uploading(self):
        files=[self.file('20261016','100000'),self.file('20261016','100300')]
        directory=self.state/'job';directory.mkdir()
        with patch('merging.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ProviderError,'insufficient_free_space'):
                merging.prepare({'files':[core.identify(p) for p in files]},directory)
        self.assertFalse((directory/'merge.json').exists())

    def test_xml_offsets_escape_content_and_clip_lost_intervals(self):
        files=[self.file('20261016','100000'),self.file('20261016','100300')]
        for f in files:
            (self.records/f).with_suffix('.xml').write_text('<i><d p="1,1,25,0,0,0,user,0">A &amp; B</d><d p="100,1">gap</d></i>')
        target=self.state/'merged.xml'
        parts=[{'path':str(self.records/f),'relative':f,'duration':10,'offset':i*10} for i,f in enumerate(files)]
        merging.merge_xml(parts,target,{'room_id':123456789,'title':'x','started_at':'2026-10-16T10:00:00+08:00'})
        digest=pipeline.danmaku_digest(target,0,20)
        self.assertEqual(digest['count'],2)
        nodes=merging.ElementTree.parse(target).findall('d')
        self.assertEqual([n.get('p').split(',')[0] for n in nodes],['1.000','11.000'])
        self.assertEqual(nodes[0].text,'A & B')

    def test_single_upload_never_walks_other_recordings(self):
        a=self.file('20261016','100000');b=self.file('20261016','150000')
        directory=self.state/'job';directory.mkdir()
        with patch('pipeline.require_offline'),patch('pipeline.baidu_command') as command,patch('pipeline.remote_size',return_value=3):
            pipeline.backup_job({'files':[core.identify(a)]},directory)
        self.assertEqual(command.call_count,1)
        self.assertIn(str(self.records/a),command.call_args.args[0])
        self.assertNotIn(str(self.records/b),str(command.call_args))

    def test_failed_merge_never_uploads_or_calls_paid_asr(self):
        files=[self.file('20261016','100000'),self.file('20261016','100300')]
        job_id=core.enqueue(files)
        payload=json.loads(core.job_rows()[0]['payload'])
        with patch('pipeline.require_offline'),patch('merging.prepare',side_effect=ProviderError('merge_failed')),\
                patch('pipeline.backup_job') as upload,patch('providers.transcribe') as asr:
            with self.assertRaisesRegex(ProviderError,'merge_failed'):
                pipeline.process_job(job_id,payload)
            upload.assert_not_called()
            asr.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg'),'requires real FFmpeg')
    def test_real_flv_merge_packet_identity_reuse_and_safe_consolidation(self):
        files=[self.file('20261016','100000'),self.file('20261016','100003')]
        for index,f in enumerate(files):
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                'color=c='+('red' if index==0 else 'blue')+':s=160x90:r=10',
                '-f','lavfi','-i','sine=frequency='+str(440+index*220)+':sample_rate=44100',
                '-t','2','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',
                '-c:a','aac','-output_ts_offset',str(index*5),'-f','flv',str(self.records/f)],check=True,timeout=30)
            os.utime(self.records/f,(time.time()-3600,time.time()-3600))
            (self.records/f).with_suffix('.xml').write_text('<i><d p="1,1">test</d></i>')
        job_id=core.enqueue(files)
        directory=self.state/'jobs'/job_id;directory.mkdir(parents=True)
        payload=json.loads(core.job_rows()[0]['payload'])
        prepared=merging.prepare(payload,directory)
        self.assertAlmostEqual(prepared['duration'],4,delta=0.15)
        with patch('merging.packet_evidence',side_effect=AssertionError('unnecessary rebuild')):
            self.assertEqual(merging.prepare(payload,directory)['sha256'],prepared['sha256'])
        # Full decoding is cheap in this small fixture; production samples joins.
        subprocess.run(['ffmpeg','-nostdin','-v','error','-xerror','-i',str(prepared['path']),
                        '-f','null','-'],check=True,timeout=30)
        row=(job_id,json.dumps(payload))
        # Missing cloud verification must never make the source files disappear.
        with self.assertRaises((AssertionError,KeyError)):
            consolidation.consolidate(row,self.cfg,apply=True)
        self.assertTrue(all((self.records/f).exists() for f in files))
        backup={'verified_at':time.time(),'method':'remote_exact_bytes','merge_sha256':prepared['sha256'],
                'source_fingerprints':prepared['fingerprints'],'files':[
                    {'relative':self.cfg['record_dir']+'/'+p.name,'size':p.stat().st_size}
                    for p in prepared['path'].parent.iterdir()]}
        core.atomic_json(directory/'backup.json',backup)
        # Publication now precedes Baidu backup; cleanup needs both receipts,
        # regardless of their timestamp order.
        core.atomic_json(directory/'published.json',{'verified_at':backup['verified_at']-1})
        core.update_job(job_id,status='done')
        with patch.object(consolidation,'offline',return_value=True):
            self.assertEqual(consolidation.consolidate(row,self.cfg)['mode'],'preview')
            self.assertTrue(all((self.records/f).exists() for f in files))
        # Simulate a live restart between deletions: retain the complete file,
        # leave a recovery marker, and finish safely in the next offline run.
        with patch.object(consolidation,'offline',side_effect=[True,True,False]):
            with self.assertRaises(AssertionError):
                consolidation.consolidate(row,self.cfg,apply=True)
        self.assertTrue((directory/'consolidation.pending.json').exists())
        self.assertFalse((self.records/files[0]).exists())
        self.assertTrue((self.records/files[1]).exists())
        with patch.object(consolidation,'offline',return_value=True):
            result=consolidation.consolidate(row,self.cfg,apply=True)
            self.assertEqual(result['parts'],2)
            self.assertIsNone(consolidation.consolidate(row,self.cfg,apply=True))
        self.assertEqual(len(list((self.records/self.cfg['record_dir']).glob('*.flv'))),1)
        self.assertFalse(any((self.records/f).exists() for f in files))
        self.assertFalse((self.state/'fragments').exists())
        self.assertTrue(all((self.records/f).with_suffix('.xml').exists() for f in files))
        self.assertEqual(core.enqueue([result['recording']]),job_id)


if __name__=='__main__':
    unittest.main()
