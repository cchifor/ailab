#!/usr/bin/env python3
"""ci-rerun-watchdog - bounded auto-rerun of Gitea Actions jobs lost with a cloud CI runner.

The cloud CI runners (`cloud-ci-N`, on the cloudlab hosts) power off every night and can die under
a human at any hour. A job caught on one of them is failed by Gitea's zombie reaper ~10 min after
the runner stops polling (or cancelled by the runner's own drain deadline just before) and is NEVER
re-queued. This service re-runs a run's failed jobs ONCE when - and only when - every failed job in
that run ran on a cloud runner that this service itself watched go offline, and the run is still
the current one for its PR or branch. Everything else is a skip with a named reason.

WHAT IT MUST NOT DO, and the gate that prevents it (2026-09-23 cloud CI runners plan, Phase 3):
  G1  never touch a run-level `cancelled` (cancel-in-progress from a newer push)
  G2  never look further back than LOOKBACK_SECONDS
  G3  never re-run a run twice within the 90-day tombstone retention (live AND shadow ledgers)
  G5  never re-run a genuine failure: every failed/cancelled job must be on a cloud runner whose
      status THIS service saw flip to offline, and the job's end must sit within
      LOSS_CORRELATION_SECONDS before that observation or any time after it (the reap lands after
      the loss; the drain cancel just before). A cancelled job that is NOT a lost-cloud job blocks
      the run - `rerun-failed-jobs` replays every failed AND cancelled job, so the replay set must
      be exactly the lost work.
  G6  never re-run a stale head: a pull_request run must match an OPEN PR's head sha (its
      head_branch is empty in the API); a push/schedule/dispatch run must match its branch tip
  G7  never race the concurrency group: no non-terminal sibling (same workflow path, same ref
      family) and no newer sibling run
  caps (per scan, per day, counted separately for the live and shadow ledgers), a `disabled` kill
  switch in the state ConfigMap, DRY_RUN (shadow ledger, `would-rerun` log line, no POST).

FAIL CLOSED. Any API error, incomplete pagination or state-write failure aborts the scan with
`ci_rerun_watchdog_errors_total{stage}` incremented and no further reruns. A reservation is
persisted BEFORE the POST; a POST that times out is `uncertain` and reconciled on later scans
(the run left `failure` -> confirmed; unchanged -> one retry; then `unresolved` + alert). The
service never claims "exactly one successful rerun".

Stdlib only, one file, no image build: the cloud-power shape (python:3.14-slim + this file from a
hash-rolled ConfigMap). Import is side-effect free; everything network-facing starts in main().
"""
import http.client
import json
import logging
import os
import re
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("ci-rerun-watchdog")

# ansible/roles/pr_reviewer/defaults/main.yml pr_reviewer_repos - the repos the reviewbots watch.
DEFAULT_REPOS = (
    "cchifor/ailab,cchifor/agentforge,cchifor/platform,"
    "cchifor/agentforge-platform,cchifor/dsh-team-conductor"
)
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
USER_AGENT = "git/2.47.0"  # Cloudflare fronting git.chifor.me 403s default python UAs (code 1010)

# Gitea's GitHub-style run/job status. Anything that is not `completed` is live - including
# values this list does not know, because an unknown status must never read as "safe to touch".
STATUS_DONE = "completed"
TERMINAL_CONCLUSIONS = ("success", "failure", "cancelled", "skipped")
REPLAYED_CONCLUSIONS = ("failure", "cancelled")  # what rerun-failed-jobs replays (+ dependents)


# --- config ---------------------------------------------------------------------------------
class Config:
    """Env-driven settings. Tests construct it directly with keyword overrides."""

    def __init__(self, **overrides):
        self.gitea_url = "http://gitea-http.gitea.svc.cluster.local:3000"
        self.gitea_token = ""
        self.gitea_org = "cchifor"
        self.repos = [r for r in DEFAULT_REPOS.split(",") if r]
        self.scan_interval = 60
        self.lookback_seconds = 7200
        self.loss_correlation_seconds = 180
        self.max_reruns_per_scan = 3
        self.max_reruns_per_day = 20
        self.dry_run = True
        self.state_configmap = "ci-rerun-watchdog-state"
        self.state_namespace = ""
        self.port = 8128
        self.cloud_runner_re = r"^cloud-ci-\d+$"
        self.tombstone_retention_seconds = 90 * 86400
        # A sibling run that turned `cancelled` within this many seconds of our POST is treated as
        # collateral of it (the post-check).
        self.collateral_window_seconds = 120
        # How long after a POST the post-check keeps looking for collateral.
        self.post_check_seconds = 600
        self.max_post_attempts = 2  # first POST + one retry
        self.page_limit = 50
        self.max_pages = 200
        self.http_timeout = 30
        for k, v in overrides.items():
            if not hasattr(self, k):
                raise TypeError("unknown config key %r" % k)
            setattr(self, k, v)
        self.cloud_re = re.compile(self.cloud_runner_re)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def _int(name, default):
            return int(env.get(name, str(default)))

        def _bool(name, default):
            return env.get(name, "true" if default else "false").strip().lower() in ("1", "true", "yes")

        namespace = env.get("STATE_NAMESPACE", "")
        if not namespace:
            try:
                with open(os.path.join(SA_DIR, "namespace")) as f:
                    namespace = f.read().strip()
            except OSError:
                namespace = ""
        return cls(
            gitea_url=env.get("GITEA_URL", "http://gitea-http.gitea.svc.cluster.local:3000").rstrip("/"),
            gitea_token=env.get("GITEA_TOKEN", ""),
            gitea_org=env.get("GITEA_ORG", "cchifor"),
            repos=[r.strip() for r in env.get("REPOS", DEFAULT_REPOS).split(",") if r.strip()],
            scan_interval=_int("SCAN_INTERVAL", 60),
            lookback_seconds=_int("LOOKBACK_SECONDS", 7200),
            loss_correlation_seconds=_int("LOSS_CORRELATION_SECONDS", 180),
            max_reruns_per_scan=_int("MAX_RERUNS_PER_SCAN", 3),
            max_reruns_per_day=_int("MAX_RERUNS_PER_DAY", 20),
            dry_run=_bool("DRY_RUN", True),
            state_configmap=env.get("STATE_CONFIGMAP", "ci-rerun-watchdog-state"),
            state_namespace=namespace,
            port=_int("PORT", 8128),
            cloud_runner_re=env.get("CLOUD_RUNNER_RE", r"^cloud-ci-\d+$"),
        )


# --- errors ---------------------------------------------------------------------------------
class ApiError(Exception):
    """A Gitea call failed, answered wrongly, or paginated incompletely. Aborts the scan."""

    def __init__(self, stage, msg, status=None):
        super().__init__(msg)
        self.stage = stage
        self.status = status


class ApiUncertain(ApiError):
    """A POST whose effect is unknown (timeout, connection loss, unexpected status)."""


class TransportError(Exception):
    """The HTTP layer got no status back at all."""


class StateError(Exception):
    """The state ConfigMap could not be read or written."""


class StateConflict(StateError):
    """PUT/POST rejected with 409: somebody else wrote the ConfigMap since we read it."""


# --- time -----------------------------------------------------------------------------------
def parse_ts(value):
    """RFC3339 -> epoch seconds, or None for unset. Gitea renders an unset time as the Go zero
    (0001-01-01T00:00:00Z) and some paths as the Unix epoch; both are 'unset', not 'very old'."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("0001-") or value.startswith("1970-01-01"):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


# --- Gitea ----------------------------------------------------------------------------------
class UrllibTransport:
    """(method, path, params, body, timeout) -> (status, headers, bytes). HTTP statuses are
    returned, not raised; only a missing answer raises TransportError."""

    def __init__(self, base_url, token, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def __call__(self, method, path, params=None, body=None, timeout=None):
        url = self.base_url + "/api/v1" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "token " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            # socket.timeout is an OSError; the message never carries the token.
            raise TransportError("%s: %s" % (type(e).__name__, e)) from e


class GiteaApi:
    """Every call returns data or raises ApiError(stage). List calls walk the pages to the
    server's total (failed_runs alone stops at the lookback, by design) and refuse to return a
    set they cannot prove complete."""

    def __init__(self, transport, page_limit=50, max_pages=200, post_timeout=60):
        self.transport = transport
        self.page_limit = page_limit
        self.max_pages = max_pages
        self.post_timeout = post_timeout

    def _get(self, stage, path, params=None):
        try:
            status, headers, raw = self.transport("GET", path, params)
        except TransportError as e:
            raise ApiError(stage, "GET %s: %s" % (path, e))
        if status != 200:
            raise ApiError(stage, "GET %s: HTTP %s" % (path, status), status=status)
        try:
            return json.loads(raw or b"null"), headers
        except ValueError:
            raise ApiError(stage, "GET %s: response is not JSON" % path)

    def _paged(self, stage, path, key, params=None, stop=None):
        """All pages of a list endpoint. `key` names the list inside the JSON object (None for a
        bare list). `stop(chunk)` may end the walk early - that is the CALLER declaring it does
        not need the tail (the runs list bounded by the lookback); without it the walk must
        reach the server's total_count / X-Total-Count or it is an error, because a job list
        with a page missing could hide the non-cloud failed job that G5 exists to catch."""
        params = dict(params or {})
        items, seen_ids, total, page = [], set(), None, 1
        while True:
            params.update({"page": page, "limit": self.page_limit})
            data, headers = self._get(stage, path, params)
            chunk = data if key is None else (data.get(key) if isinstance(data, dict) else None)
            if not isinstance(chunk, list):
                raise ApiError(stage, "GET %s page %d: no %r list in the response" % (path, page, key or "top-level"))
            if isinstance(data, dict) and isinstance(data.get("total_count"), int):
                total = data["total_count"]
            elif total is None:
                hdr = {k.lower(): v for k, v in headers.items()}.get("x-total-count")
                if hdr is not None and str(hdr).isdigit():
                    total = int(hdr)
            for item in chunk:
                ident = item.get("id") if isinstance(item, dict) else None
                if ident is not None:
                    if ident in seen_ids:
                        continue  # a list that shifted under us between pages
                    seen_ids.add(ident)
                items.append(item)
            if not chunk:
                break
            if stop is not None and stop(chunk):
                return items
            if total is not None and len(items) >= total:
                break
            if total is None and len(chunk) < self.page_limit:
                break
            page += 1
            if page > self.max_pages:
                raise ApiError(stage, "GET %s: more than %d pages" % (path, self.max_pages))
        if total is not None and len(items) < total:
            raise ApiError(stage, "GET %s: incomplete pagination, %d of %d" % (path, len(items), total))
        return items

    def runners(self, org):
        return self._paged("runners", "/orgs/%s/actions/runners" % org, "runners")

    def org_repos(self, org):
        return self._paged("org_repos", "/orgs/%s/repos" % org, None)

    def failed_runs(self, repo, cutoff):
        """Failed runs, newest first, read until a whole page ended before `cutoff`."""

        def old_enough(chunk):
            for run in chunk:
                done = parse_ts(run.get("completed_at")) if isinstance(run, dict) else None
                if done is None or done >= cutoff:
                    return False
            return True

        return self._paged("runs", "/repos/%s/actions/runs" % repo, "workflow_runs",
                           {"status": "failure"}, stop=old_enough)

    def runs_by_sha(self, repo, sha):
        return self._paged("siblings", "/repos/%s/actions/runs" % repo, "workflow_runs", {"head_sha": sha})

    def jobs(self, repo, run_id):
        return self._paged("jobs", "/repos/%s/actions/runs/%s/jobs" % (repo, run_id), "jobs")

    def open_pulls(self, repo):
        return self._paged("pulls", "/repos/%s/pulls" % repo, None, {"state": "open"})

    def branch_sha(self, repo, name):
        """Tip sha of a branch, or None when the branch is gone (404)."""
        path = "/repos/%s/branches/%s" % (repo, urllib.parse.quote(name, safe=""))
        try:
            status, _headers, raw = self.transport("GET", path)
        except TransportError as e:
            raise ApiError("branch", "GET %s: %s" % (path, e))
        if status == 404:
            return None
        if status != 200:
            raise ApiError("branch", "GET %s: HTTP %s" % (path, status), status=status)
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            raise ApiError("branch", "GET %s: response is not JSON" % path)
        sha = (data or {}).get("commit", {}).get("id") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not sha:
            raise ApiError("branch", "GET %s: no commit.id" % path)
        return sha

    def rerun_failed_jobs(self, repo, run_id):
        """'confirmed' (201) or 'rejected' (400: the run is not done). Any other answer - or no
        answer - is ApiUncertain: the server may or may not have queued the jobs."""
        path = "/repos/%s/actions/runs/%s/rerun-failed-jobs" % (repo, run_id)
        try:
            status, _headers, _raw = self.transport("POST", path, None, None, self.post_timeout)
        except TransportError as e:
            raise ApiUncertain("rerun_post", "POST %s: %s" % (path, e))
        if status == 201:
            return "confirmed"
        if status == 400:
            return "rejected"
        raise ApiUncertain("rerun_post", "POST %s: HTTP %s" % (path, status), status=status)


# --- state ConfigMap ------------------------------------------------------------------------
class KubeState:
    """The state ConfigMap through the in-cluster API with the projected SA token.
    read() -> (data, resourceVersion|None); write(data, rv) -> new rv (rv None creates)."""

    def __init__(self, namespace, name, host=None, sa_dir=SA_DIR, timeout=15):
        self.namespace = namespace
        self.name = name
        self.host = host or "https://%s:%s" % (
            os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
            os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
        )
        self.sa_dir = sa_dir
        self.timeout = timeout
        self._ctx = None

    def _context(self):
        if self._ctx is None:
            ctx = ssl.create_default_context(cafile=os.path.join(self.sa_dir, "ca.crt"))
            self._ctx = ctx
        return self._ctx

    def _token(self):
        # Re-read per call: the kubelet rotates projected tokens in place.
        with open(os.path.join(self.sa_dir, "token")) as f:
            return f.read().strip()

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self._token(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._context()) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise StateError("%s %s: %s: %s" % (method, path, type(e).__name__, e)) from e

    def _path(self, with_name=True):
        p = "/api/v1/namespaces/%s/configmaps" % self.namespace
        return p + "/" + self.name if with_name else p

    def read(self):
        status, raw = self._call("GET", self._path())
        if status == 404:
            return None, None
        if status != 200:
            raise StateError("GET configmap: HTTP %s" % status)
        obj = json.loads(raw)
        return dict(obj.get("data") or {}), obj.get("metadata", {}).get("resourceVersion")

    def write(self, data, rv):
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                "labels": {"app.kubernetes.io/name": "ci-rerun-watchdog", "app.kubernetes.io/managed-by": "ci-rerun-watchdog"},
            },
            "data": {k: str(v) for k, v in data.items()},
        }
        if rv is None:
            status, raw = self._call("POST", self._path(with_name=False), body)
        else:
            body["metadata"]["resourceVersion"] = str(rv)
            status, raw = self._call("PUT", self._path(), body)
        if status == 409:
            raise StateConflict("configmap write: HTTP 409")
        if status not in (200, 201):
            raise StateError("configmap write: HTTP %s" % status)
        return json.loads(raw).get("metadata", {}).get("resourceVersion")


LEDGERS = ("live", "shadow")
# Keys an operator writes by `kubectl patch`; a 409 retry takes THEIR fresh values, never ours.
OPERATOR_KEYS = ("disabled", "ack_unresolved_before")


class State:
    """In-memory view of the state ConfigMap: runner observations + the two ledgers."""

    def __init__(self, store):
        self.store = store
        self.data = {}
        self.rv = None
        self.runner_seen = {}
        self.ledgers = {name: [] for name in LEDGERS}

    def load(self):
        try:
            data, rv = self.store.read()
        except StateError as e:
            raise ApiError("state_read", str(e))
        self.data = dict(data or {})
        self.rv = rv
        self.runner_seen = self._json(self.data.get("runner_seen"), dict)
        for name in LEDGERS:
            self.ledgers[name] = [e for e in self._json(self.data.get(name), list) if isinstance(e, dict)]

    @staticmethod
    def _json(raw, kind):
        if not raw:
            return kind()
        try:
            v = json.loads(raw)
        except ValueError:
            log.error("state: unreadable JSON in the ConfigMap, starting that key empty")
            return kind()
        return v if isinstance(v, kind) else kind()

    @property
    def disabled(self):
        return str(self.data.get("disabled", "")).strip().lower() in ("1", "true", "yes")

    @property
    def ack_unresolved_before(self):
        try:
            return float(self.data.get("ack_unresolved_before", ""))
        except ValueError:
            return None

    def save(self):
        body = dict(self.data)
        body["runner_seen"] = json.dumps(self.runner_seen, sort_keys=True)
        for name in LEDGERS:
            body[name] = json.dumps(self.ledgers[name], sort_keys=True)
        try:
            try:
                self.rv = self.store.write(body, self.rv)
            except StateConflict:
                fresh, rv = self.store.read()
                fresh = dict(fresh or {})
                for k in OPERATOR_KEYS:
                    if k in fresh:
                        body[k] = fresh[k]
                        self.data[k] = fresh[k]
                    else:
                        body.pop(k, None)
                        self.data.pop(k, None)
                self.rv = self.store.write(body, rv)  # a second conflict aborts the scan
        except StateError as e:
            raise ApiError("state_write", str(e))
        self.data.update({k: body[k] for k in ("runner_seen",) + LEDGERS})

    # -- ledgers
    def tombstones(self):
        out = set()
        for name in LEDGERS:
            for e in self.ledgers[name]:
                out.add((e.get("repo"), e.get("run_id")))
        return out

    def find(self, ledger, repo, run_id):
        for e in self.ledgers[ledger]:
            if e.get("repo") == repo and e.get("run_id") == run_id:
                return e
        return None

    def reserve(self, ledger, repo, run, ref, now, collateral_of=None):
        entry = {
            "repo": repo,
            "run_id": run["id"],
            "sha": run.get("head_sha"),
            "path": run.get("path"),
            "event": run.get("event"),
            "ref": ref,
            "reserved_at": now,
            "updated_at": now,
            "outcome": "would_rerun" if ledger == "shadow" else "reserved",
            "attempts": 0,
        }
        if collateral_of is not None:
            entry["collateral_of"] = collateral_of
        self.ledgers[ledger].append(entry)
        return entry

    def prune(self, now, retention):
        for name in LEDGERS:
            self.ledgers[name] = [
                e for e in self.ledgers[name]
                if isinstance(e.get("reserved_at"), (int, float)) and now - e["reserved_at"] <= retention
            ]
        self.runner_seen = {
            k: v for k, v in self.runner_seen.items()
            if isinstance(v, dict) and now - max(v.get("last_online_ts") or 0, v.get("last_offline_ts") or 0) <= retention
        }

    def count_recent(self, ledger, now, window):
        return sum(1 for e in self.ledgers[ledger]
                   if isinstance(e.get("reserved_at"), (int, float)) and now - e["reserved_at"] <= window)

    def unresolved(self):
        return [e for e in self.ledgers["live"] if e.get("outcome") == "unresolved"]


def observe_runners(state, runners, now):
    """Stamp lost_at on a runner whose status flipped online -> offline under our watch. A runner
    first seen offline gets no lost_at (its loss is not correlated with anything we saw), and a
    runner that comes back clears it (a later failure on it is a real failure)."""
    seen = state.runner_seen
    for r in runners:
        if not isinstance(r, dict) or not isinstance(r.get("id"), int):
            continue
        key = str(r["id"])
        entry = seen.get(key)
        if not isinstance(entry, dict):
            entry = {}
        prev = entry.get("status")
        status = str(r.get("status", "")).lower()
        entry["name"] = r.get("name", "")
        if status == "offline":
            entry["last_offline_ts"] = now
            if prev is not None and prev != "offline":
                entry["lost_at"] = now
        else:
            entry["last_online_ts"] = now
            entry.pop("lost_at", None)
        entry["status"] = status
        seen[key] = entry


# --- metrics --------------------------------------------------------------------------------
METRIC_HELP = {
    "reruns_total": ("counter", "Rerun decisions taken, by repo and mode (live|dry_run)."),
    "candidates": ("gauge", "Runs that passed every gate in the last scan, by repo."),
    "skipped_total": ("counter", "Runs skipped, by repo and reason."),
    "last_scan_timestamp_seconds": ("gauge", "Unix time of the last FULLY successful scan."),
    "errors_total": ("counter", "Scans aborted, by stage."),
    "collateral_cancel_total": ("counter", "Sibling runs cancelled within the collateral window of our POST."),
    "unresolved": ("gauge", "Live reservations whose POST outcome is still unknown after retries."),
    "dry_run": ("gauge", "1 when DRY_RUN (shadow ledger, no POST)."),
    "disabled": ("gauge", "1 when the state ConfigMap's kill switch is on."),
    "reruns_last_24h": ("gauge", "Reservations in the last 24h, by mode (from the ledgers)."),
    "unlisted_repos": ("gauge", "Org repos with Actions that are not in REPOS."),
    "scans_total": ("counter", "Scans attempted."),
}
PREFIX = "ci_rerun_watchdog_"


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._values = {}  # (name, ((k, v), ...)) -> float

    @staticmethod
    def _key(name, labels):
        return name, tuple(sorted((labels or {}).items()))

    def inc(self, name, labels=None, by=1):
        with self._lock:
            k = self._key(name, labels)
            self._values[k] = self._values.get(k, 0) + by

    def set(self, name, value, labels=None):
        with self._lock:
            self._values[self._key(name, labels)] = value

    def clear(self, name):
        with self._lock:
            for k in [k for k in self._values if k[0] == name]:
                del self._values[k]

    def get(self, name, labels=None):
        with self._lock:
            return self._values.get(self._key(name, labels), 0)

    def render(self):
        with self._lock:
            snapshot = sorted(self._values.items())
        lines = []
        for name, (kind, help_text) in METRIC_HELP.items():
            lines.append("# HELP %s%s %s" % (PREFIX, name, help_text))
            lines.append("# TYPE %s%s %s" % (PREFIX, name, kind))
            for (n, labels), value in snapshot:
                if n != name:
                    continue
                lab = ",".join('%s="%s"' % (k, _esc(v)) for k, v in labels)
                lines.append("%s%s%s %s" % (PREFIX, name, "{%s}" % lab if lab else "", _num(value)))
        return "\n".join(lines) + "\n"


def _num(v):
    return repr(float(v)) if isinstance(v, float) and not float(v).is_integer() else str(int(v))


METRICS = Metrics()


# --- selection ------------------------------------------------------------------------------
class Ctx:
    """Everything the pure gates need, gathered once per scan."""

    def __init__(self, now, cfg, cloud, runner_seen, tombstones, repo=""):
        self.now = now
        self.cfg = cfg
        self.cutoff = now - cfg.lookback_seconds
        self.cloud = cloud  # runner_id (int) -> name
        self.runner_seen = runner_seen
        self.tombstones = tombstones  # {(repo, run_id)} over BOTH ledgers
        self.repo = repo  # the repo whose runs are being classified


def ref_family(event):
    """Which concurrency group family a run belongs to: PR events run on refs/pull/N/*, every
    other event on the branch ref, so a push run and a pull_request run of the same workflow and
    sha are NOT siblings."""
    return "pr" if str(event or "").startswith("pull_request") else "branch"


def is_live(run):
    return str(run.get("status", "")) != STATUS_DONE


def pre_classify(run, ctx):
    """G1-G3: the gates that need nothing but the run itself."""
    if not isinstance(run, dict) or not isinstance(run.get("id"), int):
        return "skip", "malformed_run"
    if is_live(run):
        return "skip", "run_live"
    if run.get("conclusion") == "cancelled":
        return "skip", "run_cancelled"  # G1: cancel-in-progress is somebody else's decision
    if run.get("conclusion") != "failure":
        return "skip", "not_failure"
    done = parse_ts(run.get("completed_at"))
    if done is None:
        return "skip", "completed_at_unset"
    if done < ctx.cutoff:
        return "skip", "outside_lookback"  # G2
    if (ctx.repo, run["id"]) in ctx.tombstones:
        return "skip", "tombstone"  # G3
    return "candidate", "pre"


def classify(run, jobs, ctx):
    """G1-G5 -> ('candidate', 'lost_cloud_jobs') or ('skip', reason). Pure: no I/O."""
    verdict, reason = pre_classify(run, ctx)
    if verdict != "candidate":
        return verdict, reason
    if not isinstance(jobs, list):
        return "skip", "jobs_unavailable"
    replayed = [j for j in jobs if isinstance(j, dict) and j.get("conclusion") in REPLAYED_CONCLUSIONS]
    if not replayed:
        return "skip", "no_failed_jobs"
    if any(is_live(j) for j in jobs if isinstance(j, dict)):
        return "skip", "job_live"
    for job in replayed:
        rid = job.get("runner_id")
        if not isinstance(rid, int) or rid not in ctx.cloud:
            # Includes runner_id 0 (never assigned / runner deleted) and every ailab runner. One
            # non-cloud replayed job makes the whole run ineligible: rerun-failed-jobs would
            # replay it too, and that is a genuine failure being retried for free.
            return "skip", "job_not_cloud"
        seen = ctx.runner_seen.get(str(rid)) or {}
        lost_at = seen.get("lost_at")
        if not isinstance(lost_at, (int, float)):
            return "skip", "runner_not_lost"  # online now, or never seen online by us
        done = parse_ts(job.get("completed_at"))
        if done is None:
            return "skip", "job_completed_at_unset"
        if done < lost_at - ctx.cfg.loss_correlation_seconds:
            # The job ended well before the runner was lost: a real failure that happened to sit
            # on a runner which later went to bed with the rest of the cloud.
            return "skip", "loss_uncorrelated"
    return "candidate", "lost_cloud_jobs"


def head_check(api, repo, run):
    """G6 -> ('ok', ref) with ref = {kind: pr, number, head_sha} | {kind: branch, name, head_sha},
    or ('skip', reason)."""
    sha = run.get("head_sha")
    if not isinstance(sha, str) or not sha:
        return "skip", "no_sha"
    if ref_family(run.get("event")) == "pr":
        for pr in api.open_pulls(repo):
            head = (pr.get("head") or {}) if isinstance(pr, dict) else {}
            if head.get("sha") == sha and isinstance(pr.get("number"), int):
                return "ok", {"kind": "pr", "number": pr["number"], "head_sha": sha}
        return "skip", "pr_not_open"
    branch = run.get("head_branch")
    if not isinstance(branch, str) or not branch:
        return "skip", "no_ref"  # AGit / detached: nothing to be current against
    tip = api.branch_sha(repo, branch)
    if tip is None:
        return "skip", "branch_gone"
    if tip != sha:
        return "skip", "branch_moved"
    return "ok", {"kind": "branch", "name": branch, "head_sha": sha}


def sibling_check(api, repo, run):
    """G7: nothing live and nothing newer in the same concurrency group."""
    family = ref_family(run.get("event"))
    for s in api.runs_by_sha(repo, run.get("head_sha")):
        if not isinstance(s, dict) or s.get("id") == run.get("id"):
            continue
        if s.get("path") != run.get("path") or ref_family(s.get("event")) != family:
            continue
        if is_live(s):
            return "skip", "sibling_live"
        if isinstance(s.get("id"), int) and s["id"] > run["id"]:
            return "skip", "superseded"
    return "ok", None


def current_head_gates(api, repo, run):
    verdict, ref = head_check(api, repo, run)
    if verdict != "ok":
        return verdict, ref
    verdict, reason = sibling_check(api, repo, run)
    if verdict != "ok":
        return verdict, reason
    return "ok", ref


def current_head_sha(api, repo, ref):
    """Where the PR/branch of a ledger entry points now; None when it is gone/closed."""
    if ref.get("kind") == "pr":
        for pr in api.open_pulls(repo):
            if isinstance(pr, dict) and pr.get("number") == ref.get("number"):
                return (pr.get("head") or {}).get("sha")
        return None
    if ref.get("kind") == "branch" and ref.get("name"):
        return api.branch_sha(repo, ref["name"])
    return None


# --- the scan -------------------------------------------------------------------------------
class Budget:
    def __init__(self, cfg, state, ledger, now):
        self.cfg = cfg
        self.ledger = ledger
        self.this_scan = 0
        self.today = state.count_recent(ledger, now, 86400)

    def check(self):
        if self.this_scan >= self.cfg.max_reruns_per_scan:
            return "cap_scan"
        if self.today >= self.cfg.max_reruns_per_day:
            return "cap_day"
        return None

    def take(self):
        self.this_scan += 1
        self.today += 1


class Scan:
    def __init__(self, api, state, cfg, now, metrics, summary):
        self.api = api
        self.state = state
        self.cfg = cfg
        self.now = now
        self.metrics = metrics
        self.summary = summary
        self.mode = "shadow" if cfg.dry_run else "live"
        self.mode_label = "dry_run" if cfg.dry_run else "live"
        self.budget = None
        self.skip_keys = set()

    def skip(self, repo, run, reason, gate=""):
        self.metrics.inc("skipped_total", {"repo": repo, "reason": reason})
        self.summary["skipped"][reason] = self.summary["skipped"].get(reason, 0) + 1
        # The same stale failed run is re-evaluated every scan; the metric counts every time,
        # the log line is written once per (run, reason) so the log stays grep-able.
        key = (repo, run.get("id"))
        self.skip_keys.add(key)
        if _LOGGED_SKIPS.get(key) != reason:
            _LOGGED_SKIPS[key] = reason
            log.info("skip repo=%s run=%s gate=%s reason=%s sha=%s event=%s path=%s",
                     repo, run.get("id"), gate, reason, run.get("head_sha"), run.get("event"), run.get("path"))

    def act(self, repo, run, ref, collateral_of=None):
        """Reserve, then (live only) re-check the head and POST. The reservation is persisted
        BEFORE the POST so a crash in between leaves a `reserved` entry for reconcile, never a
        second rerun of the same run."""
        cap = self.budget.check()
        if cap:
            self.skip(repo, run, cap, "cap")
            return None
        self.budget.take()
        entry = self.state.reserve(self.mode, repo, run, ref, self.now, collateral_of=collateral_of)
        self.state.save()
        self.metrics.inc("reruns_total", {"repo": repo, "mode": self.mode_label})
        self.summary["reruns"].append({"repo": repo, "run_id": run["id"], "mode": self.mode_label})
        if self.cfg.dry_run:
            log.info("would-rerun repo=%s run=%s sha=%s event=%s path=%s ref=%s%s", repo, run["id"],
                     run.get("head_sha"), run.get("event"), run.get("path"), _ref_str(ref),
                     " collateral_of=%s" % collateral_of if collateral_of else "")
            return entry
        # Shrink the check->POST race: the head may have moved while earlier candidates were handled.
        verdict, info = current_head_gates(self.api, repo, run)
        if verdict != "ok":
            entry.update({"outcome": "rejected", "reason": info, "updated_at": self.now})
            self.state.save()
            log.info("rerun repo=%s run=%s outcome=rejected reason=%s", repo, run["id"], info)
            return entry
        self.post(repo, run["id"], entry)
        return entry

    def post(self, repo, run_id, entry):
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["posted_at"] = self.now
        entry["updated_at"] = self.now
        try:
            outcome = self.api.rerun_failed_jobs(repo, run_id)
            entry["outcome"] = outcome
            entry.pop("reason", None)
        except ApiUncertain as e:
            entry["outcome"] = "uncertain"
            entry["reason"] = str(e)
        self.state.save()
        log.info("rerun repo=%s run=%s outcome=%s attempt=%d%s", repo, run_id, entry["outcome"],
                 entry["attempts"], " detail=%s" % entry["reason"] if entry.get("reason") else "")

    def run_by_id(self, repo, entry):
        for r in self.api.runs_by_sha(repo, entry.get("sha")):
            if isinstance(r, dict) and r.get("id") == entry.get("run_id"):
                return r
        return None

    def reconcile(self):
        """Live entries whose POST outcome is unknown: `reserved` (crashed between the
        reservation and the outcome write) or `uncertain` (timeout / odd status)."""
        for entry in list(self.state.ledgers["live"]):
            if entry.get("outcome") not in ("reserved", "uncertain"):
                continue
            repo = entry.get("repo")
            run = self.run_by_id(repo, entry)
            if run is None:
                entry.update({"outcome": "unresolved", "reason": "run_not_found", "updated_at": self.now})
                self.state.save()
                log.error("reconcile repo=%s run=%s outcome=unresolved reason=run_not_found", repo, entry.get("run_id"))
                continue
            if is_live(run) or run.get("conclusion") != "failure":
                # It left `failure`: the rerun is (or was) in flight. Only the RUN's state says
                # so, never the POST's answer - the plan's "never claim exactly one".
                entry.update({"outcome": "confirmed", "updated_at": self.now})
                entry.pop("reason", None)
                self.state.save()
                log.info("reconcile repo=%s run=%s outcome=confirmed", repo, run["id"])
                continue
            if int(entry.get("attempts") or 0) >= self.cfg.max_post_attempts:
                entry.update({"outcome": "unresolved", "updated_at": self.now})
                self.state.save()
                log.error("reconcile repo=%s run=%s outcome=unresolved attempts=%s", repo, run["id"], entry.get("attempts"))
                continue
            verdict, info = current_head_gates(self.api, repo, run)
            if verdict != "ok":
                entry.update({"outcome": "rejected", "reason": info, "updated_at": self.now})
                self.state.save()
                log.info("reconcile repo=%s run=%s outcome=rejected reason=%s", repo, run["id"], info)
                continue
            self.post(repo, run["id"], entry)

    def post_check(self):
        """A sibling run for a NEWER sha of the same PR/branch that turned `cancelled` within
        the collateral window of our POST was most likely cancelled by the jobs we queued
        (concurrency group cancel-in-progress). Count it, alert, and rerun THAT run once - it is
        the current head, so replaying its cancelled jobs is safe."""
        for entry in list(self.state.ledgers["live"]):
            if entry.get("outcome") not in ("confirmed", "uncertain", "unresolved") or entry.get("post_checked"):
                continue
            posted_at = entry.get("posted_at")
            if not isinstance(posted_at, (int, float)):
                continue
            repo, ref = entry.get("repo"), entry.get("ref") or {}
            if self.now - posted_at > self.cfg.post_check_seconds:
                entry["post_checked"] = True
                continue
            head = current_head_sha(self.api, repo, ref)
            if head is None:
                entry["post_checked"] = True  # closed or gone: nothing of ours can be cancelling anything
                continue
            if head == entry.get("sha"):
                continue  # no newer sha yet; keep looking until the window closes
            family = ref_family(entry.get("event"))
            for s in self.api.runs_by_sha(repo, head):
                if not isinstance(s, dict) or not isinstance(s.get("id"), int):
                    continue
                if s.get("path") != entry.get("path") or ref_family(s.get("event")) != family:
                    continue
                if s["id"] <= entry.get("run_id", 0) or is_live(s) or s.get("conclusion") != "cancelled":
                    continue
                done = parse_ts(s.get("completed_at"))
                if done is None or abs(done - posted_at) > self.cfg.collateral_window_seconds:
                    continue
                self.metrics.inc("collateral_cancel_total")
                self.summary["collateral"].append({"repo": repo, "run_id": s["id"], "caused_by": entry.get("run_id")})
                log.error("collateral_cancel repo=%s run=%s cancelled_by_rerun_of=%s sha=%s", repo, s["id"], entry.get("run_id"), head)
                entry["post_checked"] = True
                if (repo, s["id"]) in self.state.tombstones():
                    self.skip(repo, s, "tombstone", "G3")
                    continue
                new_ref = dict(ref, head_sha=head)
                self.act(repo, s, new_ref, collateral_of=entry.get("run_id"))

    def run(self):
        api, state, cfg, now, metrics = self.api, self.state, self.cfg, self.now, self.metrics
        state.load()
        runners = api.runners(cfg.gitea_org)
        cloud = {r["id"]: r["name"] for r in runners
                 if isinstance(r, dict) and isinstance(r.get("id"), int)
                 and isinstance(r.get("name"), str) and cfg.cloud_re.match(r["name"])}
        observe_runners(state, runners, now)
        state.prune(now, cfg.tombstone_retention_seconds)
        # Acknowledged unresolved entries leave the gauge (operator: ack_unresolved_before=<epoch>).
        ack = state.ack_unresolved_before
        if ack is not None:
            for e in state.unresolved():
                if (e.get("updated_at") or 0) <= ack:
                    e["outcome"] = "unresolved_acked"
        state.save()  # the lost_at stamps must land even if a later stage fails
        self.summary["cloud_runners"] = sorted(cloud.values())

        listed = set(cfg.repos)
        unlisted = sorted(r.get("full_name") for r in api.org_repos(cfg.gitea_org)
                          if isinstance(r, dict) and r.get("has_actions") and r.get("full_name") not in listed)
        metrics.set("unlisted_repos", len(unlisted))
        if unlisted != _LAST_UNLISTED.get("repos"):
            _LAST_UNLISTED["repos"] = unlisted
            log.info("unlisted repos with Actions: %s", ",".join(unlisted) or "-")
        self.summary["unlisted_repos"] = unlisted

        metrics.set("dry_run", 1 if cfg.dry_run else 0)
        metrics.set("disabled", 1 if state.disabled else 0)
        metrics.clear("candidates")
        for repo in cfg.repos:
            metrics.set("candidates", 0, {"repo": repo})
        if state.disabled:
            self.summary["disabled"] = True
            log.warning("disabled: the state ConfigMap kill switch is on; observing only")
            self.finish()
            return
        self.budget = Budget(cfg, state, self.mode, now)
        if not cfg.dry_run:
            self.reconcile()
            self.post_check()
        ctx = Ctx(now, cfg, cloud, state.runner_seen, state.tombstones())
        for repo in cfg.repos:
            ctx.repo = repo
            candidates = 0
            for run in api.failed_runs(repo, ctx.cutoff):
                verdict, reason = pre_classify(run, ctx)
                if verdict != "candidate":
                    self.skip(repo, run, reason, _gate_of(reason))
                    continue
                jobs = api.jobs(repo, run["id"])
                verdict, reason = classify(run, jobs, ctx)
                if verdict != "candidate":
                    self.skip(repo, run, reason, _gate_of(reason))
                    continue
                verdict, info = current_head_gates(api, repo, run)
                if verdict != "ok":
                    self.skip(repo, run, info, _gate_of(info))
                    continue
                candidates += 1
                ctx.tombstones.add((repo, run["id"]))  # dedupe within this scan too
                self.act(repo, run, info)
            metrics.set("candidates", candidates, {"repo": repo})
            self.summary["candidates"][repo] = candidates
        self.finish()

    def finish(self):
        state, metrics, now = self.state, self.metrics, self.now
        state.save()
        for key in [k for k in _LOGGED_SKIPS if k not in self.skip_keys]:
            del _LOGGED_SKIPS[key]  # bounded to the runs the last scan saw
        metrics.set("unresolved", len(state.unresolved()))
        metrics.set("reruns_last_24h", state.count_recent("live", now, 86400), {"mode": "live"})
        metrics.set("reruns_last_24h", state.count_recent("shadow", now, 86400), {"mode": "dry_run"})


_LOGGED_SKIPS = {}
_LAST_UNLISTED = {}
_GATES = {
    "run_live": "G1", "run_cancelled": "G1", "not_failure": "G1", "completed_at_unset": "G2",
    "outside_lookback": "G2", "tombstone": "G3", "no_failed_jobs": "G5", "job_live": "G5",
    "job_not_cloud": "G5", "runner_not_lost": "G5", "job_completed_at_unset": "G5",
    "loss_uncorrelated": "G5", "jobs_unavailable": "G5", "no_sha": "G6", "pr_not_open": "G6",
    "no_ref": "G6", "branch_gone": "G6", "branch_moved": "G6", "sibling_live": "G7",
    "superseded": "G7", "cap_scan": "cap", "cap_day": "cap", "malformed_run": "G1",
}


def _gate_of(reason):
    return _GATES.get(reason, "?")


def _ref_str(ref):
    if not isinstance(ref, dict):
        return "-"
    if ref.get("kind") == "pr":
        return "pr#%s" % ref.get("number")
    return "branch:%s" % ref.get("name")


def scan(api, store, cfg, now=None, metrics=None):
    """One scan. Returns a summary dict; `ok` is False when a stage aborted it (the stage is
    counted in errors_total and named in `errors`)."""
    now = time.time() if now is None else now
    metrics = METRICS if metrics is None else metrics
    summary = {"ok": False, "reruns": [], "skipped": {}, "candidates": {}, "collateral": [],
               "errors": [], "disabled": False}
    metrics.inc("scans_total")
    state = State(store)
    try:
        Scan(api, state, cfg, now, metrics, summary).run()
    except ApiError as e:
        metrics.inc("errors_total", {"stage": e.stage})
        summary["errors"].append("%s: %s" % (e.stage, e))
        log.error("scan aborted stage=%s error=%s", e.stage, e)
        return summary
    except Exception as e:  # noqa: BLE001 - the loop must survive, and the count must show it
        metrics.inc("errors_total", {"stage": "scan"})
        summary["errors"].append("scan: %s: %s" % (type(e).__name__, e))
        log.error("scan crashed: %s", traceback.format_exc())
        return summary
    summary["ok"] = True
    metrics.set("last_scan_timestamp_seconds", now)
    return summary


# --- http -----------------------------------------------------------------------------------
HEALTH = {"started": None, "last_ok": None}


class Handler(BaseHTTPRequestHandler):
    server_version = "ci-rerun-watchdog"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/healthz":
            last = METRICS.get("last_scan_timestamp_seconds")
            body = {"ok": True, "last_scan_age_seconds": (time.time() - last) if last else None}
            return self._send(200, json.dumps(body), "application/json")
        if path == "/metrics":
            return self._send(200, METRICS.render(), "text/plain; version=0.0.4; charset=utf-8")
        self._send(404, '{"error": "not found"}', "application/json")


def serve(port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, name="http", daemon=True)
    t.start()
    return srv


def main():
    logging.basicConfig(level=logging.INFO, format="[ci-rerun-watchdog] %(levelname)s %(message)s")
    cfg = Config.from_env()
    if not cfg.gitea_token:
        raise SystemExit("GITEA_TOKEN is empty")
    if not cfg.state_namespace:
        raise SystemExit("STATE_NAMESPACE is empty and the SA namespace file is unreadable")
    api = GiteaApi(UrllibTransport(cfg.gitea_url, cfg.gitea_token, cfg.http_timeout),
                   page_limit=cfg.page_limit, max_pages=cfg.max_pages)
    store = KubeState(cfg.state_namespace, cfg.state_configmap)
    HEALTH["started"] = time.time()
    serve(cfg.port)
    log.info("starting dry_run=%s org=%s repos=%s interval=%ss lookback=%ss port=%s",
             cfg.dry_run, cfg.gitea_org, ",".join(cfg.repos), cfg.scan_interval, cfg.lookback_seconds, cfg.port)
    while True:
        t0 = time.time()
        summary = scan(api, store, cfg)
        log.info("scan ok=%s reruns=%d candidates=%s skipped=%s errors=%s took=%.1fs",
                 summary["ok"], len(summary["reruns"]), summary["candidates"],
                 summary["skipped"], summary["errors"], time.time() - t0)
        time.sleep(max(1.0, cfg.scan_interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
