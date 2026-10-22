"""Deployment contracts: initialize safely, serve locally, and honor configured scope."""
import copy
from functools import partial
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import requests
import app
import core
import history
import pipeline
import providers
import storage

spec=importlib.util.spec_from_file_location('setup_project',core.APP/'scripts/setup.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
spec=importlib.util.spec_from_file_location('manage_project',core.APP/'scripts/manage.py')
manage=importlib.util.module_from_spec(spec);spec.loader.exec_module(manage)

class DeploymentTests(unittest.TestCase):
    def test_interrupted_room_bootstrap_resumes_only_its_own_intent(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(core,'STATE',Path(folder)),patch('core.secrets',return_value={'RECORDER_USER':'synthetic','RECORDER_PASS':'synthetic-password'}):
            rooms=[];global_config={};failed=[False]
            def api(method,url,**kwargs):
                route=url.split(':2356')[-1]
                payload=kwargs.get('json')
                if route=='/api/room' and method=='GET':value=list(rooms)
                elif route=='/api/room' and method=='POST':
                    if not rooms:rooms.append({'roomId':payload['roomId']})
                    value=rooms[0]
                elif route=='/api/config/global' and method=='GET':value=dict(global_config)
                elif route=='/api/config/global' and method=='POST':
                    if not failed[0]:failed[0]=True;raise requests.ConnectionError()
                    global_config.update(payload);value={}
                else:raise AssertionError(route)
                from types import SimpleNamespace
                return SimpleNamespace(raise_for_status=lambda:None,json=lambda:value)
            with patch('requests.request',side_effect=api):
                with self.assertRaises(providers.ProviderError):manage.register_room()
                self.assertTrue((core.STATE/'recorder-room.pending.json').exists())
                manage.register_room()
                self.assertFalse((core.STATE/'recorder-room.pending.json').exists())
                self.assertIn(core.settings()['record_dir'],global_config['optionalFileNameRecordTemplate']['value'])
                manage.register_room()
            self.assertEqual(len(rooms),1)

    def input(self):
        return {'config':{'room_id':987654,'streamer':'Synthetic <Host>','record_dir':'987654-recordings'},
                'recorder_secrets':{'RECORDER_USER':'test-admin','RECORDER_PASS':'synthetic-password-only'}}

    def test_clean_initialization_is_private_paused_and_never_overwrites_credentials(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(setup,'ROOT',Path(folder)):
            target=Path(folder)
            (target/'config.example.json').write_bytes((core.APP/'config.example.json').read_bytes())
            value=self.input();value['config']['auto_process']=True;value['config']['delete_completed_media']=True
            setup.initialize(value)
            cfg=json.loads((target/'data/config.json').read_text())
            self.assertFalse(cfg['auto_process']);self.assertFalse(cfg['delete_completed_media'])
            self.assertEqual((target/'.env').stat().st_mode & 0o777,0o600)
            old=(target/'data/recorder-secrets.json').read_bytes()
            with self.assertRaisesRegex(ValueError,'already initialized'):setup.initialize(value)
            self.assertEqual((target/'data/recorder-secrets.json').read_bytes(),old)

    def test_invalid_directory_origin_and_remote_scope_do_not_create_files(self):
        for mutation in ('traversal','origin','backup','password'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as folder,patch.object(setup,'ROOT',Path(folder)):
                target=Path(folder);(target/'config.example.json').write_bytes((core.APP/'config.example.json').read_bytes())
                value=self.input()
                if mutation=='traversal':value['config']['record_dir']='../other'
                elif mutation=='origin':value['deployment']={'admin_origin':'http://public.example.com'}
                elif mutation=='backup':value['config']['baidu_root']='/'
                else:value['recorder_secrets']['RECORDER_PASS']='weak'
                with self.assertRaises(ValueError):setup.initialize(value)
                self.assertFalse((target/'.env').exists());self.assertFalse((target/'data').exists())

    def test_local_server_delivers_catalogue_script_and_full_detail_but_never_private_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)
            entry={'id':'synthetic','title':'Synthetic session','started_at':'2026-01-01T00:00:00+08:00',
                'duration_seconds':60,'danmaku_count':0,'generated_at':'2026-01-01T01:00:00Z',
                'summary':{'overview':'synthetic','topics':[],'tags':[],'timeline':[],'highlights':[],'caveats':[]}}
            core.atomic_json(state/'public-summaries/test.json',entry)
            cfg={**core.settings(),'streamer':'Synthetic <Host>','room_id':987654,'publish_backend':'local'}
            with patch.object(pipeline,'STATE',state),patch.object(core,'STATE',state),patch('pipeline.settings',return_value=cfg),patch.object(providers,'STATE',state):
                pipeline.build_site()
                with patch('providers.cf',side_effect=AssertionError('local publication must never call Cloudflare')):
                    receipt=providers.deploy_site(state/'site')
                self.assertEqual(receipt['backend'],'local')
                server=ThreadingHTTPServer(('127.0.0.1',0),partial(app.Handler,directory=str(state/'site')))
                threading.Thread(target=server.serve_forever,daemon=True).start()
                try:
                    base='http://127.0.0.1:'+str(server.server_port)
                    index=requests.get(base+'/data/index.json',timeout=3).json()
                    detail=requests.get(base+index['entries'][0]['detail_url'],timeout=3)
                    self.assertEqual(detail.json(),entry)
                    self.assertEqual(requests.get(base+'/archive-utils.js',timeout=3).status_code,200)
                    page=requests.get(base+'/',timeout=3).text
                    self.assertIn('Synthetic &lt;Host&gt;',page)
                    for path in ('/secrets.json','/state.sqlite3','/data/entries/no.json'):
                        self.assertEqual(requests.get(base+path,timeout=3).status_code,404)
                finally:server.shutdown();server.server_close()

    def test_configured_room_backup_root_and_r2_prefix_replace_all_deployment_constants(self):
        cfg={**core.settings(),'room_id':987654,'record_dir':'custom','baidu_root':'/custom-backups',
             'transcript_archive':{'backend':'r2','account_id':'a'*32,'bucket':'private-test','prefix':'my-transcripts'}}
        with patch('core.settings',return_value=cfg),patch('core.read_json',return_value={'R2_ACCESS_KEY_ID':'test','R2_SECRET_ACCESS_KEY':'test'}):
            self.assertEqual(str(history.valid_remote('/custom-backups/test.flv')),'/custom-backups/test.flv')
            with self.assertRaises(providers.ProviderError):history.valid_remote('/other/test.flv')
            with self.assertRaises(providers.ProviderError):history.valid_remote('/custom-backups/../test.flv')
            key='my-transcripts/987654/2026/01/01/'+'a'*24+'/transcript.txt'
            self.assertIn('/my-transcripts/987654/',storage.presigned_read_url(key))
            with self.assertRaises(providers.ProviderError):storage.presigned_read_url(key.replace('/987654/','/123/'))

if __name__=='__main__':unittest.main()
