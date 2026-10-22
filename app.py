import argparse
import fcntl
import hmac
import json
import logging
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import core
import pipeline
import providers
import storage
import history
import scheduler

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
DISCOVERY_WAKE=threading.Event()


def discovery_loop(stop=None):
    stop=stop or threading.Event()
    while not stop.is_set():
        cfg=core.settings()
        if cfg['auto_process']:
            try:
                pipeline.discover()
            except Exception as exc:
                error=str(exc) if isinstance(exc,providers.ProviderError) else type(exc).__name__
                logging.warning('discovery_waiting reason=%s',error)
        DISCOVERY_WAKE.wait(cfg['scan_interval'])
        DISCOVERY_WAKE.clear()


def readiness():
    keys = core.secrets()
    missing = [k for k in ('QIANWEN_API_KEY','DEEPSEEK_API_KEY','RECORDER_USER','RECORDER_PASS') if not keys.get(k)]
    if core.settings().get('publish_backend','local') == 'cloudflare-pages' and not keys.get('CLOUDFLARE_API_TOKEN'):
        missing.append('CLOUDFLARE_API_TOKEN')
    if (core.settings().get('transcript_archive') or {}).get('backend') == 'r2':
        archive_keys=core.read_json(core.STATE/'r2-secrets.json',{})
        missing += [k for k in ('R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY') if not archive_keys.get(k)]
    baidu = core.read_json(core.STATE/'baidu/pcs_config.json',{})
    if not baidu.get('baidu_active_uid'):
        missing.append('BAIDU_LOGIN')
    return missing


def worker():
    lock = open(core.STATE/'worker.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with core.database() as db:
        db.execute("UPDATE jobs SET status='queued',stage='resuming' WHERE status='running'")
    while True:
        try:
            cfg = core.settings()
            missing = readiness()
            if cfg['auto_process'] and not missing:
                with core.database() as db:
                    row = scheduler.next_live(db,time.time())
                    if row is None:
                        row=history.next_job(db,time.time())
                if row:
                    # Live status is a wait condition, not a failed paid attempt.
                    pipeline.require_offline()
                    job = dict(row)
                    core.update_job(job['id'],status='running',attempts=job['attempts']+1,error=None)
                    try:
                        pipeline.require_offline()
                        payload=json.loads(job['payload'])
                        if payload.get('origin')=='baidu_history':
                            history.process_job(job['id'],payload)
                        else:
                            pipeline.process_job(job['id'],payload)
                        core.update_job(job['id'],status='done',stage='complete')
                        # A host .path unit reacts immediately. Failure to notify
                        # must not turn a completed, paid job back into a retry.
                        try:
                            core.atomic_json(core.STATE/'signals/consolidation.json',
                                {'job_id':job['id'],'completed_at':time.time()})
                        except OSError:
                            logging.warning('consolidation_signal_failed_timer_will_retry')
                    except scheduler.HistoryYield:
                        core.update_job(job['id'],status='queued',stage='yielded_for_live',
                            attempts=job['attempts'],retry_at=0,error=None)
                    except Exception as exc:
                        error = str(exc) if isinstance(exc,providers.ProviderError) else type(exc).__name__
                        status = 'retry' if job['attempts']+1<5 else 'failed'
                        delay = min(3600,60*2**job['attempts'])
                        core.update_job(job['id'],status=status,error=error,retry_at=time.time()+delay)
                        logging.error('job=%s status=%s error=%s',job['id'],status,error)
        except Exception as exc:
            error = str(exc) if isinstance(exc,providers.ProviderError) else type(exc).__name__
            logging.warning('worker_waiting reason=%s',error)
        time.sleep(5)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self,fmt,*args):
        # Webhook token is in the path. Never log request paths.
        pass

    def do_GET(self):
        route = urlsplit(self.path).path
        if route == '/health':
            self.reply(200,{'ok':True,'auto_process':core.settings()['auto_process'],
                           'missing_credentials':readiness()})
            return
        if route not in ('/','/index.html','/style.css','/app.js','/archive-utils.js','/favicon.svg','/data/archive.json','/data/index.json') and not __import__('re').fullmatch(r'/data/entries/[a-f0-9]{64}\.json',route):
            self.reply(404,{'error':'not_found'})
            return
        super().do_GET()

    def do_POST(self):
        token = core.secrets().get('WEBHOOK_TOKEN','')
        if not token or not hmac.compare_digest(self.path,'/hooks/'+token):
            self.reply(403,{'error':'forbidden'})
            return
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0<size<=1024*1024:
                self.reply(413,{'error':'body_size'})
                return
            event = json.loads(self.rfile.read(size))
            inserted = core.event_store(event)
            DISCOVERY_WAKE.set()
        except (ValueError,KeyError,TypeError,FileNotFoundError):
            self.reply(400,{'error':'invalid_event'})
            return
        self.reply(200,{'accepted':True,'inserted':inserted})

    def reply(self,status,payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def latest_files(limit=1):
    cfg = core.settings()
    files = [p for p in (core.RECORDS/cfg['record_dir']).rglob('*.flv') if not p.is_symlink()]
    return [p.relative_to(core.RECORDS).as_posix() for p in sorted(files,key=lambda p:p.stat().st_mtime,reverse=True)[:limit]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['serve','status','enable','pause','enqueue-latest','smoke','build-site','deploy','bind-domain','enrich-completed','archive-transcripts','history-start'])
    parser.add_argument('--limit',type=int,default=1)
    args = parser.parse_args()
    core.init_db()
    if args.command == 'serve':
        pipeline.build_site()
        threading.Thread(target=discovery_loop,daemon=True).start()
        threading.Thread(target=worker,daemon=True).start()
        ThreadingHTTPServer(('0.0.0.0',8080),partial(Handler,directory=str(core.STATE/'site'))).serve_forever()
    elif args.command == 'status':
        rows = core.job_rows()
        print(json.dumps({'auto_process':core.settings()['auto_process'],'missing_credentials':readiness(),
            'jobs':[{k:r[k] for k in ('id','status','stage','attempts','error','updated')} for r in rows],
            'deployment':core.read_json(core.STATE/'last-deployment.json')},ensure_ascii=False,indent=2))
    elif args.command in ('enable','pause'):
        cfg = core.settings()
        if args.command == 'enable' and readiness():
            raise providers.ProviderError('missing_credentials_'+'_'.join(readiness()))
        cfg['auto_process'] = args.command == 'enable'
        core.atomic_json(core.STATE/'config.json',cfg)
        print(json.dumps({'auto_process':cfg['auto_process']}))
    elif args.command == 'enqueue-latest':
        pipeline.require_offline()
        groups=pipeline.merging.groups(latest_files(1000000))[-args.limit:]
        if any(time.time()-core.source_path(p).stat().st_mtime<core.settings()['reconnect_grace_seconds'] for g in groups for p in g['paths']):
            raise providers.ProviderError('waiting_for_reconnect_grace')
        ids=[core.enqueue(g['paths']) for g in groups]
        print(json.dumps({'job_id':ids[0] if ids else None,'job_ids':ids}))
    elif args.command == 'smoke':
        sources = [core.identify(p) for p in latest_files(1)]
        if not sources:
            raise providers.ProviderError('no_recording_for_smoke_test')
        job_id = 'sample-'+sources[0]['fingerprint'][:16]
        result = pipeline.process_job(job_id,{'files':sources,'session_id':None},smoke=True)
        print(json.dumps({'sample_seconds':result['duration_seconds'],'id':job_id,'published':False},ensure_ascii=False))
    elif args.command == 'build-site':
        print(json.dumps({'entries':len(pipeline.build_site()['entries'])}))
    elif args.command == 'enrich-completed':
        print(json.dumps({'enriched':pipeline.enrich_completed(args.limit)}))
    elif args.command == 'archive-transcripts':
        print(json.dumps({'archived':storage.archive_completed()}))
    elif args.command == 'history-start':
        if args.limit!=10:
            raise providers.ProviderError('history_test_batch_must_be_ten_sessions')
        result=history.start_batch(core.read_json(core.STATE/'runtime/baidu-history-inventory.json'))
        print(json.dumps({'batch':result},ensure_ascii=False))
    elif args.command == 'deploy':
        pipeline.build_site()
        print(json.dumps(providers.deploy_site(core.STATE/'site')))
    elif args.command == 'bind-domain':
        result = providers.bind_domain()
        print(json.dumps({'name':result['name'],'status':result.get('status')},ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except providers.ProviderError as exc:
        print(json.dumps({'error':str(exc)}))
        raise SystemExit(1)
