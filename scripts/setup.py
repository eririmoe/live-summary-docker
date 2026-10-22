"""Docker-only bootstrap: no host Python, Git or Compose required by this file."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

def write_private(path, value, text=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.setup-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value if text else json.dumps(value, ensure_ascii=False, indent=2)+'\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def validate(cfg, deploy):
    if type(cfg['room_id']) is not int or cfg['room_id'] <= 0:
        raise ValueError('room_id must be the positive canonical room ID')
    relative = Path(cfg['record_dir'])
    if relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 1 or '\\' in cfg['record_dir'] or not cfg['record_dir']:
        raise ValueError('record_dir must be one safe relative directory name')
    if not cfg['streamer'].strip():
        raise ValueError('streamer is required')
    if not re.fullmatch(r'[+-](?:0\d|1[0-4]):[0-5]\d',cfg['recording_timezone']):
        raise ValueError('recording_timezone must be an ISO numeric UTC offset')
    remote=cfg['baidu_root']
    if not remote.startswith('/') or remote == '/' or '..' in Path(remote).parts or any(c in remote for c in '\r\n\\'):
        raise ValueError('baidu_root must be an explicit non-root remote folder')
    url=urlsplit(cfg['recorder_url'])
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
        raise ValueError('recorder_url must be an HTTP(S) URL without embedded credentials')
    origin=urlsplit(deploy['admin_origin'])
    if origin.scheme not in ('http','https') or not origin.hostname or origin.path not in ('','/') or origin.username:
        raise ValueError('admin_origin must be a single HTTP(S) origin')
    if origin.scheme!='https' and origin.hostname not in ('localhost','127.0.0.1'):
        raise ValueError('public admin_origin requires HTTPS')
    if cfg['publish_backend'] not in ('local','cloudflare-pages'):
        raise ValueError('unknown publish_backend')
    if cfg['publish_backend']=='cloudflare-pages' and not (re.fullmatch('[a-f0-9]{32}',cfg.get('cloudflare_account_id','')) or cfg.get('zone_name')):
        raise ValueError('Cloudflare Pages requires account ID or zone_name')
    if (cfg.get('transcript_archive') or {}).get('backend') not in ('local','r2'):
        raise ValueError('unknown transcript archive backend')
    for key in ('data_dir','records_dir'):
        value=deploy[key]
        if not value or any(c in value for c in '\n\r\x00') or value in ('/','.', './'):
            raise ValueError(key+' must be a dedicated data directory')
    # Initial deployment cannot silently process all old media or delete files.
    cfg.update(auto_process=False, cleanup_apply=False, delete_completed_media=False)

def prompt(label, default=''):
    answer=input(label+(f' [{default}]' if default else '')+': ').strip()
    return answer or default

def interactive(defaults):
    cfg=dict(defaults)
    cfg['room_id']=int(prompt('Canonical Bilibili room ID'))
    cfg['streamer']=prompt('Streamer display name')
    cfg['record_dir']=prompt('Recording subdirectory',str(cfg['room_id'])+'-recordings')
    cfg['recorder_url']=prompt('Recorder API URL inside Docker','http://bilirecorder:2356')
    cfg['baidu_root']=prompt('Baidu remote backup folder','/live-recordings')
    cfg['publish_backend']=prompt('Publication: local or cloudflare-pages','local')
    values={}
    if cfg['publish_backend']=='cloudflare-pages':
        cfg['cloudflare_account_id']=prompt('Cloudflare account ID')
        cfg['pages_project']=prompt('NEW Pages project name','live-summary-'+str(cfg['room_id']))
        cfg['site_domain']=prompt('Custom site domain (optional)')
        cfg['zone_name']=prompt('Cloudflare zone name (only for custom domain)')
        values['CLOUDFLARE_API_TOKEN']=getpass.getpass('Cloudflare API token (hidden): ').strip()
    for key in ('QIANWEN_API_KEY','DEEPSEEK_API_KEY'):
        values[key]=getpass.getpass(key+' (hidden; Enter to configure later): ').strip()
    user=prompt('Recorder/admin username','admin')
    password=getpass.getpass('Recorder/admin password (hidden; Enter generates a private file): ').strip() or secrets.token_urlsafe(32)
    room={'RECORDER_USER':user,'RECORDER_PASS':password,'WEBHOOK_TOKEN':secrets.token_urlsafe(32)}
    deploy={'data_dir':'./data','records_dir':'./records','admin_origin':'http://localhost:8766'}
    deploy['records_dir']=prompt('Host recording directory',deploy['records_dir'])
    print('Password is saved privately; it is never printed by this wizard.')
    return {'config':cfg,'secrets':values,'recorder_secrets':room,'deployment':deploy}

def dotenv(value):
    if any(c in str(value) for c in '\r\n\x00'):
        raise ValueError('multiline environment value is not supported')
    return "'"+str(value).replace("'", "\\'")+"'"

def initialize(value):
    cfg={**json.loads((ROOT/'config.example.json').read_text(encoding='utf-8')),**value.get('config',{})}
    deploy={'data_dir':'./data','records_dir':'./records','admin_origin':'http://localhost:8766',**value.get('deployment',{})}
    validate(cfg,deploy)
    state=Path(deploy['data_dir']); records=Path(deploy['records_dir'])
    if not state.is_absolute(): state=ROOT/state
    if not records.is_absolute(): records=ROOT/records
    if (ROOT/'.env').exists() or (state/'config.json').exists():
        raise ValueError('already initialized; existing settings and credentials were preserved')
    room=dict(value.get('recorder_secrets',{}))
    if not room.get('RECORDER_USER') or not room.get('RECORDER_PASS') or room['RECORDER_PASS']=='REPLACE_WITH_A_RANDOM_PASSWORD':
        raise ValueError('recorder/admin credentials are required')
    if len(room['RECORDER_PASS'])<12:
        raise ValueError('use at least 12 characters for recorder/admin password')
    room.setdefault('WEBHOOK_TOKEN',secrets.token_urlsafe(32))
    env={'COMPOSE_PROJECT_NAME':'live-summary','DATA_DIR':deploy['data_dir'],'RECORDS_DIR':deploy['records_dir'],
         'APP_IMAGE':'live-summary:local','RECORDER_IMAGE':'ghcr.io/bililiverecorder/bililiverecorder:2.20.0',
         'BREC_HTTP_BASIC_USER':room['RECORDER_USER'],'BREC_HTTP_BASIC_PASS':room['RECORDER_PASS'],
         'ADMIN_ORIGIN':deploy['admin_origin'].rstrip('/'),'ADMIN_COOKIE_SECURE':str(deploy['admin_origin'].startswith('https://')).lower()}
    rendered=''.join(k+'='+dotenv(v)+'\n' for k,v in env.items())
    for path in (state,records):
        path.mkdir(parents=True,exist_ok=True); path.chmod(0o700)
    for folder in ('baidu','runtime','jobs','site','public-summaries','backups','signals'):
        (state/folder).mkdir(exist_ok=True); (state/folder).chmod(0o700)
    write_private(state/'config.json',cfg)
    write_private(state/'secrets.json',value.get('secrets',{}))
    write_private(state/'recorder-secrets.json',room)
    if value.get('r2_secrets'): write_private(state/'r2-secrets.json',value['r2_secrets'])
    write_private(ROOT/'.env',rendered,text=True)
    print('Initialized. Automatic processing and deletion are OFF. Next: docker compose build')

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--input',type=Path); args=parser.parse_args()
    defaults=json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'))
    value=json.loads(args.input.read_text(encoding='utf-8')) if args.input else interactive(defaults)
    try: initialize(value)
    except (ValueError,KeyError) as exc: raise SystemExit(str(exc)) from None

if __name__=='__main__': main()
