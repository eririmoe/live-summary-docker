"""Private R2 S3 uploads, SigV4 authentication, and full readback verification."""
import hashlib
import hmac
import json
import re
import time
from datetime import datetime,timezone
from urllib.parse import quote

import requests
import core
import providers


def presigned_read_url(key,expires=300,method='GET'):
    """Browser reads private R2 directly; never log the returned bearer URL."""
    cfg=core.settings().get('transcript_archive') or {}
    keys=core.read_json(core.STATE/'r2-secrets.json',{})
    account,bucket=cfg.get('account_id',''),cfg.get('bucket','')
    if (cfg.get('backend')!='r2' or not re.fullmatch(r'[a-f0-9]{32}',account)
            or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',bucket)
            or not all(keys.get(k) for k in ('R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY'))):
        raise providers.ProviderError('r2_download_configuration_missing')
    if (method not in ('GET','HEAD') or not 1<=expires<=900
            or not re.fullmatch(re.escape(cfg.get('prefix','transcripts').strip('/')+'/'+str(core.settings()['room_id']))+r'/\d{4}/\d{2}/\d{2}/[a-f0-9]{24}/transcript\.(txt|json)',key)):
        raise providers.ProviderError('r2_download_scope_invalid')
    now=datetime.now(timezone.utc)
    date,stamp=now.strftime('%Y%m%d'),now.strftime('%Y%m%dT%H%M%SZ')
    host=account+'.r2.cloudflarestorage.com'
    uri=quote('/'+bucket+'/'+key,safe='/-_.~')
    scope=date+'/auto/s3/aws4_request'
    query={'X-Amz-Algorithm':'AWS4-HMAC-SHA256',
        'X-Amz-Credential':keys['R2_ACCESS_KEY_ID']+'/'+scope,'X-Amz-Date':stamp,
        'X-Amz-Expires':str(expires),'X-Amz-SignedHeaders':'host'}
    canonical_query='&'.join(quote(k,safe='-_.~')+'='+quote(v,safe='-_.~') for k,v in sorted(query.items()))
    canonical=method+'\n'+uri+'\n'+canonical_query+'\nhost:'+host+'\n\nhost\nUNSIGNED-PAYLOAD'
    message='AWS4-HMAC-SHA256\n'+stamp+'\n'+scope+'\n'+hashlib.sha256(canonical.encode()).hexdigest()
    secret=('AWS4'+keys['R2_SECRET_ACCESS_KEY']).encode()
    for value in (date,'auto','s3','aws4_request'):
        secret=hmac.new(secret,value.encode(),hashlib.sha256).digest()
    signature=hmac.new(secret,message.encode(),hashlib.sha256).hexdigest()
    return 'https://'+host+uri+'?'+canonical_query+'&X-Amz-Signature='+signature


def signed_request(method,key,data=b'',content_type=None):
    cfg=core.settings().get('transcript_archive') or {}
    keys=core.read_json(core.STATE/'r2-secrets.json',{})
    if not all(keys.get(k) for k in ('R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY')):
        raise providers.ProviderError('r2_credentials_missing')
    account=cfg.get('account_id','')
    bucket=cfg.get('bucket','')
    if not re.fullmatch(r'[a-f0-9]{32}',account) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',bucket):
        raise providers.ProviderError('r2_configuration_invalid')
    host=account+'.r2.cloudflarestorage.com'
    uri=quote('/'+bucket+'/'+key,safe='/-_.~')
    now=datetime.now(timezone.utc)
    amzdate=now.strftime('%Y%m%dT%H%M%SZ')
    date=now.strftime('%Y%m%d')
    body_hash=hashlib.sha256(data).hexdigest()
    headers={'host':host,'x-amz-content-sha256':body_hash,'x-amz-date':amzdate}
    signed=';'.join(sorted(headers))
    canonical=method+'\n'+uri+'\n\n'+''.join(k+':'+headers[k]+'\n' for k in sorted(headers))+'\n'+signed+'\n'+body_hash
    scope=date+'/auto/s3/aws4_request'
    message='AWS4-HMAC-SHA256\n'+amzdate+'\n'+scope+'\n'+hashlib.sha256(canonical.encode()).hexdigest()
    def sign(secret,value):
        return hmac.new(secret,value.encode(),hashlib.sha256).digest()
    secret=('AWS4'+keys['R2_SECRET_ACCESS_KEY']).encode()
    for value in (date,'auto','s3','aws4_request'):
        secret=sign(secret,value)
    signature=hmac.new(secret,message.encode(),hashlib.sha256).hexdigest()
    headers['Authorization']='AWS4-HMAC-SHA256 Credential='+keys['R2_ACCESS_KEY_ID']+'/'+scope+', SignedHeaders='+signed+', Signature='+signature
    if content_type:
        headers['Content-Type']=content_type
    try:
        response=requests.request(method,'https://'+host+uri,headers=headers,data=data if method=='PUT' else None,
            timeout=(15,60),allow_redirects=False)
    except requests.RequestException:
        raise providers.ProviderError('r2_network_'+method) from None
    if not 200<=response.status_code<300:
        raise providers.ProviderError('r2_http_'+str(response.status_code))
    return response.content


def archive(directory,job_id,metadata):
    cfg=core.settings().get('transcript_archive') or {}
    if cfg.get('backend')!='r2':
        return {'backend':'local','configured':False}
    if not re.fullmatch(r'[a-f0-9]{24}',job_id):
        raise providers.ProviderError('r2_invalid_job_id')
    date=metadata['started_at'][:10].replace('-','/')
    prefix=cfg.get('prefix','transcripts').strip('/')+'/'+str(core.settings()['room_id'])+'/'+date+'/'+job_id
    files=[]
    for name in ('transcript.json','transcript.txt'):
        data=(directory/name).read_bytes()
        files.append({'name':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    manifest={'schema_version':1,'job_id':job_id,'metadata':metadata,'files':files}
    core.atomic_json(directory/'transcript-manifest.json',manifest)
    manifest_data=(directory/'transcript-manifest.json').read_bytes()
    files.append({'name':'manifest.json','bytes':len(manifest_data),'sha256':hashlib.sha256(manifest_data).hexdigest()})
    previous=core.read_json(directory/'transcript-archive.json',{})
    previous_files={f['key']:f for f in previous.get('objects',[])} if previous.get('bucket')==cfg['bucket'] else {}
    objects=[]
    for item in files:
        name=item['name']
        key=prefix+'/'+name
        data=manifest_data if name=='manifest.json' else (directory/name).read_bytes()
        verified=False
        if previous_files.get(key,{}).get('sha256')==item['sha256']:
            try:
                verified=hashlib.sha256(signed_request('GET',key)).hexdigest()==item['sha256']
            except providers.ProviderError as exc:
                if str(exc)!='r2_http_404':
                    raise
        if not verified:
            signed_request('PUT',key,data,'text/plain; charset=utf-8' if name.endswith('.txt') else 'application/json; charset=utf-8')
            downloaded=signed_request('GET',key)
            if len(downloaded)!=item['bytes'] or hashlib.sha256(downloaded).hexdigest()!=item['sha256']:
                raise providers.ProviderError('r2_readback_checksum_mismatch')
        objects.append({**item,'key':key})
    receipt={'backend':'r2','bucket':cfg['bucket'],'verified_at':time.time(),'objects':objects}
    core.atomic_json(directory/'transcript-archive.json',receipt)
    return receipt


def archive_completed():
    archived=[]
    for row in core.job_rows():
        if row['status']!='done':
            continue
        directory=core.STATE/'jobs'/row['id']
        transcript=core.read_json(directory/'transcript.json')
        if transcript:
            receipt=archive(directory,row['id'],transcript['metadata'])
            if receipt.get('verified_at'):
                archived.append(row['id'])
    return archived
