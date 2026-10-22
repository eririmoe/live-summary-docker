"""Host-only fragment deletion after verified merge, cloud backup and publication.

Python 3.6 compatible. Only verified original FLV fragments are deleted.
The user explicitly requested keeping only the complete recording.
The application containers retain their read-only recording mounts.
"""
import argparse
import base64
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request

STATE=Path(os.environ.get('LIVE_SUMMARY_STATE','/state'))
RECORDS=Path(os.environ.get('LIVE_SUMMARY_RECORDS','/records'))
APP=Path(__file__).resolve().parents[1]


def require(condition):
    if not condition:
        raise AssertionError('consolidation_validation_failed')


def read(path,default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write(path,value):
    temp=path.with_name(path.name+'.tmp')
    with temp.open('w',encoding='utf-8') as out:
        os.chmod(str(temp),0o600)
        json.dump(value,out,ensure_ascii=False,indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(str(temp),str(path))


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            value.update(block)
    return value.hexdigest()


def inside(path,root):
    resolved=path.resolve()
    require(not path.is_symlink() and root.resolve() in resolved.parents)
    return resolved


def fingerprint(relative,path):
    st=path.stat()
    return hashlib.sha256(('{}\0{}\0{}'.format(relative,st.st_size,st.st_mtime_ns)).encode()).hexdigest()


def offline():
    keys=read(STATE/'recorder-secrets.json')
    cfg={**read(APP/'config.example.json'),**read(STATE/'config.json',{})}
    request=urllib.request.Request(cfg['recorder_url']+'/api/room')
    token=base64.b64encode((keys['RECORDER_USER']+':'+keys['RECORDER_PASS']).encode()).decode()
    request.add_header('Authorization','Basic '+token)
    with urllib.request.urlopen(request,timeout=8) as response:
        rooms=json.loads(response.read())
    room=next(r for r in rooms if int(r['roomId'])==cfg['room_id'])
    return not room.get('streaming') and not room.get('recording')


def same_content(source,target):
    return (not target.is_symlink() and target.is_file() and
            source.stat().st_size==target.stat().st_size and digest(source)==digest(target))


def install_verified(source,target,job_id):
    if target.exists():
        require(same_content(source,target))
        return 'existing_verified'
    try:
        os.link(str(source),str(target))
        return 'hardlink'
    except OSError as exc:
        if exc.errno!=errno.EXDEV:
            raise
    # systemd can place these paths on separate bind mounts even when st_dev
    # matches. Keep its narrow write permissions and copy only a verified file.
    require(shutil.disk_usage(str(target.parent)).free>=source.stat().st_size+1024**3)
    temp=target.with_name('.'+target.name+'.consolidation-'+job_id+'.tmp')
    require(not temp.is_symlink())
    if temp.exists() and not same_content(source,temp):
        require(temp.is_file())
        temp.unlink()
    if not temp.exists():
        with source.open('rb') as incoming,temp.open('xb') as outgoing:
            os.chmod(str(temp),0o600)
            shutil.copyfileobj(incoming,outgoing,1024*1024)
            outgoing.flush();os.fsync(outgoing.fileno())
    require(same_content(source,temp))
    try:
        # Link inside the destination mount: atomic, with no overwrite.
        os.link(str(temp),str(target))
    except FileExistsError:
        require(same_content(source,target))
    require(same_content(source,target))
    temp.unlink()
    return 'verified_copy'


def consolidate(row,cfg,apply=False):
    job_id,payload=row
    require(re.fullmatch('[a-f0-9]{24}',job_id))
    payload=json.loads(payload)
    if payload.get('origin')=='baidu_history':
        return None
    if len(payload['files'])<2:
        return None
    directory=STATE/'jobs'/job_id
    receipt=read(directory/'consolidated.json')
    if receipt:
        require(receipt.get('job_id')==job_id)
        pending=directory/'consolidation.pending.json'
        if apply and pending.exists():
            pending.unlink()
        return None
    merge=read(directory/'merge.json',{})
    backup=read(directory/'backup.json',{})
    published=read(directory/'published.json',{})
    require(merge.get('version')==1 and published.get('verified_at',0)>0 and backup.get('verified_at',0)>0)
    require(backup.get('method')=='remote_exact_bytes' and backup.get('merge_sha256')==merge['sha256'])
    require(backup.get('source_fingerprints')==merge['fingerprints'])
    require(sorted(s['fingerprint'] for s in payload['files'])==sorted(merge['fingerprints']))
    record_root=inside(RECORDS/cfg['record_dir'],RECORDS)
    name=merge['name']
    require(Path(name).name==name and name.endswith('-合并.flv'))
    prepared=inside(directory/'package'/cfg['record_dir']/name,directory)
    require(prepared.is_file() and prepared.stat().st_size==merge['bytes'] and digest(prepared)==merge['sha256'])
    xml=prepared.with_suffix('.xml')
    require(digest(xml)==merge['xml_sha256'])
    for path in (prepared,xml,prepared.with_suffix('.merge.json')):
        relative=cfg['record_dir']+'/'+path.name
        require(any(f['relative']==relative and f['size']==path.stat().st_size for f in backup['files']))
    pending=directory/'consolidation.pending.json'
    recovery=read(pending,{})
    if recovery:
        require(recovery.get('fingerprints')==merge['fingerprints'])
    plan=[]
    for source in payload['files']:
        original=inside(RECORDS/source['relative'],record_root)
        if original.exists():
            require(original.is_file() and not original.is_symlink())
            require(original.stat().st_size==source['size'] and fingerprint(source['relative'],original)==source['fingerprint'])
            require(time.time()-original.stat().st_mtime>=cfg['reconnect_grace_seconds'])
        else:
            require(recovery)
        require(source['fingerprint'] in merge['fingerprints'])
        plan.append((original,source))
    final=record_root/name
    for source,target in [(prepared,final),(xml,final.with_suffix('.xml')),
                          (prepared.with_suffix('.merge.json'),final.with_suffix('.merge.json'))]:
        require(not target.is_symlink())
        if target.exists():
            require(os.path.samefile(str(source),str(target)) or same_content(source,target))
    if not apply:
        return {'job_id':job_id,'parts':len(plan),'recording':str(final),'mode':'preview'}
    require(offline())
    write(pending,{'started_at':time.time(),'job_id':job_id,'fingerprints':merge['fingerprints']})
    with sqlite3.connect(str(STATE/'state.sqlite3'),timeout=20) as db:
        db.execute('BEGIN IMMEDIATE')
        for source,target in [(prepared,final),(xml,final.with_suffix('.xml')),
                              (prepared.with_suffix('.merge.json'),final.with_suffix('.merge.json'))]:
            install_verified(source,target,job_id)
            os.chmod(str(target),0o644)
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
        fd=os.open(str(record_root),os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        relative=final.relative_to(RECORDS).as_posix()
        key=fingerprint(relative,final)
        claimed=db.execute('SELECT job_id FROM claimed_files WHERE fingerprint=?',(key,)).fetchone()
        require(not claimed or claimed[0]==job_id)
        db.execute('INSERT OR IGNORE INTO claimed_files VALUES(?,?)',(key,job_id))
        for original,source in plan:
            if original.exists():
                require(offline())
                require(not original.is_symlink() and original.suffix=='.flv')
                require(fingerprint(source['relative'],original)==source['fingerprint'])
                original.unlink()
        fd=os.open(str(record_root),os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        db.commit()
    result={'verified_at':time.time(),'job_id':job_id,'parts':len(plan),
            'recording':relative,'deleted_fragments':[s['relative'] for _,s in plan],
            'mode':'fragments_deleted_complete_recording_retained'}
    write(directory/'consolidated.json',result)
    pending.unlink()
    return result



def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    with (STATE/'consolidation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        cfg={**read(APP/'config.example.json'),**read(STATE/'config.json',{})}
        if cfg.get('delete_completed_media'):
            spec=importlib.util.spec_from_file_location('completed_media',APP/'tools/24-clean-completed-media.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            result=module.run(cfg,args.apply)
            module.write(STATE/'media-cleanup-last.json',result)
            print(json.dumps(result,ensure_ascii=False))
            return
        if not offline():
            print(json.dumps({'waiting':'room_live','changed':[]}))
            return
        with sqlite3.connect(str(STATE/'state.sqlite3'),timeout=20) as db:
            rows=db.execute("SELECT id,payload FROM jobs WHERE status='done'").fetchall()
        results=[]
        for row in rows:
            result=consolidate(row,cfg,args.apply)
            if result:
                results.append(result)
        print(json.dumps({'changed':results},ensure_ascii=False))


if __name__=='__main__':
    main()
