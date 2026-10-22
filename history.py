"""A finite, serial Baidu history batch. Never uploads or mutates cloud videos."""
import collections
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import time
import core
import pipeline
import providers
import storage
from progress import download_percent
import audio_overlap
import scheduler


def valid_remote(path):
    p=PurePosixPath(path)
    if (not path.startswith(core.settings()['baidu_root'].rstrip('/')+'/') or '..' in p.parts or '\\' in path
            or '\n' in path or '\r' in path):
        raise providers.ProviderError('history_invalid_remote_path')
    return p


def metadata(paths):
    for path in paths:
        valid_remote(path)
    text=pipeline.baidu_command(['meta',*paths])
    blocks=re.split(r'(?m)^\[\d+\] - ',text)[1:]
    result={}
    for block in blocks:
        name=re.search(r'文件路径\s+(.+?)\s*$',block,re.M)
        size=re.search(r'文件大小\s+(\d+)\s*,',block)
        fsid=re.search(r'fs_id\s+(\d+)',block)
        md5=re.search(r'md5\s+(\([^)]*\))?\s*([0-9a-f]{32})',block,re.I)
        if not name or not size or not fsid:
            raise providers.ProviderError('history_remote_metadata_incomplete')
        path=name[1].strip()
        result[path]={'remote':path,'size':int(size[1]),'fs_id':fsid[1],
            'md5':md5[2] if md5 else None,'md5_reliable':bool(md5 and '可能不正确' not in (md5[1] or ''))}
    if set(result)!=set(paths):
        raise providers.ProviderError('history_remote_metadata_missing')
    return result


def plan(inventory,limit=10,before=None,exclude_names=None,allow_partial=False):
    """Explicit batch scope: same title; large same-title gaps split.

    This finite reviewed batch has distinct day/evening titles. Actual media
    durations are checked against the next file before paid transcription.
    """
    before=before or core.settings().get('history_before','21000101')
    if limit not in (10,20) or not re.fullmatch(r'\d{8}',before):
        raise providers.ProviderError('history_batch_scope_requires_review')
    names=collections.defaultdict(list)
    for f in inventory['files']:
        if not f['name'].startswith('录制-'+str(core.settings()['room_id'])+'-') or not f['name'].endswith('.flv'):
            continue
        if f['name'] in (exclude_names or set()):
            continue
        if PurePosixPath(f['name']).name!=f['name']:
            raise providers.ProviderError('history_invalid_filename')
        try:
            meta=core.recording_meta(f['name'])
        except ValueError:
            continue
        if meta['started_at'][:10].replace('-','')>=before:
            continue
        valid_remote(f['path'])
        names[f['name']].append(f)
    groups=[]
    for name,copies in sorted(names.items(),key=lambda item:core.recording_meta(item[0])['start_unix']):
        meta=core.recording_meta(name)
        if (not groups or groups[-1]['title']!=meta['title']
                or meta['start_unix']-groups[-1]['last_start']>4*3600):
            groups.append({'date':meta['started_at'][:10],'title':meta['title'],
                           'started_at':meta['started_at'],'files':[]})
        selected=sorted(copies,key=lambda f:(f.get('md5_uncertain',True),len(f['path'])))[0]
        groups[-1]['files'].append({'name':name,'remote':selected['path'],
            'aliases':[f['path'] for f in copies],'relative':core.settings()['record_dir']+'/'+name})
        groups[-1]['last_start']=meta['start_unix']
    # Explicitly reviewed title-change preludes may belong to the next session.
    # Never infer these links solely from similar titles or delete any content.
    for link in inventory.get('reviewed_session_links',[]):
        index=next((i for i,g in enumerate(groups) if len(g['files'])==1 and g['files'][0]['name']==link['before']),None)
        if index is None or index+1>=len(groups) or groups[index+1]['files'][0]['name']!=link['after']:
            raise providers.ProviderError('history_reviewed_session_link_invalid')
        first,second=groups[index:index+2]
        gap=core.recording_meta(link['after'])['start_unix']-core.recording_meta(link['before'])['start_unix']
        if not 0<gap<=900 or not link.get('reason'):
            raise providers.ProviderError('history_reviewed_session_link_boundary_invalid')
        groups[index:index+2]=[{**second,'date':first['date'],'started_at':first['started_at'],
            'files':first['files']+second['files'],'reviewed_grouping':link}]
    chosen=list(reversed(groups))[:limit]
    if len(chosen)!=limit and not (allow_partial and chosen):
        raise providers.ProviderError('history_not_enough_sessions')
    return chosen


def start_batch(inventory):
    existing=core.read_json(core.STATE/'history-batch.json')
    if existing:
        return existing
    archive=core.settings().get('transcript_archive') or {}
    keys=core.read_json(core.STATE/'r2-secrets.json',{})
    if archive.get('backend')!='r2' or not all(keys.get(k) for k in ('R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY')):
        raise providers.ProviderError('history_requires_configured_private_r2')
    chosen=plan(inventory)
    paths=[path for g in chosen for f in g['files'] for path in f['aliases']]
    exact=metadata(paths)
    allpaths={f['path'] for f in inventory['files']}
    batch_id='history-'+hashlib.sha256(json.dumps(chosen,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
    jobs=[]
    for position,g in enumerate(chosen,1):
        files=[]
        for f in g['files']:
            candidates=[exact[p] for p in f['aliases']]
            if len({x['size'] for x in candidates})!=1:
                raise providers.ProviderError('history_duplicate_size_conflict')
            reliable={x['md5'] for x in candidates if x['md5_reliable']}
            if len(reliable)>1:
                raise providers.ProviderError('history_duplicate_hash_conflict')
            selected=sorted(candidates,key=lambda x:(not x['md5_reliable'],len(x['remote'])))[0]
            xml=str(PurePosixPath(selected['remote']).with_suffix('.xml'))
            if xml not in allpaths:
                raise providers.ProviderError('history_xml_missing')
            files.append({**f,**selected,'xml_remote':xml})
        job_id=hashlib.sha256(('history-v1|'+ '|'.join(f['name']+':'+str(f['size']) for f in files)).encode()).hexdigest()[:24]
        payload={'origin':'baidu_history','batch_id':batch_id,'batch_position':position,
            'batch_limit':10,'session_id':None,'files':files,
            'metadata':{'room_id':core.settings()['room_id'],'streamer':core.settings()['streamer'],
                        'title':g['title'],'started_at':g['started_at']}}
        jobs.append((job_id,payload))
    now=time.time()
    with core.database() as db:
        for position,(job_id,payload) in enumerate(jobs):
            db.execute('INSERT OR IGNORE INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',
                (job_id,now+position/1000,now,'queued',json.dumps(payload,ensure_ascii=False)))
    batch={'id':batch_id,'created_at':now,'limit':10,'active':True,
           'jobs':[job_id for job_id,_ in jobs],
           'sessions':[{'id':job_id,'position':p['batch_position'],**p['metadata'],'parts':len(p['files']),
                        'bytes':sum(f['size'] for f in p['files'])} for job_id,p in jobs]}
    core.atomic_json(core.STATE/'history-batch.json',batch)
    return batch


def append_twenty(inventory,after=10,allow_partial=False):
    """Append at most twenty; a nonempty exhausted tail needs explicit opt-in."""
    batch=core.read_json(core.STATE/'history-batch.json',{})
    if type(after) is not int or after<10 or (after-10)%20:
        raise providers.ProviderError('history_extension_invalid_start')
    extension_id='history-next20-after-test10' if after==10 else 'history-next20-after-'+str(after)
    if any(e.get('id')==extension_id for e in batch.get('extensions',[])):
        return batch
    if batch.get('limit')!=after or len(batch.get('jobs',[]))!=after:
        raise providers.ProviderError('history_extension_queue_length_changed')
    target=core.STATE/('history-extension-next20.json' if after==10 else 'history-extension-next20-after'+str(after)+'.json')
    prior=core.read_json(target,{})
    prepared_names={f['name'] for _,p in prior.get('jobs',[]) for f in p['files']}
    excluded={}
    for row in core.job_rows():
        for source in json.loads(row['payload'])['files']:
            name=Path(source['relative']).name
            if name not in prepared_names:
                excluded[name]=True
    chosen=plan(inventory,limit=20,exclude_names=set(excluded),allow_partial=allow_partial)
    total=after+len(chosen)
    # The next batch must continue towards earlier sessions, never fill newer
    # gaps unexpectedly or re-enqueue any file already known to the database.
    if any(g['started_at']>=batch['sessions'][-1]['started_at'] for g in chosen):
        raise providers.ProviderError('history_extension_not_older_than_initial_batch')
    exact=metadata([p for g in chosen for f in g['files'] for p in f['aliases']])
    for group in chosen:
        link=group.get('reviewed_grouping')
        if link and any(exact[path]['size']!=link['before_bytes'] for path in group['files'][0]['aliases']):
            raise providers.ProviderError('history_reviewed_prelude_size_changed')
    allpaths={f['path'] for f in inventory['files']}
    jobs=[]
    for position,g in enumerate(chosen,after+1):
        files=[]
        for f in g['files']:
            candidates=[exact[p] for p in f['aliases']]
            if len({x['size'] for x in candidates})!=1:
                raise providers.ProviderError('history_duplicate_size_conflict')
            if len({x['md5'] for x in candidates if x['md5_reliable']})>1:
                raise providers.ProviderError('history_duplicate_hash_conflict')
            selected=sorted(candidates,key=lambda x:(not x['md5_reliable'],len(x['remote'])))[0]
            xml=str(PurePosixPath(selected['remote']).with_suffix('.xml'))
            if xml not in allpaths:
                raise providers.ProviderError('history_xml_missing')
            files.append({**f,**selected,'xml_remote':xml})
        job_id=hashlib.sha256(('history-v1|'+'|'.join(f['name']+':'+str(f['size']) for f in files)).encode()).hexdigest()[:24]
        if job_id in batch['jobs']:
            raise providers.ProviderError('history_extension_duplicate_job')
        payload={'origin':'baidu_history','batch_id':batch['id'],'batch_position':position,
            'batch_limit':total,'session_id':None,'files':files,
            'metadata':{'room_id':core.settings()['room_id'],'streamer':core.settings()['streamer'],
                        'title':g['title'],'started_at':g['started_at']}}
        if g.get('reviewed_grouping'):
            payload['reviewed_grouping']=g['reviewed_grouping']
        jobs.append((job_id,payload))
    now=time.time()
    # Save the immutable prepared list first. Re-entry after a crash reuses it,
    # since jobs are only appended with deterministic IDs and INSERT OR IGNORE.
    prepared={'id':extension_id,'created_at':now,'requested':20,'added':len(jobs),'jobs':jobs}
    if target.exists():
        prepared=core.read_json(target)
        if [j[0] for j in prepared['jobs']]!=[j[0] for j in jobs]:
            raise providers.ProviderError('history_extension_prepared_plan_changed')
    else:
        core.atomic_json(target,prepared)
    with core.database() as db:
        for position,(job_id,payload) in enumerate(jobs):
            db.execute('INSERT OR IGNORE INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',
                (job_id,now+position/1000,now,'queued',json.dumps(payload,ensure_ascii=False)))
        # Keep all dashboard labels consistent with the new total.
        for job_id in batch['jobs']:
            row=db.execute('SELECT payload FROM jobs WHERE id=?',(job_id,)).fetchone()
            payload=json.loads(row['payload']);payload['batch_limit']=total
            db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),job_id))
    added=[job_id for job_id,_ in jobs]
    batch['jobs'].extend(added);batch['limit']=total
    batch['sessions'].extend({'id':job_id,'position':p['batch_position'],**p['metadata'],
        'parts':len(p['files']),'bytes':sum(f['size'] for f in p['files'])} for job_id,p in jobs)
    batch.setdefault('extensions',[]).append({'id':extension_id,'created_at':now,'requested':20,'added':len(added),'jobs':added})
    core.atomic_json(core.STATE/'history-batch.json',batch)
    return batch


def next_job(db,now):
    batch=core.read_json(core.STATE/'history-batch.json',{})
    if not batch.get('active'):
        return None
    for job_id in batch['jobs']:
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row and row['status']=='failed' and row['attempts']>=5:
            continue
        if row and row['status']!='done':
            return row if row['status'] in ('queued','retry') and row['retry_at']<=now else None
    return None


def release_skipped_cache():
    """Release only failed-five-times jobs' re-downloadable FLV caches."""
    cache=core.STATE/'history-cache'
    rows={r['id']:r for r in core.job_rows()}
    released=[]
    for path in cache.rglob('*.flv'):
        parts=path.relative_to(cache).parts
        if len(parts)!=3 or not re.fullmatch('[a-f0-9]{24}',parts[0]) or not parts[1].isdigit():
            continue
        row=rows.get(parts[0])
        if not row or row['status']!='failed' or row['attempts']<5:
            continue
        payload=json.loads(row['payload'])
        if payload.get('origin')!='baidu_history':
            continue
        index=int(parts[1])
        if index>=len(payload['files']) or payload['files'][index]['name']!=path.name:
            raise providers.ProviderError('history_skipped_cache_identity_mismatch')
        source=payload['files'][index]
        actual=metadata([source['remote']])[source['remote']]
        if actual['size']!=source['size'] or actual['fs_id']!=source['fs_id']:
            raise providers.ProviderError('history_skipped_cache_remote_changed')
        marker=path.with_name(path.name+'.BaiduPCS-Go-downloading')
        if marker.is_symlink():
            raise providers.ProviderError('history_skipped_cache_marker_symlink')
        pipeline.require_offline()
        release_video(path,cache)
        marker.unlink(missing_ok=True)
        record={'released_at':time.time(),'source':source['remote'],'size':source['size'],
            'fs_id':source['fs_id'],'original_cloud_video_preserved':True,'paid_checkpoints_preserved':True}
        core.atomic_json(core.STATE/'jobs'/row['id']/'skipped-cache-release.json',record)
        released.append(row['id'])
    return released


def download(remote,directory,expected,progress):
    name=valid_remote(remote).name
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/name
    marker=directory/(name+'.BaiduPCS-Go-downloading')
    if target.is_symlink():
        raise providers.ProviderError('history_cache_symlink')
    if target.is_file() and target.stat().st_size==expected and not marker.exists():
        return target
    if target.exists() and not marker.is_file():
        # Never let the CLI silently skip a short final file left by an error.
        raise providers.ProviderError('history_partial_final_file_needs_review')
    if shutil.disk_usage(core.STATE).free<expected+2*1024**3:
        raise providers.ProviderError('history_insufficient_disk_space')
    log=directory/'download.log'
    env={**os.environ,'BAIDUPCS_GO_CONFIG_DIR':'/state/baidu'}
    for mode in ('locate','pcs'):
        with log.open('w') as out:
            os.chmod(log,0o600)
            process=subprocess.Popen([core.settings()['baidu_binary'],'download','--saveto',str(directory),
                '-p','2','-l','1','--retry','2','--nocheck','--mode',mode,remote],stdout=out,stderr=subprocess.STDOUT,env=env)
            deadline=time.monotonic()+12*3600
            try:
                while process.poll() is None:
                    with log.open('rb') as stream:
                        stream.seek(max(0,log.stat().st_size-8192))
                        text=stream.read().decode('utf-8',errors='replace')
                    percent=download_percent(text,expected)
                    progress(download_percent=percent,download_activity_at=log.stat().st_mtime)
                    if time.monotonic()>deadline:
                        raise providers.ProviderError('history_download_timeout')
                    time.sleep(5)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait()
        if process.returncode==0 and target.is_file() and target.stat().st_size==expected and not marker.exists():
            return target
    raise providers.ProviderError('history_download_failed_or_size_mismatch')


def hash_file(path,algorithm='sha256'):
    h=hashlib.new(algorithm)
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def write_transcript(directory,transcript):
    core.atomic_json(directory/'transcript.json',transcript)
    (directory/'transcript.txt').write_text('\n'.join(
        f"[{s['start_seconds']:.3f}] {s['text']}" for s in transcript['segments']),encoding='utf-8')


def metadata_only_flv(path):
    """Recognize FLV headers/script tags with no audio or video tags.

    A broken file containing even an incomplete AV tag is not classified empty.
    Never decide this from file size or the generic FFprobe error alone.
    """
    size=path.stat().st_size
    with path.open('rb') as stream:
        header=stream.read(9)
        if len(header)!=9 or header[:4]!=b'FLV\x01':return None
        offset=int.from_bytes(header[5:9],'big')
        if offset<9 or offset+4>size:return None
        stream.seek(offset)
        if stream.read(4)!=b'\0\0\0\0':return None
        tags=0
        while stream.tell()<size:
            tag=stream.read(11)
            if len(tag)!=11 or tag[0]!=18:return None
            length=int.from_bytes(tag[1:4],'big');tags+=1
            if stream.tell()+length+4>size:
                return {'reason':'truncated_metadata_without_media','bytes':size,'script_tags':tags}
            stream.seek(length,1)
            if int.from_bytes(stream.read(4),'big')!=11+length:return None
        return {'reason':'metadata_without_media','bytes':size,'script_tags':tags}


def completed_part(directory):
    marker=core.read_json(directory/'history-part-complete.json',{})
    if not marker:
        return None
    path=directory/'transcript.json'
    receipt=core.read_json(directory/'transcript-archive.json',{})
    if (not path.is_file() or hash_file(path)!=marker['transcript_sha256']
            or hash_file(directory/'evidence.json')!=marker['evidence_sha256'] or not receipt.get('verified_at')):
        raise providers.ProviderError('history_part_checkpoint_corrupt')
    if marker.get('boundary_sha256'):
        boundary=directory/'boundary-to-next.json'
        if not boundary.is_file() or hash_file(boundary)!=marker['boundary_sha256']:
            raise providers.ProviderError('history_audio_boundary_checkpoint_corrupt')
    return core.read_json(directory/'evidence.json')


def release_video(path,cache):
    resolved=path.resolve()
    if path.is_symlink() or path.suffix!='.flv' or not resolved.is_relative_to(cache.resolve()):
        raise providers.ProviderError('history_invalid_cache_cleanup')
    path.unlink(missing_ok=True)


def session_boundary(source,following,length,sha,directory):
    """Long reconnect gaps require a review bound to this exact source pair."""
    gap=core.recording_meta(following['name'])['start_unix']-core.recording_meta(source['name'])['start_unix']-length
    if gap < -audio_overlap.WINDOW_SECONDS:
        raise providers.ProviderError('history_session_boundary_needs_review')
    if gap<=900:
        return gap,None
    expected={'source_name':source['name'],'source_sha256':sha,'source_duration_seconds':length,
        'gap_seconds':gap,'next_source_name':following['name'],'next_remote':following['remote'],
        'next_bytes':following['size'],'next_fs_id':following['fs_id']}
    review=core.read_json(directory/'session-boundary-review.json',{})
    matched=(review.get('action')=='keep_same_session' and bool(review.get('reason'))
        and isinstance(review.get('reviewed_at'),(int,float)) and math.isfinite(review['reviewed_at']) and review['reviewed_at']>0)
    for key,value in expected.items():
        actual=review.get(key)
        if key in ('source_duration_seconds','gap_seconds'):
            matched=matched and type(actual) in (int,float) and math.isfinite(actual) and abs(actual-value)<0.001
        else:
            matched=matched and actual==value
    if not matched:
        core.atomic_json(directory/'boundary-review-required.json',expected)
        raise providers.ProviderError('history_session_boundary_needs_review')
    return gap,review


def process_job(job_id,payload):
    directory=core.STATE/'jobs'/job_id
    directory.mkdir(parents=True,exist_ok=True)
    cache=core.STATE/'history-cache'
    progress_state={'total_parts':len(payload['files']),'completed_parts':0,
                    'batch_position':payload['batch_position'],'batch_limit':payload['batch_limit']}
    def progress(**fields):
        progress_state.update(fields)
        core.atomic_json(directory/'history-progress.json',{**progress_state,'updated_at':time.time()})
    def stage(value):
        scheduler.history_checkpoint()
        core.update_job(job_id,stage=value)
        progress(stage=value)
    segments=[];buckets=collections.defaultdict(collections.Counter)
    count=0;offset=0.0;missing=False;provenance=[];bucket_totals=collections.Counter();caveats=[];seen_comments=set();skipped_media=0
    for index,source in enumerate(payload['files']):
        scheduler.history_checkpoint()
        partdir=directory/f'file-{index:03d}'
        partdir.mkdir(exist_ok=True)
        current_cache=cache/job_id/str(index)
        cached=current_cache/source['name']
        progress(current_part=index+1,current_file=source['name'],download_percent=None,current_trim_seconds=None,overlap_check=None)
        evidence=completed_part(partdir)
        if evidence:
            release_video(cached,cache)
        else:
            pipeline.require_offline()
            # No new file may be fetched while another FLV is cached.
            release_skipped_cache()
            others=[p for p in cache.rglob('*.flv') if p!=cached]
            if others:
                raise providers.ProviderError('history_other_video_still_cached')
            remote=metadata([source['remote'],source['xml_remote']])
            actual=remote[source['remote']]
            if actual['size']!=source['size'] or actual['fs_id']!=source['fs_id']:
                raise providers.ProviderError('history_remote_source_changed')
            stage('downloading')
            video=download(source['remote'],current_cache,source['size'],progress)
            xml=download(source['xml_remote'],partdir,remote[source['xml_remote']]['size'],lambda **f:None)
            stage('verifying_download')
            sha=hash_file(video)
            if actual['md5_reliable'] and hash_file(video,'md5')!=actual['md5']:
                raise providers.ProviderError('history_download_md5_mismatch')
            seen=core.read_json(core.STATE/'history-content.json',{})
            identity=job_id+':'+str(index)
            unavailable=None
            try:
                length=pipeline.probe(video)
            except (providers.ProviderError,KeyError,ValueError):
                unavailable=metadata_only_flv(video)
                if unavailable is None:raise
                length=0.0
            if sha in seen and seen[sha]!=identity and not unavailable:
                raise providers.ProviderError('history_duplicate_content_review_required')
            boundary=partdir/'boundary-to-next.json'
            reviewed_boundary=None
            if not unavailable and index+1<len(payload['files']):
                gap,reviewed_boundary=session_boundary(source,payload['files'][index+1],length,sha,partdir)
                if gap < -5:
                    stage('verifying_audio_overlap')
                    core.atomic_json(boundary,{'source_sha256':sha,'next_source_name':payload['files'][index+1]['name'],
                        'expected_overlap_seconds':-gap,'tail_packets':audio_overlap.packet_window(video,'tail')})
            trim=0;alignment=None
            if index and not unavailable:
                previous=directory/f'file-{index-1:03d}'
                reference=core.read_json(previous/'boundary-to-next.json',{})
                if reference:
                    previous_evidence=completed_part(previous)
                    if (not previous_evidence or reference.get('source_sha256')!=previous_evidence['sha256']
                            or reference.get('next_source_name')!=source['name']):
                        raise providers.ProviderError('history_audio_boundary_source_mismatch')
                    stage('verifying_audio_overlap')
                    alignment=audio_overlap.confirm_overlap(reference['tail_packets'],
                        audio_overlap.packet_window(video,'head'),reference['expected_overlap_seconds'])
                    trim=alignment['trim_seconds']
                    if trim>length+0.1:
                        raise providers.ProviderError('history_audio_overlap_out_of_bounds')
                    trim=min(trim,length)
                    core.atomic_json(partdir/'audio-overlap.json',{'source_sha256':sha,
                        'previous_source_sha256':previous_evidence['sha256'],**alignment})
                    progress(current_trim_seconds=trim,overlap_check=alignment['status'])
            stage('extracting_audio')
            effective_length=round(max(0,length-trim),6)
            parts=(pipeline.audio_parts(video,partdir,effective_length,source_offset=trim) if trim else
                   pipeline.audio_parts(video,partdir,length)) if effective_length>0.1 else []
            local_segments=[]
            for part_index,part in enumerate(parts):
                stage('transcribing')
                local_segments.extend(pipeline.normalized_transcript(
                    providers.transcribe(part['path'],partdir/f'asr-{part_index:03d}.json'),part['start']))
            digest=pipeline.danmaku_digest(xml,0,length)
            meta={'room_id':core.settings()['room_id'],'streamer':core.settings()['streamer'],
                  'leading_trim_seconds':trim,'source_duration_seconds':length,
                  **{k:core.recording_meta(source['name'])[k] for k in ('title','started_at')}}
            if unavailable:
                meta['media_unavailable']={**unavailable,'unlocated_danmaku_count':digest['count']}
            write_transcript(partdir,{'metadata':meta,'duration_seconds':effective_length,'segments':local_segments})
            evidence={'duration':effective_length,'source_duration':length,'leading_trim_seconds':trim,
                'audio_alignment':alignment,'danmaku':digest,'sha256':sha,'source_name':source['name']}
            if unavailable:
                evidence['media_unavailable']=meta['media_unavailable']
                evidence['danmaku']={'count':0,'missing':digest['missing'],'buckets':[]}
            if reviewed_boundary:
                evidence['reviewed_reconnect_gap']=reviewed_boundary
            core.atomic_json(partdir/'evidence.json',evidence)
            stage('archiving_part')
            part_id=hashlib.sha256(identity.encode()).hexdigest()[:24]
            receipt=storage.archive(partdir,part_id,meta)
            if not receipt.get('verified_at'):
                raise providers.ProviderError('history_private_r2_required_before_cleanup')
            marker={'verified_at':time.time(),'transcript_sha256':hash_file(partdir/'transcript.json'),
                    'evidence_sha256':hash_file(partdir/'evidence.json'),'part_id':part_id}
            if boundary.exists():
                marker['boundary_sha256']=hash_file(boundary)
            core.atomic_json(partdir/'history-part-complete.json',marker)
            seen[sha]=identity;core.atomic_json(core.STATE/'history-content.json',seen)
            release_video(video,cache)
        transcript=core.read_json(partdir/'transcript.json')
        if evidence.get('media_unavailable'):
            skipped_media+=1
            omitted=evidence['media_unavailable']['unlocated_danmaku_count']
            caveats.append(f'第 {index+1} 个录播分片仅有文件头/元数据，没有可用音视频，已跳过转写且未推测时长。原弹幕XML保留；其中{omitted}条弹幕因缺少媒体时间定位，未纳入带时间点的弹幕热点。')
        if evidence.get('reviewed_reconnect_gap'):
            seconds=round(evidence['reviewed_reconnect_gap']['gap_seconds'])
            caveats.append(f'相邻录播片段之间约{seconds}秒无可用音视频，已保留前后全部片段；时间点按现有分片连续拼接计算。')
        if any(core.read_json(p,{}).get('result',{}).get('empty_reason')=='ASR_RESPONSE_HAVE_NO_WORDS'
                for p in partdir.glob('asr-*.json')):
            caveats.append(f'第 {index+1} 个录播分片中有音轨未识别到文字，已跳过对应转写，时长及弹幕保留。')
        extra_trim=0;alignment=evidence.get('audio_alignment')
        adjustment=core.read_json(partdir/'audio-overlap-adjustment.json',{})
        if adjustment:
            if (adjustment.get('source_sha256')!=evidence['sha256'] or
                    adjustment.get('transcript_sha256')!=hash_file(partdir/'transcript.json') or
                    adjustment.get('alignment',{}).get('status')!='confirmed_exact_audio'):
                raise providers.ProviderError('history_audio_adjustment_evidence_mismatch')
            extra_trim=adjustment['trim_seconds']-evidence.get('leading_trim_seconds',0)
            if not math.isfinite(extra_trim) or not 0<=extra_trim<=evidence['duration']:
                raise providers.ProviderError('history_audio_adjustment_out_of_bounds')
            alignment=adjustment['alignment']
        for sentence in transcript['segments']:
            if sentence['end_seconds']<=extra_trim:
                continue
            if sentence['start_seconds']<extra_trim:
                caveats.append('少量转写句子跨越分片去重边界，已保留整句以免遗漏内容，边界附近可能仍有少量重复文字。')
            segments.append({**sentence,'start_seconds':round(offset+max(0,sentence['start_seconds']-extra_trim),3),
                             'end_seconds':round(offset+sentence['end_seconds']-extra_trim,3)})
        # Re-read preserved XML with the final media offset, rather than shifting
        # a previously rounded minute bucket across a fragment boundary.
        trim=evidence.get('leading_trim_seconds',0)+extra_trim
        digest=({'count':0,'missing':evidence['danmaku']['missing'],'buckets':[]} if evidence.get('media_unavailable') else
            pipeline.danmaku_digest(partdir/PurePosixPath(source['xml_remote']).name,offset-trim,
                evidence.get('source_duration',evidence['duration']),seen_messages=seen_comments))
        for bucket in digest['buckets']:
            stamp=bucket['start_seconds']
            bucket_totals[stamp]+=bucket['count']
            for message in bucket['messages']:
                buckets[stamp][message['text']]+=message['count']
        count+=digest['count']
        missing|=digest['missing']
        effective_length=evidence['duration']-extra_trim
        provenance.append({'source':source['name'],'sha256':evidence['sha256'],'offset':offset,'duration':effective_length,
            'leading_trim_seconds':trim,'audio_alignment':alignment})
        if (alignment or {}).get('status')=='not_confirmed':
            caveats.append('部分历史分片的文件名时间有交叠，但音频未确认重复，因此完整保留，局部可能有少量重复内容。')
        offset+=effective_length
        progress(completed_parts=index+1,skipped_media_parts=skipped_media,
            verified_overlap_seconds=round(sum(p['leading_trim_seconds'] for p in provenance),6))
    stage('combining_transcripts')
    core.atomic_json(directory/'history-sources.json',{'parts':provenance,'batch_id':payload['batch_id']})
    # Preserve actual per-bucket counts even when the top-five messages omit rarer text.
    danmaku={'count':count,'missing':missing,'buckets':[
        {'start_seconds':stamp,'count':bucket_totals[stamp],'messages':[
            {'text':text,'count':n} for text,n in counter.most_common(5)]} for stamp,counter in sorted(buckets.items())]}
    return pipeline.finish_evidence(job_id,payload,directory,payload['metadata'],segments,danmaku,offset,stage,
        extra_caveats=list(dict.fromkeys(caveats)))
