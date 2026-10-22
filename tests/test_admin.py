import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests
import admin
import core


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.patches = [patch.object(core, 'STATE', self.state), patch.object(admin, 'AUTH_FILE', self.state/'auth.json')]
        for item in self.patches:
            item.start()
        core.atomic_json(admin.AUTH_FILE, {'RECORDER_USER':'test-user','RECORDER_PASS':'test-password'})
        core.init_db()
        admin.SESSIONS.clear()
        admin.ATTEMPTS.clear()
        self.server = ThreadingHTTPServer(('127.0.0.1',0),admin.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def login(self):
        return requests.post(self.url+'/api/login',headers={'Origin':admin.ORIGIN},json={'username':'test-user','password':'test-password'},timeout=3)

    def test_status_requires_authentication_and_no_cache(self):
        response = requests.get(self.url+'/api/status',timeout=3)
        self.assertEqual(response.status_code,401)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertNotIn('jobs',response.json())

    def test_login_cookie_and_server_side_logout(self):
        response = self.login()
        self.assertEqual(response.status_code,200)
        cookie = response.headers['Set-Cookie']
        for flag in ('Secure','HttpOnly','SameSite=Strict'):
            self.assertIn(flag,cookie)
        cookie = cookie.split(';')[0]
        with patch('admin.snapshot',return_value={'jobs':[]}):
            self.assertEqual(requests.get(self.url+'/api/status',headers={'Cookie':cookie},timeout=3).status_code,200)
        self.assertEqual(requests.post(self.url+'/api/logout',headers={'Origin':admin.ORIGIN,'Cookie':cookie},json={},timeout=3).status_code,200)
        self.assertEqual(requests.get(self.url+'/api/status',headers={'Cookie':cookie},timeout=3).status_code,401)

    def test_wrong_password_rate_limit_and_origin(self):
        self.assertEqual(requests.post(self.url+'/api/login',headers={'Origin':'https://example.org'},json={},timeout=3).status_code,403)
        for _ in range(5):
            response=requests.post(self.url+'/api/login',headers={'Origin':admin.ORIGIN},json={'username':'test-user','password':'wrong'},timeout=3)
            self.assertEqual(response.status_code,401)
        self.assertEqual(self.login().status_code,429)

    def test_expired_and_forged_sessions_rejected(self):
        token=admin.create_session()
        cookie='live_summary_admin='+token
        self.assertTrue(admin.authenticated(cookie))
        self.assertFalse(admin.authenticated(cookie+'forged'))
        with patch('admin.time.time',return_value=time.time()+admin.TTL+1):
            self.assertFalse(admin.authenticated(cookie))

    def test_transcript_download_requires_session_and_rejects_other_paths(self):
        path='/api/transcript/'+'a'*24+'/transcript.txt'
        target=self.state/'jobs'/('a'*24)/'transcript.txt'
        target.parent.mkdir(parents=True)
        target.write_text('private transcript',encoding='utf-8')
        self.assertEqual(requests.get(self.url+path,timeout=3).status_code,401)
        cookie='live_summary_admin='+admin.create_session()
        response=requests.get(self.url+path,headers={'Cookie':cookie},timeout=3)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.text,'private transcript')
        with patch('core.settings',return_value={**core.settings(),'transcript_archive':{'backend':'r2'}}):
            self.assertEqual(requests.get(self.url+path,headers={'Cookie':cookie},timeout=3).status_code,409)
        key='transcripts/123456789/2026/10/13/'+'a'*24+'/transcript.txt'
        core.atomic_json(target.parent/'transcript-archive.json',{'verified_at':1,'objects':[{'name':'transcript.txt','key':key}]})
        with patch('storage.presigned_read_url',return_value='https://private-r2.example/file?signature=test') as signer:
            response=requests.get(self.url+path,headers={'Cookie':cookie},timeout=3,allow_redirects=False)
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.content,b'')
        self.assertEqual(response.headers['Location'],'https://private-r2.example/file?signature=test')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        signer.assert_called_once_with(key,method='GET')
        self.assertEqual(requests.get(self.url+'/api/transcript/../secrets.json',headers={'Cookie':cookie},timeout=3).status_code,404)

    def test_snapshot_excludes_provider_responses_and_secrets(self):
        job_id='a'*24
        now=time.time()
        payload={'files':[{'relative':'private/test.flv','size':42}]}
        with core.database() as db:
            db.execute('INSERT INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',(job_id,now,now,'running',json.dumps(payload)))
        core.atomic_json(self.state/'jobs'/job_id/'file-000/asr-000.json',{'task_id':'private-task-id','result':{'url':'SECRET_SIGNED_URL'}})
        core.atomic_json(self.state/'secrets.json',{'QIANWEN_API_KEY':'SECRET_API_KEY'})
        with patch('admin.requests.get',side_effect=requests.ConnectionError()):
            data=admin.snapshot()
        serialized=json.dumps(data)
        for secret in ('SECRET_SIGNED_URL','SECRET_API_KEY','test-password','private-task-id','private/test.flv'):
            self.assertNotIn(secret,serialized)
        self.assertEqual(data['jobs'][0]['asr']['completed'],1)
        self.assertEqual(data['jobs'][0]['files'][0]['name'],'test.flv')

    def test_wal_snapshot_is_current_and_does_not_modify_source(self):
        with core.database() as writer:
            writer.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',('wal-test','SessionEnded','session',123456789,time.time(),'{}'))
            writer.commit()
            database=self.state/'state.sqlite3'
            wal=self.state/'state.sqlite3-wal'
            before=[p.stat().st_mtime_ns for p in (database,wal)]
            with patch('admin.requests.get',side_effect=requests.ConnectionError()):
                data=admin.snapshot()
            self.assertEqual(data['events'][0]['kind'],'SessionEnded')
            self.assertEqual(before,[p.stat().st_mtime_ns for p in (database,wal)])

    def test_history_batch_progress_exposes_only_safe_fields(self):
        job_id='b'*24
        payload={'origin':'baidu_history','batch_position':1,'batch_limit':10,
            'metadata':{'title':'history title','started_at':'2026-10-13T12:00:00+08:00'},
            'files':[{'relative':'history/video.flv','size':42,'remote':'/PRIVATE_REMOTE_PATH'}]}
        with core.database() as db:
            db.execute('INSERT INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',
                       (job_id,time.time(),time.time(),'running',json.dumps(payload)))
        core.atomic_json(self.state/'history-batch.json',{'id':'test','limit':10,'active':True,'jobs':[job_id]})
        core.atomic_json(self.state/'jobs'/job_id/'history-progress.json',{
            'total_parts':2,'completed_parts':0,'current_part':1,'current_file':'video.flv',
            'download_percent':33,'download_url':'SECRET_DOWNLOAD_URL'})
        with patch('admin.requests.get',side_effect=requests.ConnectionError()):
            result=admin.snapshot()
        self.assertEqual(result['jobs'][0]['history']['download_percent'],33)
        self.assertEqual(result['history_batch']['done'],0)
        self.assertEqual(result['history_batch']['limit'],10)
        self.assertNotIn('SECRET_DOWNLOAD_URL',json.dumps(result))
        self.assertNotIn('PRIVATE_REMOTE_PATH',json.dumps(result))

    def test_pending_live_precedes_history_and_candidates_only_expose_safe_fields(self):
        core.atomic_json(self.state/'live-detection.json',{'phase':'waiting_reconnect','observed_at':time.time(),
            'recordings':[{'name':'new.flv','title':'new','started_at':'2026-10-17T12:00:00+08:00',
                'bytes':3,'modified':time.time(),'secret':'NEVER_PUBLIC'}]})
        with core.database() as db:
            for job_id,origin,created in [('a'*24,'baidu_history',9),('b'*24,'live',10)]:
                payload={'origin':origin,'files':[],'batch_position':1,'batch_limit':10,
                    'metadata':{'title':'history','started_at':'2026-10-13T12:00:00+08:00'}}
                db.execute('INSERT INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',
                    (job_id,created,created,'queued',json.dumps(payload)))
        with patch('admin.requests.get',side_effect=requests.ConnectionError()):data=admin.snapshot()
        self.assertEqual(data['jobs'][0]['origin'],'live')
        self.assertEqual(data['live_detection']['recordings'][0]['title'],'new')
        self.assertNotIn('NEVER_PUBLIC',json.dumps(data))


if __name__=='__main__':
    unittest.main()
