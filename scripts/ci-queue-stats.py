#!/usr/bin/env python3
"""ci-queue-stats — how long Gitea Actions jobs WAIT for a runner vs how long they RUN.

    GITEA_TOKEN=... python scripts/ci-queue-stats.py [--days 7] [--repos a/b,c/d] [--json]

The V0/V9 evidence for the opportunistic cloud runners (ADR 0032): the pool is wait-bound when
queue wait (job started_at - created_at) dwarfs job duration (completed_at - started_at). Prints
p50/p90/p99 of both, jobs/day, and the per-runner share, over the completed jobs of the last N days.
Read-only: GET /repos/{o}/{r}/actions/runs (paginated) + GET .../runs/{id}/jobs. The token needs
read:repository; it rides only in the Authorization header. User-Agent is git/... because
Cloudflare 403s default python UAs (code 1010).
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GITEA_URL = os.environ.get("GITEA_URL", "https://git.chifor.me")
DEFAULT_REPOS = "cchifor/ailab,cchifor/agentforge,cchifor/platform,cchifor/agentforge-platform,cchifor/dsh-team-conductor"
EPOCH_UNSET = 1e9  # Gitea sends 0001-01-01T00:00:00Z / 1970-01-01 for unset timestamps


def parse_ts(v):
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return t if t > EPOCH_UNSET else None


def get(token, path, params=None):
    url = GITEA_URL + "/api/v1" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"token {token}", "User-Agent": "git/2.47.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def runs_since(token, repo, cutoff):
    out, page = [], 1
    while True:
        d = get(token, f"/repos/{repo}/actions/runs", {"page": page, "limit": 50})
        batch = d.get("workflow_runs") or []
        for r in batch:
            created = parse_ts(r.get("created_at")) or parse_ts(r.get("started_at"))
            if created is not None and created < cutoff:
                return out
            out.append(r)
        if len(batch) < 50:
            return out
        page += 1


def percentile(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, round(p / 100 * (len(xs) - 1))))
    return xs[k]


def summarize(jobs, days):
    """Pure. jobs: [{created, started, completed, runner}] with epoch floats (None = unset)."""
    # `is not None`, not truthiness: an epoch of 0 is a valid (if unlikely) timestamp.
    waits = [j["started"] - j["created"] for j in jobs if j["started"] is not None and j["created"] is not None]
    durs = [j["completed"] - j["started"] for j in jobs if j["completed"] is not None and j["started"] is not None]
    by_runner = {}
    for j in jobs:
        by_runner[j["runner"] or "(none)"] = by_runner.get(j["runner"] or "(none)", 0) + 1
    return {
        "jobs": len(jobs), "jobs_per_day": round(len(jobs) / max(days, 1e-9), 1),
        "wait_s": {"p50": percentile(waits, 50), "p90": percentile(waits, 90), "p99": percentile(waits, 99),
                   "over_5min": sum(1 for w in waits if w > 300), "n": len(waits)},
        "run_s": {"p50": percentile(durs, 50), "p90": percentile(durs, 90), "p99": percentile(durs, 99), "n": len(durs)},
        "by_runner": dict(sorted(by_runner.items(), key=lambda kv: -kv[1])),
    }


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--repos", default=DEFAULT_REPOS)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    token = os.environ.get("GITEA_TOKEN") or os.environ.get("AF_GITEA_TOKEN")
    if not token:
        print("GITEA_TOKEN (read:repository) is required", file=sys.stderr)
        return 2
    cutoff = time.time() - a.days * 86400
    jobs = []
    for repo in [r.strip() for r in a.repos.split(",") if r.strip()]:
        for run in runs_since(token, repo, cutoff):
            for j in get(token, f"/repos/{repo}/actions/runs/{run['id']}/jobs").get("jobs") or []:
                if j.get("status") != "completed":
                    continue
                jobs.append({"repo": repo, "created": parse_ts(j.get("created_at")), "started": parse_ts(j.get("started_at")),
                             "completed": parse_ts(j.get("completed_at")), "runner": j.get("runner_name") or ""})
    s = summarize(jobs, a.days)
    s["days"] = a.days
    s["measured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if a.json:
        print(json.dumps(s, indent=2))
    else:
        w, r = s["wait_s"], s["run_s"]
        print(f"last {a.days:g} days: {s['jobs']} completed jobs ({s['jobs_per_day']}/day)")
        print(f"  queue wait  p50 {w['p50']:.0f}s  p90 {w['p90']:.0f}s  p99 {w['p99']:.0f}s  (>5 min: {w['over_5min']} of {w['n']})")
        print(f"  job runtime p50 {r['p50']:.0f}s  p90 {r['p90']:.0f}s  p99 {r['p99']:.0f}s")
        print("  by runner: " + ", ".join(f"{k}={v}" for k, v in s["by_runner"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
