"""Durable state and strict source-path handling. No video deletion lives here."""
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parent
STATE = Path(os.environ.get('LIVE_SUMMARY_STATE', '/state'))
RECORDS = Path(os.environ.get('LIVE_SUMMARY_RECORDS', '/records'))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as out:
        os.chmod(tmp, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def read_json(path, default=None):
    p = Path(path)
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default


def settings():
    return {**read_json(APP / 'config.example.json'), **read_json(STATE / 'config.json', {})}


def secrets():
    return {**read_json(STATE / 'recorder-secrets.json', {}), **read_json(STATE / 'secrets.json', {})}


@contextmanager
def database():
    STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / 'state.sqlite3', timeout=20)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=20000')
        yield db
        db.commit()
    finally:
        db.close()


def init_db():
    with database() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS events (
          event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT,
          room_id INTEGER NOT NULL, received REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
          error TEXT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS claimed_files (
          fingerprint TEXT PRIMARY KEY, job_id TEXT NOT NULL);
        ''')


def source_path(relative):
    path = RECORDS / relative
    allowed = (RECORDS / settings()['record_dir']).resolve()
    if path.is_symlink() or path.suffix.lower() != '.flv':
        raise ValueError('source_must_be_regular_flv')
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(allowed) or not resolved.is_file():
        raise ValueError('source_outside_allowed_recording_directory')
    return resolved


def identify(relative):
    p = source_path(relative)
    st = p.stat()
    key = hashlib.sha256(f'{relative}\0{st.st_size}\0{st.st_mtime_ns}'.encode()).hexdigest()
    return {'relative': relative, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
            'fingerprint': key, 'modified': st.st_mtime}


def unchanged(source):
    return identify(source['relative'])['fingerprint'] == source['fingerprint']


def recording_meta(relative):
    # BililiveRecorder's default filename supplies a stable local +08:00 start.
    match = re.match(r'^.*?-(\d+)-(\d{8})-(\d{6})-(\d{3})-(.*)$', Path(relative).stem)
    if not match:
        raise ValueError('unrecognized_recording_filename')
    stamp = datetime.strptime(match[2] + match[3]+match[4], '%Y%m%d%H%M%S%f')
    return {'title': match[5], 'started_at': stamp.isoformat() + settings().get('recording_timezone','+08:00'),
            'start_unix': datetime.fromisoformat(stamp.isoformat() + settings().get('recording_timezone','+08:00')).timestamp()}


def enqueue(relatives, session_id=None):
    sources = [identify(str(p)) for p in sorted(set(relatives))]
    if not sources:
        return None
    job_id = hashlib.sha256('|'.join(s['fingerprint'] for s in sources).encode()).hexdigest()[:24]
    payload = {'files': sources, 'session_id': session_id}
    now = time.time()
    with database() as db:
        claimed = [db.execute('SELECT job_id FROM claimed_files WHERE fingerprint=?',
                             (s['fingerprint'],)).fetchone() for s in sources]
        if any(claimed):
            if all(claimed) and len({r['job_id'] for r in claimed}) == 1:
                return claimed[0]['job_id']
            raise ValueError('some_recordings_already_belong_to_another_job')
        db.execute('INSERT INTO jobs(id,created,updated,status,payload) VALUES(?,?,?,?,?)',
                   (job_id, now, now, 'queued', json.dumps(payload)))
        db.executemany('INSERT INTO claimed_files VALUES(?,?)',
                       [(s['fingerprint'],job_id) for s in sources])
    return job_id


def event_store(event):
    cfg = settings()
    if not isinstance(event, dict) or not isinstance(event.get('EventData'), dict):
        raise ValueError('invalid_event')
    data = event['EventData']
    if int(data.get('RoomId', 0)) != cfg['room_id']:
        return False
    if not isinstance(event.get('EventId'), str) or len(event['EventId']) > 150:
        raise ValueError('missing_event_id')
    if event.get('EventType') == 'FileClosed':
        source_path(data.get('RelativePath', ''))
    with database() as db:
        cursor = db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)',
            (event['EventId'], event.get('EventType',''), data.get('SessionId'),
             cfg['room_id'], time.time(), json.dumps(event)))
        return bool(cursor.rowcount)


def update_job(job_id, **fields):
    if not set(fields).issubset({'status','stage','attempts','retry_at','error'}):
        raise ValueError('invalid_job_field')
    fields['updated'] = time.time()
    with database() as db:
        db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',
                   [*fields.values(), job_id])


def job_rows():
    with database() as db:
        return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created DESC')]
