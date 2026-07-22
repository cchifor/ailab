#!/usr/bin/env python3
"""PREFLIGHT #2 — assert the host-mode Gitea act_runner CI pool is fit for AgentForge v2.

The AgentForge v2 activation (plans/2026-07-13-iac-activation-plan.md, Stage 0/4) builds+pushes its
bootstrap/workload images ON this pool (the user's host-mode override of the P2 "k8s-native CI runner"
proposal — ADR 0019; see docs/runbooks/ci-runners.md §8). This is a READ-ONLY activation gate: it never
mutates a VM, a registration, or local trust state.

    python scripts/check-ci-runners.py                 # probe the default pool (.14-.18) + Gitea API
    python scripts/check-ci-runners.py --skip-api       # host-side only (no GITEA_TOKEN needed)
    python scripts/check-ci-runners.py 192.168.0.14     # target one runner

Per host (SSH as `ubuntu`, key ~/.ssh/id_ed25519), asserts: the act_runner daemon is active; the `runner`
service account can reach Docker; the runner is registered host-mode (label `self-hosted-hv:host`) to the
Gitea instance; egress to registry.chifor.me (anonymous pull path) and git.chifor.me is healthy; and
capacity == 1. Then (unless --skip-api) queries the Gitea org runners API and asserts every expected
ci-runner-N reports status=online — the authoritative "Gitea sees a schedulable runner" signal that a
live daemon + a static .runner file cannot prove on their own.

GITEA_TOKEN (or AF_GITEA_TOKEN) with scope `read:admin,read:organization` (both — verified against the
deployed Gitea; a site-admin or org-owner token) is REQUIRED for the API check; it is kept local, never
placed in argv/SSH/logs, and redacted from all error output. Exit 0 = pool fit; exit 1 = any host or the
API check FAILED/unreachable (fail-closed).
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# ---- pool definition (single source of truth: inventory/hosts.yml + infra/runners/variables.tf) ----
DEFAULT_RUNNERS = [
    ("192.168.0.14", "ci-runner-1"),
    ("192.168.0.15", "ci-runner-2"),
    ("192.168.0.16", "ci-runner-3"),
    ("192.168.0.17", "ci-runner-4"),
    ("192.168.0.18", "ci-runner-5"),
]
KNOWN_BY_IP = dict(DEFAULT_RUNNERS)

# ---- invariants ----
# NB: the registry/gitea URLs are also hardcoded inline in PROBE (a static remote shell string that can't
# safely .format() around the `{{.Server.Version}}` braces); keep the two in sync by hand.
GITEA_URL = "https://git.chifor.me"
GITEA_ORG = "cchifor"
EXPECTED_LABEL = "self-hosted-hv:host"  # host-execution schema (NOT docker://)
EXPECTED_CAPACITY = 1
# git.chifor.me/api/v1/version needs sign-in (403) — any of these proves TLS+L7 egress; 000/3xx/5xx = FAIL
# (a 3xx redirect is NOT accepted: the finalized plan's status policy is exactly {200,401,403}).
GITEA_REACHABLE = {"200", "401", "403"}
REGISTRY_OK = "200"
REQUIRED_KEYS = ("daemon", "docker", "label", "address", "registry", "gitea", "capacity")

DUP = "__DUPLICATE__"  # sentinel: a required probe key emitted more than once (fail-closed)
SSH_USER = "ubuntu"
SSH_KEY = "~/.ssh/id_ed25519"
CONNECT_TIMEOUT = 12
EXEC_TIMEOUT = 30
HTTP_TIMEOUT = 12

# Single combined remote probe. Each subcheck emits its OWN `key=value` line, independently guarded so
# one failing command never blanks the others. Reads ONLY non-secret fields from .runner (label/address);
# the token/uuid are never read out. Docker is probed as the `runner` service account (the identity that
# actually runs jobs), not root.
PROBE = r"""
printf 'daemon=%s\n' "$(systemctl is-active gitea-act-runner.service 2>/dev/null || echo inactive)"
dv=$(sudo -n -u runner docker version --format '{{.Server.Version}}' 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -n "$dv" ]; then echo "docker=$dv"
elif echo "$dv" | grep -qi 'password is required'; then echo "docker=SUDO_DENIED"
elif echo "$dv" | grep -qi 'permission denied'; then echo "docker=SOCKET_DENIED"
else echo "docker=FAIL"; fi
sudo -n python3 -c 'import json
d=json.load(open("/home/runner/act-runner/.runner"))
ls=d.get("labels") if isinstance(d.get("labels"),list) else []
print("label="+",".join(str(x) for x in ls))
print("address="+str(d.get("address","")))' 2>/dev/null || { echo label=FAIL; echo address=FAIL; }
echo "registry=$(curl --connect-timeout 5 --max-time 8 -s -o /dev/null -w '%{http_code}' https://registry.chifor.me/v2/ 2>/dev/null || echo 000)"
echo "gitea=$(curl --connect-timeout 5 --max-time 8 -s -o /dev/null -w '%{http_code}' https://git.chifor.me/api/v1/version 2>/dev/null || echo 000)"
echo "capacity=$(sudo -n awk '/^[[:space:]]*capacity:/{print $2; exit}' /home/runner/act-runner/config.yaml 2>/dev/null || echo FAIL)"
"""


@dataclass
class HostResult:
    ok: bool
    failures: list = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass
class ApiResult:
    ok: bool
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# pure logic (no I/O — unit-tested in scripts/tests/test_check_ci_runners.py)
# ---------------------------------------------------------------------------
def parse_probe_output(text: str) -> dict:
    """Tolerant `key=value` parser. Blank/`=`-less lines are ignored; the value keeps embedded `=` and is
    stripped. A key seen more than once collapses to the DUP sentinel (fail-closed downstream)."""
    fields: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        # Any repeat of a key is ambiguous → DUP (fail-closed), even if the value is identical.
        fields[k] = DUP if k in fields else v
    return fields


def evaluate_host(fields: dict) -> HostResult:
    """Decide pool-fitness for one runner from its parsed probe fields. Pure."""
    failures = []

    for k in REQUIRED_KEYS:
        if k not in fields:
            failures.append(f"{k}: missing from probe output")
        elif fields[k] == DUP:
            failures.append(f"{k}: duplicated in probe output (ambiguous)")

    def present(k):
        return k in fields and fields[k] != DUP

    if present("daemon") and fields["daemon"] != "active":
        failures.append(f"daemon: gitea-act-runner.service is '{fields['daemon']}' (want active)")

    if present("docker"):
        dv = fields["docker"]
        if not re.match(r"^\d+\.\d+", dv):
            failures.append(f"docker: runner account cannot reach Docker ({dv or 'empty'})")

    if present("label"):
        labels = [x.strip() for x in fields["label"].split(",") if x.strip()]
        if EXPECTED_LABEL not in labels:
            failures.append(f"label: {labels or '[]'} lacks '{EXPECTED_LABEL}' (host-mode)")

    if present("address"):
        addr = fields["address"].rstrip("/")
        if addr != GITEA_URL:
            failures.append(f"address: registered to '{fields['address']}' (want {GITEA_URL})")

    if present("registry") and fields["registry"] != REGISTRY_OK:
        failures.append(f"registry: registry.chifor.me/v2/ -> {fields['registry']} (want {REGISTRY_OK})")

    if present("gitea") and fields["gitea"] not in GITEA_REACHABLE:
        failures.append(f"gitea: git.chifor.me -> {fields['gitea']} (unreachable/5xx)")

    if present("capacity"):
        try:
            cap = int(fields["capacity"])
        except (TypeError, ValueError):
            failures.append(f"capacity: unreadable ({fields['capacity']!r})")
        else:
            if cap != EXPECTED_CAPACITY:
                failures.append(f"capacity: {cap} (want {EXPECTED_CAPACITY})")

    return HostResult(ok=not failures, failures=failures, details=dict(fields))


def evaluate_api(runners, expected_names) -> ApiResult:
    """Assert every expected ci-runner-N is present + online in the Gitea org runners list. Pure.
    Unexpected extra registrations only warn (unrelated org runners must not fail the gate)."""
    failures = []
    warnings = []
    if not isinstance(runners, list):
        return ApiResult(ok=False, failures=["api: runners payload is not a list (schema mismatch)"])

    by_name = {}
    for r in runners:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not isinstance(r.get("status"), str):
            failures.append(f"api: malformed runner entry {str(r)[:60]!r} (name/status not strings)")
            continue
        nm = r["name"]
        if nm in by_name:  # duplicate name could mask an offline one depending on order → fail-closed
            failures.append(f"api: duplicate runner name {nm!r} in payload (ambiguous schema)")
            continue
        by_name[nm] = r["status"]

    if failures:  # a malformed/duplicate entry means we cannot trust the payload
        return ApiResult(ok=False, failures=failures, warnings=warnings)

    for name in sorted(expected_names):
        if name not in by_name:
            failures.append(f"api: expected runner {name} MISSING from Gitea")
        elif by_name[name] != "online":
            failures.append(f"api: {name} status={by_name[name]} (want online)")

    for name in sorted(by_name):
        if name not in expected_names:
            warnings.append(f"api: stale/unexpected runner {name} (status={by_name[name]}) — not gating")

    return ApiResult(ok=not failures, failures=failures, warnings=warnings)


# ---------------------------------------------------------------------------
# transport (I/O — not imported during tests)
# ---------------------------------------------------------------------------
def run_probe(host: str) -> str:
    import paramiko

    c = paramiko.SSHClient()
    # Repo convention for read-only LAN probes (node-ssh.py / check-nested-virt.py): AutoAddPolicy, and
    # the system known_hosts is intentionally NOT loaded — a legitimately rebuilt/renumbered runner VM's
    # changed key must not raise BadHostKey, and the user's known_hosts is never mutated (idempotent).
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = paramiko.Ed25519Key.from_private_key_file(os.path.expanduser(SSH_KEY))
    c.connect(
        host,
        username=SSH_USER,
        pkey=pkey,
        timeout=CONNECT_TIMEOUT,
        look_for_keys=False,
        allow_agent=False,  # key-only; no password fallback, no interactive prompt
    )
    try:
        _in, out, _err = c.exec_command(PROBE, timeout=EXEC_TIMEOUT)
        return out.read().decode(errors="replace")
    finally:
        c.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse ALL redirects so the Authorization header is never re-sent to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _validate_token(token: str) -> str:
    """A PAT is an opaque token; reject empty or whitespace/control-bearing values (they could break the
    header or smuggle content). The value is NEVER echoed in the error."""
    t = (token or "").strip()
    if not t or any(c.isspace() or ord(c) < 32 for c in t):
        raise RuntimeError("GITEA_TOKEN is empty or malformed")
    return t


def query_gitea_runners(token: str, page_size: int = 50, max_pages: int = 20) -> list:
    """GET the Gitea org runners list (paginated). Returns the combined `runners` array. The token rides
    only in the Authorization header (never argv/logs); redirects are refused (no header leak to another
    host); and EVERY error is re-raised as a fixed, token-free message (nothing verbatim reaches stdout)."""
    token = _validate_token(token)
    runners: list = []
    for page in range(1, max_pages + 1):
        url = f"{GITEA_URL}/api/v1/orgs/{GITEA_ORG}/actions/runners?page={page}&limit={page_size}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"token {token}", "Accept": "application/json"})
        try:
            with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Gitea API HTTP {e.code} (auth/scope?)") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Gitea API unreachable ({type(e).__name__})") from None
        except json.JSONDecodeError:
            raise RuntimeError("Gitea API returned non-JSON (redirect or error body?)") from None
        except Exception:  # catch-all: never let a message that might echo the request/header escape
            raise RuntimeError("Gitea API request failed") from None
        if not isinstance(data, dict) or not isinstance(data.get("runners"), list):
            raise RuntimeError("Gitea API payload missing 'runners' (schema mismatch)")
        batch = data["runners"]
        runners.extend(batch)
        if len(batch) < page_size:  # last (short) page
            break
    return runners


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def _reconfigure_streams():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv) -> int:
    _reconfigure_streams()
    skip_api = "--skip-api" in argv
    positional = [a for a in argv if not a.startswith("-")]

    if positional:
        targets = [(ip, KNOWN_BY_IP.get(ip, ip)) for ip in positional]
    else:
        targets = list(DEFAULT_RUNNERS)
    # The API online-check always gates the WHOLE pool (ci-runner-1..5), regardless of which hosts were
    # SSH-probed: positional IPs only narrow the host-side probe (a debug affordance) and must not be able
    # to shrink the schedulability gate to a subset — or to a vacuous 0/0 when an unknown IP is passed.
    expected_names = {name for _ip, name in DEFAULT_RUNNERS}

    ok = True
    for ip, name in targets:
        try:
            text = run_probe(ip)
        except Exception as exc:  # unreachable host = gate FAILURE, not a skip
            print(f"[FAIL] {name} {ip}: unreachable ({type(exc).__name__}: {exc})")
            ok = False
            continue
        result = evaluate_host(parse_probe_output(text))
        if result.ok:
            v = result.details
            print(f"[ OK ] {name} {ip}: daemon=active docker={v.get('docker')} "
                  f"label={EXPECTED_LABEL} registry={v.get('registry')} gitea={v.get('gitea')} "
                  f"capacity={v.get('capacity')}")
        else:
            print(f"[FAIL] {name} {ip}:")
            for f in result.failures:
                print(f"         - {f}")
            ok = False

    if skip_api:
        print("[WARN] Gitea API online-check SKIPPED (--skip-api) — host-side checks only; "
              "pool schedulability NOT verified.")
    else:
        token = os.environ.get("GITEA_TOKEN") or os.environ.get("AF_GITEA_TOKEN")
        if not token:
            print("[FAIL] Gitea API online-check: no GITEA_TOKEN/AF_GITEA_TOKEN in env "
                  "(scope read:admin,read:organization). Set one, or pass --skip-api to run host-side only.")
            ok = False
        else:
            try:
                runners = query_gitea_runners(token)
            except Exception as exc:
                print(f"[FAIL] Gitea API online-check: {exc}")  # exc is pre-sanitized (no token)
                ok = False
            else:
                api = evaluate_api(runners, expected_names)
                for w in api.warnings:
                    print(f"[WARN] {w}")
                if api.ok:
                    online = sorted(expected_names)
                    print(f"[ OK ] Gitea API: {len(online)}/{len(online)} expected runners online "
                          f"({', '.join(online)})")
                else:
                    print("[FAIL] Gitea API online-check:")
                    for f in api.failures:
                        print(f"         - {f}")
                    ok = False

    print("ci-runners preflight:", "PASS" if ok else "FAIL — host-mode CI pool NOT fit")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
