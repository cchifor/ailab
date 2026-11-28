"""dw.py: phase A observations on dev-worker-1, run as c4 (UID 1000) by the host observer over ssh stdin
(`python3 -I - MODE ARGS...`). One JSON line on stdout; only allowlisted, typed fields. Never executes a printed
command, never reads credential contents. The read-only modes are below; the four `mutate-*` modes (holder start and
stop, admission resume, unit enable plus reconciliation start) are the handoff's only writes. Read-only modes:
  clock                                   - this host's epoch ms
  marker PROVIDER RUN PHASE NOT_BEFORE_MS - the driver's marker report (wait past NOT_BEFORE on THIS clock)
  holder STATE_DIR                        - typed holder record: hello + journal + systemd + artifact hashes
  credfile BINDING                        - credential.json metadata through one O_NOFOLLOW descriptor
  transcript NATIVE_HOME                  - the Pi native session record's settled-tool and config facts
  prerun STATE_DIR PREPARATION            - the read-only pre-run facts (units, receipt, journal admission, seal)
"""
import datetime
import hashlib
import json
import math
import os
import re
import socket
import stat
import subprocess
import sys
import time
import uuid

UID = 1000
HOME = "/home/c4"
PROFILES = {"codex": "1d9a7d6e-4b26-49fd-9053-7b0a22e1baae", "qwen": "f25c187b-1e50-4c7c-860e-52947bfdf39e"}
WORKSPACES = HOME + "/.local/state/relay-holder/workspaces"  # where the marker files are read
RENDER_ROOT = HOME + "/.local/state/relay-holder/workspaces"  # the driver's fixed markerCommand() root
PHASES = ("baseline", "denied", "approved", "recovered")
UNITS = ("relay-holder.service", "relay-holder-control-plane.service")


def out(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")


def die(word, code=1):
    sys.stderr.write(word + "\n")
    sys.exit(code)


def canon_uuid(v):
    try:
        if str(uuid.UUID(v)) == v:
            return v
    except (ValueError, TypeError, AttributeError):
        pass
    die("bad uuid", 2)


def sha_file(path, limit=1 << 28):
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            die("not a regular file")
        n = 0
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            n += len(b)
            if n > limit:
                die("file too large")
            h.update(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def now_ms():
    return int(time.time() * 1000)


# ------------------------------------------------------------------------------------------ marker (observe.py)
def file_evidence(dfd, name, expected):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        return {"state": "absent"}
    except OSError:
        return {"state": "unsafe-or-unreadable"}
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != UID or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600:
            return {"state": "unsafe-or-unreadable"}
        data = os.read(fd, 4097)
        if len(data) > 4096:
            return {"state": "oversized"}
        return {"state": "present", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "exactlyOneMarker": data == expected, "markerOccurrences": data.count(expected)}
    finally:
        os.close(fd)


def marker(provider, run, phase, not_before):
    if provider not in PROFILES or phase not in PHASES:
        die("bad selection", 2)
    run = canon_uuid(run)
    while now_ms() < not_before:
        time.sleep(min(0.25, (not_before - now_ms()) / 1000.0))
    root = WORKSPACES + "/" + PROFILES[provider]
    mark = "relay-pi-" + provider + "-" + run
    names = {"denied": "acceptance-" + run + ".denied", "effects": "acceptance-" + run + ".effects"}
    report = {"version": 1, "observedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "provider": provider, "profileId": PROFILES[provider], "runId": run, "phase": phase, "marker": mark,
              "scope": "Local marker effects only; verify hub permissions, router receipts and native history separately."}
    # The driver's fixed markerCommand() rendering, independent of where the files are read.
    report["commands"] = {k: "umask 077; printf '%s\\n' '" + mark + "' >> '" + RENDER_ROOT + "/" + PROFILES[provider]
                          + "/" + n + "'" for k, n in names.items()}
    try:
        dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        report.update(state="workspace-unavailable", phaseChecksPass=False)
        return report
    try:
        s = os.fstat(dfd)
        if s.st_uid != UID or stat.S_IMODE(s.st_mode) != 0o700:
            report.update(state="unsafe-workspace", phaseChecksPass=False)
            return report
        files = {k: file_evidence(dfd, n, (mark + "\n").encode()) for k, n in names.items()}
    finally:
        os.close(dfd)
    effect_ok = (files["effects"]["state"] == "absent" if phase in ("baseline", "denied")
                 else files["effects"].get("exactlyOneMarker") is True and files["effects"].get("markerOccurrences") == 1)
    report.update(state="observed", files=files, phaseChecksPass=files["denied"]["state"] == "absent" and effect_ok)
    return report


# ------------------------------------------------------------------------------------------ holder
def hello(state_dir, wait_s=0.0):
    sock = os.path.join(state_dir, "control.sock")
    end = time.monotonic() + wait_s
    while True:
        try:
            st = os.lstat(sock)
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != UID or stat.S_IMODE(st.st_mode) & 0o077:
                return None
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect(sock)
            s.sendall(b'{"version":1,"action":{"type":"hello"}}\n')
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 1 << 20:
                c = s.recv(65536)
                if not c:
                    break
                buf += c
            s.close()
            r = json.loads(buf)
            if r.get("ok") is True and isinstance(r.get("result"), dict):
                return r["result"]
        except (OSError, ValueError):
            pass
        if time.monotonic() >= end:
            return None
        time.sleep(1)


def systemd(unit):
    p = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
                        "-p", "NRestarts", "-p", "UnitFileState", "-p", "DropInPaths", "-p", "ExecStart"],
                       capture_output=True, text=True, timeout=10)
    d = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
    x = d.get("ExecStart", "")
    argv = x.split("argv[]=", 1)[1].split(" ;", 1)[0].split() if "argv[]=" in x else []
    return {"active": d.get("ActiveState"), "sub": d.get("SubState"), "mainPid": int(d.get("MainPID") or 0),
            "restarts": int(d.get("NRestarts") or 0), "enabled": d.get("UnitFileState"),
            "dropIns": d.get("DropInPaths", ""), "argv": argv}


def journal(state_dir):
    """The last holder-started incarnation and the last admission event (paused, revision, operationId)."""
    inc, adm, n, kinds = None, None, 0, []
    path = os.path.join(state_dir, "journal.jsonl")
    with open(path, "rb") as f:
        for line in f:
            n += 1
            d = json.loads(line)
            e = d.get("event") or {}
            kinds.append(str(e.get("kind")))
            if e.get("kind") == "holder-started":
                inc = d.get("incarnation")
            elif e.get("kind") == "holder-admission":
                adm = {"paused": e.get("paused") is True, "revision": e.get("revision"),
                       "operationId": e.get("operationId"), "incarnation": d.get("incarnation")}
    return {"records": n, "lastStartedIncarnation": inc, "admission": adm, "kinds": kinds, "sha256": sha_file(path)}


MAINTENANCE_IMAGE = ("registry.chifor.me/relay/control-plane@sha256:"
                     "7fce02539992382d946ce523ead109cd3a325a8112fe60b619ed783cef7295ed")


def maintenance_status(state_dir):
    """holder-maintenance `status` (it answers only a RUNNING holder, over its private socket)."""
    p = subprocess.run(["timeout", "--kill-after=2", "40", "docker", "run", "--rm", "--network", "none",
                        "--user", "1000:1000", "-v", state_dir + ":" + state_dir, MAINTENANCE_IMAGE, "node",
                        "dist/scripts/holder-maintenance.js", state_dir, "status"],
                       capture_output=True, timeout=45)
    if p.returncode != 0:
        return None
    d = json.loads(p.stdout)
    adm = d.get("admission") if isinstance(d.get("admission"), dict) else {}
    return {"incarnation": d.get("incarnation"), "admissionPaused": adm.get("paused"),
            "admissionRevision": adm.get("revision"), "activeSessions": d.get("activeSessions"),
            "pendingCommands": d.get("pendingCommands"), "nativeStateReady": d.get("nativeStateReady"),
            "sealed": bool(d.get("sealId"))}


def holder(state_dir):
    h = hello(state_dir)
    rec = {"observedMs": now_ms(), "hello": None, "journal": journal(state_dir),
           "units": {u: systemd(u) for u in UNITS}}
    rec["status"] = maintenance_status(state_dir) if h is not None else None
    if h is not None:
        sessions = h.get("sessions")
        profs = h.get("profiles") or []
        rec["hello"] = {
            "incarnation": h.get("incarnation"), "journalId": h.get("journalId"),
            "nativeRecovery": h.get("nativeRecovery"), "nativeStateReady": h.get("nativeStateReady"),
            "admissionPaused": (h.get("admission") or {}).get("paused") if isinstance(h.get("admission"), dict) else None,
            "admissionRevision": (h.get("admission") or {}).get("revision") if isinstance(h.get("admission"), dict) else None,
            "pendingCommands": h.get("pendingCommands"), "activeSessions": h.get("activeSessions"),
            "sessions": len(sessions) if isinstance(sessions, list) else None,
            "profileDigests": sorted(str(p.get("digest")) for p in profs if isinstance(p, dict)),
        }
    holder_argv = rec["units"]["relay-holder.service"]["argv"]
    binary = holder_argv[0] if holder_argv and holder_argv[0].startswith("/opt/relay-holder/") else None
    rec["binarySha256"] = sha_file(binary) if binary else None
    rec["binaryPath"] = binary
    rec["profilesSha256"] = sha_file(HOME + "/.config/relay-holder/profiles.json")
    return rec


# ------------------------------------------------------------------------------------------ credential file
def credfile(binding):
    binding = canon_uuid(binding)
    d = HOME + "/.local/state/relay/router-managed/" + binding
    try:
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return {"state": "directory-unavailable"}
    try:
        ds = os.fstat(dfd)
        if ds.st_uid != UID or stat.S_IMODE(ds.st_mode) & 0o077:
            return {"state": "directory-unsafe"}
        try:
            fd = os.open("credential.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except FileNotFoundError:
            return {"state": "absent"}
        except OSError:
            return {"state": "unsafe"}
        try:
            s = os.fstat(fd)  # metadata of THIS inode only; the contents are never read
            return {"state": "present", "regular": stat.S_ISREG(s.st_mode), "uid": s.st_uid,
                    "mode": oct(stat.S_IMODE(s.st_mode)), "links": s.st_nlink, "mtimeMs": int(s.st_mtime * 1000)}
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


# ------------------------------------------------------------------------------------------ native transcript
def transcript(native_home):
    """Settled-tool and config facts from the Pi session record(s) under NATIVE_HOME (a holder native home)."""
    base = os.path.realpath(native_home)
    if not base.startswith(HOME + "/.local/state/") or "/native-homes/" not in base:
        die("bad native home", 2)
    sdir = os.path.join(base, ".pi", "agent", "sessions")
    files = []
    for root, dirs, names in os.walk(sdir, followlinks=False):
        files += [os.path.join(root, n) for n in names]
    if len(files) != 1:
        return {"state": "sessions-" + str(len(files))}
    calls, results, shell, levels, notices, stops, n = {}, {}, [], [], [], [], 0
    with open(files[0], "rb") as f:
        for line in f:
            n += 1
            d = json.loads(line)
            t = d.get("type")
            if t == "thinking_level_change":
                levels.append(str(d.get("thinkingLevel") or d.get("level")))
            elif t == "message":
                m = d.get("message") or {}
                role, content = m.get("role"), m.get("content")
                if role == "assistant":
                    stops.append(str(m.get("stopReason")))
                    for part in content if isinstance(content, list) else []:
                        if part.get("type") == "toolCall":
                            calls[str(part.get("id"))] = str(part.get("name"))
                            args = part.get("arguments") or {}
                            if isinstance(args, dict) and isinstance(args.get("command"), str):
                                shell.append({"id": str(part.get("id")),
                                              "sha256": hashlib.sha256(args["command"].encode()).hexdigest()})
                        elif part.get("type") == "text" and "SESSION_CONFIGURATION_CHANGED" in str(part.get("text")):
                            notices.append(hashlib.sha256(str(part.get("text")).encode()).hexdigest())
                elif role == "toolResult":
                    results.setdefault(str(m.get("toolCallId")), []).append(
                        m.get("isError") if isinstance(m.get("isError"), bool) else None)
    for s in shell:  # each shell call's results: exactly one, with a boolean isError, or the outcome is unknown
        s["results"] = results.get(s["id"], [])
        del s["id"]
    return {"state": "observed", "entries": n, "toolCalls": len(calls),
            "unsettled": sorted(set(calls) - set(results)),
            "duplicateResults": sorted(k for k, v in results.items() if len(v) > 1),
            "toolNames": sorted(set(calls.values())), "shellCommands": shell, "thinkingLevels": levels,
            "configChangedNotices": notices, "stopReasons": stops}


# Acceptance reader for Pi2604/holder64dc. No discovery outside the bound native home,
# no credential/config contents, no writable descriptors and no arbitrary-file fallback.
def native_need(value):
    if not value:
        raise ValueError("native checkpoint unavailable")


def private_dir_identity(path):
    native_need(os.path.isabs(path) and os.path.realpath(path) == path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        native_need(stat.S_ISDIR(s.st_mode) and s.st_uid == UID and stat.S_IMODE(s.st_mode) == 0o700)
        native_need(os.path.realpath('/proc/self/fd/' + str(fd)) == path)
        return {"path": path, "device": str(s.st_dev), "inode": str(s.st_ino), "uid": str(s.st_uid)}
    finally:
        os.close(fd)


def checkpoint_bytes(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        native_need(stat.S_ISREG(s.st_mode) and s.st_uid == UID and s.st_nlink == 1
                    and stat.S_IMODE(s.st_mode) == 0o600 and 0 < s.st_size <= limit
                    and os.path.realpath('/proc/self/fd/' + str(fd)) == path)
        pieces, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            pieces.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        native_need(size == s.st_size and size <= limit and all(getattr(s, k) == getattr(after, k)
                    for k in ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size',
                              'st_mtime_ns', 'st_ctime_ns')))
        return b''.join(pieces)
    finally:
        os.close(fd)


def checkpoint_json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            native_need(k not in result)
            result[k] = v
        return result
    def number(text):
        value = float(text)
        native_need(math.isfinite(value))
        return value
    return json.loads(raw, object_pairs_hook=pairs, parse_float=number,
                      parse_constant=lambda _: native_need(False))


def native_ancestry(raw, native_id, workspace):
    native_need(len(raw) <= 16 * 1024 * 1024)
    lines = raw.decode('utf-8-sig').strip('\ufeff \r\n\t').splitlines()
    native_need(1 < len(lines) <= 100000 and all(len(x.encode()) <= 1024 * 1024 for x in lines))
    header, *entries = [checkpoint_json(x) for x in lines]
    native_need(isinstance(header, dict) and set(header) == {'type', 'version', 'id', 'timestamp', 'cwd'}
                and header.get('type') == 'session' and header.get('version') == 3
                and header.get('id') == native_id and header.get('cwd') == workspace)
    stamp = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
    def timestamp(value):
        native_need(isinstance(value, str) and stamp.fullmatch(value))
        datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')
    timestamp(header.get('timestamp'))
    by_id = {}
    for e in entries:
        native_need(isinstance(e, dict) and re.fullmatch(r'[a-f0-9]{8}', str(e.get('id', '')))
                    and e['id'] not in by_id and 'parentId' in e
                    and (e['parentId'] is None or e['parentId'] in by_id)
                    and e.get('type') in ('message', 'custom', 'model_change', 'thinking_level_change', 'compaction'))
        timestamp(e.get('timestamp'))
        by_id[e['id']] = e
    branch, leaf = [], entries[-1]
    while leaf:
        branch.append(leaf)
        leaf = by_id.get(leaf['parentId'])
    branch.reverse()
    receipt, calls, results, shells = None, {}, {}, []
    for e in branch:
        if e['type'] == 'compaction':
            native_need(not (set(calls) - set(results)))
        if e['type'] == 'custom':
            native_need(e.get('customType') == 'relay-inference-session-v1')
            n = e.get('data')
            native_need(isinstance(n, dict) and {'conversation', 'branch', 'nativeSession'} <= set(n)
                        and set(n) <= {'conversation', 'branch', 'nativeSession', 'parent', 'pending', 'reconciledFrom'}
                        and all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', v) for v in n.values())
                        and n['nativeSession'] == native_id)
            receipt = n
        elif e['type'] == 'message':
            m = e.get('message')
            native_need(isinstance(m, dict) and m.get('role') in ('user', 'assistant', 'toolResult', 'system'))
            content = m.get('content')
            native_need(isinstance(content, (str, list)))
            if m['role'] in ('user', 'assistant'):
                native_need(not (set(calls) - set(results)))
            if m['role'] == 'toolResult':
                key = m.get('toolCallId')
                native_need(isinstance(key, str) and key in calls and key not in results
                            and type(m.get('isError')) is bool and m.get('toolName') == calls[key]['name'])
                results[key] = m['isError']
            if m['role'] == 'assistant':
                native_need(isinstance(content, list))
                for part in content:
                    native_need(isinstance(part, dict) and part.get('type') in ('text', 'thinking', 'toolCall'))
                    if part['type'] == 'toolCall':
                        key, name, args = part.get('id'), part.get('name'), part.get('arguments')
                        native_need(isinstance(key, str) and 0 < len(key) <= 200 and key not in calls
                                    and isinstance(name, str) and isinstance(args, dict))
                        calls[key] = {'name': name, 'args': args}
                        if name == 'bash':
                            native_need(isinstance(args.get('command'), str))
                            shells.append({'id': key, 'sha256': hashlib.sha256(args['command'].encode()).hexdigest()})
    native_need(receipt is not None)
    return {'native': receipt, 'activeLeaf': entries[-1]['id'], 'entries': len(entries),
            'activeEntries': len(branch), 'toolCalls': len(calls), 'unsettled': sorted(set(calls) - set(results)),
            'duplicateResults': [], 'toolNames': sorted({v['name'] for v in calls.values()}),
            'shellCommands': [{'sha256': s['sha256'], 'results': [] if s['id'] not in results
                               else [results[s['id']]]} for s in shells]}


def native_checkpoint(state_dir, session_id, native_id, profile_id, workspace, holder_id, journal_id):
    for value in (session_id, native_id, profile_id, holder_id, journal_id):
        native_need(isinstance(value, str) and str(uuid.UUID(value)) == value)
    state = private_dir_identity(state_dir)
    homes = os.path.join(state_dir, 'native-homes')
    private_dir_identity(homes)
    home = os.path.join(homes, session_id)
    identity = private_dir_identity(home)
    workspace_stat = os.stat(workspace, follow_symlinks=False)
    native_need(stat.S_ISDIR(workspace_stat.st_mode) and os.path.realpath(workspace) == workspace)
    ws = {'path': workspace, 'device': str(workspace_stat.st_dev), 'inode': str(workspace_stat.st_ino),
          'uid': str(workspace_stat.st_uid)}
    journal_raw = checkpoint_bytes(os.path.join(state_dir, 'journal.jsonl'), 64 * 1024 * 1024)
    records = [checkpoint_json(line) for line in journal_raw.splitlines()]
    native_need(records and records[0].get('sequence') == 1 and records[0].get('incarnation') == journal_id)
    for index, r in enumerate(records):
        native_need(r.get('version') == 1 and r.get('sequence') == index + 1 and isinstance(r.get('event'), dict))
    starts = [r for r in records if r['event'].get('kind') == 'holder-started']
    native_need(starts and starts[-1]['incarnation'] == holder_id)
    opened = [r for r in records if r['event'].get('kind') == 'session-opened'
              and r['event'].get('sessionId') == session_id and r['incarnation'] == holder_id]
    native_need(len(opened) == 1)
    opened_value = opened[0]['event']['value']
    profile_digest = opened_value.get('profileDigest')
    native_need(opened_value.get('nativeSessionId') == native_id and opened_value.get('profileId') == profile_id
                and re.fullmatch(r'[a-f0-9]{64}', str(profile_digest)))
    retained = [r for r in records if r['event'].get('kind') == 'native-state-retained'
                and r['event'].get('sessionId') == session_id and r['incarnation'] == holder_id]
    def read_retained(owner, expected_digest):
        parent = os.path.join(homes, owner)
        parent_id = private_dir_identity(parent)
        raw = checkpoint_bytes(os.path.join(parent, 'relay-retained-state.json'), 16384)
        native_need(hashlib.sha256(raw).hexdigest() == expected_digest)
        r = checkpoint_json(raw)
        native_need(r.get('version') == 1 and r.get('admission') == 'blocked'
                    and r.get('containment') == 'owned-process-group-only'
                    and r.get('home') == {k: int(parent_id[k]) for k in ('device', 'inode', 'uid')})
        m = r.get('metadata') or {}
        native_need(m.get('sessionId') == owner and m.get('nativeSessionId') == native_id
                    and m.get('profileDigest') == profile_digest and m.get('workspace') == ws
                    and m.get('outcome') in ('closed', 'interrupted'))
        history = m.get('history') or {}
        path = history.get('path')
        native_need(isinstance(path, str) and os.path.commonpath([parent, path]) == parent and path != parent
                    and os.path.realpath(path) == path and path.endswith('.jsonl'))
        # Only this descriptor-selected file is read, never a directory scan.
        raw_history = checkpoint_bytes(path, 16 * 1024 * 1024)
        native_need(hashlib.sha256(raw_history).hexdigest() == history.get('sha256'))
        native_need(private_dir_identity(parent) == parent_id)
        return raw_history, history['sha256']
    imported = None
    if retained:
        native_need(len(retained) == 1)
        receipt_sha = retained[0]['event']['value'].get('receiptSha256')
        raw, source_sha = read_retained(session_id, receipt_sha)
        source_session = session_id
    else:
        recovery_raw = checkpoint_bytes(os.path.join(home, 'native-recovery.json'), 16384)
        recovery = checkpoint_json(recovery_raw)
        source_session = recovery.get('sourceSessionId')
        native_need(recovery.get('version') == 2 and recovery.get('admission') == 'holder-owned'
                    and recovery.get('ownerSessionId') == session_id and recovery.get('nativeSessionId') == native_id
                    and recovery.get('workspace') == ws and source_session != session_id
                    and isinstance(source_session, str) and str(uuid.UUID(source_session)) == source_session)
        native_need(re.fullmatch(r'[a-f0-9]{64}', str(recovery.get('runtimeManifestSha256', ''))))
        source_events = [r for r in records if r['event'].get('kind') == 'native-state-retained'
                         and r['event'].get('sessionId') == source_session and r['incarnation'] == holder_id]
        native_need(len(source_events) == 1)
        receipt_sha = source_events[0]['event']['value'].get('receiptSha256')
        source_raw, source_sha = read_retained(source_session, receipt_sha)
        native_need(recovery.get('sourceSha256') == source_sha and recovery.get('retainedReceiptSha256') == receipt_sha
                    and recovery.get('nativeHome') == private_dir_identity(os.path.join(homes, source_session)))
        descriptor = checkpoint_json(checkpoint_bytes(os.path.join(home, 'holder-recovery.json'), 16384))
        native_need(descriptor.get('version') == 2 and descriptor.get('admission') == 'holder-owned'
                    and descriptor.get('sourceSessionId') == source_session and descriptor.get('ownerSessionId') == session_id
                    and descriptor.get('nativeSessionId') == native_id and descriptor.get('workspace') == ws
                    and descriptor.get('historySha256') == source_sha
                    and descriptor.get('retainedReceiptSha256') == receipt_sha
                    and descriptor.get('nativeHome') == recovery.get('nativeHome')
                    and descriptor.get('runtimeManifestSha256') == recovery.get('runtimeManifestSha256')
                    and 'acknowledgeUnknownToolEffects' not in descriptor)
        raw = checkpoint_bytes(os.path.join(home, 'resumed-session.jsonl'), 16 * 1024 * 1024)
        # Pi can append after import. Prove the exact imported prefix; do not retag the current hash as imported.
        rolling, prefix_end, offset = hashlib.sha256(), None, 0
        for line in raw.splitlines(keepends=True):
            offset += len(line)
            rolling.update(line)
            if rolling.hexdigest() == recovery.get('importedSha256'):
                native_need(prefix_end is None)
                prefix_end = offset
        native_need(prefix_end is not None)
        original = native_ancestry(source_raw, native_id, workspace)
        initial = native_ancestry(raw[:prefix_end], native_id, workspace)
        native_need(original['native'] == initial['native'])
        for line in raw[prefix_end:].splitlines():
            native_need(checkpoint_json(line).get('type') in ('custom', 'model_change', 'thinking_level_change'))
        imported = recovery['importedSha256']
    projection = native_ancestry(raw, native_id, workspace)
    native_need(private_dir_identity(home) == identity and private_dir_identity(state_dir) == state)
    # Re-read the same bounded journal to refuse a concurrent mutation, not invent an idle snapshot.
    native_need(checkpoint_bytes(os.path.join(state_dir, 'journal.jsonl'), 64 * 1024 * 1024) == journal_raw)
    return {'version': 1, 'state': 'observed', 'sessionId': session_id, 'sourceSessionId': source_session,
            'nativeSessionId': native_id, 'profileId': profile_id, 'profileDigest': profile_digest,
            'holderId': holder_id, 'journalId': journal_id, 'generation': opened_value.get('generation'),
            'observedMs': now_ms(), 'nativeHome': identity, 'retainedReceiptSha256': receipt_sha,
            'sourceSha256': source_sha, 'importedSha256': imported,
            'currentSha256': hashlib.sha256(raw).hexdigest(), 'journalSha256': hashlib.sha256(journal_raw).hexdigest(),
            **projection}


# ------------------------------------------------------------------------------------------ pre-run facts
def last_event(path):
    last = None
    with open(path, "rb") as f:
        for line in f:
            last = json.loads(line)
    e = (last or {}).get("event") or {}
    return {"kind": e.get("kind"), "sealId": e.get("sealId"), "sealed": e.get("sealed") is True,
            "expectedIncarnation": e.get("expectedIncarnation")}


def prerun(state_dir, preparation):
    """Everything the pre-run must verify BEFORE the holder starts: the receipt and its preserved old journal, the
    sealed old root, the prepared new root, the installed artifacts, and the units' effective definitions."""
    preparation = canon_uuid(preparation)
    receipt = HOME + "/.config/relay-holder/used-handoffs/" + preparation + "/complete.json"
    r = json.load(open(receipt))
    pj = ((r.get("preserved") or {}).get("journal")) or {}
    old_state = r.get("oldState")
    old_journal = os.path.join(old_state, "journal.jsonl") if isinstance(old_state, str) else None
    unit_dir = HOME + "/.config/systemd/user/"
    return {"receiptSha256": sha_file(receipt),
            "receipt": {"operationId": r.get("operationId"), "newState": r.get("newState"),
                        "oldState": old_state, "profilesFile": r.get("profilesFile"),
                        "state": r.get("state") if isinstance(r.get("state"), str) else None,
                        "preservedJournal": {k: pj.get(k) for k in ("incarnation", "journalId", "sealId", "sha256",
                                                                     "records", "unknownCommands")}},
            "oldJournalSha256": sha_file(old_journal) if old_journal else None,
            "oldJournalLast": last_event(old_journal) if old_journal else None,
            "journal": journal(state_dir), "units": {u: systemd(u) for u in UNITS},
            "binarySha256": (sha_file(systemd("relay-holder.service")["argv"][0])
                             if systemd("relay-holder.service")["argv"] else None),
            "artifacts": {"relay-holder.service": sha_file(unit_dir + "relay-holder.service"),
                          "relay-holder-control-plane.service": sha_file(unit_dir + "relay-holder-control-plane.service"),
                          "service.json": sha_file(HOME + "/.config/relay-holder/service.json"),
                          "profiles.json": sha_file(HOME + "/.config/relay-holder/profiles.json")},
            "stateDirMode": oct(stat.S_IMODE(os.lstat(state_dir).st_mode))}


# ------------------------------------------------------------------------------------------ handoff mutations
# The ONLY writes on dev-worker-1. The host calls them solely from the ledgered handoff path (relay_phase_a_handoff.py),
# each after its durable intent record; each re-checks its own precondition here and returns a readback.
def run_bounded(argv, seconds):
    p = subprocess.run(["timeout", "--kill-after=2", str(seconds)] + argv, capture_output=True, timeout=seconds + 5)
    return p.returncode


STOPPED = ("inactive", "failed")
STOP_RESERVE_S = 40  # always left for a verified stop (systemctl stop 30 s + readback)


def stop_and_verify():
    """Stop the holder and positively confirm a terminal stopped state."""
    rc = run_bounded(["systemctl", "--user", "stop", "relay-holder.service"], 30)
    after = systemd("relay-holder.service")
    return {"stopRc": rc, "unitAfterStop": after["active"], "contained": rc == 0 and after["active"] in STOPPED}


def mutate_start_holder(state_dir, deadline_s):
    """Start ONLY the holder, then prove it came up paused; anything else (an unpaused start, an exception, an
    unreadable status, running out of time) is contained HERE, with STOP_RESERVE_S always kept for the stop."""
    u = {x: systemd(x) for x in UNITS}
    j = journal(state_dir)
    if u["relay-holder.service"]["active"] != "inactive" or u["relay-holder-control-plane.service"]["active"] != "inactive":
        return {"started": False, "why": "units-not-inactive", "units": u}
    if not (j["admission"] and j["admission"]["paused"] is True):
        return {"started": False, "why": "journal-admission-not-paused", "journal": j}
    end = time.monotonic() + deadline_s - STOP_RESERVE_S
    if end - time.monotonic() < 45:
        return {"started": False, "why": "budget-too-small-for-a-contained-start"}
    rc, rec, paused = None, None, False
    try:
        rc = run_bounded(["systemctl", "--user", "start", "relay-holder.service"],
                         max(1, int(min(20, end - time.monotonic()))))
        hello(state_dir, wait_s=max(0.0, min(15.0, end - time.monotonic() - 25)))
        rec = holder(state_dir)
        paused = bool(rec.get("status")) and rec["status"].get("admissionPaused") is True
    except BaseException:  # noqa: BLE001 - an exception after the start is contained like an unpaused start
        paused = False
    if not paused:  # contain HERE, on this host: not dependent on the response reaching the operator
        return {"started": rc == 0, "rc": rc, "paused": False, **stop_and_verify(), "holder": rec}
    return {"started": rc == 0, "rc": rc, "paused": True, "contained": False, "holder": rec}


def mutate_stop_holder():
    s = stop_and_verify()
    return {"stopped": s["contained"], **s, "units": {x: systemd(x) for x in UNITS}}


def mutate_resume(state_dir, revision, operation):
    operation = canon_uuid(operation)
    s = maintenance_status(state_dir)
    if not s or s["admissionPaused"] is not True or s["admissionRevision"] != revision or s["activeSessions"] != 0 \
            or s["pendingCommands"] != 0 or s["sealed"]:
        return {"resumed": False, "why": "precondition", "status": s}
    rc = run_bounded(["docker", "run", "--rm", "--network", "none", "--user", "1000:1000", "-v",
                      state_dir + ":" + state_dir, MAINTENANCE_IMAGE, "node", "dist/scripts/holder-maintenance.js",
                      state_dir, "resume", str(revision), operation], 25)
    return {"resumed": rc == 0, "rc": rc, "status": maintenance_status(state_dir), "journal": journal(state_dir)}


def mutate_enable_start():
    rc1 = run_bounded(["systemctl", "--user", "enable"] + list(UNITS), 10)
    rc2 = run_bounded(["systemctl", "--user", "start", "relay-holder-control-plane.service"], 15) if rc1 == 0 else None
    time.sleep(3)
    return {"enabled": rc1 == 0, "started": rc2 == 0, "rc": [rc1, rc2], "units": {x: systemd(x) for x in UNITS}}


def main():
    a = sys.argv[1:]
    if not a:
        die("usage", 2)
    if os.getuid() != UID:
        die("run as c4", 2)
    m = a[0]
    if m == "mutate-start-holder" and len(a) == 3 and a[2].isdigit():
        out(mutate_start_holder(a[1], int(a[2])))
        return
    if m == "mutate-stop-holder" and len(a) == 1:
        out(mutate_stop_holder())
        return
    if m == "mutate-resume" and len(a) == 4 and a[2].isdigit():
        out(mutate_resume(a[1], int(a[2]), a[3]))
        return
    if m == "mutate-enable-start" and len(a) == 1:
        out(mutate_enable_start())
        return
    if m == "clock" and len(a) == 1:
        out({"now": now_ms()})
    elif m == "marker" and len(a) == 5:
        if not a[4].isdigit():
            die("bad notBefore", 2)
        out(marker(a[1], a[2], a[3], int(a[4])))
    elif m == "holder" and len(a) == 2:
        out(holder(a[1]))
    elif m == "credfile" and len(a) == 2:
        out(credfile(a[1]))
    elif m == "transcript" and len(a) == 2:
        out(transcript(a[1]))
    elif m == "native-checkpoint" and len(a) == 8:
        try:
            native_need(a[1].startswith(HOME + '/.local/state/'))
            out(native_checkpoint(*a[1:]))
        except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError):
            die('native checkpoint unavailable', 3)
    elif m == "prerun" and len(a) == 3:
        out(prerun(a[1], a[2]))
    else:
        die("usage", 2)


if __name__ == "__main__":
    main()
