#!/usr/bin/env python3
"""ci-queue-stats — how long Gitea Actions jobs WAIT for a runner vs how long they RUN.

    GITEA_TOKEN=... python scripts/ci-queue-stats.py [--days 7] [--repos a/b,c/d] [--json]

The V0/V9 evidence for the opportunistic cloud runners (ADR 0032): the pool is wait-bound when
queue wait (job started_at - created_at) dwarfs job duration (completed_at - started_at). Prints
p50/p90/p99 of scheduling delay and runtime, jobs/day, and outcomes, including cancelled jobs.
Created-to-started is NOT runner queue time for a dependent job. Supply --dependency-map to
separate readiness from scheduling; unknown dependencies yield unknown runner wait. Reruns
can preserve historical created_at: supply --dispatches for controlled rerun measurements.
Read-only: GET /repos/{o}/{r}/actions/runs (paginated) + GET .../runs/{id}/jobs. The token needs
read:repository; it rides only in the Authorization header. User-Agent is git/... because
Cloudflare 403s default python UAs (code 1010).
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
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


def paged(token, path, key):
    """Every page of a list endpoint, stopping on a short page (Gitea's default page is 30)."""
    out, page = [], 1
    while True:
        batch = get(token, path, {"page": page, "limit": 50}).get(key) or []
        out.extend(batch)
        # Stop on an EMPTY page, not a short one: Gitea clamps `limit` to MAX_RESPONSE_ITEMS
        # (admin-configurable), and a clamp below 50 would make every full page look short.
        if not batch:
            return out
        page += 1


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
            activity = [parse_ts(r.get(k)) for k in ("created_at", "started_at", "completed_at")]
            known = [x for x in activity if x is not None]
            if not known or max(known) >= cutoff:
                out.append(r)
        # API pages are ordered by run id, not latest attempt activity. An old run can be rerun
        # recently, behind pages of old runs. Never stop on an old timestamp or a short page.
        if not batch:  # empty, not short: see paged()
            return out
        page += 1


def percentile(xs, p):
    if not xs:
        return None
    xs = sorted(xs)  # nearest-rank: the smallest value with at least p% of the samples at or below it
    k = max(0, min(len(xs) - 1, math.ceil(p / 100 * len(xs)) - 1))
    return xs[k]


def job_row(repo, run, job, dependencies=None, dispatch_at=None, known_rerun=False):
    """Normalize one attempt. None dependencies means unknown; [] means confirmed root."""
    created = parse_ts(dispatch_at) if dispatch_at or known_rerun else parse_ts(job.get("created_at"))
    started, completed = parse_ts(job.get("started_at")), parse_ts(job.get("completed_at"))
    ready = None
    if dependencies is not None and created is not None:
        ends = [parse_ts(d.get("completed_at")) for d in dependencies]
        if all(t is not None for t in ends):
            ready = max([created] + ends)
    return {"repo": repo, "run_id": run["id"], "source_revision": run.get("head_sha"),
            "run_attempt": run.get("run_attempt"), "job_id": job["id"], "name": job.get("name"),
            "event": run.get("event"), "workflow": run.get("path"),
            "status": job.get("status"), "conclusion": job.get("conclusion"),
            "created": created, "ready": ready, "started": started, "completed": completed,
            "creation_source": "observed-dispatch" if dispatch_at else "rerun-dispatch-unknown" if known_rerun else "api-job-created; may predate rerun",
            "runner": job.get("runner_name") or "", "steps": job.get("steps", [])}


def interval(end, start):
    return end - start if end is not None and start is not None and end >= start else None


def distribution(values):
    return {"p50": percentile(values, 50), "p90": percentile(values, 90), "p99": percentile(values, 99), "n": len(values)}


def summarize(jobs, days):
    """Pure. jobs: [{created, started, completed, runner}] with epoch floats (None = unset)."""
    # `is not None`, not truthiness: an epoch of 0 is a valid (if unlikely) timestamp.
    waits = [v for j in jobs if (v := interval(j["started"], j["created"])) is not None]
    durs = [v for j in jobs if (v := interval(j["completed"], j["started"])) is not None]
    runner_waits = [v for j in jobs if (v := interval(j["started"], j.get("ready"))) is not None]
    dependency_waits = [v for j in jobs if (v := interval(j.get("ready"), j["created"])) is not None]
    outcomes = {}
    for job in jobs:
        outcome = job.get("conclusion") or job.get("status") or "unknown"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    by_runner = {}
    for j in jobs:
        by_runner[j["runner"] or "(none)"] = by_runner.get(j["runner"] or "(none)", 0) + 1
    return {
        "jobs": len(jobs), "jobs_per_day": round(len(jobs) / max(days, 1e-9), 1),
        "wait_s": {"p50": percentile(waits, 50), "p90": percentile(waits, 90), "p99": percentile(waits, 99),
                   "over_5min": sum(1 for w in waits if w > 300), "n": len(waits)},
        "run_s": {"p50": percentile(durs, 50), "p90": percentile(durs, 90), "p99": percentile(durs, 99), "n": len(durs)},
        "runner_wait_s": distribution(runner_waits), "dependency_wait_s": distribution(dependency_waits),
        "runner_minutes": sum(durs) / 60, "outcomes": outcomes,
        "unknown_readiness_jobs": sum(j.get("ready") is None for j in jobs),
        "by_runner": dict(sorted(by_runner.items(), key=lambda kv: -kv[1])),
    }


def _f(v):
    return "-" if v is None else f"{v:.0f}s"  # an uncomputable percentile renders, never crashes


def render(s):
    """Pure: the text report for a summarize() result (None-safe)."""
    w, r = s["wait_s"], s["run_s"]
    lines = [f"last {s['days']:g} days: {s['jobs']} jobs including unfinished/cancelled ({s['jobs_per_day']}/day)"]
    if w["n"]:
        lines.append(f"  created-to-started scheduling delay (includes dependencies; reruns need dispatch evidence) p50 {_f(w['p50'])}  p90 {_f(w['p90'])}  p99 {_f(w['p99'])}  (>5 min: {w['over_5min']} of {w['n']})")
    else:
        lines.append("  scheduling delay  no samples (no job with both created_at and started_at)")
    if r["n"]:
        lines.append(f"  job runtime p50 {_f(r['p50'])}  p90 {_f(r['p90'])}  p99 {_f(r['p99'])}")
    else:
        lines.append("  job runtime no samples")
    lines.append("  by runner: " + (", ".join(f"{k}={v}" for k, v in s["by_runner"].items()) or "-"))
    q = s['runner_wait_s']
    lines.append(f"  confirmed ready-to-started runner wait p50 {_f(q['p50'])} p90 {_f(q['p90'])}; {q['n']} samples, {s['unknown_readiness_jobs']} jobs with unknown readiness")
    lines.append("  outcomes: " + json.dumps(s['outcomes'], sort_keys=True))
    if s.get("skipped_repos"):
        lines.append("  skipped repos: " + ", ".join(s["skipped_repos"]))
    return chr(10).join(lines)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--repos", default=DEFAULT_REPOS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dependency-map", help='JSON: repository -> workflow filename -> job name -> dependency job names')
    ap.add_argument("--dispatches", help='JSON: repository -> run id -> observed dispatch timestamp; use for reruns')
    ap.add_argument("--records", help='write normalized job records as JSON for reproducible analysis')
    a = ap.parse_args(argv)
    token = os.environ.get("GITEA_TOKEN") or os.environ.get("AF_GITEA_TOKEN")
    if not token:
        print("GITEA_TOKEN (read:repository) is required", file=sys.stderr)
        return 2
    cutoff = time.time() - a.days * 86400
    jobs, skipped = [], []
    def read_map(path):
        if not path:
            return {}
        with open(path) as stream:
            return json.load(stream)
    dependency_map, dispatches = read_map(a.dependency_map), read_map(a.dispatches)
    for repo in [r.strip() for r in a.repos.split(",") if r.strip()]:
        try:
            for run in runs_since(token, repo, cutoff):
                run_jobs = paged(token, f"/repos/{repo}/actions/runs/{run['id']}/jobs", "jobs")
                workflow = (run.get('path') or '').split('@', 1)[0].rsplit('/', 1)[-1]
                graph = dependency_map.get(repo, {}).get(workflow, {})
                by_name = {}
                for job in run_jobs:
                    by_name.setdefault(job.get('name'), []).append(job)
                for j in run_jobs:
                    names = graph.get(j.get('name'))
                    dependencies = [] if len(run_jobs) == 1 else None
                    if names is not None and all(len(by_name.get(n, [])) == 1 for n in names):
                        dependencies = [by_name[n][0] for n in names]
                    overrides = dispatches.get(repo, {})
                    jobs.append(job_row(repo, run, j, dependencies, overrides.get(str(run['id'])), str(run['id']) in overrides))
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            # One repo with Actions off / a 404 / a transient 5xx must not void the measurement:
            # report it as skipped and keep the partial result (comparable V0-vs-V9 as long as the
            # same repos answer both times, which the report shows).
            skipped.append(f"{repo}: {type(e).__name__} {getattr(e, 'code', '')}".strip())
            print(f"warning: skipping {skipped[-1]}", file=sys.stderr)
    s = summarize(jobs, a.days)
    s["skipped_repos"] = skipped
    s["days"] = a.days
    s["measured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    s["schema_version"] = 1
    s["wait_s_definition"] = "created-to-started scheduling delay, including dependency waiting; not runner queue time"
    if a.records:
        with open(a.records, 'w') as stream:
            json.dump(jobs, stream, indent=2)
            stream.write('\n')
    if a.json:
        print(json.dumps(s, indent=2))
    else:
        print(render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
