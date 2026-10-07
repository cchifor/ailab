"""Phase 0 spike (Gitea) on cchifor/primes-lab. Flat names under a unique prefix; everything created
is deleted at the end (tags via git push, branch/PR via API). Never prints credentials."""
import base64, collections, concurrent.futures as cf, json, random, subprocess, sys, tempfile, time, uuid
import urllib.error, urllib.parse, urllib.request
from email.utils import parsedate_to_datetime

cred = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=git.chifor.me\n\n",
                      capture_output=True, text=True).stdout
kv = dict(l.split("=", 1) for l in cred.splitlines() if "=" in l)
AUTH = "Basic " + base64.b64encode(f"{kv['username']}:{kv['password']}".encode()).decode()
WEB, BASE = "https://git.chifor.me", "https://git.chifor.me/api/v1"
REPO = sys.argv[1] if len(sys.argv) > 1 else "cchifor/primes-lab"
GITDIR = tempfile.mkdtemp(prefix="rbspike-gitea-")
subprocess.run(["git", "init", "-q", GITDIR], capture_output=True)
RUN = f"rbs3{int(time.time())}"
ALL_TAGS = set()
OUT = {}


def call(method, url, body=None, timeout=60, raw=False):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json",
                                          "User-Agent": "git/2.47.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, (data if raw else (json.loads(data) if data else {})), dict(r.headers)
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data), dict(e.headers)
        except ValueError:
            return e.code, {"raw": data[:200].decode("utf-8", "replace")}, dict(e.headers)


def api(method, path, body=None, timeout=60, raw=False):
    return call(method, BASE + path, body, timeout, raw)


def create_tag(name, msg, target, timeout=60):
    ALL_TAGS.add(name)
    return api("POST", f"/repos/{REPO}/tags", {"tag_name": name, "target": target, "message": msg}, timeout)


def read_owner(name):
    s, b, _ = api("GET", f"/repos/{REPO}/git/refs/tags/{name}")
    if s != 200:
        return None
    refs = b if isinstance(b, list) else [b]
    exact = [r for r in refs if r.get("ref") == f"refs/tags/{name}"]
    if not exact:
        return None
    s2, t, _ = api("GET", f"/repos/{REPO}/git/tags/{exact[0]['object']['sha']}")
    return t if s2 == 200 else None


if api("GET", f"/repos/{REPO}/branches/main")[0] != 200:      # seed an empty repository
    readme = b"# reviewer\n\nTest repository for reviewbot coordination spikes.\n"
    api("POST", f"/repos/{REPO}/contents/README.md", {"content": base64.b64encode(readme).decode(),
                                                      "message": "seed for reviewbot spikes", "branch": "main"})
    OUT["seeded_now"] = True
sha = api("GET", f"/repos/{REPO}/branches/main")[1]["commit"]["id"]
pr_number = branch = None
try:
    # A. readback yields exactly one owner per name, staggered races
    rounds, ok = 15, 0
    for rnd in range(rounds):
        name = f"{RUN}.A{rnd}.a1"
        nonces = [uuid.uuid4().hex for _ in range(20)]

        def one(i):
            time.sleep(random.uniform(0, 0.08))
            return create_tag(name, json.dumps({"nonce": nonces[i]}), sha)[0]

        with cf.ThreadPoolExecutor(20) as ex:
            codes = collections.Counter(ex.map(one, range(20)))
        t = read_owner(name)
        owner = json.loads(t["message"].strip())["nonce"] if t else None
        ok += owner in nonces
    OUT["A_readback_unique_owner"] = f"{ok}/{rounds} rounds: exactly one git object whose nonce belongs to one caller"

    # B. message fidelity
    payload = {"nonce": "n-" + uuid.uuid4().hex, "q": 'quote " back \\ slash', "u": "ünïcødé ✓",
               "nl": "line1\nline2", "tab": "a\tb"}
    msg = json.dumps(payload, ensure_ascii=False) + "\n"
    create_tag(f"{RUN}.B.a1", msg, sha)
    t = read_owner(f"{RUN}.B.a1")
    got = t["message"] if t else ""
    try:
        same = json.loads(got.strip()) == payload
    except ValueError:
        same = False
    OUT["B_message_roundtrip"] = f"semantic equal: {same}; raw equal: {got == msg}; trailing newline kept: {got.endswith(chr(10))}"

    # C. tagger.date vs server Date header
    s, _, h = create_tag(f"{RUN}.C.a1", json.dumps({"nonce": "c"}), sha)
    t = read_owner(f"{RUN}.C.a1")
    tagger = t.get("tagger", {}) if t else {}
    try:
        from datetime import datetime
        td = datetime.fromisoformat(tagger.get("date", "").replace("Z", "+00:00"))
        dd = parsedate_to_datetime(h.get("Date"))
        OUT["C_tagger_date"] = f"tagger={tagger.get('name')} <{tagger.get('email')}> date={tagger.get('date')}; response Date={h.get('Date')}; delta={(dd - td).total_seconds():.1f}s"
    except Exception as e:
        OUT["C_tagger_date"] = f"parse failed: {e} tagger={tagger}"

    # D. late landing after a client-side timeout
    landed = []
    for i in range(5):
        name = f"{RUN}.D{i}.a1"
        try:
            create_tag(name, json.dumps({"nonce": f"d{i}"}), sha, timeout=0.05)
            res = "client got a response"
        except Exception as e:
            res = f"client {type(e).__name__}"
        t0 = time.time(); seen = None
        while time.time() - t0 < 20:
            if read_owner(name):
                seen = time.time() - t0
                break
            time.sleep(0.5)
        landed.append(f"{res}; tag {'appeared after %.1fs' % seen if seen is not None else 'never appeared (20s)'}")
    OUT["D_late_landing"] = landed

    # E. prefix listing completeness at 300
    for i in range(300):
        create_tag(f"{RUN}.E.p1.h{i:03d}.a1", "", sha)
    s, b, _ = api("GET", f"/repos/{REPO}/git/refs/tags/{RUN}.E.")
    s2, b2, _ = api("GET", f"/repos/{REPO}/git/refs/tags/{RUN}.E.p1.h00")
    OUT["E_prefix_listing"] = (f"300 created -> listing returned {len(b) if isinstance(b, list) else b} "
                               f"(status {s}); narrower prefix h00 -> {len(b2) if isinstance(b2, list) else b2} (expect 10)")

    # F. delete + create race on one name
    name = f"{RUN}.F.a1"
    results = []
    for rnd in range(8):
        create_tag(name, json.dumps({"nonce": f"f-old{rnd}"}), sha)
        new = f"f-new{rnd}"

        def deleter():
            return subprocess.run(["git", "-C", GITDIR, "push", "-q",
                                   f"{WEB}/{REPO}.git", f":refs/tags/{name}"],
                                  capture_output=True, text=True).returncode

        def creator():
            time.sleep(random.uniform(0, 0.3))
            return create_tag(name, json.dumps({"nonce": new}), sha)[0]

        with cf.ThreadPoolExecutor(2) as ex:
            fd, fc = ex.submit(deleter), ex.submit(creator)
            d_rc, c_st = fd.result(), fc.result()
        t = read_owner(name)
        end = json.loads(t["message"].strip())["nonce"] if t else None
        results.append(f"delete rc={d_rc} create={c_st} -> end={end}")
        if t:
            subprocess.run(["git", "-C", GITDIR, "push", "-q", f"{WEB}/{REPO}.git",
                            f":refs/tags/{name}"], capture_output=True)
    OUT["F_delete_create_race"] = results

    # G + H need a branch with a commit and a PR
    branch = f"{RUN}-spike"
    api("POST", f"/repos/{REPO}/branches", {"new_branch_name": branch, "old_ref_name": sha})
    content = base64.b64encode(b"spike file for reviewbot coordination test\nline 2\nline 3\n").decode()
    s, b, _ = api("POST", f"/repos/{REPO}/contents/rbspike/{RUN}.txt",
                  {"content": content, "message": "reviewbot coordination spike (temporary)", "branch": branch})
    head = b.get("commit", {}).get("sha")
    # G. immutable compare diff
    s, b, _ = api("GET", f"/repos/{REPO}/compare/{sha}...{head}")
    files = [f.get("filename") for f in (b.get("files") or [])] if isinstance(b, dict) else b
    s2, d2, h2 = call("GET", f"{WEB}/{REPO}/compare/{sha}...{head}.diff", raw=True)
    s3, d3, _ = api("GET", f"/repos/{REPO}/git/commits/{head}.diff", raw=True)
    OUT["G_compare"] = (f"API compare JSON status {s}, files={files}; web compare .diff status {s2} "
                        f"({len(d2) if isinstance(d2, bytes) else d2} bytes, starts {d2[:40] if isinstance(d2, bytes) else ''}); "
                        f"API commit .diff status {s3}")
    # H. pending review behaviour: PR, then a review POST with an invalid inline position
    s, b, _ = api("POST", f"/repos/{REPO}/pulls", {"title": f"WIP: reviewbot coordination spike {RUN} - do not merge",
                                                    "head": branch, "base": "main",
                                                    "body": "Temporary PR for a reviewbot coordination spike; closed automatically."})
    pr_number = b.get("number")
    s, b, _ = api("POST", f"/repos/{REPO}/pulls/{pr_number}/reviews",
                  {"commit_id": head, "event": "COMMENT", "body": "spike: invalid inline position",
                   "comments": [{"path": f"rbspike/{RUN}.txt", "body": "bad", "new_position": 999}]})
    invalid = f"{s} {(b.get('message') if isinstance(b, dict) else '')}"
    s, revs, _ = api("GET", f"/repos/{REPO}/pulls/{pr_number}/reviews")
    states = [(r.get("state"), r.get("comments_count"), (r.get("body") or "")[:30]) for r in revs] if isinstance(revs, list) else revs
    s, b, _ = api("POST", f"/repos/{REPO}/pulls/{pr_number}/reviews",
                  {"commit_id": head, "event": "COMMENT", "body": "spike: valid review with marker\n\n<!-- review-bot:v1 persona=spike head=" + head + " verdict=clean -->",
                   "comments": [{"path": f"rbspike/{RUN}.txt", "body": "ok", "new_position": 2}]})
    valid = f"{s} id={b.get('id') if isinstance(b, dict) else ''} state={b.get('state') if isinstance(b, dict) else ''} comments={b.get('comments_count') if isinstance(b, dict) else ''}"
    s, revs2, _ = api("GET", f"/repos/{REPO}/pulls/{pr_number}/reviews")
    states2 = [(r.get("state"), r.get("comments_count"), r.get("commit_id", "")[:9]) for r in revs2] if isinstance(revs2, list) else revs2
    OUT["H_pending_review"] = {"invalid_post": invalid, "reviews_after_invalid": states,
                               "valid_post": valid, "reviews_after_valid": states2}
    # I. commit statuses: zero statuses, then success
    s, st0, _ = api("GET", f"/repos/{REPO}/commits/{head}/status")
    api("POST", f"/repos/{REPO}/statuses/{head}", {"state": "success", "context": "spike/ci", "description": "spike"})
    s, st1, _ = api("GET", f"/repos/{REPO}/commits/{head}/status")
    OUT["I_combined_status"] = (f"no statuses -> state={st0.get('state')!r} total_count={st0.get('total_count')}; "
                                f"after one success -> state={st1.get('state')!r} total_count={st1.get('total_count')}")
    # K. merge with a wrong head_commit_id, then the right one
    s, b, _ = api("POST", f"/repos/{REPO}/pulls/{pr_number}/merge", {"Do": "merge", "head_commit_id": sha})
    wrong = f"{s} {(b.get('message') if isinstance(b, dict) else '')}"[:160]
    s, b, _ = api("POST", f"/repos/{REPO}/pulls/{pr_number}/merge", {"Do": "merge", "head_commit_id": head})
    right = f"{s} {(b.get('message') if isinstance(b, dict) else '')}"[:160]
    s, prx, _ = api("GET", f"/repos/{REPO}/pulls/{pr_number}")
    OUT["K_merge"] = {"wrong_head_commit_id": wrong, "right_head_commit_id": right,
                      "after": f"merged={prx.get('merged')} base={prx.get('base', {}).get('ref')}"}
    # J. local-git immutable diff with the same account
    bare = tempfile.mkdtemp(prefix="rbspike-gitea-bare-")
    subprocess.run(["git", "init", "-q", "--bare", bare], capture_output=True)
    r = subprocess.run(["git", "-C", bare, "fetch", "-q", "--filter=blob:none", f"{WEB}/{REPO}.git",
                        f"+refs/pull/{pr_number}/head:refs/pr/{pr_number}", "+refs/heads/main:refs/base/main"],
                       capture_output=True, text=True)
    got = subprocess.run(["git", "-C", bare, "rev-parse", f"refs/pr/{pr_number}"], capture_output=True, text=True).stdout.strip()
    mb = subprocess.run(["git", "-C", bare, "merge-base", "refs/base/main", got], capture_output=True, text=True).stdout.strip()
    stat = subprocess.run(["git", "-C", bare, "diff", "--stat", mb, got], capture_output=True, text=True).stdout.strip().splitlines()
    OUT["J_local_git_diff"] = f"fetch rc={r.returncode}; head matches: {got == head}; {stat[-1] if stat else r.stderr[-120:]}"
finally:
    # cleanup: PR, branch, tags
    if pr_number and not api("GET", f"/repos/{REPO}/pulls/{pr_number}")[1].get("merged"):
        api("PATCH", f"/repos/{REPO}/pulls/{pr_number}", {"state": "closed"})
    if branch:
        OUT["cleanup_branch"] = api("DELETE", f"/repos/{REPO}/branches/{urllib.parse.quote(branch, safe='')}")[0]
    names = sorted(ALL_TAGS)
    for i in range(0, len(names), 100):
        subprocess.run(["git", "-C", GITDIR, "push", "-q", f"{WEB}/{REPO}.git"]
                       + [f":refs/tags/{n}" for n in names[i:i + 100]], capture_output=True)
    s, b, _ = api("GET", f"/repos/{REPO}/git/refs/tags/{RUN}")
    OUT["cleanup_tags_left"] = len(b) if (s == 200 and isinstance(b, list)) else 0
    OUT["cleanup_pr"] = f"PR #{pr_number} merged or closed" if pr_number else "no PR"
    print(json.dumps(OUT, indent=1, ensure_ascii=False))
