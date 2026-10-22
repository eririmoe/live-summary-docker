"""Video-only retention. Dry-run unless both config and CLI explicitly enable it."""
import argparse
import json
import time
from pathlib import Path
import core


def candidates():
    cfg = core.settings()
    paths = sorted((core.RECORDS/cfg['record_dir']).rglob('*.flv'),key=lambda p:p.stat().st_mtime,reverse=True)
    keep = {p.resolve() for p in paths[:cfg['keep_newest_flv']]}
    jobs = {r['id']:r for r in core.job_rows() if r['status']=='done'}
    eligible = []
    for job_id,row in jobs.items():
        if json.loads(row['payload']).get('origin')=='baidu_history':
            continue
        directory = core.STATE/'jobs'/job_id
        receipt = core.read_json(directory/'backup.json',{})
        published = core.read_json(directory/'published.json',{})
        if receipt.get('method')!='remote_exact_bytes' or not receipt.get('verified_at') or not published.get('verified_at'):
            continue
        if not (directory/'transcript.json').exists() or not (directory/'summary.json').exists():
            continue
        for source in json.loads(row['payload'])['files']:
            try:
                p = core.source_path(source['relative'])
                if p in keep or p.suffix!='.flv' or time.time()-p.stat().st_mtime<cfg['minimum_age_days']*86400:
                    continue
                if not core.unchanged(source):
                    continue
                if not any(f['relative']==source['relative'] and f['size']==source['size'] for f in receipt.get('files',[])):
                    continue
                eligible.append(source)
            except (ValueError,FileNotFoundError):
                continue
    return eligible


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    if args.apply and not core.settings()['cleanup_apply']:
        raise SystemExit('cleanup_apply is false; no file was deleted')
    core.init_db()
    chosen = candidates()
    result = {'mode':'apply' if args.apply else 'dry-run','candidates':chosen,'deleted':[]}
    if args.apply:
        for source in chosen:
            if core.unchanged(source):
                core.source_path(source['relative']).unlink()
                result['deleted'].append(source['relative'])
    core.atomic_json(core.STATE/'cleanup-last.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
