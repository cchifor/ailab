#!/usr/bin/python3
"""Retain one approved image; no validation jobs, network, or broad Docker cleanup."""
import hashlib
import json
import os
import stat
import subprocess

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
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_size!=ARCHIVE_SIZE:
            raise RuntimeError('Unsafe archive ownership, type, mode or size')
        data=file.read(ARCHIVE_SIZE+1)
    if len(data)!=ARCHIVE_SIZE or hashlib.sha256(data).hexdigest()!=ARCHIVE_SHA:
        raise RuntimeError('Archive checksum mismatch')
    return data

def validate_container(value):
    config=value.get('Config',{});host=value.get('HostConfig',{})
    required=[value.get('Image')==IMAGE,(config.get('Labels') or {}).get(LABEL)=='0.1.0',
              config.get('User')=='65532:65532',config.get('Entrypoint')==['/bin/sleep'],config.get('Cmd')==['2147483647'],
              host.get('NetworkMode')=='none',host.get('ReadonlyRootfs') is True,host.get('Privileged') is False,
              host.get('Memory')==MEMORY,host.get('MemorySwap')==MEMORY,host.get('NanoCpus')==10000000,
              host.get('PidsLimit')==4,host.get('CapDrop')==['ALL'],host.get('SecurityOpt')==['no-new-privileges'],
              (host.get('RestartPolicy') or {}).get('Name')=='unless-stopped',
              not value.get('Mounts'),not host.get('Binds'),not host.get('PortBindings'),
              not host.get('CapAdd'),not host.get('Devices'),not host.get('DeviceRequests'),not host.get('DeviceCgroupRules'),
              not host.get('PidMode'),not host.get('UTSMode'),host.get('IpcMode')=='private',
              set((value.get('NetworkSettings') or {}).get('Networks',{}))=={'none'},
              (host.get('LogConfig') or {}).get('Type')=='none']
    if not all(required):
        raise RuntimeError('Named container ownership or isolation mismatch; left untouched')

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
    image=inspect('image',IMAGE);restored=image is None
    if restored:
        # Feed exactly the verified bytes, never re-open the archive for publication.
        if docker('load',input=archive_bytes()).returncode:
            raise RuntimeError('Verified image restore failed')
        image=inspect('image',IMAGE)
    if image is None or image.get('Id')!=IMAGE or image.get('Config',{}).get('Volumes'):
        raise RuntimeError('Pinned image identity or volume declaration mismatch')
    result=docker('run','--detach','--name',NAME,'--label',LABEL+'=0.1.0','--restart=unless-stopped',
                  '--pull=never','--network=none','--ipc=private','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                  '--pids-limit=4','--memory=16m','--memory-swap=16m','--cpus=0.01','--user=65532:65532',
                  '--log-driver=none','--entrypoint=/bin/sleep',IMAGE,'2147483647')
    if result.returncode:
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
        print('Image retention refused: '+str(error),file=__import__('sys').stderr)
        raise SystemExit(1)
