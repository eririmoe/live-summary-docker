"""Offline broadcast grouping and lossless, verified media preparation.

Original recordings remain read-only here. Host-side consolidation may delete them
only after this job's backup and publication have both succeeded.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from xml.etree import ElementTree as XML
from defusedxml import ElementTree
import core
from providers import ProviderError


def media_info(path):
    result = subprocess.run(['ffprobe','-v','error','-show_format','-show_streams',
        '-show_data_hash','sha256','-of','json',str(path)],capture_output=True,text=True,timeout=90)
    if result.returncode:
        raise ProviderError('merge_probe_failed')
    value = json.loads(result.stdout)
    duration = float(value['format']['duration'])
    if not math.isfinite(duration) or duration<=0:
        raise ProviderError('merge_invalid_duration')
    streams = [s for s in value['streams'] if s['codec_type'] in ('audio','video')]
    if len(streams)!=len(value['streams']) or not any(s['codec_type']=='audio' for s in streams):
        raise ProviderError('merge_unsupported_stream_layout')
    fields = ('codec_type','codec_name','time_base','width','height','pix_fmt',
              'sample_rate','channels','extradata_hash')
    return {'duration':duration,'streams':[{k:s.get(k) for k in fields} for s in streams]}


def groups(paths, session_ids=None):
    """A date boundary is irrelevant; a >15 minute interruption starts a new job.

    Even matching SessionIds cannot override a long gap. Missing/different IDs
    are expected when the recorder reconnects or the service restarts.
    """
    cfg = core.settings()
    session_ids = session_ids or {}
    cache = core.read_json(core.STATE/'group-probes.json',{})
    output=[]
    end=None
    for relative in sorted(set(paths),key=lambda p:core.recording_meta(p)['start_unix']):
        source = core.identify(relative)
        key = source['fingerprint']
        if key not in cache:
            cache[key] = media_info(core.source_path(relative))['duration']
        start = core.recording_meta(relative)['start_unix']
        gap = start-end if end is not None else None
        if gap is None or gap>cfg['merge_gap_seconds'] or gap < -5:
            output.append({'paths':[],'session_ids':[]})
        output[-1]['paths'].append(relative)
        sid=session_ids.get(relative)
        if sid and sid not in output[-1]['session_ids']:
            output[-1]['session_ids'].append(sid)
        end=max(end or 0,start+cache[key])
    core.atomic_json(core.STATE/'group-probes.json',cache)
    return output


def packet_evidence(paths):
    """Stream packet hashes in bounded memory, with per-stream timestamp checks."""
    hashes={}
    counts={}
    sizes={}
    for path in paths:
        previous={}
        process = subprocess.Popen(['ffprobe','-v','error','-show_packets',
            '-show_data_hash','sha256','-show_entries','packet=stream_index,dts_time,size,data_hash',
            '-of','compact=p=0',str(path)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        try:
            for line in process.stdout:
                fields=dict(item.split('=',1) for item in line.strip().split('|') if '=' in item)
                if 'stream_index' not in fields:
                    continue
                index=int(fields['stream_index'])
                stamp=float(fields['dts_time'])
                if not math.isfinite(stamp) or stamp<previous.get(index,-math.inf)-0.001:
                    raise ProviderError('merge_non_monotonic_timestamps')
                previous[index]=stamp
                digest=fields.get('data_hash','')
                if not digest.startswith('SHA256:'):
                    raise ProviderError('merge_packet_hash_missing')
                hashes.setdefault(index,hashlib.sha256()).update(digest.encode())
                counts[index]=counts.get(index,0)+1
                sizes[index]=sizes.get(index,0)+int(fields['size'])
            if process.wait(timeout=90):
                raise ProviderError('merge_packet_validation_failed')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    return {str(i):{'packets':counts[i],'bytes':sizes[i],'sha256':h.hexdigest()} for i,h in hashes.items()}


def file_hash(path):
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def h264_header_change_only(infos):
    """Only permit AVC parameter-set changes with identical stream layouts."""
    def layout(info):
        if not all(isinstance(s,dict) for s in info['streams']):
            return None
        return [{k:v for k,v in s.items() if not (k=='extradata_hash' and
            s.get('codec_type')=='video' and s.get('codec_name')=='h264')} for s in info['streams']]
    baseline=layout(infos[0])
    return (baseline is not None and any(s.get('codec_type')=='video' and
        s.get('codec_name')=='h264' for s in baseline) and
        all(s.get('codec_type')!='video' or s.get('codec_name')=='h264' for s in baseline) and
        all(layout(i)==baseline for i in infos[1:]))


def h264_content_evidence(paths):
    """Hash unchanged coded media, excluding only AVC SPS/PPS/AUD packaging.

    This filter is used only for verification, never for the saved recording.
    The saved stream retains parameter sets and also must fully decode.
    """
    hashes,counts,sizes={},{},{}
    for path in paths:
        previous={}
        process=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-i',str(path),'-map','0','-c','copy',
            '-bsf:v','filter_units=remove_types=7|8|9','-f','framehash','-hash','sha256','-'],
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        try:
            for line in process.stdout:
                if line.startswith('#') or not line.strip():
                    continue
                fields=[v.strip() for v in line.split(',')]
                index,stamp,size=int(fields[0]),int(fields[1]),int(fields[4])
                if stamp<previous.get(index,-math.inf):
                    raise ProviderError('merge_non_monotonic_timestamps')
                previous[index]=stamp
                digest=fields[5]
                if not re.fullmatch('[a-fA-F0-9]{64}',digest):
                    raise ProviderError('merge_packet_hash_missing')
                hashes.setdefault(index,hashlib.sha256()).update(digest.encode())
                counts[index]=counts.get(index,0)+1
                sizes[index]=sizes.get(index,0)+size
            if process.wait(timeout=90):
                raise ProviderError('merge_packet_validation_failed')
        finally:
            if process.poll() is None:
                process.kill();process.wait()
            process.stdout.close()
    return {str(i):{'packets':counts[i],'bytes':sizes[i],'sha256':h.hexdigest()} for i,h in hashes.items()}


def merge_xml(parts,target,metadata):
    """Preserve comments/gifts/SC privately, rebasing their media-relative times."""
    missing=[]
    temporary=target.with_suffix('.part.xml')
    with temporary.open('wb') as out:
        os.chmod(temporary,0o600)
        out.write(b'<?xml version="1.0" encoding="utf-8"?>\n<i>\n')
        info=XML.Element('BililiveRecorderRecordInfo',roomid=str(metadata['room_id']),
                         title=metadata['title'],start_time=metadata['started_at'])
        out.write(XML.tostring(info,encoding='utf-8'))
        for part in parts:
            path=Path(part['path']).with_suffix('.xml')
            if not path.exists():
                missing.append(part['relative'])
                continue
            for _,node in ElementTree.iterparse(path,events=('end',)):
                if node.tag in ('d','sc','gift','guard'):
                    try:
                        if node.tag=='d':
                            attrs=node.attrib['p'].split(',')
                            stamp=float(attrs[0])
                        else:
                            stamp=float(node.attrib['ts'])
                        if not math.isfinite(stamp) or stamp<0 or stamp>part['duration']:
                            node.clear()
                            continue
                        if node.tag=='d':
                            attrs[0]=f"{stamp+part['offset']:.3f}"
                            node.set('p',','.join(attrs))
                        else:
                            node.set('ts',f"{stamp+part['offset']:.3f}")
                        out.write(XML.tostring(node,encoding='utf-8'))
                        out.write(b'\n')
                    except (KeyError,ValueError):
                        pass
                node.clear()
        out.write(b'</i>\n')
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary,target)
    return missing


def check_merge_boundaries(path,parts,total,directory):
    """Decode short samples at joins instead of decoding the entire broadcast."""
    samples=sorted({0.0,max(0.0,total-3),*(max(0.0,p['offset']-1) for p in parts[1:])})
    with (directory/'merge-decode.log').open('w') as log:
        for start in samples:
            result=subprocess.run(['ffmpeg','-nostdin','-v','error','-xerror','-threads','1',
                '-ss',str(start),'-i',str(path),'-t','3','-map','0','-threads','1','-f','null','-'],
                stdout=log,stderr=subprocess.STDOUT,timeout=120)
            if result.returncode:
                raise ProviderError('merge_boundary_decode_failed')
    return samples


def prepare(payload,directory):
    sources=sorted(payload['files'],key=lambda s:core.recording_meta(s['relative'])['start_unix'])
    if not all(core.unchanged(s) for s in sources):
        raise ProviderError('source_changed_before_merge')
    paths=[core.source_path(s['relative']) for s in sources]
    if len(paths)==1:
        return {'path':paths[0],'xml':paths[0].with_suffix('.xml'),'merged':False,'missing_xml':[]}
    marker=directory/'merge.json'
    saved=core.read_json(marker,{})
    package=directory/'package'/core.settings()['record_dir']
    package.mkdir(parents=True,exist_ok=True)
    name=paths[0].stem+'-合并.flv'
    target=package/name
    if (saved.get('fingerprints')==[s['fingerprint'] for s in sources]
            and target.is_file() and target.stat().st_size==saved.get('bytes')
            and file_hash(target)==saved.get('sha256')
            and target.with_suffix('.xml').is_file()
            and file_hash(target.with_suffix('.xml'))==saved.get('xml_sha256')):
        return {'path':target,'xml':target.with_suffix('.xml'),'merged':True,**saved}
    if shutil.disk_usage(core.STATE).free < sum(s['size'] for s in sources)*1.1+1024**3:
        raise ProviderError('insufficient_free_space_for_merge')
    infos=[media_info(p) for p in paths]
    parts=[]
    offset=0.0
    for source,path,info in zip(sources,paths,infos):
        parts.append({'relative':source['relative'],'path':str(path),'duration':info['duration'],'offset':offset})
        offset+=info['duration']
    listing=directory/'merge.ffconcat'
    def quote(value):
        if '\n' in value or '\r' in value:
            raise ProviderError('merge_invalid_filename')
        return "'"+value.replace("'","'\\''")+"'"
    listing.write_text('ffconcat version 1.0\n'+''.join(
        'file '+quote(str(p))+'\nduration '+str(info['duration'])+'\n' for p,info in zip(paths,infos)),encoding='utf-8')
    temporary=target.with_suffix('.part.flv')
    try:
        with (directory/'merge.log').open('w') as log:
            result=subprocess.run(['ffmpeg','-nostdin','-v','warning','-y','-f','concat','-safe','0','-auto_convert','1',
                '-i',str(listing),'-map','0','-c','copy','-avoid_negative_ts','make_zero','-f','flv',str(temporary)],
                stdout=log,stderr=subprocess.STDOUT,timeout=max(900,offset))
        if result.returncode:
            raise ProviderError('merge_ffmpeg_failed')
        actual=media_info(temporary)['duration']
        if abs(actual-offset)>max(1,len(paths)*0.1):
            raise ProviderError('merge_duration_mismatch')
        samples=check_merge_boundaries(temporary,parts,actual,directory)
        if not all(core.unchanged(s) for s in sources):
            raise ProviderError('source_changed_during_merge')
        os.chmod(temporary,0o600)
        os.replace(temporary,target)
        metadata=core.recording_meta(sources[0]['relative'])
        missing=merge_xml(parts,target.with_suffix('.xml'),{'room_id':core.settings()['room_id'],**metadata})
        # A manifest accompanies the recording in Baidu; no unrelated recordings
        # or original fragments enter this upload directory.
        manifest={'version':1,'verified_at':time.time(),'fingerprints':[s['fingerprint'] for s in sources],
            'name':name,'bytes':target.stat().st_size,'sha256':file_hash(target),
            'xml_sha256':file_hash(target.with_suffix('.xml')),'duration':actual,
            'parts':[{k:v for k,v in p.items() if k!='path'} for p in parts],
            'missing_xml':missing,'merge_method':'ffmpeg_stream_copy',
            'validation':'duration_and_boundary_decode','decode_samples_seconds':samples}
        core.atomic_json(target.with_suffix('.merge.json'),manifest)
        core.atomic_json(marker,manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return {'path':target,'xml':target.with_suffix('.xml'),'merged':True,**manifest}
