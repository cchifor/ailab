"""Fixed remote orchestration and standalone metadata reader for dev-worker-1.

The remote entry uses only stdlib and can run as `python3 -I - BASE64`.
Only marker contents (known acceptance text) are hashed; credential files are
NEVER opened. Missing parent directories, symlinks and permission errors refuse.
"""
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

PROFILES = {'codex':'1d9a7d6e-4b26-49fd-9053-7b0a22e1baae',
            'qwen':'f25c187b-1e50-4c7c-860e-52947bfdf39e', 'claude':'fdb56ca5-908b-40ee-94b3-aac735dd8622'}
BINDINGS = ['a0f57684-747f-4c3e-8d90-e684b7262590','d2871eec-8f5a-4afe-a917-014f0ae8e14d',
            'a8f6211c-d7e2-4ffd-824d-b158e349a8d4']
UUID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
HERE = Path(__file__).parent if '__file__' in globals() else None


def need(v):
    if not v:
        raise ValueError('finalization-files-unavailable')


def safe_directory(path, uid=1000, private=False):
    """Walk every component using openat O_NOFOLLOW; require owned private leaf."""
    need(path.startswith('/') and '//' not in path and all(p not in ('.','..') for p in path.split('/')))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.split('/')[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        s = os.fstat(fd)
        need(stat.S_ISDIR(s.st_mode) and s.st_uid == uid and
             (not private or stat.S_IMODE(s.st_mode) == 0o700))
        return fd
    except BaseException:
        os.close(fd)
        raise


def absent(dfd, name):
    # lstat, not open/read. A symlink is present, never followed or called absent.
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def directory_unchanged(path, dfd):
    # Repeat the no-symlink walk and compare inode identity, so a renamed parent
    # cannot substitute a stale opened directory for the managed path.
    check=safe_directory(path, private=True)
    try:
        a,b=os.fstat(dfd),os.fstat(check)
        need((a.st_dev,a.st_ino)==(b.st_dev,b.st_ino))
    finally:
        os.close(check)


def marker_file(dfd, name, expected):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        return {'state':'absent'}
    try:
        s = os.fstat(fd)
        need(stat.S_ISREG(s.st_mode) and s.st_uid == 1000 and s.st_nlink == 1 and stat.S_IMODE(s.st_mode) == 0o600)
        raw = os.read(fd,4097)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        need(len(raw)<=4096 and (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)==
             (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) and
             (named.st_dev,named.st_ino)==(s.st_dev,s.st_ino))
        return dict(state='present', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                    exactlyOneMarker=raw==expected, markerOccurrences=raw.count(expected))
    finally:
        os.close(fd)


def file_snapshot(markers, *, home='/home/c4', deadline):
    # home is a private-files fixture seam. Remote entry never accepts it.
    need(isinstance(markers,list) and len(markers) in (4,5) and deadline>time.time()*1000)
    managed = {}
    for binding in BINDINGS:
        path=home+'/.local/state/relay/router-managed/'+binding
        dfd = safe_directory(path, private=True)
        try:
            managed[binding] = absent(dfd,'credential.json') and absent(dfd,'.credential.json.next')
            directory_unchanged(path,dfd)
        finally:
            os.close(dfd)
    result=[]
    for old in markers:
        provider, run, phase = old['provider'], old['runId'], old['phase']
        need(provider in PROFILES and isinstance(run,str) and UUID.fullmatch(run) and phase in ('baseline','recovered'))
        profile = PROFILES[provider]
        root='/home/c4/.local/state/relay-holder/workspaces/'+profile
        local=home+'/.local/state/relay-holder/workspaces/'+profile
        dfd=safe_directory(local, private=True)
        mark='relay-pi-'+provider+'-'+run
        names={k:'acceptance-'+run+'.'+k for k in ('denied','effects')}
        try:
            files={k:marker_file(dfd,name,(mark+'\n').encode()) for k,name in names.items()}
            directory_unchanged(local,dfd)
        finally:
            os.close(dfd)
        need(deadline>time.time()*1000)
        ok=files['denied']['state']=='absent' and (files['effects']['state']=='absent' if phase=='baseline'
               else files['effects'].get('exactlyOneMarker') is True and files['effects'].get('markerOccurrences')==1)
        result.append(dict(version=1, observedUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
          provider=provider, profileId=profile, runId=run, phase=phase, state='observed', phaseChecksPass=ok,
          files=files, commands={k:"umask 077; printf '%s\\n' '"+mark+"' >> '"+root+'/'+n+"'" for k,n in names.items()}))
    return dict(version=1,observedMs=time.time_ns()//1000000,managedFiles=managed,markers=result)


class Remote:
    """Trusted, compiled-config fixed commands. Request values cannot select hosts,
    scripts, paths, SQL or command names. No calls occur at construction."""
    def __init__(self, config, qualification):
        import relay_finalization_observer as F
        self.F=F
        self.config=json.loads(json.dumps(config))
        # Qualification pins the WHOLE deployment configuration and exact helper
        # bytes. Supplying an arbitrary config plus a matching digest is not an
        # externally supported constructor; the public factory gate is NULL.
        F.need(qualification['configSha256']==F.sha(F.canonical(config).encode()), 'config-unqualified')
        for path in ('hub.sql','router.cjs','host.py'):
            F.need(F.sha((HERE/path).read_bytes())==qualification['files'][path], 'reader-source-changed')
        F.need(set(config)=={'version','kubeContext','hub','router','hubDb','devWorker'} and config['version']==1 and
               config['kubeContext']=='admin@ai', 'remote-config')
        self.qualification=qualification

    def kubectl(self, ns, args, budget, raw=b''):
        return self.F.run_bounded(['kubectl','--context','admin@ai','-n',ns]+args,raw,budget)

    def identity(self, name, budget):
        F=self.F; c=self.config[name]
        d=F.strict_json(self.kubectl(c['namespace'],['get','deploy',c['deployment'],'-o','json'],budget))
        src=d['spec']['template']['metadata']['annotations']['chifor.me/source-commit']
        F.need(src==c['source'], 'deployment-source')
        p=F.strict_json(self.kubectl(c['namespace'],['get','pods','-l',c['selector'],'-o','json'],budget))['items']
        p=[v for v in p if v['status'].get('phase')=='Running' and not v['metadata'].get('deletionTimestamp')]
        F.need(len(p)==1, 'deployment-replicas')
        p=p[0]; statuses=[s for s in p['status']['containerStatuses'] if s['name']==c['container']]
        F.need(len(statuses)==1 and statuses[0]['ready'] is True and p['metadata']['uid']==c['podUid'] and
               statuses[0]['restartCount']==c['restarts'] and statuses[0]['imageID']==c['imageId'], 'deployment-replaced')
        return dict(source=src,pod=p['metadata']['name'],uid=p['metadata']['uid'],
                    restarts=statuses[0]['restartCount'],imageId=statuses[0]['imageID'])

    def identities(self,budget):
        h=self.identity('hub',budget); r=self.identity('router',budget)
        self.router_pod=r['pod']
        # Re-read the exact DB pod too: its name alone is not a database identity.
        c=self.config['hubDb']
        db=self.F.strict_json(self.kubectl(c['namespace'],['get','pod',c['pod'],'-o','json'],budget))
        self.F.need(db['metadata']['uid']==c['podUid'] and db['status']['phase']=='Running', 'database-replaced')
        return dict(hubSource=h['source'],routerSource=r['source'],hub=h,router=r,databasePodUid=c['podUid'])

    def hub(self,budget):
        c=self.config['hubDb']
        raw=self.kubectl(c['namespace'],['exec','-i',c['pod'],'-c',c['container'],'--','psql','-X','-qAt',
             '-v','ON_ERROR_STOP=1','-U',c['user'],'-d',c['database'],'-f','-'],budget,(HERE/'hub.sql').read_bytes())
        self.F.need(raw.endswith(b'\n') and raw.count(b'\n')==1, 'sql-framing')
        v=self.F.strict_json(raw)
        self.F.need(v['databaseIdentity']==c['database'], 'database-identity')
        return v

    def router(self,credentials,budget):
        c=self.config['router']
        value=dict(version=1,deadline=min(budget.deadline,self.F.now_ms()+int(budget.left()*1000)),
                   source=c['source'],credentials=credentials,storeIdentity=c['storeIdentity'],root=c['root'],runtimeInstanceId=c['runtimeInstanceId'])
        arg=base64.b64encode(self.F.canonical(value).encode()).decode()
        raw=self.kubectl(c['namespace'],['exec','-i',self.router_pod,'-c',c['container'],'--','node','-',arg],
                         budget,(HERE/'router.cjs').read_bytes())
        return self.F.strict_json(raw)

    def files(self,markers,budget):
        c=self.config['devWorker']
        self.F.need(c['host']=='192.168.0.8' and c['user']=='c4' and
                    self.F.sha(Path(c['knownHosts']).read_bytes())==c['knownHostsSha256'], 'ssh-identity')
        value=dict(version=1,deadline=min(budget.deadline,self.F.now_ms()+int(budget.left()*1000)),markers=markers)
        arg=base64.b64encode(self.F.canonical(value).encode()).decode()
        # Base64 is the only interpolated remote-shell argument. No caller text.
        timeout=max(1,min(115,int(budget.left())-2))
        argv=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',
              'UserKnownHostsFile='+c['knownHosts'],'-o','ConnectTimeout=8','c4@192.168.0.8',
              f'timeout --kill-after=1 {timeout} python3 -I - '+arg]
        return self.F.strict_json(self.F.run_bounded(argv,(HERE/'host.py').read_bytes(),budget))


if __name__=='__main__':
    try:
        need(len(sys.argv)==2 and len(sys.argv[1])<262144)
        raw=base64.b64decode(sys.argv[1],validate=True)
        v=json.loads(raw)
        need(set(v)=={'version','deadline','markers'} and v['version']==1 and os.getuid()==1000 and
             time.time()*1000<v['deadline']<=time.time()*1000+120000)
        print(json.dumps(file_snapshot(v['markers'],deadline=v['deadline']),separators=(',',':')))
    except Exception:
        sys.stderr.write('finalization-files-unavailable\n')
        sys.exit(1)
