#!/usr/bin/env python3
"""S2S Phase 4 probes: the half that runs INSIDE one gatekeeper pod.

`scripts/s2s/phase4-probes.sh` ships this file into container `gatekeeper` of each gatekeeper
replica and runs it with the pod's own python:

    kubectl exec -i <pod> -c gatekeeper -- python -c "<bootstrap>" <args>

stdin line 1 is this file, base64-encoded (the bootstrap reads and executes it). Every further
line is `<name>=<token>` (names: harness, wrong_aud, alt_sa, held). Tokens therefore travel on
stdin only: never in argv, the environment, a file, or the output.

It drives gatekeeper over loopback (http://127.0.0.1:5000). The gatekeeper NetworkPolicy admits
only Traefik and the allowedClients, so in-pod loopback is how ONE replica is tested on its own.

The in-cluster Job (scripts/s2s/phase4_job.py, kubernetes/apps/infrastructure/s2s-phase4-probe/)
imports this module and calls main() with `pod_ip_ok=True`: it then drives ONE replica at its pod
IP (http://<private IP>:<port>, from the gatekeeper Endpoints), admitted by an ailab NetworkPolicy
for the probe pod only. That opening exists only through the Python call, never through argv, so
the exec path stays loopback-only. `--own-token-file ''` skips the OWNTOKEN line (the Job's own
token file is the probe's ServiceAccount, not gatekeeper's).

Output: one line per check (`PASS <id> ...` / `FAIL <id>: ...`), `INFO ...`, `REGISTRY ...` and
`OWNTOKEN ...` lines, and a final `RESULT pass|fail`. Exit 0 = every check passed, 1 = a check
failed, 2 = bad invocation. It never prints a token: the input tokens are never echoed, a 200's
access_token is decoded only for the claims asserted, and anything JWT-shaped in a printed
refusal is redacted.

Expected values come from gatekeeper at platform main ac123f047 (cited per check as
`infra/gatekeeper/src/app/gatekeeper/<file>:<line>`) and from the harness's request shape
(`services/harness/src/plugins/identity-gatekeeper/s2s.ts`, cited as `s2s.ts:<line>`).

Stdlib only. PyYAML is optional (the gatekeeper image has it: service_registry.py imports it); it
reads the mounted extras registry for the D3 policy, the expected scopes and any second k8s entry.
Without PyYAML (the Job's stock python image) parse_yaml_subset() reads it: JSON, or the
block-style YAML Helm's toYaml renders, and nothing else (anything outside that subset is a
ValueError, so the extras check FAILS rather than passing on a misread document).

Modes:
  probe    (default) the mandatory post-flip checks (plan Phase 4) plus the registry acceptance;
  refused  svc-harness must be REFUSED (cold start, deletion drill, rollback drill);
  single   one svc-mcp mint with the `held` token; prints `MINT <status> <error>` (revocation drill).
"""

import argparse
import base64
import hashlib
import ipaddress
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:5000"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# GC7: the gatekeeper chart mounts ConfigMap gatekeeper-registry-extras here (optional: true).
EXTRAS_FILE = "/etc/gatekeeper/registry-extras/registry.yaml"
# GC1: gatekeeper's own TokenReview bearer, rotated by the kubelet and re-read on every review.
OWN_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"

HARNESS_CLIENT = "svc-harness"
HARNESS_SUBJECT = "system:serviceaccount:strive-ailab:harness"
CC_AUDIENCE = "svc-mcp"
# D3 (ADR-034, restrict pre-flip): svc-harness may use client_credentials for svc-mcp only; every
# other audience is token_exchange only. The fallback list is used only when the mounted extras
# document cannot be read (no PyYAML); normally the audiences come from that document.
D3_CC_AUDIENCES = (CC_AUDIENCE,)
D3_TX_ONLY_AUDIENCES = (
    "svc-integration",
    "svc-airlock",
    "svc-workflow",
    "svc-knowledge",
    "svc-profile",
    "svc-notification",
    "svc-digest",
)
PRESHARED_CLIENT = "svc-deepagent"
UNREGISTERED_CLIENT = "svc-phase4-unregistered"
# service_verifier.py:57 (GENERIC_CLIENT_AUTH_MESSAGE); service_token.py:192-194 maps a
# ClientAuthError to 401 and service_token.py:93-96 renders the RFC 6749 body.
GENERIC_REFUSAL = {"error": "invalid_client", "error_description": "invalid client credentials"}
TENANT_CLAIM = "https://platform/tenant_id"  # internal_token.py:38
# charts/gatekeeper/values.yaml:154 sets INTERNAL_TOKEN_TTL_SECONDS=300; internal_token.py:184 clamps.
MAX_TTL_SECONDS = 300
GRANT_CLIENT_CREDENTIALS = "client_credentials"
DEFAULT_TENANT = "phase4-probe"

TOKEN_NAMES = ("harness", "wrong_aud", "alt_sa", "held")
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_JWT_SHAPED = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_ERROR_CODE = re.compile(r"^[a-z_]{1,64}$")


def redact(text):
    """Anything JWT-shaped in `text` replaced: printed output never carries a token."""
    return _JWT_SHAPED.sub("<redacted-jwt>", str(text))


def _b64url_json(segment):
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def jwt_claims(token):
    """The UNVERIFIED payload of a JWT, or None.

    Decoding only: the probe asserts what gatekeeper wrote into its own token; it does not
    authenticate it (the token never leaves this process).
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    try:
        claims = _b64url_json(parts[1])
    except (ValueError, UnicodeDecodeError):  # binascii.Error and JSONDecodeError are ValueErrors
        return None
    return claims if isinstance(claims, dict) else None


def fingerprint(token):
    """A correlatable, non-reversible tag for a token: 12 hex of its sha256."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def read_tokens(stream):
    """`<name>=<token>` lines from stdin. Errors name the line's role, never its value."""
    tokens = {}
    for raw in stream:
        line = raw.strip()
        if not line:
            continue
        name, sep, value = line.partition("=")
        if not sep or name not in TOKEN_NAMES:
            raise ValueError("stdin: expected <name>=<token> lines with names " + ", ".join(TOKEN_NAMES))
        if not _TOKEN_SHAPE.match(value):
            raise ValueError("stdin: the %s token is not a JWT" % name)
        tokens[name] = value
    return tokens


# ── HTTP (loopback only, no proxy, no redirect) ─────────────────────────────


class Response(object):
    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect is an answer to judge, never a hop to follow with a Bearer on it.
        return None


# ProxyHandler({}): an HTTP(S)_PROXY in the pod's environment must never see a token.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def http_request(method, url, data=None, headers=None, timeout=15.0):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers is not None else {}
        return Response(exc.code, headers, body)
    except (urllib.error.URLError, OSError) as exc:
        return Response(0, {}, type(exc).__name__.encode("ascii", "replace"))


def is_loopback_http(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS


def is_pod_ip_http(url):
    """Plain http to a private (pod network) IP literal, no path: one gatekeeper replica's address.

    Only the in-cluster Job may pass such a URL, and only through main(pod_ip_ok=True).
    """
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
        address = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and port is not None
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
        and parts.username is None
        and address.is_private
        and not address.is_unspecified
        and not address.is_multicast
    )


def token_form(client_id, audience, tenant):
    """The harness's client_credentials request body, field for field and in its order.

    s2s.ts:346-348 builds {grant_type, audience, tenant_id} (no `scope`: clientToken without
    scopes) and s2s.ts:300 appends client_id; token-file mode adds NO client_secret
    (s2s.ts:194 returns `fields: {}`).
    """
    return [
        ("grant_type", GRANT_CLIENT_CREDENTIALS),
        ("audience", audience),
        ("tenant_id", tenant),
        ("client_id", client_id),
    ]


def post_token(base_url, form, bearer=None, http=http_request):
    """POST /auth/token with the harness's headers (s2s.ts:297-301; the Bearer is s2s.ts:194)."""
    headers = {"accept": "application/json", "content-type": "application/x-www-form-urlencoded"}
    if bearer is not None:
        headers["authorization"] = "Bearer " + bearer
    data = urllib.parse.urlencode(form).encode("ascii")
    return http("POST", base_url + "/auth/token", data, headers)


def json_body(resp):
    try:
        doc = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return doc if isinstance(doc, dict) else None


def describe(resp):
    """A response, safe to print: status, error code and description. Never a 200's body."""
    if resp.status == 0:
        return "no HTTP answer (%s)" % redact(resp.body.decode("ascii", "replace"))
    if resp.status == 200:
        return "status 200"
    doc = json_body(resp)
    if doc is None:
        return "status %d with a non-JSON body" % resp.status
    text = "status %d error=%s description=%s" % (
        resp.status,
        redact(doc.get("error")),
        redact(doc.get("error_description")),
    )
    if resp.status == 503 and resp.headers.get("retry-after"):
        text += " retry-after=%s" % redact(resp.headers.get("retry-after"))
    return text


# ── Verdicts (pure: response in, list of problems out; empty = pass) ────────


def check_mint(resp, client_id, audience, tenant, expected_scopes=None):
    """A client_credentials mint for `client_id`: 200 and the claims service_token.py sets."""
    if resp.status != 200:
        return ["expected 200, got " + describe(resp)]
    doc = json_body(resp)
    if doc is None:
        return ["200 without a JSON object body"]
    claims = jwt_claims(doc.get("access_token"))
    if claims is None:
        return ["200 without a decodable access_token"]
    problems = []
    if doc.get("token_type") != "Bearer":  # service_token.py:141
        problems.append("token_type %r != 'Bearer'" % doc.get("token_type"))
    expires_in = doc.get("expires_in")  # service_token.py:286
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not 0 < expires_in <= MAX_TTL_SECONDS:
        problems.append("expires_in %r not in (0, %d]" % (expires_in, MAX_TTL_SECONDS))
    # sub = the client: service_token.py:410 (_synthetic_service_payload) -> internal_token.py:207.
    if claims.get("sub") != client_id:
        problems.append("sub %r != %r" % (claims.get("sub"), client_id))
    # azp = the client: service_token.py:419 -> internal_token.py:274-275.
    if claims.get("azp") != client_id:
        problems.append("azp %r != %r" % (claims.get("azp"), client_id))
    # The target service, for audit: service_token.py:420 -> internal_token.py:281-282.
    if claims.get("platform_target_service") != audience:
        problems.append("platform_target_service %r != %r" % (claims.get("platform_target_service"), audience))
    # The caller-chosen tenant: service_token.py:421 -> internal_token.py:239-252.
    if claims.get(TENANT_CLAIM) != tenant:
        problems.append("tenant claim %r != %r" % (claims.get(TENANT_CLAIM), tenant))
    # No actor chain on client_credentials: the payload has none (service_token.py:408-424) and
    # internal_token.py:276-277 copies `act` only when present.
    if "act" in claims:
        problems.append("unexpected act claim on a client_credentials token")
    # scope: the response (service_token.py:297) and the claim (:423 -> internal_token.py:261-262).
    scope = doc.get("scope")
    if not isinstance(scope, str) or not scope.split():
        problems.append("empty scope")
    elif claims.get("scope") != scope:
        problems.append("scope claim differs from the response scope")
    elif expected_scopes is not None and set(scope.split()) != set(expected_scopes):
        problems.append(
            "scope %r != the registry grant %r" % (" ".join(sorted(scope.split())), " ".join(sorted(expected_scopes)))
        )
    iat, exp = claims.get("iat"), claims.get("exp")
    ints = all(isinstance(v, int) and not isinstance(v, bool) for v in (iat, exp))
    if not ints or not 0 < exp - iat <= MAX_TTL_SECONDS:  # internal_token.py:184,208,210
        problems.append("exp - iat not in (0, %d]" % MAX_TTL_SECONDS)
    return problems


def check_refusal(resp, expected=GENERIC_REFUSAL):
    """A pre-authentication refusal: 401 with `expected` as the whole body.

    `expected=None` accepts any `invalid_client` 401 (refused mode: on a rollback the preshared
    backend answers `client_secret required`, service_verifier.py:152-153).
    """
    if resp.status != 401:
        return ["expected 401, got " + describe(resp)]
    doc = json_body(resp)
    if expected is None:
        if doc is None or doc.get("error") != "invalid_client":
            return ["expected error invalid_client, got " + describe(resp)]
        return []
    if doc != expected:
        return ["expected the generic body %s, got %s" % (json.dumps(expected, sort_keys=True), describe(resp))]
    return []


def grant_refused_body(client_id, audience):
    """service_token.py:115-128 (_grant_refused), reached at :246-247 before tenant or scope work."""
    return {
        "error": "unauthorized_client",
        "error_description": "client %r not allowed grant %r for audience %r"
        % (client_id, GRANT_CLIENT_CREDENTIALS, audience),
    }


def check_grant_refused(resp, client_id, audience):
    """D3: client_credentials for a token_exchange-only audience is a 403 naming the grant.

    The exact description tells this refusal apart from the audience-not-granted 403
    (service_token.py:203-208), so an audience missing from the grants cannot pass for D3.
    """
    if resp.status != 403:
        return ["expected 403, got " + describe(resp)]
    want = grant_refused_body(client_id, audience)
    if json_body(resp) != want:
        return ["expected %s, got %s" % (json.dumps(want, sort_keys=True), describe(resp))]
    return []


def check_identical(bodies):
    """Every pre-authentication refusal shares ONE body, so registry membership cannot be probed."""
    distinct = set(bodies)
    if len(distinct) > 1:
        return ["%d distinct refusal bodies across %d refusals" % (len(distinct), len(bodies))]
    return []


# ── Metrics (GC3) ───────────────────────────────────────────────────────────

_SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text):
    """Prometheus text exposition -> {(name, sorted label pairs): value}."""
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if not match:
            continue
        name, labels, value = match.groups()
        try:
            number = float(value)
        except ValueError:
            continue
        samples[(name, tuple(sorted(_LABEL.findall(labels or ""))))] = number
    return samples


def metric(samples, name, **labels):
    return samples.get((name, tuple(sorted(labels.items()))))


def series(samples, name):
    return [(dict(labels), value) for (n, labels), value in samples.items() if n == name]


def fetch_metrics(base_url, http=http_request):
    resp = http("GET", base_url + "/metrics", None, {"accept": "text/plain"})
    if resp.status != 200:
        return None
    try:
        return parse_metrics(resp.body.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def registry_line(samples, file_sha):
    infos = [labels for labels, value in series(samples or {}, "gatekeeper_service_registry_info") if value == 1]
    info = infos[0] if len(infos) == 1 else {}
    return "REGISTRY base_sha=%s extras_sha=%s file_sha=%s" % (
        info.get("base_sha", "-") or "-",
        info.get("extras_sha", "-") or "-",
        file_sha or "-",
    )


def check_registry(samples, file_sha, expect_extras):
    """The startup registry load of this replica (GC2/GC3)."""
    if samples is None:
        return ["GET /metrics failed"]
    infos = [labels for labels, value in series(samples, "gatekeeper_service_registry_info") if value == 1]
    if not expect_extras:
        if len(infos) == 1 and infos[0].get("extras_sha"):
            return ["extras are loaded (extras_sha=%s) but svc-harness is expected refused" % infos[0]["extras_sha"]]
        return []
    problems = []
    if len(infos) != 1:
        problems.append("expected one gatekeeper_service_registry_info series, found %d" % len(infos))
    elif not infos[0].get("extras_sha"):
        problems.append("no extras loaded (extras_sha is empty)")
    elif file_sha is None:
        problems.append("the mounted extras file is unreadable")
    elif infos[0]["extras_sha"] != file_sha:
        problems.append(
            "loaded extras_sha %s != mounted file %s: the ConfigMap changed after boot, roll gatekeeper"
            % (infos[0]["extras_sha"], file_sha)
        )
    if metric(samples, "gatekeeper_service_registry_extras_rejected") != 0:
        problems.append("extras_rejected=%s" % metric(samples, "gatekeeper_service_registry_extras_rejected"))
    refused = series(samples, "gatekeeper_service_registry_extras_refused_total")
    if not refused:
        problems.append("no gatekeeper_service_registry_extras_refused_total series")
    bad = sorted("%s=%g" % (labels.get("reason"), value) for labels, value in refused if value != 0)
    if bad:
        problems.append("extras entries refused: " + ", ".join(bad))
    return problems


def check_review_happened(before, after):
    """A fresh token's first mint is an uncached TokenReview on THIS replica.

    tokenreview_verifier.py:489 counts the positive-cache miss and :570 the authenticated review;
    each run mints a new token, so a replica that never reviewed it did not authenticate it.
    """
    if before is None or after is None:
        return ["GET /metrics failed around the mint"]
    problems = []
    for name, labels, what in (
        ("gatekeeper_tokenreview_cache_total", {"cache": "positive", "result": "miss"}, "positive-cache miss"),
        ("gatekeeper_tokenreview_total", {"outcome": "authenticated"}, "authenticated TokenReview"),
    ):
        b, a = metric(before, name, **labels), metric(after, name, **labels)
        if a is None or b is None:
            problems.append("metric %s missing" % name)
        elif a - b < 1:
            problems.append("no %s counted for this run's fresh token" % what)
    return problems


# ── The extras document (D3 policy, expected scopes, other k8s entries) ─────


def load_extras(path):
    """(document, sha256 of the file bytes). The document is None when unreadable or without PyYAML."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    sha = hashlib.sha256(data).hexdigest()
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is None:
        try:
            doc = parse_yaml_subset(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, sha
    else:
        try:
            doc = yaml.safe_load(data)
        except yaml.YAMLError:
            return None, sha
    return (doc if isinstance(doc, dict) else None), sha


# ── A strict stdlib reader for the extras document (no PyYAML) ──────────────

def _yaml_scalar(raw):
    """One scalar: '...' / "..." quoted, [] / {} empty, or plain. Plain stays a string."""
    raw = raw.strip()
    if raw in ("[]", "{}"):
        return [] if raw == "[]" else {}
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'") or "'" in raw[1:-1].replace("''", ""):
            raise ValueError("bad single-quoted scalar")
        return raw[1:-1].replace("''", "'")
    if raw.startswith('"'):
        value = json.loads(raw)  # ValueError on anything that is not one JSON string
        if not isinstance(value, str):
            raise ValueError("bad double-quoted scalar")
        return value
    if not raw or raw[0] in "[{&*!|>%@`#" or " #" in raw or raw.startswith("- ") or ": " in raw:
        raise ValueError("unsupported scalar")
    return raw


def parse_yaml_subset(text):
    """JSON, or block-style YAML of mappings, sequences and scalars, as Helm's toYaml renders it.

    A sequence under a key may sit at the key's own indentation (`key:` then `- item`). Comments
    are allowed on their own line only; flow collections, anchors, tags, multi-line and
    multi-document input are ValueErrors. Every scalar is a string: the extras checks compare
    strings only.
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    lines = []
    for raw in text.split("\n"):
        if "\t" in raw:
            raise ValueError("tab in YAML")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("---", "...") and lines:
            raise ValueError("more than one YAML document")
        if stripped == "---":
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))
    if not lines:
        return None
    value, pos = _yaml_block(lines, 0, lines[0][0])
    if pos != len(lines):
        raise ValueError("unexpected indentation at YAML line %d" % (pos + 1))
    return value


def _yaml_block(lines, pos, indent):
    if lines[pos][1] == "-" or lines[pos][1].startswith("- "):
        return _yaml_sequence(lines, pos, indent)
    return _yaml_mapping(lines, pos, indent, {})


def _yaml_value(lines, pos, indent, rest):
    """The value of `key:` (rest = text after the colon) whose key sits at `indent`."""
    if rest:
        return _yaml_scalar(rest), pos
    if pos < len(lines):
        child_indent, child = lines[pos]
        if child_indent > indent or (child_indent == indent and (child == "-" or child.startswith("- "))):
            return _yaml_block(lines, pos, child_indent)
    return None, pos


def _yaml_mapping(lines, pos, indent, mapping):
    while pos < len(lines) and lines[pos][0] == indent:
        text = lines[pos][1]
        if text == "-" or text.startswith("- "):
            break
        key, sep, rest = text.partition(":")
        if not sep or (rest and not rest.startswith(" ")):
            raise ValueError("expected `key: value` at YAML line %d" % (pos + 1))
        key = _yaml_scalar(key) if key[:1] in ("'", '"') else key
        if not isinstance(key, str) or not key or key in mapping:
            raise ValueError("bad or duplicate key at YAML line %d" % (pos + 1))
        value, pos = _yaml_value(lines, pos + 1, indent, rest.strip())
        mapping[key] = value
    if pos < len(lines) and lines[pos][0] > indent:
        raise ValueError("unexpected indentation at YAML line %d" % (pos + 1))
    return mapping, pos


def _yaml_sequence(lines, pos, indent):
    items = []
    while pos < len(lines) and lines[pos][0] == indent and (lines[pos][1] == "-" or lines[pos][1].startswith("- ")):
        rest = lines[pos][1][1:].strip()
        if not rest:
            value, pos = _yaml_value(lines, pos + 1, indent, "")
            if value is None:
                raise ValueError("empty sequence item at YAML line %d" % pos)  # pos: the item's own line + 1
            items.append(value)
            continue
        head, sep, tail = rest.partition(":")
        if sep and (not tail or tail.startswith(" ")) and rest[:1] not in ("'", '"'):
            # `- key: value` opens a mapping whose keys sit two columns in.
            inner = indent + 2
            lines[pos] = (inner, rest)
            value, pos = _yaml_mapping(lines, pos, inner, {})
            items.append(value)
        else:
            items.append(_yaml_scalar(rest))
            pos += 1
    if pos < len(lines) and lines[pos][0] > indent:
        raise ValueError("unexpected indentation at YAML line %d" % (pos + 1))
    return items, pos


def harness_policy(doc):
    """What the mounted extras grant svc-harness, checked against D3 and the identity boundary."""
    policy = {"problems": [], "tx_only": [], "scopes": None, "others": []}
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, list):
        policy["problems"].append("the extras document has no services list")
        return policy
    entries = [s for s in services if isinstance(s, dict)]
    policy["others"] = sorted(
        s["client_id"]
        for s in entries
        if s.get("auth_method") == "k8s" and isinstance(s.get("client_id"), str) and s["client_id"] != HARNESS_CLIENT
    )
    mine = [s for s in entries if s.get("client_id") == HARNESS_CLIENT]
    if len(mine) != 1:
        policy["problems"].append("the extras carry %d svc-harness entries, expected 1" % len(mine))
        return policy
    entry = mine[0]
    if entry.get("auth_method") != "k8s":
        policy["problems"].append("svc-harness auth_method %r != 'k8s'" % entry.get("auth_method"))
    if entry.get("k8s_subject") != HARNESS_SUBJECT:  # the identity boundary (ADR-034, D3)
        policy["problems"].append("svc-harness k8s_subject %r != %r" % (entry.get("k8s_subject"), HARNESS_SUBJECT))
    audiences = entry.get("audiences") if isinstance(entry.get("audiences"), dict) else {}
    open_cc = []
    for audience in sorted(audiences):
        cfg = audiences[audience] if isinstance(audiences[audience], dict) else {}
        grants = cfg.get("grant_types")
        # service_registry.py:166-174: no grant_types on an audience leaves both grants open.
        if grants is None or GRANT_CLIENT_CREDENTIALS in grants:
            open_cc.append(audience)
        else:
            policy["tx_only"].append(audience)
    if open_cc != list(D3_CC_AUDIENCES):
        policy["problems"].append(
            "D3: client_credentials is open for %s, expected only %s" % (", ".join(open_cc) or "none", CC_AUDIENCE)
        )
    cc_cfg = audiences.get(CC_AUDIENCE)
    if isinstance(cc_cfg, dict) and isinstance(cc_cfg.get("scopes"), list):
        policy["scopes"] = [str(s) for s in cc_cfg["scopes"]]
    return policy


# ── Report and modes ────────────────────────────────────────────────────────


class Report(object):
    def __init__(self, out):
        self.out = out
        self.failed = False

    def check(self, check_id, problems, detail=""):
        if problems:
            self.failed = True
            self.line("FAIL %s: %s" % (check_id, "; ".join(redact(p) for p in problems)))
        else:
            self.line("PASS %s%s" % (check_id, (" (%s)" % detail) if detail else ""))

    def info(self, text):
        self.line("INFO " + redact(text))

    def line(self, text):
        self.out.write(text + "\n")


def own_token_line(path, report):
    """Gatekeeper's own reviewer token, as a fingerprint (rotation drill). Never the token."""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except (OSError, UnicodeDecodeError):
        report.info("gatekeeper's own token file is unreadable")
        return
    claims = jwt_claims(token) or {}
    report.line("OWNTOKEN fp=%s iat=%s exp=%s" % (fingerprint(token), claims.get("iat"), claims.get("exp")))


def _require(tokens, names, report):
    missing = [n for n in names if n not in tokens]
    if missing:
        report.check("stdin", ["missing token(s): " + ", ".join(missing)])
        return False
    return True


def _refusal_cases(others):
    """(check id, client_id, token name or None) for every mandatory refusal (plan Phase 4)."""
    cases = [
        # A preshared entry with the harness Bearer and no secret: service_verifier.py:541-542
        # (the composite never tries the Bearer on a preshared entry: no fallback).
        ("refuse-preshared[%s]" % PRESHARED_CLIENT, PRESHARED_CLIENT, "harness"),
        # An unregistered client_id with the harness token: service_verifier.py:528-530.
        ("refuse-unregistered[%s]" % UNREGISTERED_CLIENT, UNREGISTERED_CLIENT, "harness"),
    ]
    if others:
        # A second k8s entry claimed with the harness token: the sub precheck,
        # tokenreview_verifier.py:177-178 -> :471-475.
        cases.append(("refuse-second-k8s[%s]" % others[0], others[0], "harness"))
    cases += [
        # Another ServiceAccount's strive-gatekeeper token claiming svc-harness: the same sub
        # precheck from the other side (the substitute while svc-harness is the only k8s entry).
        ("refuse-other-sa", HARNESS_CLIENT, "alt_sa"),
        # The harness's own ServiceAccount with the wrong audience: tokenreview_verifier.py:173-176.
        ("refuse-wrong-audience", HARNESS_CLIENT, "wrong_aud"),
        # No token at all for a k8s entry: service_verifier.py:532-534.
        ("refuse-no-token", HARNESS_CLIENT, None),
    ]
    return cases


def _run_refusals(args, tokens, cases, report, http, expected, detail):
    """Each case must be refused with `expected`; then every refusal body must be the same."""
    bodies = []
    for check_id, client_id, token_name in cases:
        if token_name is not None and token_name not in tokens:
            report.info("%s skipped: no %s token on stdin" % (check_id, token_name))
            continue
        bearer = tokens[token_name] if token_name is not None else None
        resp = post_token(args.base_url, token_form(client_id, CC_AUDIENCE, args.tenant), bearer, http)
        report.check(check_id, check_refusal(resp, expected), detail)
        bodies.append(resp.body)
    report.check("refusals-identical", check_identical(bodies), "one body for every refusal")
    return bodies


def run_probe(args, tokens, report, http):
    if not _require(tokens, ("harness", "wrong_aud", "alt_sa"), report):
        return
    base = args.base_url
    if args.own_token_file:
        own_token_line(args.own_token_file, report)
    doc, file_sha = load_extras(args.extras_file)
    samples = fetch_metrics(base, http)
    report.line(registry_line(samples, file_sha))
    if args.registry_check:
        report.check(
            "registry",
            check_registry(samples, file_sha, expect_extras=True),
            "extras loaded = mounted file, extras_rejected 0, every extras_refused_total 0",
        )

    if doc is not None:
        policy = harness_policy(doc)
        report.check("d3-policy", policy["problems"], "client_credentials only for svc-mcp; subject " + HARNESS_SUBJECT)
        tx_only, scopes, others = policy["tx_only"], policy["scopes"], policy["others"]
    else:
        if file_sha is not None:
            # The file is there but did not parse: never fall back silently on a misread registry.
            report.check("d3-policy", ["the mounted extras file %s does not parse as the extras document" % args.extras_file])
        report.info("extras document unreadable here: the built-in D3 audience list is used, scopes are not compared")
        tx_only, scopes, others = list(D3_TX_ONLY_AUDIENCES), None, []
    if not others:
        report.info(
            "svc-harness is the only k8s entry: refuse-unregistered and refuse-other-sa stand in for "
            "'a second k8s entry claimed with the harness token'"
        )

    resp = post_token(base, token_form(HARNESS_CLIENT, CC_AUDIENCE, args.tenant), tokens["harness"], http)
    problems = check_mint(resp, HARNESS_CLIENT, CC_AUDIENCE, args.tenant, scopes)
    if not problems:
        problems = check_review_happened(samples, fetch_metrics(base, http))
    report.check("k8s-mint[%s]" % CC_AUDIENCE, problems, "200; sub=azp=svc-harness, tenant, no act, ttl<=300; a fresh TokenReview")

    for audience in tx_only:
        resp = post_token(base, token_form(HARNESS_CLIENT, audience, args.tenant), tokens["harness"], http)
        report.check(
            "d3-refused[%s]" % audience,
            check_grant_refused(resp, HARNESS_CLIENT, audience),
            "403 unauthorized_client for client_credentials",
        )

    _run_refusals(args, tokens, _refusal_cases(others), report, http, GENERIC_REFUSAL, "401, the generic body")


def run_refused(args, tokens, report, http):
    """svc-harness must be refused here: extras absent (cold start / deletion) or rolled back.

    Only `alt_sa` is required: after a rollback the harness ServiceAccount is gone, and with it
    every pod that could hold svc-harness; its cases are then skipped.
    """
    if not _require(tokens, ("alt_sa",), report):
        return
    if args.own_token_file:
        own_token_line(args.own_token_file, report)
    _, file_sha = load_extras(args.extras_file)
    samples = fetch_metrics(args.base_url, http)
    report.line(registry_line(samples, file_sha))
    if samples is not None and series(samples, "gatekeeper_service_registry_info"):
        report.check("registry-no-extras", check_registry(samples, file_sha, expect_extras=False), "extras_sha empty")
    else:
        report.info("no registry metrics on this replica (an image older than Phase 2a?)")

    cases = [("harness-refused[%s]" % CC_AUDIENCE, HARNESS_CLIENT, "harness")] + _refusal_cases([])
    bodies = _run_refusals(args, tokens, cases, report, http, None, "401 invalid_client")
    if bodies:
        report.info("refusal body: " + describe(Response(401, {}, bodies[0])))


def run_single(args, tokens, report, http):
    """One svc-mcp mint with the `held` token; the caller judges the status (revocation drill)."""
    if not _require(tokens, ("held",), report):
        return
    resp = post_token(args.base_url, token_form(HARNESS_CLIENT, CC_AUDIENCE, args.tenant), tokens["held"], http)
    doc = json_body(resp) if resp.status != 200 else None
    code = doc.get("error") if doc else None
    code = code if isinstance(code, str) and _ERROR_CODE.match(code) else "-"
    report.line("MINT %d %s" % (resp.status, code))
    if resp.status == 0:
        report.check("single-mint", ["no HTTP answer from gatekeeper"])


MODES = {"probe": run_probe, "refused": run_refused, "single": run_single}


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="phase4_probe.py", description="S2S Phase 4 probes, run inside one gatekeeper pod")
    parser.add_argument("--mode", choices=sorted(MODES), default="probe")
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--extras-file", default=EXTRAS_FILE)
    parser.add_argument("--own-token-file", default=OWN_TOKEN_FILE)
    parser.add_argument("--no-registry-check", dest="registry_check", action="store_false")
    return parser.parse_args(argv)


def main(argv=None, stdin=None, out=None, http=http_request, pod_ip_ok=False):
    """`pod_ip_ok` (the in-cluster Job only, never argv) also admits one replica's pod IP URL."""
    out = out or sys.stdout
    args = parse_args(argv)
    if not (is_loopback_http(args.base_url) or (pod_ip_ok and is_pod_ip_http(args.base_url))):
        if pod_ip_ok:
            out.write("ERROR --base-url must be plain http on loopback or a private pod IP\n")
        else:
            out.write("ERROR --base-url must be plain http on loopback: tokens never leave the pod\n")
        return 2
    try:
        tokens = read_tokens(stdin if stdin is not None else sys.stdin)
    except ValueError as exc:
        out.write("ERROR %s\n" % exc)
        return 2
    report = Report(out)
    try:
        MODES[args.mode](args, tokens, report, http)
    except Exception as exc:  # noqa: BLE001 - never a traceback: a message could carry request material
        report.check("internal", ["unexpected %s" % type(exc).__name__])
    report.line("RESULT %s" % ("fail" if report.failed else "pass"))
    out.flush()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
