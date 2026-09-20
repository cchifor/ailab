#!/usr/bin/env python3
"""Operator-only private-release provisioning; no credentials stored in this manifest.

Use the downloaded, independently reviewed release artifact. No network download,
replacement, secret lookup or mutable patch is performed. Deleted objects require
explicit operator restoration from the private release; GitOps only references it.
"""
import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess

SHA='7a97432202131d79f11bb578708f2a3df32cde65700a28c778ca958ad4bdcb28'
SIZE=54302
NAME='dsh-conductor-artifact-'+SHA[:16]
KEY='dsh-team-conductor-0.1.0.tgz'


def read_verified(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size!=SIZE:
            raise ValueError('Expected exact regular release artifact')
        with os.fdopen(fd,'rb',closefd=False) as source:
            payload=source.read(SIZE+1)
        if len(payload)!=SIZE or hashlib.sha256(payload).hexdigest()!=SHA:
            raise ValueError('Published release checksum mismatch')
        return payload
    finally:
        os.close(fd)


def manifest(payload):
    if len(payload)!=SIZE or hashlib.sha256(payload).hexdigest()!=SHA:
        raise ValueError('Unverified artifact bytes')
    return {'apiVersion':'v1','kind':'ConfigMap','metadata':{
        'name':NAME,'namespace':'dsh',
        'annotations':{'dsh.chifor.me/artifact-sha256':SHA,
                       'dsh.chifor.me/artifact-source':'private-release-v0.1.0'}},
        'immutable':True,'binaryData':{KEY:base64.b64encode(payload).decode('ascii')}}


def verify_existing(value,payload):
    if (value.get('kind')!='ConfigMap' or value.get('metadata',{}).get('name')!=NAME
        or value.get('metadata',{}).get('namespace')!='dsh' or value.get('immutable') is not True
        or value.get('metadata',{}).get('ownerReferences') or value.get('metadata',{}).get('deletionTimestamp')
        or value.get('data') or set(value.get('binaryData',{}))!={KEY}
        or value.get('metadata',{}).get('annotations',{}).get('dsh.chifor.me/artifact-sha256')!=SHA):
        raise ValueError('Existing artifact object does not match; left untouched')
    if base64.b64decode(value['binaryData'][KEY],validate=True)!=payload:
        raise ValueError('Existing artifact bytes differ; left untouched')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    payload=read_verified(args.artifact)
    desired=manifest(payload)
    existing=subprocess.check_output(['kubectl','get','configmap',NAME,'-n','dsh','--ignore-not-found','-o','json'],text=True,timeout=30)
    if existing.strip():
        verify_existing(json.loads(existing),payload)
        print(json.dumps({'name':NAME,'sha256':SHA,'bytes':SIZE,'created':False,'verified':True}))
        return
    command=['kubectl','create','-f','-','-o','name']
    if args.dry_run:command.append('--dry-run=server')
    # Publish the SAME verified buffer, never reopen the source after hashing.
    subprocess.run(command,input=json.dumps(desired),text=True,check=True,timeout=30)
    if not args.dry_run:
        observed=json.loads(subprocess.check_output(['kubectl','get','configmap',NAME,'-n','dsh','-o','json'],text=True,timeout=30))
        verify_existing(observed,payload)
    print(json.dumps({'name':NAME,'sha256':SHA,'bytes':SIZE,'created':not args.dry_run,'serverDryRun':args.dry_run,'verified':True}))


if __name__=='__main__':main()
