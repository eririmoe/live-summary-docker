import hashlib
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import core
import history
import pipeline
import providers


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)
        for module in (core,pipeline):
            p=patch.object(module,'STATE',self.state);p.start();self.addCleanup(p.stop)
        core.init_db()
        core.atomic_json(self.state/'config.json',{'transcript_archive':{'backend':'r2'},'history_before':'20261014'})
        core.atomic_json(self.state/'r2-secrets.json',{'R2_ACCESS_KEY_ID':'test','R2_SECRET_ACCESS_KEY':'test'})
        self.job_id='a'*24

    def source(self,day=13,clock='120000',title='测试',size=3):
        name=f'录制-123456789-202610{day:02d}-{clock}-000-{title}.flv'
        remote='/live-recordings/123456789-example/'+name
        return {'name':name,'path':remote,'remote':remote,'size':size,'fs_id':'42',
            'relative':'123456789-example/'+name,'xml_remote':remote[:-4]+'.xml',
            'md5':None,'md5_reliable':False,'md5_uncertain':True}

    def payload(self,files):
        return {'origin':'baidu_history','batch_id':'test','batch_position':1,'batch_limit':10,
            'session_id':None,'files':files,'metadata':{'room_id':123456789,'streamer':'测试',
            'title':'测试','started_at':'2026-10-13T12:00:00+08:00'}}

    def job(self,payload):
        with core.database() as db:
            db.execute('INSERT INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',
                       (self.job_id,time.time(),time.time(),'running',json.dumps(payload)))

    def inventory(self):
        files=[]
        for day in range(2,16):
            f=self.source(day)
            files.extend([f,{'name':f['name'][:-4]+'.xml','path':f['xml_remote']}])
        # Duplicate backup aliases must not become extra sessions.
        f=self.source(13);f={**f,'path':'/live-recordings/'+f['name']}
        files.append(f)
        files.append({'name':f['name'][:-4]+'.xml','path':f['path'][:-4]+'.xml'})
        return {'files':files}

    def test_finite_scope_dedup_and_descending_order(self):
        plan=history.plan(self.inventory())
        self.assertEqual(len(plan),10)
        self.assertEqual([p['date'] for p in plan],['2026-10-'+str(i).zfill(2) for i in range(13,3,-1)])
        self.assertEqual(len(plan[0]['files'][0]['aliases']),2)
        with self.assertRaises(providers.ProviderError):
            history.plan(self.inventory(),limit=11)

    def test_distinct_titles_same_day_are_separate_sessions(self):
        inv=self.inventory()
        f=self.source(8,'230000','晚安')
        inv['files'].append(f)
        result=history.plan(inv)
        oct8=[s for s in result if s['date']=='2026-10-08']
        self.assertEqual(len(oct8),2)

    def extension_inventory(self):
        files=[]
        for index in range(80):
            day=(datetime(2026,10,13)-timedelta(days=index)).strftime('%Y%m%d')
            name=f'录制-123456789-{day}-120000-000-测试.flv'
            remote='/live-recordings/123456789-example/'+name
            files.extend([{'name':name,'path':remote},{'name':name[:-4]+'.xml','path':remote[:-4]+'.xml'}])
        return {'files':files}

    def test_append_twenty_excludes_initial_batch_is_serial_and_idempotent(self):
        inventory=self.extension_inventory()
        def meta(paths):
            return {p:{'remote':p,'size':3,'fs_id':str(i),'md5':None,'md5_reliable':False} for i,p in enumerate(paths)}
        with patch('history.metadata',side_effect=meta):
            initial=history.start_batch(inventory)
            extended=history.append_twenty(inventory)
            self.assertEqual(history.append_twenty(inventory),extended)
        self.assertEqual(extended['limit'],30)
        self.assertEqual(extended['jobs'][:10],initial['jobs'])
        self.assertEqual(len(set(extended['jobs'])),30)
        self.assertEqual(len(core.job_rows()),30)
        self.assertTrue(extended['sessions'][10]['started_at'].startswith('2026-10-03'))
        self.assertTrue(extended['sessions'][-1]['started_at'].startswith('2026-09-14'))
        with core.database() as db:
            self.assertEqual(history.next_job(db,time.time())['id'],initial['jobs'][0])
            db.execute("UPDATE jobs SET status='done' WHERE id IN ("+','.join('?' for _ in initial['jobs'])+")",initial['jobs'])
            self.assertEqual(history.next_job(db,time.time())['id'],extended['jobs'][10])

    def test_extension_recovers_after_jobs_inserted_before_batch_manifest(self):
        inventory=self.extension_inventory()
        def meta(paths):
            return {p:{'remote':p,'size':3,'fs_id':str(i),'md5':None,'md5_reliable':False} for i,p in enumerate(paths)}
        with patch('history.metadata',side_effect=meta):
            initial=history.start_batch(inventory)
            extended=history.append_twenty(inventory)
            core.atomic_json(self.state/'history-batch.json',initial)
            self.assertEqual(history.append_twenty(inventory)['jobs'],extended['jobs'])
        self.assertEqual(len(core.job_rows()),30)

    def test_second_twenty_extends_to_fifty_without_requeueing_existing_work(self):
        inventory=self.extension_inventory()
        def meta(paths):
            return {p:{'remote':p,'size':3,'fs_id':str(i),'md5':None,'md5_reliable':False} for i,p in enumerate(paths)}
        with patch('history.metadata',side_effect=meta):
            history.start_batch(inventory)
            initial=history.append_twenty(inventory)
            current=initial['jobs'][25]
            core.update_job(current,status='running',stage='transcribing',attempts=2)
            extended=history.append_twenty(inventory,after=30)
            self.assertEqual(history.append_twenty(inventory,after=30),extended)
            self.assertEqual(len(core.job_rows()),50)
            self.assertEqual(extended['jobs'][:30],initial['jobs'])
            self.assertEqual(extended['limit'],50)
            self.assertEqual(len(set(extended['jobs'])),50)
            self.assertEqual([s['position'] for s in extended['sessions']],list(range(1,51)))
            self.assertLess(extended['sessions'][30]['started_at'],initial['sessions'][-1]['started_at'])
            row=next(r for r in core.job_rows() if r['id']==current)
            self.assertEqual((row['status'],row['stage'],row['attempts']),('running','transcribing',2))
            self.assertTrue(all(json.loads(r['payload'])['batch_limit']==50 for r in core.job_rows()))
            # Recover after deterministic DB inserts but before manifest write.
            core.atomic_json(self.state/'history-batch.json',initial)
            self.assertEqual(history.append_twenty(inventory,after=30)['jobs'],extended['jobs'])
            self.assertEqual(len(core.job_rows()),50)

    def test_extension_rejects_stale_start_without_changing_queue(self):
        inventory=self.extension_inventory()
        with patch('history.metadata',side_effect=lambda paths:{p:{'remote':p,'size':3,'fs_id':str(i),'md5':None,'md5_reliable':False} for i,p in enumerate(paths)}):
            history.start_batch(inventory)
            before=core.read_json(self.state/'history-batch.json')
            with self.assertRaisesRegex(providers.ProviderError,'queue_length_changed'):
                history.append_twenty(inventory,after=30)
            self.assertEqual(core.read_json(self.state/'history-batch.json'),before)
            self.assertEqual(len(core.job_rows()),10)

    def test_reviewed_title_change_prelude_is_kept_in_following_session(self):
        inventory=self.extension_inventory()
        first=self.source(12,'230000','旧标题');second=self.source(12,'230100','晚安电台')
        inventory['files'].extend([first,second])
        inventory['reviewed_session_links']=[{'before':first['name'],'after':second['name'],
            'before_bytes':3,'reason':'Reviewed title-change prelude; all content retained'}]
        groups=history.plan(inventory)
        matched=[g for g in groups if g['title']=='晚安电台']
        self.assertEqual(len(matched),1)
        self.assertEqual([f['name'] for f in matched[0]['files']],[first['name'],second['name']])
        self.assertTrue(matched[0]['started_at'].startswith('2026-10-12T23:00:00'))
        inventory['reviewed_session_links'][0]['after']=self.source(13)['name']
        with self.assertRaises(providers.ProviderError):
            history.plan(inventory)

    def test_long_gap_needs_exact_source_bound_review_and_never_trims_audio(self):
        first=self.source(8,'133738');second=self.source(8,'135843')
        folder=self.state/'part';folder.mkdir()
        sha='a'*64
        with self.assertRaisesRegex(providers.ProviderError,'boundary_needs_review'):
            history.session_boundary(first,second,17.3,sha,folder)
        expected=core.read_json(folder/'boundary-review-required.json')
        review={**expected,'action':'keep_same_session','reason':'Reviewed outage messages and media duration','reviewed_at':time.time()}
        core.atomic_json(folder/'session-boundary-review.json',review)
        gap,value=history.session_boundary(first,second,17.3,sha,folder)
        self.assertGreater(gap,900);self.assertEqual(value,review)
        for changed in ({**review,'source_sha256':'b'*64},{**review,'next_fs_id':'other'},
                        {**review,'gap_seconds':gap+1},{**review,'source_duration_seconds':99}):
            core.atomic_json(folder/'session-boundary-review.json',changed)
            with self.assertRaises(providers.ProviderError):
                history.session_boundary(first,second,17.3,sha,folder)
        core.atomic_json(folder/'session-boundary-review.json',review)
        with self.assertRaises(providers.ProviderError):
            history.session_boundary(first,second,3600,sha,folder)

    def test_third_twenty_extends_fifty_to_seventy(self):
        inventory=self.extension_inventory()
        with patch('history.metadata',side_effect=lambda paths:{p:{'remote':p,'size':3,'fs_id':str(i),'md5':None,'md5_reliable':False} for i,p in enumerate(paths)}):
            history.start_batch(inventory);history.append_twenty(inventory)
            fifty=history.append_twenty(inventory,after=30)
            seventy=history.append_twenty(inventory,after=50)
            self.assertEqual(seventy['limit'],70)
            self.assertEqual(seventy['jobs'][:50],fifty['jobs'])
            self.assertEqual(len(set(seventy['jobs'])),70)
            self.assertEqual(history.append_twenty(inventory,after=50),seventy)

    def test_exhausted_tail_requires_opt_in_and_preserves_running_job(self):
        inventory=self.extension_inventory()
        # Exactly 64 genuine sessions; duplicate aliases cannot fill the quota.
        inventory['files']=inventory['files'][:128]
        duplicate=inventory['files'][100]
        inventory['files'].extend([{**duplicate,'path':'/live-recordings/'+duplicate['name']},
            {'name':duplicate['name'][:-4]+'.xml','path':'/live-recordings/'+duplicate['name'][:-4]+'.xml'}])
        with patch('history.metadata',side_effect=lambda paths:{p:{'remote':p,'size':3,'fs_id':str(i),'md5':None,'md5_reliable':False} for i,p in enumerate(paths)}):
            history.start_batch(inventory);history.append_twenty(inventory)
            fifty=history.append_twenty(inventory,after=30)
            current=fifty['jobs'][32]
            core.update_job(current,status='running',stage='downloading',attempts=2)
            with self.assertRaisesRegex(providers.ProviderError,'not_enough_sessions'):
                history.append_twenty(inventory,after=50)
            self.assertEqual(len(core.job_rows()),50)
            self.assertEqual(core.read_json(self.state/'history-batch.json'),fifty)
            tail=history.append_twenty(inventory,after=50,allow_partial=True)
            self.assertEqual(tail['limit'],64)
            self.assertEqual(tail['jobs'][:50],fifty['jobs'])
            self.assertEqual(len(set(tail['jobs'])),64)
            self.assertEqual(tail['extensions'][-1]['added'],14)
            self.assertEqual(tail['extensions'][-1]['requested'],20)
            self.assertEqual([s['position'] for s in tail['sessions']],list(range(1,65)))
            self.assertTrue(all(json.loads(r['payload'])['batch_limit']==64 for r in core.job_rows()))
            row=next(r for r in core.job_rows() if r['id']==current)
            self.assertEqual((row['status'],row['stage'],row['attempts']),('running','downloading',2))
            self.assertEqual(history.append_twenty(inventory,after=50,allow_partial=True),tail)
            # Re-entry after DB commit but before manifest write recovers the same tail.
            core.atomic_json(self.state/'history-batch.json',fifty)
            self.assertEqual(history.append_twenty(inventory,after=50,allow_partial=True)['jobs'],tail['jobs'])
            self.assertEqual(len(core.job_rows()),64)

    def test_partial_plan_still_rejects_empty_inventory(self):
        with self.assertRaisesRegex(providers.ProviderError,'not_enough_sessions'):
            history.plan({'files':[]},limit=20,allow_partial=True)

    def test_same_title_fragments_crossing_midnight_stay_one_session(self):
        inventory=self.extension_inventory()
        excluded={f['name'] for f in inventory['files'] if f['name'].split('-')[2]>='20260928'}
        for day,clock in [('20260922','232659'),('20260923','001350')]:
            name=f'录制-123456789-{day}-{clock}-000-跨夜.flv'
            inventory['files'].append({'name':name,'path':'/live-recordings/'+name})
        result=history.plan(inventory,20,exclude_names=excluded)
        matched=[g for g in result if g['title']=='跨夜']
        self.assertEqual(len(matched),1)
        self.assertEqual(len(matched[0]['files']),2)
        self.assertEqual(matched[0]['date'],'2026-09-22')

    def test_metadata_exit_zero_api_error_is_not_accepted(self):
        with patch('pipeline.baidu_command',return_value='错误 31045'):
            with self.assertRaisesRegex(providers.ProviderError,'metadata_missing'):
                history.metadata(['/live-recordings/test.flv'])
        path='/live-recordings/test.flv'
        text='[0] - [x] ---\n文件路径 '+path+'\n文件大小 123, 123B\nfs_id 42\nmd5 (可能不正确) '+'a'*32+'\n'
        with patch('pipeline.baidu_command',return_value=text):
            actual=history.metadata([path])[path]
        self.assertEqual(actual['size'],123)
        self.assertFalse(actual['md5_reliable'])

    def test_batch_creation_idempotent_and_retry_blocks_later_sessions(self):
        def meta(paths):
            return {p:{'remote':p,'size':3,'fs_id':str(i),'md5':None,'md5_reliable':False} for i,p in enumerate(paths)}
        with patch('history.metadata',side_effect=meta):
            first=history.start_batch(self.inventory())
            self.assertEqual(first,history.start_batch(self.inventory()))
        self.assertEqual(len(core.job_rows()),10)
        with core.database() as db:
            self.assertEqual(history.next_job(db,time.time())['id'],first['jobs'][0])
        core.update_job(first['jobs'][0],status='retry',retry_at=time.time()+3600)
        with core.database() as db:
            self.assertIsNone(history.next_job(db,time.time()))
        core.update_job(first['jobs'][0],status='failed')
        with core.database() as db:
            self.assertIsNone(history.next_job(db,time.time()))
        core.update_job(first['jobs'][0],attempts=5)
        with core.database() as db:
            self.assertEqual(history.next_job(db,time.time())['id'],first['jobs'][1])
        core.update_job(first['jobs'][0],status='done')
        with core.database() as db:
            self.assertEqual(history.next_job(db,time.time())['id'],first['jobs'][1])

    def test_download_validates_scope_and_rejects_short_final_file(self):
        with self.assertRaises(providers.ProviderError):
            history.download('/live-recordings/../else.flv',self.state,3,lambda **_:None)
        target=self.state/'a.flv';target.write_bytes(b'x')
        with self.assertRaisesRegex(providers.ProviderError,'partial_final'):
            history.download('/live-recordings/a.flv',self.state,3,lambda **_:None)

    def test_zero_exit_download_without_file_is_not_success(self):
        process=SimpleNamespace(returncode=0,poll=lambda:0)
        with patch('history.subprocess.Popen',return_value=process),\
                patch('history.shutil.disk_usage',return_value=SimpleNamespace(free=10*1024**3)):
            with self.assertRaisesRegex(providers.ProviderError,'download_failed'):
                history.download('/live-recordings/a.flv',self.state,3,lambda **_:None)

    def test_failed_five_times_cache_released_only_after_cloud_source_verified(self):
        source=self.source();payload=self.payload([source]);self.job(payload)
        core.update_job(self.job_id,status='failed',attempts=5)
        cache=self.state/'history-cache'/self.job_id/'0';cache.mkdir(parents=True)
        video=cache/source['name'];video.write_bytes(b'partial-video')
        marker=video.with_name(video.name+'.BaiduPCS-Go-downloading');marker.write_text('partial')
        checkpoint=self.state/'jobs'/self.job_id/'file-000/asr-000.json'
        core.atomic_json(checkpoint,{'task_id':'paid-task','result':{'transcripts':[]}})
        with patch('pipeline.require_offline'),patch('history.metadata',return_value={source['remote']:{'size':4,'fs_id':'42'}}):
            with self.assertRaisesRegex(providers.ProviderError,'remote_changed'):
                history.release_skipped_cache()
        self.assertTrue(video.exists())
        with patch('pipeline.require_offline'),patch('history.metadata',return_value={source['remote']:{'size':3,'fs_id':'42'}}):
            self.assertEqual(history.release_skipped_cache(),[self.job_id])
        self.assertFalse(video.exists());self.assertFalse(marker.exists())
        self.assertEqual(core.read_json(checkpoint)['task_id'],'paid-task')

    def test_retrying_or_running_jobs_cache_is_not_released(self):
        source=self.source();self.job(self.payload([source]))
        cache=self.state/'history-cache'/self.job_id/'0';cache.mkdir(parents=True)
        video=cache/source['name'];video.write_bytes(b'keep')
        with patch('history.metadata',side_effect=AssertionError('must not query')):
            for status in ('running','retry'):
                core.update_job(self.job_id,status=status,attempts=5)
                self.assertEqual(history.release_skipped_cache(),[])
        self.assertTrue(video.exists())

    def test_unfinished_marker_prevents_accepting_full_size_file(self):
        (self.state/'a.flv').write_bytes(b'abc')
        (self.state/'a.flv.BaiduPCS-Go-downloading').write_text('unfinished')
        process=SimpleNamespace(returncode=0,poll=lambda:0)
        with patch('history.subprocess.Popen',return_value=process),\
                patch('history.shutil.disk_usage',return_value=SimpleNamespace(free=10*1024**3)):
            with self.assertRaisesRegex(providers.ProviderError,'download_failed'):
                history.download('/live-recordings/a.flv',self.state,3,lambda **_:None)

    def test_baidu_size_progress_without_percent_is_recognized(self):
        expected=4802199527
        self.assertAlmostEqual(history.download_percent('1.44GB/4.47GB 2.73MB/s',expected),32.2,delta=0.1)
        self.assertEqual(history.download_percent('45.5%',expected),45.5)
        self.assertIsNone(history.download_percent('1.0MB/2.0MB',expected))

    def complete(self,index,source,duration,text,comments):
        directory=self.state/'jobs'/self.job_id/f'file-{index:03d}';directory.mkdir(parents=True)
        transcript={'metadata':{},'duration_seconds':duration,'segments':[{'start_seconds':1,'end_seconds':2,'text':text}]}
        history.write_transcript(directory,transcript)
        (directory/Path(source['xml_remote']).name).write_text('<i>'+comments+'</i>',encoding='utf-8')
        evidence={'duration':duration,'danmaku':{},'sha256':str(index),'source_name':source['name']}
        core.atomic_json(directory/'evidence.json',evidence)
        core.atomic_json(directory/'transcript-archive.json',{'verified_at':time.time()})
        core.atomic_json(directory/'history-part-complete.json',{
            'transcript_sha256':history.hash_file(directory/'transcript.json'),
            'evidence_sha256':history.hash_file(directory/'evidence.json')})

    def test_resume_never_redownloads_or_retranscribes_and_offsets_real_xml(self):
        files=[self.source(clock='120000'),self.source(clock='120100')]
        payload=self.payload(files);self.job(payload)
        comments=''.join(f'<d p="59,1">text{i}</d>' for i in range(9))
        self.complete(0,files[0],60,'first',comments)
        self.complete(1,files[1],30,'second','<d p="1,1">next</d>')
        with patch('history.download',side_effect=AssertionError('download repeated')),\
                patch('providers.transcribe',side_effect=AssertionError('paid ASR repeated')),\
                patch('pipeline.finish_evidence',return_value={'ok':True}) as finish:
            self.assertEqual(history.process_job(self.job_id,payload),{'ok':True})
        args=finish.call_args.args
        self.assertEqual([s['start_seconds'] for s in args[4]],[1,61])
        self.assertEqual(args[5]['count'],10)
        self.assertEqual([b['count'] for b in args[5]['buckets']],[9,1])
        self.assertEqual(args[6],90)

    def test_failed_r2_keeps_only_current_video_and_never_downloads_next(self):
        files=[self.source(clock='120000'),self.source(clock='120005')]
        payload=self.payload(files);self.job(payload)
        downloads=[]
        def download(remote,directory,size,progress):
            directory.mkdir(parents=True,exist_ok=True)
            path=directory/Path(remote).name
            path.write_bytes(b'abc' if remote.endswith('.flv') else b'<i/>')
            downloads.append(remote)
            return path
        def meta(paths):
            return {p:{'remote':p,'size':3,'fs_id':'42','md5':None,'md5_reliable':False} for p in paths}
        result={'transcripts':[{'sentences':[{'begin_time':0,'end_time':1000,'text':'hello'}]}]}
        with patch('pipeline.require_offline'),patch('history.metadata',side_effect=meta),\
                patch('history.download',side_effect=download),patch('pipeline.probe',return_value=5),\
                patch('pipeline.audio_parts',return_value=[{'path':'/unused','start':0}]),\
                patch('providers.transcribe',return_value=result),\
                patch('storage.archive',side_effect=providers.ProviderError('r2_failed')):
            with self.assertRaisesRegex(providers.ProviderError,'r2_failed'):
                history.process_job(self.job_id,payload)
        self.assertEqual(downloads,[files[0]['remote'],files[0]['xml_remote']])
        self.assertEqual(len(list((self.state/'history-cache').rglob('*.flv'))),1)

    def test_no_words_part_retains_duration_danmaku_and_caveat(self):
        files=[self.source(clock='120000'),self.source(clock='120100')]
        payload=self.payload(files);self.job(payload)
        self.complete(0,files[0],60,'first','<d p="59,1">old</d>')
        self.complete(1,files[1],2,'','<d p="1,1">comment</d>')
        directory=self.state/'jobs'/self.job_id/'file-001'
        transcript=core.read_json(directory/'transcript.json');transcript['segments']=[]
        history.write_transcript(directory,transcript)
        marker=core.read_json(directory/'history-part-complete.json')
        marker['transcript_sha256']=history.hash_file(directory/'transcript.json')
        core.atomic_json(directory/'history-part-complete.json',marker)
        core.atomic_json(directory/'asr-000.json',{'task_id':'original-task',
            'result':{'transcripts':[],'empty_reason':'ASR_RESPONSE_HAVE_NO_WORDS'}})
        with patch('history.download',side_effect=AssertionError('must not download')),\
                patch('providers.transcribe',side_effect=AssertionError('must not submit')),\
                patch('pipeline.finish_evidence',return_value={}) as finish:
            history.process_job(self.job_id,payload)
        args=finish.call_args.args
        self.assertEqual(args[6],62)
        self.assertEqual(args[5]['count'],2)
        self.assertEqual(len(args[4]),1)
        self.assertIn('未识别到文字',finish.call_args.kwargs['extra_caveats'][0])

    def test_cleanup_rejects_outside_source_and_symlink(self):
        original=self.state/'original.flv';original.write_bytes(b'keep')
        cache=self.state/'cache';cache.mkdir()
        link=cache/'bad.flv';link.symlink_to(original)
        for path in (original,link):
            with self.assertRaises(providers.ProviderError):
                history.release_video(path,cache)
        self.assertEqual(original.read_bytes(),b'keep')

    def metadata_shell(self,tag_type=18):
        return b'FLV\x01\x05\0\0\0\x09'+b'\0'*4+bytes([tag_type])+b'\x00\x01\x00'+b'\0'*7+b'truncated-script'

    def test_metadata_only_parser_recognizes_truncated_script_without_media(self):
        path=self.state/'shell.flv';path.write_bytes(self.metadata_shell())
        self.assertEqual(history.metadata_only_flv(path)['reason'],'truncated_metadata_without_media')
        path.write_bytes(b'FLV\x01\x05\0\0\0\x09'+b'\0'*4)
        self.assertEqual(history.metadata_only_flv(path)['reason'],'metadata_without_media')

    def test_metadata_only_parser_rejects_av_tags_and_unrelated_corruption(self):
        path=self.state/'damaged.flv'
        for raw in (self.metadata_shell(8),self.metadata_shell(9),b'not a flv',self.metadata_shell()[:15]):
            path.write_bytes(raw);self.assertIsNone(history.metadata_only_flv(path))

    def media_shell_case(self,raw,count=1,skipped=True):
        files=[self.source(clock='120000')]+[self.source(clock='120'+str(i+1).zfill(3),size=len(raw)) for i in range(count)]
        self.job(self.payload(files));self.complete(0,files[0],60,'first','<d p="1,1">old</d>')
        paid=self.state/'jobs'/self.job_id/'file-000/asr-000.json'
        core.atomic_json(paid,{'task_id':'paid-existing','result':{'transcripts':[]}});original=paid.read_bytes()
        downloads=[]
        def download(remote,directory,size,progress):
            directory.mkdir(parents=True,exist_ok=True);path=directory/Path(remote).name
            path.write_bytes(raw if remote.endswith('.flv') else b'<i><d p="5,1">unlocated</d></i>')
            downloads.append(remote);return path
        def metadata(paths):return {p:{'remote':p,'size':len(raw),'fs_id':'42','md5_reliable':False} for p in paths}
        def archive(directory,*args):
            receipt={'verified_at':time.time()};core.atomic_json(directory/'transcript-archive.json',receipt);return receipt
        with patch('pipeline.require_offline'),patch('history.metadata',side_effect=metadata),\
             patch('history.download',side_effect=download),patch('pipeline.probe',side_effect=providers.ProviderError('media_command_failed')),\
             patch('pipeline.audio_parts',side_effect=AssertionError('must not extract')),\
             patch('providers.transcribe',side_effect=AssertionError('must not submit paid ASR')),\
             patch('storage.archive',side_effect=archive) as archived,patch('pipeline.finish_evidence',return_value={}) as finish:
            if not skipped:
                with self.assertRaisesRegex(providers.ProviderError,'media_command_failed'):
                    history.process_job(self.job_id,self.payload(files))
                archived.assert_not_called();finish.assert_not_called()
            else:
                history.process_job(self.job_id,self.payload(files))
                self.assertEqual(finish.call_args.args[6],60)
                self.assertEqual(finish.call_args.args[5]['count'],1)
                self.assertIn('没有可用音视频',finish.call_args.kwargs['extra_caveats'][0])
                for index in range(1,count+1):
                    part=self.state/'jobs'/self.job_id/f'file-{index:03d}'
                    self.assertTrue(history.completed_part(part)['media_unavailable'])
                    self.assertTrue((part/Path(files[index]['xml_remote']).name).exists())
                    self.assertEqual(core.read_json(part/'transcript.json')['metadata']['media_unavailable']['unlocated_danmaku_count'],1)
                before=len(downloads);history.process_job(self.job_id,self.payload(files))
                self.assertEqual(len(downloads),before)
                self.assertEqual(len(list((self.state/'history-cache').rglob('*.flv'))),0)
        self.assertEqual(paid.read_bytes(),original)

    def test_metadata_only_part_skips_asr_preserves_xml_and_does_not_guess_duration(self):
        self.media_shell_case(self.metadata_shell())

    def test_damaged_part_with_audio_tag_is_not_silently_skipped(self):
        self.media_shell_case(self.metadata_shell(8),skipped=False)

    def test_identical_metadata_only_parts_are_not_duplicate_media(self):
        self.media_shell_case(self.metadata_shell(),count=2)

    def overlap_case(self,confirmed):
        files=[self.source(clock='120000'),self.source(clock='120100')]
        payload=self.payload(files);self.job(payload)
        self.complete(0,files[0],60,'first','<d p="59,1">old</d>')
        previous=self.state/'jobs'/self.job_id/'file-000'
        core.atomic_json(previous/'boundary-to-next.json',{'source_sha256':'0',
            'next_source_name':files[1]['name'],'expected_overlap_seconds':5.5,'tail_packets':[]})
        marker=core.read_json(previous/'history-part-complete.json')
        marker['boundary_sha256']=history.hash_file(previous/'boundary-to-next.json')
        core.atomic_json(previous/'history-part-complete.json',marker)
        def download(remote,directory,size,progress):
            directory.mkdir(parents=True,exist_ok=True)
            target=directory/Path(remote).name
            target.write_bytes(b'abc' if remote.endswith('.flv') else b'<i><d p="1,1">repeat</d><d p="6,1">new</d></i>')
            return target
        def meta(paths):
            return {p:{'remote':p,'size':3,'fs_id':'42','md5_reliable':False} for p in paths}
        def archive(directory,*_):
            receipt={'verified_at':time.time()}
            core.atomic_json(directory/'transcript-archive.json',receipt)
            return receipt
        alignment={'status':'confirmed_exact_audio' if confirmed else 'not_confirmed','trim_seconds':5.5 if confirmed else 0}
        speech={'transcripts':[{'sentences':[{'begin_time':0,'end_time':1000,'text':'new'}]}]}
        with patch('pipeline.require_offline'),patch('history.metadata',side_effect=meta),\
                patch('history.download',side_effect=download),patch('pipeline.probe',return_value=10),\
                patch('audio_overlap.packet_window',return_value=[]),patch('audio_overlap.confirm_overlap',return_value=alignment),\
                patch('pipeline.audio_parts',return_value=[{'path':'unused','start':0}]) as audio,\
                patch('providers.transcribe',return_value=speech) as asr,patch('storage.archive',side_effect=archive),\
                patch('pipeline.finish_evidence',return_value={}) as finish:
            history.process_job(self.job_id,payload)
            args=finish.call_args.args
            self.assertAlmostEqual(args[6],64.5 if confirmed else 70)
            self.assertEqual(args[5]['count'],3)
            self.assertEqual([s['start_seconds'] for s in args[4]],[1,60])
            self.assertEqual(bool(finish.call_args.kwargs['extra_caveats']),not confirmed)
            if confirmed:
                self.assertEqual(audio.call_args.kwargs['source_offset'],5.5)
            history.process_job(self.job_id,payload)
            self.assertEqual(asr.call_count,1)
        self.assertEqual(len(list((self.state/'history-cache').rglob('*.flv'))),0)

    def test_confirmed_audio_overlap_rebases_transcript_and_xml_once(self):
        self.overlap_case(True)

    def test_unconfirmed_overlap_preserves_all_content_and_adds_caveat(self):
        self.overlap_case(False)

    def test_completed_transcript_overlap_adjustment_preserves_paid_checkpoint(self):
        files=[self.source(clock='120000'),self.source(clock='120100')]
        payload=self.payload(files);self.job(payload)
        self.complete(0,files[0],60,'first','<d p="59,1">old</d>')
        self.complete(1,files[1],10,'second','<d p="1,1">unique</d><d p="6,1">new</d>')
        directory=self.state/'jobs'/self.job_id/'file-001'
        transcript=core.read_json(directory/'transcript.json')
        transcript['segments']=[{'start_seconds':0,'end_seconds':2,'text':'duplicate'},
            {'start_seconds':4,'end_seconds':7,'text':'crossing'},
            {'start_seconds':8,'end_seconds':9,'text':'new'}]
        history.write_transcript(directory,transcript)
        digest=history.hash_file(directory/'transcript.json')
        marker=core.read_json(directory/'history-part-complete.json')
        marker['transcript_sha256']=digest
        core.atomic_json(directory/'history-part-complete.json',marker)
        core.atomic_json(directory/'audio-overlap-adjustment.json',{'source_sha256':'1',
            'transcript_sha256':digest,'trim_seconds':5.5,
            'alignment':{'status':'confirmed_exact_audio','trim_seconds':5.5}})
        with patch('history.download',side_effect=AssertionError('download repeated')),\
                patch('providers.transcribe',side_effect=AssertionError('paid ASR repeated')),\
                patch('pipeline.finish_evidence',return_value={}) as finish:
            history.process_job(self.job_id,payload)
        args=finish.call_args.args
        self.assertEqual(args[6],64.5)
        self.assertEqual([s['text'] for s in args[4]],['first','crossing','new'])
        self.assertEqual([s['start_seconds'] for s in args[4]],[1,60,62.5])
        self.assertEqual([s['end_seconds'] for s in args[4]],[2,61.5,63.5])
        self.assertEqual(args[5]['count'],3)
        self.assertTrue(finish.call_args.kwargs['extra_caveats'])
        self.assertEqual(history.hash_file(directory/'transcript.json'),digest)


if __name__=='__main__':
    unittest.main()
