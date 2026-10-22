"""Prove short overlaps from identical encoded audio packets, not filenames."""
import collections
import hashlib
import math
import re
import subprocess
from providers import ProviderError

WINDOW_SECONDS=120


def packet_window(path,edge):
    if edge not in ('head','tail'):
        raise ValueError('invalid_audio_window_edge')
    packets=collections.deque()
    process=subprocess.Popen(['ffprobe','-v','error','-select_streams','a:0','-show_packets',
        '-show_data_hash','sha256','-show_entries','packet=pts_time,dts_time,duration_time,data_hash',
        '-of','compact=p=0',str(path)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    stopped=False;first=None
    try:
        for line in process.stdout:
            values=dict(v.split('=',1) for v in line.strip().split('|') if '=' in v)
            if 'data_hash' not in values:
                continue
            try:
                stamp=float(values.get('pts_time',values.get('dts_time')))
                duration=float(values.get('duration_time','0'))
            except (ValueError,TypeError):
                raise ProviderError('history_audio_packet_timing_missing') from None
            digest=values['data_hash'].removeprefix('SHA256:')
            if not math.isfinite(stamp) or not math.isfinite(duration) or duration<0 or not re.fullmatch('[a-fA-F0-9]{64}',digest):
                raise ProviderError('history_audio_packet_evidence_invalid')
            first=stamp if first is None else first
            if edge=='head' and stamp-first>WINDOW_SECONDS:
                stopped=True;process.kill();break
            packets.append({'time':stamp,'duration':duration,'sha256':digest})
            if edge=='tail':
                while packets and stamp-packets[0]['time']>WINDOW_SECONDS:
                    packets.popleft()
            if len(packets)>20000:
                raise ProviderError('history_audio_packet_window_too_large')
        code=process.wait(timeout=90)
        if code and not stopped:
            raise ProviderError('history_audio_packet_probe_failed')
    finally:
        if process.poll() is None:
            process.kill();process.wait()
        process.stdout.close()
    if not packets:
        raise ProviderError('history_audio_packet_window_empty')
    return list(packets)


def confirm_overlap(tail,head,expected_seconds):
    """Only an exact, varied packet suffix/prefix match permits trimming."""
    if not 0<expected_seconds<=WINDOW_SECONDS:
        raise ProviderError('history_overlap_exceeds_reviewed_window')
    prefix=[p['sha256'] for p in head]
    sequence=prefix+['boundary']+[p['sha256'] for p in tail]
    table=[0]*len(sequence)
    for i in range(1,len(sequence)):
        j=table[i-1]
        while j and sequence[i]!=sequence[j]:
            j=table[j-1]
        if sequence[i]==sequence[j]:
            j+=1
        table[i]=j
    matched=min(table[-1],len(head),len(tail))
    seconds=head[matched-1]['time']+head[matched-1]['duration'] if matched else 0
    diverse=len({p['sha256'] for p in head[:matched]})
    timing_consistent=bool(matched) and all(abs((tail[-matched+i]['time']-tail[-matched]['time'])-
        (head[i]['time']-head[0]['time']))<=0.05 for i in range(matched))
    # Container/video end times can extend beyond the final audio packet.
    # A shorter exact audio match is valid; never trim the filename's estimate.
    confirmed=(matched>=30 and diverse>=12 and timing_consistent and seconds>=1 and seconds<=expected_seconds+1)
    return {'status':'confirmed_exact_audio' if confirmed else 'not_confirmed',
        'expected_overlap_seconds':round(expected_seconds,6),'matched_packets':matched,
        'distinct_packets':diverse,'packet_timing_consistent':timing_consistent,'matched_audio_end_seconds':round(seconds,6),
        'matched_sequence_sha256':hashlib.sha256(''.join(prefix[:matched]).encode()).hexdigest(),
        'trim_seconds':round(seconds,6) if confirmed else 0}
