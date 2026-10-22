"""Separate, read-only operations console. No processing worker is started here."""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests
import core
import storage
from progress import download_percent

ORIGIN = os.environ.get('ADMIN_ORIGIN', 'http://localhost:8766')
COOKIE_SECURE = os.environ.get('ADMIN_COOKIE_SECURE','true').lower() == 'true'
if not COOKIE_SECURE and urlsplit(ORIGIN).hostname not in ('localhost','127.0.0.1'):
    raise RuntimeError('insecure_admin_cookie_requires_loopback_origin')
COOKIE_FLAGS = '; Path=/; HttpOnly; SameSite=Strict' + ('; Secure' if COOKIE_SECURE else '')
AUTH_FILE = Path(os.environ.get('ADMIN_AUTH_FILE', '/auth/recorder-secrets.json'))
TTL = 8 * 3600
SESSIONS = OrderedDict()
ATTEMPTS = OrderedDict()
LOCK = threading.Lock()


def valid_credentials(username, password):
    keys = core.read_json(AUTH_FILE, {})
    def equal(a, b):
        return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())
    return bool(keys.get('RECORDER_PASS')) and equal(username, keys.get('RECORDER_USER', '')) & equal(password, keys.get('RECORDER_PASS', ''))


def create_session():
    token = secrets.token_urlsafe(32)
    with LOCK:
        now = time.time()
        for key in list(SESSIONS):
            if SESSIONS[key] <= now:
                del SESSIONS[key]
        SESSIONS[hashlib.sha256(token.encode()).hexdigest()] = now + TTL
        while len(SESSIONS) > 256:
            SESSIONS.popitem(last=False)
    return token


def token_key(cookie):
    try:
        value = SimpleCookie(cookie).get('live_summary_admin')
        return hashlib.sha256(value.value.encode()).hexdigest() if value else ''
    except Exception:
        return ''


def authenticated(cookie):
    with LOCK:
        return SESSIONS.get(token_key(cookie), 0) > time.time()


def allow_attempt(client):
    with LOCK:
        now = time.time()
        recent = [stamp for stamp in ATTEMPTS.pop(client, []) if stamp > now - 900]
        # Bound storage even if callers supply many different addresses.
        ATTEMPTS[client] = recent
        while len(ATTEMPTS) > 2048:
            ATTEMPTS.popitem(last=False)
        if len(recent) >= 5:
            return False
        recent.append(now)
        return True


def snapshot(room_keys=None):
    cfg = core.settings()
    # SQLite needs to create -shm even for mode=ro when no connection currently
    # holds the WAL open. Keep the production mount strictly read-only: copy DB
    # and WAL only across an unchanged generation, then let SQLite recover that
    # private temporary snapshot. Retry if a writer/checkpoint raced the copy.
    source = core.STATE/'state.sqlite3'
    files = [source, source.with_name(source.name+'-wal')]
    def generation():
        values=[]
        for path in files:
            try:
                stat=path.stat()
                values.append((stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
            except FileNotFoundError:
                values.append(None)
        return values
    with tempfile.TemporaryDirectory(prefix='live-summary-status-') as folder:
        target=Path(folder)/source.name
        for attempt in range(3):
            before=generation()
            try:
                for path,stat in zip(files,before):
                    dest=Path(folder)/path.name
                    dest.unlink(missing_ok=True)
                    if stat is not None:
                        shutil.copyfile(path,dest)
            except FileNotFoundError:
                continue
            if before==generation():
                break
        else:
            raise RuntimeError('database_busy_retry_later')
        with sqlite3.connect(str(target),timeout=3) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            counts = dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status').fetchall())
            rows = db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100').fetchall()
            events = [dict(row) for row in db.execute('SELECT kind,received,session_id FROM events ORDER BY received DESC LIMIT 12')]
    jobs = []
    for row in rows:
        job = {k: row[k] for k in ('id','created','updated','status','stage','attempts','retry_at','error')}
        payload = json.loads(row['payload'])
        job['origin']=payload.get('origin','live')
        if job['origin']=='baidu_history':
            job['history']={'position':payload['batch_position'],'limit':payload['batch_limit'],
                            'title':payload['metadata']['title'],'started_at':payload['metadata']['started_at']}
        job['files'] = [{'name': Path(f['relative']).name, 'bytes': f['size']} for f in payload['files']]
        # Only return scalar checkpoint facts. Provider responses and upload URLs
        # can contain credentials; they must never be exposed as log excerpts.
        directory = core.STATE / 'jobs' / job['id']
        if re.fullmatch(r'[a-f0-9]{24}', job['id']):
            checkpoints = [core.read_json(p, {}) for p in sorted(directory.glob('file-*/asr-*.json'))]
            job['asr'] = {'submitted': sum(bool(c.get('task_id')) for c in checkpoints),
                          'completed': sum(bool(c.get('result')) for c in checkpoints),
                          'skipped_no_words':sum(c.get('result',{}).get('empty_reason')=='ASR_RESPONSE_HAVE_NO_WORDS' for c in checkpoints),
                          'uncertain': any(c.get('submission_uncertain', False) for c in checkpoints)}
            job['backup_verified'] = (directory / 'backup.json').exists()
            merge=core.read_json(directory/'merge.json',{})
            job['merge']={'parts':len(merge.get('parts',[])),'name':merge.get('name'),
                          'verified_at':merge.get('verified_at'),'consolidated':(directory/'consolidated.json').exists()}
            job['published'] = (directory / 'published.json').exists()
            cleanup=core.read_json(directory/'media-cleanup.json',{})
            job['media_cleanup']={'completed_at':cleanup.get('completed_at'),'released_bytes':cleanup.get('released_bytes',0)}
            job['summary_windows'] = len(list(directory.glob('summary-window-*.json')))
            job['recap_parts'] = len(list(directory.glob('recap-v1-*.json')))
            receipt=core.read_json(directory/'transcript-archive.json',{})
            job['transcript_archive']={k:receipt.get(k) for k in ('backend','bucket','verified_at')}
            job['has_transcript']=(directory/'transcript.txt').exists()
            if job['origin']=='baidu_history':
                progress=core.read_json(directory/'history-progress.json',{})
                job['history'].update({k:progress.get(k) for k in ('total_parts','completed_parts','current_part',
                    'current_file','download_percent','download_activity_at','updated_at',
                    'current_trim_seconds','overlap_check','verified_overlap_seconds','skipped_media_parts')})
                part=progress.get('current_part')
                if job['stage']=='downloading' and isinstance(part,int) and 1<=part<=len(payload['files']):
                    download_log=core.STATE/'history-cache'/job['id']/str(part-1)/'download.log'
                    if download_log.is_file():
                        with download_log.open('rb') as stream:
                            stream.seek(max(0,download_log.stat().st_size-8192))
                            text=stream.read().decode('utf-8',errors='replace')
                        job['history']['download_percent']=download_percent(text,payload['files'][part-1]['size'])
            logs=list(directory.glob('baidu-upload*.log'))
            log=max(logs,key=lambda p:p.stat().st_mtime) if logs else directory/'baidu-upload.log'
            if log.exists():
                stat = log.stat()
                job['upload_activity_at'] = stat.st_mtime
                with log.open('rb') as stream:
                    stream.seek(max(0, stat.st_size-16384))
                    tail = stream.read().decode('utf-8', errors='replace')
                matches = re.findall(r'(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%', tail)
                job['upload_file_percent'] = next((float(n) for n in reversed(matches) if 0 <= float(n) <= 100), None)
        jobs.append(job)
    def priority(job):
        if job['status']=='running':return (0,0,-job['created'])
        if job['status']!='done' and job['origin']!='baidu_history':return (1,0,-job['created'])
        if job['status']!='done':return (2,job.get('history',{}).get('position',0),0)
        return (3,0,-job['created'])
    jobs.sort(key=priority)
    observed=core.read_json(core.STATE/'live-detection.json',{})
    live_detection={k:observed.get(k) for k in ('observed_at','phase','offline_since','eligible_at')}
    live_detection['recordings']=[{k:r.get(k) for k in ('name','title','started_at','bytes','modified')}
        for r in observed.get('recordings',[])]
    keys = room_keys if room_keys is not None else core.read_json(AUTH_FILE, {})
    room = {'available': False}
    try:
        response = requests.get(cfg['recorder_url']+'/api/room', auth=(keys['RECORDER_USER'], keys['RECORDER_PASS']), timeout=(2,3))
        response.raise_for_status()
        record = next(r for r in response.json() if int(r['roomId']) == cfg['room_id'])
        room = {'available': True, 'streaming': bool(record.get('streaming')), 'recording': bool(record.get('recording')), 'title': record.get('title','')}
    except (requests.RequestException, ValueError, KeyError, TypeError, StopIteration):
        pass
    disk = shutil.disk_usage(core.STATE)
    deployment = core.read_json(core.STATE/'last-deployment.json', {})
    batch=core.read_json(core.STATE/'history-batch.json',{})
    history_batch={}
    if batch:
        selected=[j for j in jobs if j['id'] in batch['jobs']]
        done=sum(j['status']=='done' for j in selected)
        skipped=sum(j['status']=='failed' and j['attempts']>=5 for j in selected)
        history_batch={'id':batch['id'],'limit':batch['limit'],'done':done,'active':batch.get('active',False),
                       'failed':sum(j['status']=='failed' for j in selected),'complete':done==batch['limit'],
                       'skipped':skipped,'finished':done+skipped==batch['limit'],
                       'retrying':any(j['status']=='retry' for j in selected)}
    return {'now': time.time(), 'auto_process': cfg['auto_process'], 'cleanup_apply': cfg['cleanup_apply'],
            'delete_completed_media':bool(cfg.get('delete_completed_media')),
            'room': room, 'jobs': jobs, 'counts': counts, 'events': events,'live_detection':live_detection,
            'history_batch':history_batch,
            'disk': {'total': disk.total, 'free': disk.free},
            'deployment': {k: deployment.get(k) for k in ('id','verified_at','verified_url')},
            'detection': {'scan_interval': cfg['scan_interval'], 'stable_seconds': cfg['stable_seconds'],
                          'reconnect_grace_seconds':cfg['reconnect_grace_seconds'],'merge_gap_seconds':cfg['merge_gap_seconds']}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data, content_type='application/json; charset=utf-8', cookie=None, location=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if location:
            self.send_header('Location',location)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = urlsplit(self.path).path
        static = {'/': 'index.html', '/admin.js': 'admin.js', '/admin.css': 'admin.css', '/favicon.svg': 'favicon.svg'}
        if route in static:
            path = core.APP/'admin-site'/static[route]
            content_type = 'image/svg+xml' if route.endswith('.svg') else ('text/html; charset=utf-8' if route == '/' else ('text/css; charset=utf-8' if route.endswith('.css') else 'text/javascript; charset=utf-8'))
            self.reply(200, path.read_bytes(), content_type)
        elif re.fullmatch(r'/api/transcript/[a-f0-9]{24}/transcript\.(txt|json)',route):
            if not authenticated(self.headers.get('Cookie','')):
                self.reply(401,{'error':'login_required'})
                return
            parts=route.split('/')
            directory=core.STATE/'jobs'/parts[3]
            receipt=core.read_json(directory/'transcript-archive.json',{})
            key=next((obj.get('key') for obj in receipt.get('objects',[]) if obj.get('name')==parts[4]),None)
            if not receipt.get('verified_at') or not key:
                path=directory/parts[4]
                if (core.settings().get('transcript_archive') or {}).get('backend')!='local' or not path.is_file() or path.is_symlink():
                    self.reply(409,{'error':'transcript_not_ready'})
                    return
                self.reply(200,path.read_bytes(),'application/json; charset=utf-8' if path.suffix=='.json' else 'text/plain; charset=utf-8')
                return
            try:
                url=storage.presigned_read_url(key,method=self.command)
            except Exception:
                self.reply(503,{'error':'transcript_download_temporarily_unavailable'})
                return
            self.reply(302,b'',location=url)
        elif route == '/api/status':
            if not authenticated(self.headers.get('Cookie','')):
                self.reply(401, {'error':'login_required'})
                return
            try:
                self.reply(200, snapshot())
            except Exception:
                self.reply(503, {'error':'status_temporarily_unavailable'})
        elif route == '/health':
            self.reply(200, {'ok': True})
        else:
            self.reply(404, {'error':'not_found'})

    def do_POST(self):
        origin = self.headers.get('Origin','')
        host = self.headers.get('Host','')
        local = urlsplit('http://'+host).hostname in ('localhost','127.0.0.1')
        if origin != ORIGIN and not (local and origin == 'http://'+host):
            self.reply(403, {'error':'origin_rejected'})
            return
        route = urlsplit(self.path).path
        if route == '/api/logout':
            with LOCK:
                SESSIONS.pop(token_key(self.headers.get('Cookie','')), None)
            self.reply(200, {'ok':True}, cookie='live_summary_admin=; Max-Age=0'+COOKIE_FLAGS)
            return
        if route != '/api/login':
            self.reply(404, {'error':'not_found'})
            return
        client = self.client_address[0]
        if not allow_attempt(client):
            self.reply(429, {'error':'too_many_attempts'})
            return
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 4096 or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                raise ValueError()
            value = json.loads(self.rfile.read(size))
            user, password = value['username'], value['password']
            if not isinstance(user,str) or not isinstance(password,str):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            self.reply(400, {'error':'invalid_request'})
            return
        if not valid_credentials(user,password):
            self.reply(401, {'error':'invalid_credentials'})
            return
        with LOCK:
            ATTEMPTS.pop(client, None)
        token = create_session()
        self.reply(200, {'ok': True}, cookie='live_summary_admin='+token+'; Max-Age='+str(TTL)+COOKIE_FLAGS)


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0',8081), Handler).serve_forever()
