import json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch,mock_open
import app,core,pipeline,scheduler


class StopLoop(Exception):pass


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)/'state';self.records=Path(self.temp.name)/'records'
        for module,field,value in [(core,'STATE',self.state),(pipeline,'STATE',self.state),
                (core,'RECORDS',self.records),(pipeline,'RECORDS',self.records)]:
            p=patch.object(module,field,value);p.start();self.addCleanup(p.stop)
        core.init_db()
        self.cfg={**core.settings(),'auto_process':True,'reconnect_grace_seconds':600,'scan_interval':60}
        core.atomic_json(self.state/'config.json',self.cfg)
        app.DISCOVERY_WAKE.clear()

    def live_file(self,age=700):
        relative=self.cfg['record_dir']+'/录制-123456789-20261017-213703-092-示例晚间直播.flv'
        path=self.records/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'FLV')
        import os
        os.utime(path,(time.time()-age,time.time()-age))
        return relative

    def test_stale_poll_uses_latest_end_event_without_restarting_ten_minutes(self):
        self.live_file();now=time.time()
        with core.database() as db:
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',('ended','StreamEnded',None,self.cfg['room_id'],now-700,'{}'))
        core.atomic_json(self.state/'offline-state.json',{'offline_since':now-500,'checked_at':now-400})
        with patch('pipeline.recorder_room',return_value={}),patch('merging.media_info',return_value={'duration':60}):
            jobs=pipeline.discover()
        self.assertEqual(len(jobs),1)
        self.assertEqual(len(core.job_rows()),1)

    def test_a_new_start_does_not_reuse_an_old_end_event(self):
        self.live_file();now=time.time()
        with core.database() as db:
            for i,kind,age in [('end','StreamEnded',1200),('start','StreamStarted',800)]:
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(i,kind,None,self.cfg['room_id'],now-age,'{}'))
        with patch('pipeline.recorder_room',return_value={}),patch('merging.media_info') as probe:
            self.assertEqual(pipeline.discover(),[]);probe.assert_not_called()
        self.assertEqual(core.job_rows(),[])

    def test_claimed_earlier_live_is_not_regrouped_with_a_new_recording(self):
        relative=self.live_file();core.enqueue([relative])
        new=relative.replace('213703-092','214000-092')
        import os
        path=self.records/new;path.write_bytes(b'NEW');os.utime(path,(time.time()-700,time.time()-700))
        core.atomic_json(self.state/'offline-state.json',{'offline_since':time.time()-700,'checked_at':time.time()})
        with patch('pipeline.recorder_room',return_value={}),patch('merging.media_info',return_value={'duration':60}):
            self.assertEqual(len(pipeline.discover()),1)
        self.assertEqual(len(core.job_rows()),2)

    def test_recording_and_reconnect_wait_are_visible_before_a_job_is_created(self):
        self.live_file(age=60)
        with patch('pipeline.recorder_room',return_value={'recording':True}),patch('merging.media_info') as probe:
            pipeline.discover();probe.assert_not_called()
        observed=core.read_json(self.state/'live-detection.json')
        self.assertEqual(observed['phase'],'recording');self.assertEqual(len(observed['recordings']),1)
        self.assertEqual(core.job_rows(),[])
        with patch('pipeline.recorder_room',return_value={}):pipeline.discover()
        observed=core.read_json(self.state/'live-detection.json')
        self.assertEqual(observed['phase'],'waiting_reconnect')
        self.assertGreater(observed['eligible_at'],time.time())

    def test_discovery_runs_while_history_is_busy_and_yield_preserves_attempts(self):
        history_id='a'*24;live_id='b'*24
        with core.database() as db:
            db.execute('INSERT INTO jobs(id,created,updated,status,attempts,payload) VALUES(?,?,?,?,?,?)',
                (history_id,0,0,'queued',2,json.dumps({'origin':'baidu_history','files':[]})))
        core.atomic_json(self.state/'history-batch.json',{'active':True,'jobs':[history_id]})
        busy=threading.Event();admitted=threading.Event();stop=threading.Event();errors=[]
        def process(*args):
            busy.set()
            if not admitted.wait(3):raise AssertionError('discovery blocked behind history')
            scheduler.history_checkpoint()
        def discover():
            assert busy.is_set()
            with core.database() as db:
                db.execute('INSERT INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',
                    (live_id,1,1,'queued',json.dumps({'files':[]})))
            admitted.set();stop.set();app.DISCOVERY_WAKE.set()
        def worker():
            try:app.worker()
            except StopLoop:pass
            except BaseException as exc:errors.append(type(exc).__name__)
        with patch('app.readiness',return_value=[]),patch('pipeline.require_offline'),\
             patch('history.process_job',side_effect=process),patch('pipeline.discover',side_effect=discover),\
             patch('app.time.sleep',side_effect=StopLoop),patch('app.open',mock_open()),patch('app.fcntl.flock'):
            thread=threading.Thread(target=worker);thread.start()
            self.assertTrue(busy.wait(3))
            scanner=threading.Thread(target=app.discovery_loop,args=(stop,));scanner.start()
            scanner.join(4);thread.join(4)
        self.assertFalse(scanner.is_alive());self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        row=next(r for r in core.job_rows() if r['id']==history_id)
        self.assertEqual((row['status'],row['stage'],row['attempts']),('queued','yielded_for_live',2))
        with core.database() as db:self.assertEqual(scheduler.next_live(db,time.time())['id'],live_id)
