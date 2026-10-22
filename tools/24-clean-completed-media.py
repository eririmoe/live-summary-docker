"""Host Python 3.6: delete only completed, archived jobs' verified media.

Keep XML, transcripts, summaries and all checkpoints. Recording mounts stay RO
in application containers. The existing completion signal/timer invokes this.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.request

STATE=Path(os.environ.get('LIVE_SUMMARY_STATE','/state'))
RECORDS=Path(os.environ.get('LIVE_SUMMARY_RECORDS','/records'))
APP=Path(__file__).resolve().parents[1]

def require(condition):
    if not condition:
        raise ValueError('completed_media_validation_failed')

def read(path,default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

def write(path,value):
    temp=path.with_name(path.name+'.tmp')
    with temp.open('w',encoding='utf-8') as stream:
        os.chmod(str(temp),0o600)
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(str(temp),str(path))

def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            result.update(block)
    return result.hexdigest()

def inside(path,root):
    require(not path.is_symlink() and root.resolve() in path.resolve().parents)
    return path.resolve()

def fingerprint(relative,path):
    stat=path.stat()
    return hashlib.sha256(('{}\0{}\0{}'.format(relative,stat.st_size,stat.st_mtime_ns)).encode()).hexdigest()

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

def verify_remote(files,cfg):
    paths=[]
    for item in files:
        relative=item['relative']
        require(relative.startswith(cfg['record_dir']+'/') and '..' not in Path(relative).parts)
        remote=cfg['baidu_root'].rstrip('/')+'/'+relative
        require(item['remote']==remote and '\n' not in remote and '\r' not in remote)
        paths.append(remote)
    result=subprocess.run([cfg['baidu_binary'],'meta']+paths,env={**os.environ,'BAIDUPCS_GO_CONFIG_DIR':str(STATE/'baidu')},stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        universal_newlines=True,timeout=180)
    require(result.returncode==0)
    found={}
    for block in re.split(r'(?m)^\[\d+\] - ',result.stdout)[1:]:
        path=re.search(r'文件路径\s+(.+?)\s*$',block,re.M)
        size=re.search(r'文件大小\s+(\d+)\s*,',block)
        if path and size:
            found[path[1].strip()]=int(size[1])
    require(set(found)==set(paths))
    require(all(found[f['remote']]==f['size'] for f in files))

def plan(row,cfg):
    job_id,payload=row
    require(re.fullmatch('[a-f0-9]{24}',job_id))
    require(not Path(cfg['record_dir']).is_absolute() and '..' not in Path(cfg['record_dir']).parts and not (RECORDS/cfg['record_dir']).is_symlink())
    payload=json.loads(payload)
    directory=inside(STATE/'jobs'/job_id,STATE)
    require(read(directory/'published.json',{}).get('verified_at',0)>0)
    require((directory/'summary.json').is_file() and (directory/'result.json').is_file())
    archive=read(directory/'transcript-archive.json',{})
    require(archive.get('backend')=='r2' and archive.get('verified_at',0)>0)
    for name in ('transcript.json','transcript.txt'):
        path=inside(directory/name,directory)
        require(path.is_file())
        require(any(o.get('name')==name and o.get('bytes')==path.stat().st_size and
                    o.get('sha256')==digest(path) for o in archive.get('objects',[])))
    result=[]
    def add(path,kind,relative=None,expected_sha=None,expected_fingerprint=None):
        root=RECORDS/cfg['record_dir'] if kind=='recording' else directory
        path=inside(path,root)
        if path.exists():
            require(path.is_file() and path.suffix in ('.flv','.mp3'))
            if expected_sha:
                require(digest(path)==expected_sha)
            if expected_fingerprint:
                require(fingerprint(relative,path)==expected_fingerprint)
            stat=path.stat()
            if kind=='recording':
                require(time.time()-stat.st_mtime>=cfg['reconnect_grace_seconds'])
            result.append({'path':str(path),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                           'kind':kind,'sha256':expected_sha})
    if payload.get('origin')!='baidu_history':
        backup=read(directory/'backup.json',{})
        require(backup.get('verified_at',0)>0 and backup.get('method')=='remote_exact_bytes')
        backed=[f for f in backup.get('files',[]) if f['relative'].endswith('.flv')]
        require(len(backed)==1)
        if len(payload['files'])>1:
            merge=read(directory/'merge.json',{})
            require(merge.get('version')==1 and backup.get('merge_sha256')==merge['sha256'])
            require(sorted(backup.get('source_fingerprints',[]))==sorted(merge['fingerprints'])==
                    sorted(s['fingerprint'] for s in payload['files']))
            require(Path(merge['name']).name==merge['name'] and merge['name'].endswith('-合并.flv'))
            require(backed[0]['relative']==cfg['record_dir']+'/'+merge['name'] and backed[0]['size']==merge['bytes'])
            add(directory/'package'/cfg['record_dir']/merge['name'],'package',expected_sha=merge['sha256'])
            add(RECORDS/cfg['record_dir']/merge['name'],'recording',expected_sha=merge['sha256'])
        else:
            source=payload['files'][0]
            require(backed[0]['relative']==source['relative'] and backed[0]['size']==source['size'])
        for source in payload['files']:
            require(source['relative'].endswith('.flv'))
            add(RECORDS/source['relative'],'recording',relative=source['relative'],expected_fingerprint=source['fingerprint'])
    else:
        for index,source in enumerate(payload['files']):
            cached=STATE/'history-cache'/job_id/str(index)/source['name']
            require(not cached.is_symlink() and (STATE/'history-cache').resolve() in cached.resolve().parents)
            if cached.exists():
                evidence=read(directory/('file-%03d'%index)/'evidence.json',{})
                require(cached.suffix=='.flv' and digest(cached)==evidence['sha256'])
                stat=cached.stat()
                result.append({'path':str(cached.resolve()),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                               'kind':'history-cache','sha256':evidence['sha256']})
    for part in sorted(directory.glob('file-*')):
        require(not part.is_symlink())
        for audio in sorted(part.glob('audio-*.mp3')):
            require(re.fullmatch(r'audio-\d{3}(?:\.part)?\.mp3',audio.name))
            add(audio,'audio')
    if result and payload.get('origin')!='baidu_history':
        # Verify the cloud recording still exists before removing local media.
        verify_remote(backed,cfg)
    return result

def clean(row,cfg,apply=False):
    chosen=plan(row,cfg)
    if not chosen:
        return None
    report={'job_id':row[0],'verified_at':time.time(),'mode':'apply' if apply else 'preview',
        'files':chosen,'deleted':[],'released_bytes':0}
    if apply:
        require(cfg.get('delete_completed_media') is True and offline())
        directory=STATE/'jobs'/row[0]
        write(directory/'media-cleanup.pending.json',report)
        for item in chosen:
            require(offline())
            path=Path(item['path']);require(not path.is_symlink() and path.is_file())
            stat=path.stat()
            require(stat.st_size==item['bytes'] and stat.st_mtime_ns==item['mtime_ns'])
            if item['sha256']:
                require(digest(path)==item['sha256'])
            path.unlink()
            report['deleted'].append(item['path']);report['released_bytes']+=item['bytes']
            write(directory/'media-cleanup.pending.json',report)
        report['completed_at']=time.time()
        write(directory/'media-cleanup.json',report)
        (directory/'media-cleanup.pending.json').unlink()
    return report

def run(cfg,apply=False):
    require(not apply or cfg.get('delete_completed_media') is True)
    if not offline():
        return {'waiting':'room_live','changed':[]}
    with sqlite3.connect(str(STATE/'state.sqlite3'),timeout=20) as db:
        rows=db.execute("SELECT id,payload FROM jobs WHERE status='done'").fetchall()
    results=[];errors=[]
    for row in rows:
        try:
            result=clean(row,cfg,apply)
            if result:
                results.append(result)
        except Exception as exc:
            errors.append({'job_id':row[0],'error':str(exc) if isinstance(exc,ValueError) else type(exc).__name__})
    return {'verified_at':time.time(),'mode':'apply' if apply else 'preview','changed':results,'errors':errors,
        'released_bytes':sum(r['released_bytes'] for r in results)}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    with (STATE/'consolidation.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        cfg={**read(APP/'config.example.json'),**read(STATE/'config.json',{})}
        result=run(cfg,args.apply)
        write(STATE/'media-cleanup-last.json',result)
        print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    main()
