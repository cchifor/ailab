#!/usr/bin/env python3
"""Phase 0b spike (GitHub): measures the forge behaviours the stateless-reviewers plan relies on.

Runs against a dedicated test repository (default cchifor/reviewer), authenticated with the gh CLI's
token (read in memory, never printed). Seeds an empty repository with `main` + a CI workflow, opens a
test PR, measures, and deletes every tag/ref it created. Plan: plans/2026-10-07-stateless-reviewers-plan.md.
"""
import collections, concurrent.futures as cf, json, os, random, shutil, subprocess, sys, tempfile, time, uuid
import urllib.error, urllib.request

REPO = sys.argv[1] if len(sys.argv) > 1 else "cchifor/reviewer"
SSH = f"git@github.com:{REPO}.git"
API = "https://api.github.com"
TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
RUN = f"rbs{int(time.time())}"
OUT = collections.OrderedDict()
CREATED_REFS = set()


def call(method, path, body=None, headers=None, raw=False, timeout=60):
    # A connect timeout is retried: harmless for creates, because ownership is decided by readback.
    for attempt in range(4):
        try:
            return _call(method, path, body, headers, raw, timeout)
        except urllib.error.URLError as e:
            if isinstance(e, urllib.error.HTTPError) or attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def _call(method, path, body=None, headers=None, raw=False, timeout=60):
    h = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "reviewbot-spike"}
    h.update(headers or {})
    req = urllib.request.Request(API + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
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


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


work = tempfile.mkdtemp(prefix="rbspike-gh-")
try:
    # --- seed the repository (main + workflow) and a PR branch
    r = git("ls-remote", SSH)
    seeded = "refs/heads/main" in r.stdout
    if not seeded:
        os.makedirs(f"{work}/seed/.github/workflows")
        open(f"{work}/seed/README.md", "w").write("# reviewer\n\nTest repository for reviewbot coordination spikes.\n")
        open(f"{work}/seed/.github/workflows/ci.yml", "w").write(
            "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  check:\n"
            "    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n")
        open(f"{work}/seed/app.py", "w").write("".join(f"line {i}\n" for i in range(1, 21)))
        for a in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=spike", "-c", "user.email=spike@example.invalid",
                  "commit", "-q", "-m", "seed for reviewbot spikes"], ["push", "-q", SSH, "main"]):
            res = git(*a, cwd=f"{work}/seed")
            if res.returncode:
                sys.exit(f"seed failed: {a}: {res.stderr[-300:]}")
    OUT["seeded_now"] = not seeded
    main_sha = call("GET", f"/repos/{REPO}/branches/main")[1]["commit"]["sha"]
    git("clone", "-q", SSH, f"{work}/clone")
    c = f"{work}/clone"
    branch = f"spike-{RUN}"
    git("checkout", "-q", "-b", branch, cwd=c)
    lines = open(f"{c}/app.py").read().splitlines()
    lines[4] = "line 5 changed"
    lines.append("line 21 added")
    open(f"{c}/app.py", "w").write("\n".join(lines) + "\n")
    git("-c", "user.name=spike", "-c", "user.email=spike@example.invalid", "commit", "-qam", "spike change", cwd=c)
    git("push", "-q", "origin", branch, cwd=c)
    head = git("rev-parse", "HEAD", cwd=c).stdout.strip()
    s, pr, _ = call("POST", f"/repos/{REPO}/pulls", {"title": f"spike {RUN} (temporary)", "head": branch,
                                                      "base": "main", "body": "reviewbot coordination spike"})
    n = pr["number"]
    OUT["pr"] = f"#{n} created ({s}); mergeable on first read: {pr.get('mergeable')!r}"

    # --- B. race: tag object + ref, ownership by readback
    def make_obj(name, msg, date):
        s, b, _ = call("POST", f"/repos/{REPO}/git/tags", {"tag": name, "message": msg, "object": main_sha,
                       "type": "commit", "tagger": {"name": "reviewbot", "email": "reviewbot@example.invalid", "date": date}})
        return s, b.get("sha")

    rounds = []
    for rnd in range(5):
        name = f"{RUN}.r{rnd}.pub1"
        CREATED_REFS.add(f"tags/{name}")
        nonces = [uuid.uuid4().hex for _ in range(8)]
        date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        def one(i):
            os_, sha = make_obj(name, json.dumps({"nonce": nonces[i]}), date)
            time.sleep(random.uniform(0, 0.08))
            rs, rb, _ = call("POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/tags/{name}", "sha": sha})
            return i, sha, rs, (rb.get("message") if isinstance(rb, dict) else "")

        with cf.ThreadPoolExecutor(8) as ex:
            res = list(ex.map(one, range(8)))
        s, ref, _ = call("GET", f"/repos/{REPO}/git/ref/tags/{name}")
        target = ref["object"]["sha"]
        s, obj, _ = call("GET", f"/repos/{REPO}/git/tags/{target}")
        owner_nonce = json.loads(obj["message"].strip())["nonce"]
        winners = [i for i, sha, rs, _ in res if rs == 201]
        consistent = len(winners) == 1 and res[winners[0]][1] == target and nonces[winners[0]] == owner_nonce
        rounds.append({"statuses": dict(collections.Counter(rs for _, _, rs, _ in res)),
                       "201_is_owner": consistent,
                       "loser_message": next((m for _, _, rs, m in res if rs != 201), "")})
        time.sleep(4)
    OUT["B_race"] = rounds

    # --- C. frozen payload -> same object SHA
    d = "2026-10-07T12:00:00Z"
    _, a = make_obj(f"{RUN}.frozen", '{"nonce":"fixed"}', d)
    _, b = make_obj(f"{RUN}.frozen", '{"nonce":"fixed"}', d)
    _, c2 = make_obj(f"{RUN}.frozen", '{"nonce":"fixed"}', "2026-10-07T12:00:01Z")
    OUT["C_frozen_payload"] = f"same payload -> same sha: {a == b}; different tagger.date -> different sha: {a != c2}"
    s, t, _ = call("GET", f"/repos/{REPO}/git/tags/{a}")
    OUT["C_tagger_date_kept"] = f"tagger.date read back: {t.get('tagger', {}).get('date')} (client-supplied)"

    # --- D. lightweight ref (outcome tag) and exact read of a missing ref
    s, _, _ = call("POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/tags/{RUN}.r0.pubfin1", "sha": main_sha})
    CREATED_REFS.add(f"tags/{RUN}.r0.pubfin1")
    s2, _, _ = call("GET", f"/repos/{REPO}/git/ref/tags/{RUN}.nope")
    s3, b3, _ = call("POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/tags/{RUN}.r0.pubfin1", "sha": main_sha})
    OUT["D_lightweight_ref"] = f"create {s}; exact read of a missing ref {s2}; duplicate create {s3} {b3.get('message')}"

    # --- E. matching-refs with 120 refs, created through the paced API (a 300-tag git push put the
    # repository into a 20-minute write outage on the previous run)
    for i in range(120):
        call("POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/tags/{RUN}.E.p1.h{i:03d}", "sha": main_sha})
        CREATED_REFS.add(f"tags/{RUN}.E.p1.h{i:03d}")
        time.sleep(0.9)
    s, lst, h = call("GET", f"/repos/{REPO}/git/matching-refs/tags/{RUN}.E.")
    s2, lst2, _ = call("GET", f"/repos/{REPO}/git/matching-refs/tags/{RUN}.E.p1.h00")
    OUT["E_matching_refs"] = (f"120 refs -> {len(lst) if isinstance(lst, list) else lst} returned (status {s}, "
                              f"Link header: {'Link' in h or 'link' in h}); narrower prefix -> {len(lst2) if isinstance(lst2, list) else lst2}")

    # --- F. reviews: valid line/side, invalid line, pending reviews
    s, rv, _ = call("POST", f"/repos/{REPO}/pulls/{n}/reviews", {"commit_id": head, "event": "COMMENT",
                    "body": f"spike valid\n\n<!-- review-bot:v1 persona=spike head={head} verdict=clean -->",
                    "comments": [{"path": "app.py", "line": 5, "side": "RIGHT", "body": "ok"}]})
    valid = f"{s} state={rv.get('state')}"
    s, inv, _ = call("POST", f"/repos/{REPO}/pulls/{n}/reviews", {"commit_id": head, "event": "COMMENT",
                     "body": "spike invalid", "comments": [{"path": "app.py", "line": 999, "side": "RIGHT", "body": "bad"}]})
    invalid = f"{s} {str(inv.get('message'))[:80]} {str(inv.get('errors'))[:120]}"
    s, revs, _ = call("GET", f"/repos/{REPO}/pulls/{n}/reviews")
    after_invalid = [r["state"] for r in revs]
    s, p1, _ = call("POST", f"/repos/{REPO}/pulls/{n}/reviews", {"commit_id": head, "body": "pending 1"})
    s2, p2, _ = call("POST", f"/repos/{REPO}/pulls/{n}/reviews", {"commit_id": head, "body": "pending 2"})
    s3, p3, _ = call("POST", f"/repos/{REPO}/pulls/{n}/reviews", {"commit_id": head, "event": "COMMENT", "body": "submitted while a pending exists"})
    s, revs2, _ = call("GET", f"/repos/{REPO}/pulls/{n}/reviews")
    if p1.get("id"):
        call("DELETE", f"/repos/{REPO}/pulls/{n}/reviews/{p1['id']}")
    s, apr, _ = call("POST", f"/repos/{REPO}/pulls/{n}/reviews", {"commit_id": head, "event": "APPROVE", "body": "self approve"})
    OUT["F_reviews"] = {"valid_comment": valid, "invalid_line": invalid, "states_after_invalid": after_invalid,
                        "pending_1": f"{s} state={p1.get('state')}", "pending_2": f"{s2} {str(p2.get('message') or p2.get('errors'))[:120]}",
                        "submit_while_pending": f"{s3} state={p3.get('state') if isinstance(p3, dict) else p3}",
                        "states_after": [r["state"] for r in revs2],
                        "self_approve": f"{s} {str(apr.get('message') or apr.get('errors'))[:100]}"}

    # --- G. CI: which commit carries the PR's check runs; combined status
    deadline = time.time() + 300
    while time.time() < deadline:
        s, cr, _ = call("GET", f"/repos/{REPO}/commits/{head}/check-runs?per_page=100")
        if cr.get("total_count") and all(x["status"] == "completed" for x in cr["check_runs"]):
            break
        time.sleep(10)
    s, pr2, _ = call("GET", f"/repos/{REPO}/pulls/{n}")
    merge_sha = pr2.get("merge_commit_sha")
    s, crm, _ = call("GET", f"/repos/{REPO}/commits/{merge_sha}/check-runs") if merge_sha else (0, {}, {})
    s, st, _ = call("GET", f"/repos/{REPO}/commits/{head}/status")
    OUT["G_ci"] = {"head_check_runs": [(x["name"], x["status"], x["conclusion"]) for x in cr.get("check_runs", [])],
                   "test_merge_commit_check_runs": crm.get("total_count"),
                   "combined_status_on_head": f"state={st.get('state')} total_count={st.get('total_count')}",
                   "mergeable_after_wait": pr2.get("mergeable"), "mergeable_state": pr2.get("mergeable_state")}

    # --- H. ETag 304 and rate-limit headers
    s, _, h1 = call("GET", f"/repos/{REPO}/pulls?state=open&per_page=100")
    rem1 = h1.get("X-RateLimit-Remaining") or h1.get("x-ratelimit-remaining")
    etag = h1.get("ETag") or h1.get("etag")
    s2, _, h2 = call("GET", f"/repos/{REPO}/pulls?state=open&per_page=100", headers={"If-None-Match": etag})
    rem2 = h2.get("X-RateLimit-Remaining") or h2.get("x-ratelimit-remaining")
    OUT["H_etag"] = f"first {s} remaining={rem1}; conditional {s2} remaining={rem2} (304 free: {s2 == 304 and rem1 == rem2})"
    OUT["H_rate_headers"] = sorted(k for k in h1 if k.lower().startswith("x-ratelimit"))

    # --- J. git fetch refs/pull/<n>/head over HTTPS with the token, local diff
    bare = f"{work}/bare.git"
    git("init", "-q", "--bare", bare)
    helper = "!f() { echo username=x-access-token; echo password=$(gh auth token); }; f"
    r = git("-c", "credential.helper=", "-c", f"credential.helper={helper}", "fetch", "-q", "--filter=blob:none",
            f"https://github.com/{REPO}.git", f"+refs/pull/{n}/head:refs/pr/{n}", "+refs/heads/main:refs/base/main", cwd=bare)
    got = git("rev-parse", f"refs/pr/{n}", cwd=bare).stdout.strip()
    mb = git("merge-base", "refs/base/main", got, cwd=bare).stdout.strip()
    stat = git("-c", "credential.helper=", "-c", f"credential.helper={helper}", "diff", "--stat", mb, got, cwd=bare).stdout.strip().splitlines()
    OUT["J_git_fetch"] = f"fetch rc={r.returncode}; head matches: {got == head}; diff: {stat[-1] if stat else r.stderr[-150:]}"

    # --- K. merge with a wrong sha, then the right one
    s, b, _ = call("PUT", f"/repos/{REPO}/pulls/{n}/merge", {"sha": main_sha, "merge_method": "merge"})
    wrong = f"{s} {b.get('message')}"
    s, b, _ = call("PUT", f"/repos/{REPO}/pulls/{n}/merge", {"sha": head, "merge_method": "merge"})
    OUT["K_merge"] = {"wrong_sha": wrong, "right_sha": f"{s} merged={b.get('merged')} {b.get('message')}"}
    s, pr3, _ = call("GET", f"/repos/{REPO}/pulls/{n}")
    OUT["K_after_merge"] = f"merged={pr3.get('merged')} base.ref={pr3.get('base', {}).get('ref')}"
finally:
    # cleanup: race/lightweight refs via API; the 300 E tags via one git push; the branch
    for ref in sorted(CREATED_REFS):
        call("DELETE", f"/repos/{REPO}/git/refs/{ref}")
        time.sleep(0.9)
    call("DELETE", f"/repos/{REPO}/git/refs/heads/spike-{RUN}")
    s, left, _ = call("GET", f"/repos/{REPO}/git/matching-refs/tags/{RUN}")
    OUT["cleanup_tags_left"] = len(left) if isinstance(left, list) else left
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(OUT, indent=1, default=str))
