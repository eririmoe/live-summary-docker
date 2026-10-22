import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import core

spec=importlib.util.spec_from_file_location('completed_media',core.APP/'tools/24-clean-completed-media.py')
media=importlib.util.module_from_spec(spec);spec.loader.exec_module(media)

class CompletedMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)/'state';self.records=Path(self.temp.name)/'records'
        self.state.mkdir();self.records.mkdir()
        for module in (core,media):
            for key,value in [('STATE',self.state),('RECORDS',self.records)]:
                item=patch.object(module,key,value);item.start();self.addCleanup(item.stop)
        core.init_db()
        self.cfg={**core.settings(),'delete_completed_media':True}
        self.job_id='a'*24;self.directory=self.state/'jobs'/self.job_id;self.directory.mkdir(parents=True)
        self.relative=self.cfg['record_dir']+'/录制-123456789-20261013-120000-000-test.flv'
        self.video=self.records/self.relative;self.video.parent.mkdir();self.video.write_bytes(b'video')
        os.utime(self.video,(time.time()-3600,time.time()-3600))
        self.xml=self.video.with_suffix('.xml');self.xml.write_text('<i/>')
        self.part=self.directory/'file-000';self.part.mkdir()
        self.audio=self.part/'audio-000.mp3';self.audio.write_bytes(b'audio')
        self.source=core.identify(self.relative)
        self.payload={'files':[self.source]}
        core.atomic_json(self.directory/'published.json',{'verified_at':time.time()})
        for name in ('summary.json','result.json','transcript.json'):
            core.atomic_json(self.directory/name,{'ok':True})
        (self.directory/'transcript.txt').write_text('text')
        self.archive={'backend':'r2','verified_at':time.time(),'objects':[
            {'name':name,'bytes':(self.directory/name).stat().st_size,'sha256':media.digest(self.directory/name)}
            for name in ('transcript.json','transcript.txt')]}
        core.atomic_json(self.directory/'transcript-archive.json',self.archive)
        self.backup={'method':'remote_exact_bytes','verified_at':time.time(),'files':[
            {**self.source,'remote':'/live-recordings/'+self.relative}]}
        core.atomic_json(self.directory/'backup.json',self.backup)
        for target,kwargs in [('offline',{'return_value':True}),('verify_remote',{'return_value':None})]:
            item=patch.object(media,target,**kwargs);item.start();self.addCleanup(item.stop)

    def row(self):
        return (self.job_id,json.dumps(self.payload))

    def test_verified_single_video_and_fresh_audio_deleted_other_files_preserved(self):
        result=media.clean(self.row(),self.cfg,True)
        self.assertEqual(result['released_bytes'],10)
        self.assertFalse(self.video.exists());self.assertFalse(self.audio.exists())
        self.assertTrue(self.xml.exists());self.assertTrue((self.directory/'transcript.json').exists())
        self.assertTrue((self.directory/'media-cleanup.json').exists())
        self.assertIsNone(media.clean(self.row(),self.cfg,True))

    def test_changed_recording_is_not_deleted(self):
        self.video.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            media.clean(self.row(),self.cfg,True)
        self.assertTrue(self.video.exists());self.assertTrue(self.audio.exists())

    def test_missing_cloud_backup_prevents_all_deletion(self):
        with patch.object(media,'verify_remote',side_effect=ValueError('missing_remote')):
            with self.assertRaises(ValueError):
                media.clean(self.row(),self.cfg,True)
        self.assertTrue(self.video.exists());self.assertTrue(self.audio.exists())

    def test_r2_hash_mismatch_prevents_deletion(self):
        (self.directory/'transcript.txt').write_text('modified')
        with self.assertRaises(ValueError):
            media.clean(self.row(),self.cfg,True)
        self.assertTrue(self.video.exists());self.assertTrue(self.audio.exists())

    def test_symlink_audio_and_live_room_prevent_deletion(self):
        self.audio.unlink();self.audio.symlink_to(self.xml)
        with self.assertRaises(ValueError):
            media.clean(self.row(),self.cfg,True)
        self.assertTrue(self.video.exists());self.assertTrue(self.xml.exists())
        self.audio.unlink();self.audio.write_bytes(b'audio')
        with patch.object(media,'offline',return_value=False):
            with self.assertRaises(ValueError):
                media.clean(self.row(),self.cfg,True)
        self.assertTrue(self.video.exists());self.assertTrue(self.audio.exists())

    def test_preview_and_disabled_apply_preserve_files(self):
        result=media.clean(self.row(),self.cfg,False)
        self.assertEqual(len(result['files']),2)
        with self.assertRaises(ValueError):
            media.clean(self.row(),{**self.cfg,'delete_completed_media':False},True)
        self.assertTrue(self.video.exists());self.assertTrue(self.audio.exists())

    def test_running_jobs_are_excluded_from_cleanup(self):
        with core.database() as db:
            db.execute('INSERT INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',
                (self.job_id,time.time(),time.time(),'running',self.row()[1]))
        self.assertEqual(media.run(self.cfg,True)['changed'],[])
        self.assertTrue(self.video.exists());self.assertTrue(self.audio.exists())

    def test_verified_merged_package_final_copy_and_fragments_deleted(self):
        other_relative=self.relative.replace('120000','120100')
        other=self.records/other_relative;other.write_bytes(b'other')
        os.utime(other,(time.time()-3600,time.time()-3600))
        self.payload['files'].append(core.identify(other_relative))
        name=self.video.stem+'-合并.flv'
        package=self.directory/'package'/self.cfg['record_dir'];package.mkdir(parents=True)
        merged=package/name;merged.write_bytes(b'videoother')
        final=self.video.parent/name;final.write_bytes(merged.read_bytes())
        os.utime(final,(time.time()-3600,time.time()-3600))
        xml=merged.with_suffix('.xml');xml.write_text('<i/>')
        fingerprints=[s['fingerprint'] for s in self.payload['files']]
        merge={'version':1,'name':name,'bytes':10,'sha256':media.digest(merged),'fingerprints':fingerprints}
        core.atomic_json(self.directory/'merge.json',merge)
        self.backup.update({'merge_sha256':merge['sha256'],'source_fingerprints':fingerprints,
            'files':[{'relative':self.cfg['record_dir']+'/'+name,'size':10,
                'remote':'/live-recordings/'+self.cfg['record_dir']+'/'+name}]})
        core.atomic_json(self.directory/'backup.json',self.backup)
        media.clean(self.row(),self.cfg,True)
        self.assertTrue(xml.exists());self.assertTrue(self.xml.exists())
        self.assertTrue(all(not p.exists() for p in (self.video,other,merged,final,self.audio)))

if __name__=='__main__':
    unittest.main()
