"""Fail closed on risky tracked files and common credential formats. No values printed."""
import argparse
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
PATTERNS={
    'private_key':rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'github_token':rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})',
    'api_key':rb'\bsk-[A-Za-z0-9]{24,}\b',
    'aws_key':rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'account_cookie':rb'(?i)\b(?:BDUSS|STOKEN|SESSDATA)\s*[=:]\s*[A-Za-z0-9%._-]{16,}',
    'windows_user_path':rb'(?i)[A-Z]:[\\/]Users[\\/](?!Public[\\/])[^\s"\r\n]+',
}
PRIVATE_NAMES={'secrets.json','recorder-secrets.json','r2-secrets.json','pcs_config.json','config.json','.env'}
PRIVATE_SUFFIXES={'.pem','.key','.p12','.sqlite3','.flv','.mp3','.wav','.zip','.tgz'}

def scan(paths):
    findings=[]
    for relative in paths:
        path=ROOT/relative
        if not path.is_file():continue
        if path.is_symlink() or path.name in PRIVATE_NAMES or path.name.endswith(('.private.json','.private.env')) or path.suffix in PRIVATE_SUFFIXES or relative.split('/')[0] in ('data','records','state'):
            findings.append({'file':relative,'rule':'private_file'});continue
        data=path.read_bytes()
        for label,pattern in PATTERNS.items():
            for match in re.finditer(pattern,data):
                findings.append({'file':relative,'line':data[:match.start()].count(b'\n')+1,'rule':label})
    return findings

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tracked',action='store_true');args=parser.parse_args()
    if args.tracked:
        result=subprocess.run(['git','ls-files','-z'],cwd=ROOT,check=True,stdout=subprocess.PIPE)
        paths=[p for p in result.stdout.decode().split('\0') if p]
    else:
        paths=[p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and not set(p.relative_to(ROOT).parts)&{'.git','__pycache__','data','records','.venv'}]
    findings=scan(paths)
    import json
    print(json.dumps({'ok':not findings,'files_scanned':len(paths),'findings':findings},indent=2))
    return 1 if findings else 0

if __name__=='__main__':raise SystemExit(main())
