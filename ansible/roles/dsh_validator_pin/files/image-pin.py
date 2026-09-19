#!/usr/bin/python3
"""Retain one approved image; no validation jobs, network, or broad Docker cleanup."""
import hashlib
import json
import os
import stat
import subprocess
import sys

IMAGE='sha256:318b8ae52ecf3656a602ba9edcc29d2130728a5f2ea9ac5ece2658df77fda39d'
ARCHIVE='/var/lib/dsh-validation/images/'+IMAGE[7:]+'.tar'
ARCHIVE_SHA='fa6750ea493fdbd59119e0474c56c68ab861e22b9304cbc981639e03654a2fb8'
ARCHIVE_SIZE=133688832
NAME='dsh-conductor-image-pin'
LABEL='io.dsh.conductor.image-pin'
MEMORY=16*1024*1024
ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/nonexistent'}

def docker(*args,input=None):
    return subprocess.run(['/usr/bin/docker',*args],input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=ENV,timeout=180,check=False)

def inspect(kind,target):
    result=docker('inspect','--type='+kind,target)
    if result.returncode:
        if b'No such' in result.stderr or b'no such' in result.stderr:
            return None
        raise RuntimeError('Docker inspection unavailable')
    values=json.loads(result.stdout)
    if not isinstance(values,list) or len(values)!=1:
        raise RuntimeError('Unexpected Docker inspection shape')
    return values[0]

def archive_bytes():
    fd=os.open(ARCHIVE,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as file:
        info=os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size!=ARCHIVE_SIZE:
            raise RuntimeError('Unsafe archive ownership, type, mode or size')
        data=file.read(ARCHIVE_SIZE+1)
    if len(data)!=ARCHIVE_SIZE or hashlib.sha256(data).hexdigest()!=ARCHIVE_SHA:
        raise RuntimeError('Archive checksum mismatch')
    return data

def validate_container(value):
    config=value.get('Config',{});host=value.get('HostConfig',{})
    # Field NAMES only in the refusal (never the inspected values), so a daemon-side change to how
    # one field is stored is diagnosable from the journal without dumping the payload into it.
    required={'Image':value.get('Image')==IMAGE,'Labels':(config.get('Labels') or {}).get(LABEL)=='0.1.0',
              'User':config.get('User')=='65532:65532','Entrypoint':config.get('Entrypoint')==['/bin/sleep'],'Cmd':config.get('Cmd')==['2147483647'],
              'NetworkMode':host.get('NetworkMode')=='none','ReadonlyRootfs':host.get('ReadonlyRootfs') is True,'Privileged':host.get('Privileged') is False,
              'Memory':host.get('Memory')==MEMORY,'MemorySwap':host.get('MemorySwap')==MEMORY,'NanoCpus':host.get('NanoCpus')==10000000,
              'PidsLimit':host.get('PidsLimit')==4,'CapDrop':host.get('CapDrop')==['ALL'],'SecurityOpt':host.get('SecurityOpt')==['no-new-privileges'],
              'RestartPolicy':(host.get('RestartPolicy') or {}).get('Name')=='unless-stopped',
              'Mounts':not value.get('Mounts'),'Binds':not host.get('Binds'),'PortBindings':not host.get('PortBindings'),
              'CapAdd':not host.get('CapAdd'),'Devices':not host.get('Devices'),'DeviceRequests':not host.get('DeviceRequests'),'DeviceCgroupRules':not host.get('DeviceCgroupRules'),
              'PidMode':not host.get('PidMode'),'UTSMode':not host.get('UTSMode'),'IpcMode':host.get('IpcMode')=='private',
              'Networks':set((value.get('NetworkSettings') or {}).get('Networks',{}))=={'none'},
              'LogConfig':(host.get('LogConfig') or {}).get('Type')=='none'}
    failed=[name for name,ok in required.items() if not ok]
    if failed:
        raise RuntimeError('Named container ownership or isolation mismatch ('+','.join(failed)+'); left untouched')

def ensure():
    if os.geteuid()!=0:
        raise RuntimeError('Root operator required')
    existing=inspect('container',NAME)
    if existing is not None:
        validate_container(existing)
        started=not existing.get('State',{}).get('Running',False)
        if started and docker('start',NAME).returncode:
            raise RuntimeError('Could not start owned retention container')
        current=inspect('container',NAME)
        if current is None:
            raise RuntimeError('Retention container disappeared')
        validate_container(current)
        if not current.get('State',{}).get('Running',False):
            raise RuntimeError('Retention container is not running')
        return {'created':False,'started':started,'imageRestored':False}
    restored=False
    for attempt in (1,2):
        image=inspect('image',IMAGE)
        if image is None:
            # Feed exactly the verified bytes, never re-open the archive for publication.
            if docker('load',input=archive_bytes()).returncode:
                raise RuntimeError('Verified image restore failed')
            restored=True
            image=inspect('image',IMAGE)
        if image is None or image.get('Id')!=IMAGE or image.get('Config',{}).get('Volumes'):
            raise RuntimeError('Pinned image identity or volume declaration mismatch')
        result=docker('run','--detach','--name',NAME,'--label',LABEL+'=0.1.0','--restart=unless-stopped',
                      '--pull=never','--network=none','--ipc=private','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                      '--pids-limit=4','--memory=16m','--memory-swap=16m','--cpus=0.01','--user=65532:65532',
                      '--log-driver=none','--entrypoint=/bin/sleep',IMAGE,'2147483647')
        if not result.returncode:
            break
        # Until the container exists nothing references the image, so the runner cleanup's
        # `image prune -af` can remove it between the inspection above and this run. That one
        # cause gets one restore-and-retry; any other failure (a name taken meanwhile) is refused.
        if attempt==2 or inspect('image',IMAGE) is not None:
            raise RuntimeError('Retention creation did not confirm success; inspect reserved name')
    current=inspect('container',NAME)
    if current is None:
        raise RuntimeError('Retention container disappeared')
    validate_container(current)
    if not current.get('State',{}).get('Running',False):
        raise RuntimeError('Retention container did not remain running')
    return {'created':True,'started':True,'imageRestored':restored}

if __name__=='__main__':
    try:
        print(json.dumps({'ready':True,'image':IMAGE,'container':NAME,**ensure()}))
    except Exception as error:
        # No Docker stderr, inspection payload, credentials or archive bytes in logs.
        print('Image retention refused: '+str(error),file=sys.stderr)
        raise SystemExit(1)
