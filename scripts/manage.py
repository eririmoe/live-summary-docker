"""Explicit setup and readiness commands; never echo credentials or webhook URLs."""
import argparse
import getpass
import json
from pathlib import Path
import re
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
import providers
import pipeline
import app

def doctor():
    cfg=core.settings(); checks={}
    checks['credentials']={'ok':not app.readiness(),'missing':app.readiness()}
    checks['recordings']={'ok':core.RECORDS.is_dir() and (core.RECORDS/cfg['record_dir']).is_dir()}
    try:
        room=pipeline.recorder_room()
        checks['recorder']={'ok':True,'streaming':bool(room.get('streaming')),'recording':bool(room.get('recording'))}
    except Exception:
        checks['recorder']={'ok':False,'error':'recorder_or_room_unavailable'}
    if not app.readiness() or checks['credentials']['missing']==['BAIDU_LOGIN']:
        try:
            pipeline.baidu_command(['ls',cfg['baidu_root']])
            checks['baidu_command']={'ok':True,'note':'CLI exit only; per-file backup still requires exact-byte receipts'}
        except Exception:
            checks['baidu_command']={'ok':False}
    ok=all(c['ok'] for c in checks.values())
    print(json.dumps({'ok':ok,'auto_process':cfg['auto_process'],'checks':checks},ensure_ascii=False,indent=2))
    return 0 if ok else 1

def wire_recorder():
    cfg=core.settings(); keys=core.secrets()
    auth=(keys['RECORDER_USER'],keys['RECORDER_PASS'])
    import requests
    endpoint=cfg['recorder_url']+'/api/config/global'
    def call(method,payload=None):
        try:
            response=requests.request(method,endpoint,auth=auth,json=payload,timeout=(5,20))
            response.raise_for_status();return response.json()
        except Exception: raise providers.ProviderError('recorder_configuration_request_failed') from None
    previous=call('GET')
    field=previous.get('optionalWebHookUrlsV2')
    old=(field.get('value') if isinstance(field,dict) else field) or ''
    if not isinstance(old,str): raise providers.ProviderError('unexpected_webhook_config')
    desired='http://worker:8080/hooks/'+keys['WEBHOOK_TOKEN']
    urls=[u for u in old.splitlines() if u.strip()]
    if desired in urls:
        print(json.dumps({'webhook_configured':True,'changed':False}));return
    core.atomic_json(core.STATE/'backups'/('recorder-global-'+str(time.time_ns())+'.json'),previous)
    call('POST',{'optionalWebHookUrlsV2':{'hasValue':True,'value':'\n'.join(urls+[desired])}})
    current=call('GET').get('optionalWebHookUrlsV2')
    current=(current.get('value') if isinstance(current,dict) else current) or ''
    if desired not in current.splitlines(): raise providers.ProviderError('webhook_readback_failed')
    print(json.dumps({'webhook_configured':True,'changed':True}))

def credentials():
    values=core.read_json(core.STATE/'secrets.json',{})
    for key in ('QIANWEN_API_KEY','DEEPSEEK_API_KEY','CLOUDFLARE_API_TOKEN'):
        value=getpass.getpass(key+' (hidden; Enter keeps existing): ').strip()
        if value:
            if key=='QIANWEN_API_KEY' and value.startswith('sk-sp-'):
                raise providers.ProviderError('token_plan_key_does_not_support_file_upload')
            values[key]=value
    core.atomic_json(core.STATE/'secrets.json',values)
    print('Credentials saved privately. No API requests were sent.')

def configure_r2():
    cfg=core.settings(); value=core.read_json(core.STATE/'r2-secrets.json',{})
    account=input('Cloudflare account ID: ').strip(); bucket=input('Existing PRIVATE R2 bucket: ').strip()
    if not re.fullmatch('[a-f0-9]{32}',account) or not re.fullmatch('[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',bucket):
        raise providers.ProviderError('r2_configuration_invalid')
    for key in ('R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY'):
        new=getpass.getpass(key+' (hidden; Enter keeps existing): ').strip()
        if new: value[key]=new
    if not all(value.get(k) for k in ('R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY')):
        raise providers.ProviderError('r2_credentials_missing')
    cfg['transcript_archive']={'backend':'r2','account_id':account,'bucket':bucket,'prefix':'transcripts'}
    core.atomic_json(core.STATE/'r2-secrets.json',value);core.atomic_json(core.STATE/'config.json',cfg)
    print('Private R2 configured. Each transcript must pass upload/readback SHA-256 verification.')

def register_room(record=False):
    """Create a new room paused and use a deterministic recording subdirectory."""
    import requests
    cfg=core.settings();keys=core.secrets();base=cfg['recorder_url']
    def call(method,path,payload=None):
        try:
            response=requests.request(method,base+path,auth=(keys['RECORDER_USER'],keys['RECORDER_PASS']),json=payload,timeout=(5,20))
            response.raise_for_status();return response.json()
        except Exception:raise providers.ProviderError('recorder_room_configuration_failed') from None
    rooms=call('GET','/api/room')
    found=next((r for r in rooms if int(r['roomId'])==cfg['room_id']),None)
    intent={'room_id':cfg['room_id'],'record_dir':cfg['record_dir']}
    pending=core.STATE/'recorder-room.pending.json'
    resuming=core.read_json(pending)==intent
    if found and not record and not resuming:
        print(json.dumps({'room_exists':True,'changed':False,'note':'Existing filename settings preserved; verify record_dir matches.'}));return
    if not found and record:
        raise providers.ProviderError('register_room_before_enabling_recording')
    if not found and rooms:
        raise providers.ProviderError('new_room_filename_configuration_requires_dedicated_recorder')
    if resuming and any(int(r['roomId'])!=cfg['room_id'] for r in rooms):
        raise providers.ProviderError('recorder_bootstrap_scope_changed')
    configure=not found or resuming
    if configure:core.atomic_json(pending,intent)
    room=call('POST','/api/room',{'roomId':cfg['room_id'],'autoRecord':record})
    if int(room['roomId'])!=cfg['room_id']:
        raise providers.ProviderError('use_canonical_room_id_not_short_id')
    if configure:
        # Directory is user-configured, filename timestamps retain recorder defaults.
        template=cfg['record_dir']+'/录制-{{ roomId }}-{{ "now" | time_zone: "Asia/Shanghai" | format_date: "yyyyMMdd-HHmmss-fff" }}-{{ title | unescape }}.flv'
        previous=call('GET','/api/config/global')
        core.atomic_json(core.STATE/'backups'/('recorder-filename-'+str(time.time_ns())+'.json'),previous)
        call('POST','/api/config/global',
             {'optionalFileNameRecordTemplate':{'hasValue':True,'value':template}})
        verified=call('GET','/api/config/global')
        field=verified.get('optionalFileNameRecordTemplate',{})
        if not isinstance(field,dict) or field.get('value')!=template:
            raise providers.ProviderError('recorder_filename_readback_failed')
        core.atomic_json(core.STATE/'recorder-room-bootstrap.json',intent)
        pending.unlink()
    print(json.dumps({'room_configured':True,'auto_record':record,'recording_directory':cfg['record_dir']}))

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['doctor','wire-recorder','credentials','configure-r2','register-room','record']);args=p.parse_args()
    if args.command=='doctor':raise SystemExit(doctor())
    {'wire-recorder':wire_recorder,'credentials':credentials,'configure-r2':configure_r2,
     'register-room':register_room,'record':lambda:register_room(True)}[args.command]()

if __name__=='__main__':
    try:main()
    except providers.ProviderError as exc:print(json.dumps({'error':str(exc)}));raise SystemExit(1)
