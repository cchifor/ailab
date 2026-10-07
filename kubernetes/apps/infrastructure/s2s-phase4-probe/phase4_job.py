#!/usr/bin/env python3
"""S2S Phase 4 probes as an in-cluster Job: every gatekeeper replica, from one pod, no exec.

The Job (kubernetes/apps/infrastructure/s2s-phase4-probe/) runs this file with stock python under
ServiceAccount s2s-phase4-probe. It is the `--gatekeeper-only` part of scripts/s2s/phase4-probes.sh
(the workstation script stays the fallback and the only home of the drills):

  1. the gatekeeper Service's Endpoints (`get endpoints/gatekeeper` in the GATEKEEPER namespace):
     every replica's pod IP; a roll in flight (any not-ready address) or fewer than
     --expect-replicas is a FAIL before a token exists;
  2. ConfigMap gatekeeper-registry-extras (`get configmaps/gatekeeper-registry-extras`, gatekeeper
     namespace): its data.registry.yaml is written byte for byte to --work-dir (the bytes the
     kubelet mounts into gatekeeper), so `registry` compares each replica's loaded extras_sha with
     the ConfigMap as it is now. Read through the API, not mounted: a pod cannot mount another
     namespace's ConfigMap, and gatekeeper is moving to its own namespace (A.2);
  3. three TokenRequests (`create serviceaccounts/token`, 600 s, no stored object) with this
     pod's own kube token: SA harness for strive-gatekeeper, SA harness for not-strive-gatekeeper,
     SA default for strive-gatekeeper -- the same three the workstation script mints;
  4. per replica, phase4_probe.main() in this process, against http://<pod IP>:<port>
     (main(pod_ip_ok=True); the ailab NetworkPolicy s2s-phase4-probe-to-gatekeeper admits this
     pod's labels from this namespace on that port);
  5. base_sha agreement across replicas, then a per-replica PASS/FAIL table.

TWO NAMESPACES: --namespace (strive-ailab) holds the harness and `default` ServiceAccounts, so the
TokenRequests go there; --gatekeeper-namespace (env GATEKEEPER_NAMESPACE, required, no default) is
where gatekeeper runs. The Job's manifests set it from one line
(s2s-phase4-probe/gatekeeper-ns/kustomization.yaml), which a test holds equal to GK_NS in
phase4-probes.sh.

TOKENS stay in this process's memory: never argv, the environment, a file, or the output. They
reach phase4_probe.main() through an in-memory stdin. Every printed line goes through redact(), and
a Kubernetes API error is reported by status code and Status.reason only, never its body.

Output: `S2S-PHASE4 ...` header lines, the probe's own lines prefixed `  [<pod>] `, a `SUMMARY`
table and a final `RESULT pass|fail`. Exit 0 only when every replica passed; 1 otherwise; 2 on a
bad invocation.
"""

import argparse
import hashlib
import io
import ipaddress
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase4_probe as probe  # noqa: E402  (the same directory, in the repo and in the pod)

NAMESPACE = "strive-ailab"
SERVICE = "gatekeeper"
EXTRAS_CM = "gatekeeper-registry-extras"
EXTRAS_KEY = "registry.yaml"
GK_PORT_NAME = "http"
HARNESS_SA = "harness"
ALT_SA = "default"
AUDIENCE = "strive-gatekeeper"
WRONG_AUDIENCE = "not-strive-gatekeeper"
# The API server's minimum; the probes finish in seconds and nothing is stored.
TOKEN_SECONDS = 600
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeError(Exception):
    """An API call that did not succeed. The message never carries a response body."""

    def __init__(self, message, status=None):
        super(KubeError, self).__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Kube(object):
    """The two API calls this Job makes, with the pod's own (kubelet-rotated) token."""

    def __init__(self, base_url, token_file, ca_file=None, timeout=15.0):
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.timeout = timeout
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if ca_file:
            try:
                context = ssl.create_default_context(cafile=ca_file)
            except (OSError, ssl.SSLError) as exc:
                raise KubeError("cannot load the cluster CA %s (%s)" % (ca_file, type(exc).__name__))
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def in_cluster(cls, sa_dir=SA_DIR, environ=None):
        env = os.environ if environ is None else environ
        host, port = env.get("KUBERNETES_SERVICE_HOST"), env.get("KUBERNETES_SERVICE_PORT")
        if not host or not port:
            raise KubeError("KUBERNETES_SERVICE_HOST/PORT unset: not in a pod")
        if ":" in host:
            host = "[%s]" % host
        return cls("https://%s:%s" % (host, port), os.path.join(sa_dir, "token"), os.path.join(sa_dir, "ca.crt"))

    def _call(self, method, path, doc=None):
        with open(self.token_file, encoding="utf-8") as f:
            bearer = f.read().strip()
        headers = {"authorization": "Bearer " + bearer, "accept": "application/json"}
        data = None
        if doc is not None:
            data = json.dumps(doc).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                status, body = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, (exc.read() if exc.fp is not None else b"")
        except (urllib.error.URLError, OSError) as exc:
            raise KubeError("%s %s: no answer (%s)" % (method, path, type(exc).__name__))
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if not 200 <= status < 300:
            reason = parsed.get("reason") if isinstance(parsed, dict) else None
            reason = reason if isinstance(reason, str) and reason.isalnum() else "-"
            raise KubeError("%s %s: HTTP %d reason=%s" % (method, path, status, reason), status)
        if not isinstance(parsed, dict):
            raise KubeError("%s %s: HTTP %d without a JSON object" % (method, path, status))
        return parsed

    def endpoints(self, namespace, name):
        return self._call("GET", "/api/v1/namespaces/%s/endpoints/%s" % (namespace, name))

    def configmap(self, namespace, name):
        return self._call("GET", "/api/v1/namespaces/%s/configmaps/%s" % (namespace, name))

    def token(self, namespace, sa, audience, seconds=TOKEN_SECONDS):
        doc = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenRequest",
            "spec": {"audiences": [audience], "expirationSeconds": seconds},
        }
        answer = self._call("POST", "/api/v1/namespaces/%s/serviceaccounts/%s/token" % (namespace, sa), doc)
        token = (answer.get("status") or {}).get("token") if isinstance(answer.get("status"), dict) else None
        if not isinstance(token, str) or not probe._TOKEN_SHAPE.match(token):
            raise KubeError("TokenRequest for %s (audience %s) did not return a JWT" % (sa, audience))
        return token


def replicas_from_endpoints(doc, port_name=GK_PORT_NAME):
    """([(pod, ip, port)], [problem]) from an Endpoints object.

    Ready addresses only are probed; any not-ready address is a roll in flight (a problem). The
    port is the subset's port named `port_name`, or its only port.
    """
    replicas, problems = [], []
    for subset in doc.get("subsets") or []:
        ports = [p for p in subset.get("ports") or [] if isinstance(p, dict)]
        named = [p for p in ports if p.get("name") == port_name]
        chosen = named[0] if named else (ports[0] if len(ports) == 1 else None)
        if chosen is None or not isinstance(chosen.get("port"), int):
            problems.append("endpoints: no usable port in a subset")
            continue
        for addr in subset.get("notReadyAddresses") or []:
            name = ((addr.get("targetRef") or {}).get("name")) or addr.get("ip")
            problems.append("endpoints: %s is not ready (a roll in flight? wait for it)" % name)
        for addr in subset.get("addresses") or []:
            ip = addr.get("ip")
            try:
                ipaddress.ip_address(ip or "")
            except ValueError:
                problems.append("endpoints: address %r is not an IP" % (ip,))
                continue
            name = (addr.get("targetRef") or {}).get("name") or ip
            replicas.append((name, ip, chosen["port"]))
    replicas.sort()
    return replicas, problems


def base_url(ip, port):
    host = "[%s]" % ip if ":" in ip else ip
    return "http://%s:%d" % (host, port)


def fetch_extras(kube, namespace, path, say):
    """ConfigMap gatekeeper-registry-extras' data.registry.yaml -> `path`, byte for byte.

    Absent (404, or no such key): no file is written and the probe's own `registry` check fails
    (nothing to compare; svc-harness has no extras). Any other API error is a problem returned.
    """
    if os.path.exists(path):
        os.remove(path)
    try:
        doc = kube.configmap(namespace, EXTRAS_CM)
    except KubeError as exc:
        if exc.status == 404:
            say("INFO configmap %s/%s is absent: no extras to compare" % (namespace, EXTRAS_CM))
            return None
        return "extras: %s" % exc
    data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    text = data.get(EXTRAS_KEY)
    if not isinstance(text, str):
        say("INFO configmap %s/%s has no %s" % (namespace, EXTRAS_CM, EXTRAS_KEY))
        return None
    raw = text.encode("utf-8")
    with open(path, "wb") as f:
        f.write(raw)
    say("S2S-PHASE4 extras configmap=%s/%s sha256=%s" % (namespace, EXTRAS_CM, hashlib.sha256(raw).hexdigest()))
    return None


def _registry_field(line, field):
    for part in line.split():
        if part.startswith(field + "="):
            return part[len(field) + 1 :]
    return "-"


def run(args, kube, out, http=probe.http_request):
    def say(text):
        out.write(probe.redact(text) + "\n")

    with open(probe.__file__, "rb") as f:
        program_sha = hashlib.sha256(f.read()).hexdigest()
    say(
        "S2S-PHASE4 namespace=%s gatekeeper_namespace=%s service=%s revision=%s program_sha256=%s"
        % (args.namespace, args.gatekeeper_namespace, SERVICE, args.revision or "-", program_sha)
    )
    failures = []

    try:
        replicas, problems = replicas_from_endpoints(kube.endpoints(args.gatekeeper_namespace, SERVICE))
    except KubeError as exc:
        replicas, problems = [], [str(exc)]
    if not problems and len(replicas) < args.expect_replicas:
        problems.append(
            "endpoints: %d ready gatekeeper replica(s), expected %d; every replica must be probed" % (len(replicas), args.expect_replicas)
        )
    for p in problems:
        say("FAIL " + p)
    failures += problems
    if replicas:
        say("S2S-PHASE4 replicas " + " ".join("%s=%s" % (n, base_url(ip, port)) for n, ip, port in replicas))

    extras_file = os.path.join(args.work_dir, EXTRAS_KEY)
    if not failures:
        problem = fetch_extras(kube, args.gatekeeper_namespace, extras_file, say)
        if problem:
            say("FAIL " + problem)
            failures.append(problem)

    tokens = {}
    if not failures:
        for key, sa, aud in (("harness", HARNESS_SA, AUDIENCE), ("wrong_aud", HARNESS_SA, WRONG_AUDIENCE), ("alt_sa", ALT_SA, AUDIENCE)):
            try:
                tokens[key] = kube.token(args.namespace, sa, aud)
                say("PASS token %s (TokenRequest sa=%s audience=%s %ds; never printed)" % (key, sa, aud, TOKEN_SECONDS))
            except KubeError as exc:
                say("FAIL token %s: %s" % (key, exc))
                failures.append("token %s" % key)

    results = []  # (pod, ip, verdict, failed check ids)
    registry = {}
    if not failures:
        stdin_text = "".join("%s=%s\n" % (k, tokens[k]) for k in ("harness", "wrong_aud", "alt_sa"))
        for name, ip, port in replicas:
            say("== replica %s %s" % (name, ip))
            buf = io.StringIO()
            argv = [
                "--mode", "probe",
                "--tenant", args.tenant,
                "--base-url", base_url(ip, port),
                "--extras-file", extras_file,
                "--own-token-file", "",
            ]
            if not args.registry_check:
                argv.append("--no-registry-check")
            rc = probe.main(argv, stdin=io.StringIO(stdin_text), out=buf, http=http, pod_ip_ok=True)
            failed, finished = [], False
            for line in buf.getvalue().splitlines():
                line = probe.redact(line)
                say("  [%s] %s" % (name, line))
                if line.startswith("FAIL "):
                    failed.append(line[5:].split(":", 1)[0].split(" ", 1)[0])
                elif line.startswith("REGISTRY "):
                    registry[name] = (_registry_field(line, "base_sha"), _registry_field(line, "extras_sha"))
                elif line == "RESULT pass":
                    finished = True
            if rc != 0 or not finished:
                failed = failed or ["exit %d" % rc]
            results.append((name, ip, "FAIL" if failed else "PASS", failed))
            if failed:
                failures.append(name)
        tokens.clear()
        stdin_text = None

        if args.registry_check and len(results) >= 2:
            bases = set(registry.get(n, ("-", "-"))[0] for n, _, _ in replicas)
            if len(bases) != 1 or "-" in bases:
                say("FAIL registry-agreement: base_sha missing or different across replicas (%s)" % ", ".join(sorted(bases)))
                failures.append("registry-agreement")
            else:
                say("PASS registry-agreement (base_sha %s on every replica)" % bases.pop())

    say("SUMMARY")
    say("  %-40s %-40s %-6s %s" % ("replica", "address", "result", "failed checks"))
    for name, ip, verdict, failed in results:
        say("  %-40s %-40s %-6s %s" % (name, ip, verdict, ", ".join(failed) or "-"))
    if not results:
        say("  (no replica probed: %s)" % "; ".join(failures))
    ok = not failures and bool(results)
    say("RESULT %s" % ("pass" if ok else "fail"))
    out.flush()
    return 0 if ok else 1


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="phase4_job.py", description="S2S Phase 4 probes of every gatekeeper replica, in-cluster")
    parser.add_argument("--namespace", default=NAMESPACE, help="where the harness and default ServiceAccounts live")
    parser.add_argument(
        "--gatekeeper-namespace",
        default=os.environ.get("GATEKEEPER_NAMESPACE", ""),
        help="where gatekeeper runs (env GATEKEEPER_NAMESPACE; required)",
    )
    parser.add_argument("--expect-replicas", type=int, default=2)
    parser.add_argument("--tenant", default=probe.DEFAULT_TENANT)
    parser.add_argument("--work-dir", default="/work", help="writable dir for the extras copy (an emptyDir)")
    parser.add_argument("--revision", default=os.environ.get("PROBE_REVISION", ""))
    parser.add_argument("--no-registry-check", dest="registry_check", action="store_false")
    return parser.parse_args(argv)


def main(argv=None, kube=None, out=None, http=probe.http_request):
    out = out or sys.stdout
    try:
        args = parse_args(argv)
    except SystemExit:
        return 2
    if not args.gatekeeper_namespace:
        out.write("ERROR --gatekeeper-namespace (env GATEKEEPER_NAMESPACE) is required\n")
        return 2
    try:
        kube = kube or Kube.in_cluster()
    except KubeError as exc:
        out.write("ERROR %s\n" % exc)
        return 2
    try:
        return run(args, kube, out, http)
    except Exception as exc:  # noqa: BLE001 - never a traceback: a message could carry request material
        out.write("FAIL internal: unexpected %s\nRESULT fail\n" % type(exc).__name__)
        out.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
