#!/usr/bin/env python3
"""gen-broker-inventory.py — ONE source of truth for the broker seat inventory.

The broker seat list used to be hand-maintained in four places that had to
agree with nothing forcing them to: the two `AFP_*` JSON maps on the control
-plane Deployment, `AF_PROVISIONER_BROKER_READYZ_URLS` on the provisioner, the
broker manifests, and the SOPS operator seeds file. Two production incidents
came out of that: a seat added to some places and not others left three broker
pods stuck for 21h (the enumeration that provisions their signing material did
not include the new seat), and a credential ceremony that wrote empty values
into one of these places crash-looped a control-plane worker.

THE SOURCE is the broker manifest set itself —
`kubernetes/apps/infrastructure/agentforge-broker/broker-*.yaml`. Every seat is
a Deployment whose `AF_BROKER_AUDIENCE` IS its identity, flanked by its
ClusterIP + headless Services. Deriving from those manifests (rather than from
a second hand-written list) means the generator cannot describe an estate that
does not exist: a seat exists exactly when its manifest does.

The one thing the manifests cannot state structurally is whether a seat belongs
in the capability-kid ACTIVATION BARRIER, which is an operational judgement
(the barrier is ALL-OR-NOTHING per kid — one unreachable broker defers the
signing-key commit for EVERY audience, which is the #127 outage). That is
declared per seat by the `agentforge.io/kid-barrier` annotation on the broker
Deployment. A seat missing the annotation is an ERROR, never a default.

DERIVED ARTEFACTS (this script owns these spans end to end):
  1. `AFP_BROKER_READYZ_URLS`             — apps/agentforge/deployment.yaml
  2. `AFP_SANDBOX_BROKER_URLS`            — apps/agentforge/deployment.yaml
  3. `AF_PROVISIONER_BROKER_READYZ_URLS`  — openbao/provisioner-deploy.yaml
  4. `AF_PROVISIONER_BROKER_READYZ_MAP`   — openbao/provisioner-deploy.yaml
  5. `broker-inventory.yaml`              — the generated inventory + the KV-GC
                                            seed-coverage record

(4) is the aud -> URL MAP the merged KV garbage collector requires
(agentforge #69) — distinct from (3), which is a comma-separated URL LIST and a
GATE, not a writer.

THE SEED-COVERAGE RECORD is the one part that is NOT derivable in CI. CI cannot
decrypt `openbao/operator-seeds.sops.yaml` — and must not be able to — so an
operator who can refreshes a KEY-NAMES-ONLY record of what that document
declares (`--refresh-seed-coverage`), and the record is then what CI checks. A
record can go stale silently, so the refresh also stamps
`seedCoverage.seedsDocumentSha256`: the SHA-256 of the ENCRYPTED document as it
sits on disk. `--check` re-hashes the same ciphertext (no decryption, nothing
secret in the hash) and fails when it differs from the stamp — because a seeds
document that moved after the record was taken is a document the record no
longer describes. Since agentforge #276/#277 the operator seeds are
create-if-absent, so this document's remaining job is to be what a wiped vault
restores from: its completeness IS the disaster-recovery guarantee.

USAGE
  python scripts/gen-broker-inventory.py            # check (default) — exit 1 on drift
  python scripts/gen-broker-inventory.py --write    # regenerate in place
  sops -d --extract '["stringData"]["seeds.json"]' \
      kubernetes/apps/infrastructure/security/openbao/operator-seeds.sops.yaml \
    | python scripts/gen-broker-inventory.py --refresh-seed-coverage

`--check` renders every derived artefact into memory and compares it byte for
byte with what is on disk, so the checker cannot disagree with the generator:
"the check passes" and "running the generator changes nothing" are the same
statement. Any parse failure is a FAILURE, never a skip.

Stdlib-only (no PyYAML), matching scripts/check-inline-hashes.py: the manifests
are read as text and the specific derived spans are located with targeted
scans, so this carries no runtime dependency beyond Python itself.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BROKER_DIR = REPO / "kubernetes/apps/infrastructure/agentforge-broker"
CP_DEPLOY = REPO / "kubernetes/apps/apps/agentforge/deployment.yaml"
PROVISIONER_DEPLOY = REPO / "kubernetes/apps/infrastructure/security/openbao/provisioner-deploy.yaml"
OPERATOR_SEEDS = REPO / "kubernetes/apps/infrastructure/security/openbao/operator-seeds.sops.yaml"
SEEDS_LABEL = OPERATOR_SEEDS.relative_to(REPO).as_posix()
INVENTORY = BROKER_DIR / "broker-inventory.yaml"

BROKER_NS = "agentforge-broker"
BROKER_PORT = 8700
BARRIER_ANNOTATION = "agentforge.io/kid-barrier"

#: The cluster's Service CIDR (`kube-apiserver --service-cluster-ip-range`).
#: Every pinned broker ClusterIP MUST fall inside it; see docs/network-plan.md.
SERVICE_CIDR = "10.96.0.0/12"
_SERVICE_CIDR_LO = (10 << 24) | (96 << 16)
_SERVICE_CIDR_HI = (10 << 24) | (111 << 16) | (255 << 8) | 255

#: The KV objects each seat owns under `af/data/`. `oauth` is the one the KV
#: garbage collector's coverage gate reads (agentforge kv_gc.BROKER_OAUTH_OBJECT).
CRED_OBJECTS = ("oauth", "kids", "ledger")
OAUTH_OBJECT = "oauth"

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SourceError(RuntimeError):
    """The declared source could not be read or is not self-consistent."""


# ---------------------------------------------------------------------------
# the source: the broker manifest set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Seat:
    """One broker seat, entirely derived from its own manifest."""

    aud: str
    provider: str
    account: str
    deployment: str
    service: str
    headless: str
    cluster_ip: str | None
    barrier: bool
    source: str

    @property
    def base_url(self) -> str:
        return f"http://{self.service}.{BROKER_NS}.svc.cluster.local:{BROKER_PORT}"

    @property
    def headless_url(self) -> str:
        return f"http://{self.headless}.{BROKER_NS}.svc.cluster.local:{BROKER_PORT}"

    def kv_path(self, obj: str) -> str:
        return f"operator/broker/{self.provider}/{self.account}/{obj}"


def _docs(text: str) -> list[str]:
    """Split a multi-document YAML file on its top-level `---` separators."""
    return re.split(r"(?m)^---[ \t]*$", text)


def _top_block(doc: str, key: str) -> str:
    """Return the lines of the top-level mapping `key` (excluding its own line)."""
    lines = doc.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            if re.match(rf"^{re.escape(key)}:[ \t]*$", line):
                inside = True
            continue
        if line.strip() and not line.startswith((" ", "\t")):
            break
        out.append(line)
    return "\n".join(out)


def _sub_block(block: str, key: str, indent: int) -> str:
    """Return the lines of the nested mapping `key` found at `indent` spaces."""
    lines = block.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            if re.match(rf"^[ ]{{{indent}}}{re.escape(key)}:[ \t]*$", line):
                inside = True
            continue
        stripped = line.strip()
        if stripped and (len(line) - len(line.lstrip(" "))) <= indent:
            break
        out.append(line)
    return "\n".join(out)


def _field(block: str, name: str, indent: int) -> str | None:
    m = re.search(rf"(?m)^[ ]{{{indent}}}{re.escape(name)}:[ \t]*(\S.*?)[ \t]*$", block)
    if not m:
        return None
    value = m.group(1)
    # strip a trailing YAML comment, then surrounding quotes
    value = re.sub(r"\s+#.*$", "", value).strip()
    return value.strip('"').strip("'")


def _kind(doc: str) -> str | None:
    m = re.search(r"(?m)^kind:[ \t]*(\S+)[ \t]*$", doc)
    return m.group(1) if m else None


def _name(doc: str) -> str | None:
    return _field(_top_block(doc, "metadata"), "name", 2)


def parse_seat(path: Path) -> Seat:
    """Derive one Seat from one `broker-*.yaml`, failing closed on anything odd."""
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(REPO).as_posix()
    deployments = [d for d in _docs(text) if _kind(d) == "Deployment"]
    if len(deployments) != 1:
        raise SourceError(f"{rel}: expected exactly 1 Deployment, found {len(deployments)}")
    deploy = deployments[0]

    name = _name(deploy)
    if not name:
        raise SourceError(f"{rel}: Deployment has no metadata.name")

    m = re.search(
        r"(?m)^[ ]*-[ ]*name:[ ]*AF_BROKER_AUDIENCE[ \t]*\n[ ]*value:[ ]*\"([^\"]+)\"",
        deploy,
    )
    if not m:
        raise SourceError(f"{rel}: no inline AF_BROKER_AUDIENCE value on the Deployment")
    aud = m.group(1)
    if aud.count("/") != 1:
        raise SourceError(f"{rel}: AF_BROKER_AUDIENCE {aud!r} is not '<provider>/<account>'")
    provider, account = aud.split("/")
    for part, label in ((provider, "provider"), (account, "account")):
        if not _SLUG.match(part):
            raise SourceError(f"{rel}: AF_BROKER_AUDIENCE {label} {part!r} is not a canonical slug")

    annotations = _sub_block(_top_block(deploy, "metadata"), "annotations", 2)
    raw_barrier = _field(annotations, BARRIER_ANNOTATION, 4)
    if raw_barrier is None:
        raise SourceError(
            f"{rel}: Deployment metadata.annotations is missing {BARRIER_ANNOTATION!r}. "
            "Barrier membership is an operational decision and has NO default: the barrier is "
            "all-or-nothing per kid, so an unreachable listed broker defers the signing-key "
            "commit for every audience. Declare \"true\" or \"false\" explicitly."
        )
    if raw_barrier not in ("true", "false"):
        raise SourceError(f"{rel}: {BARRIER_ANNOTATION} must be \"true\" or \"false\", got {raw_barrier!r}")

    services = {n: d for d in _docs(text) if _kind(d) == "Service" and (n := _name(d))}
    headless = f"{name}-headless"
    for expected in (name, headless):
        if expected not in services:
            raise SourceError(f"{rel}: no Service named {expected!r} (found {sorted(services)})")

    if _field(_top_block(services[headless], "spec"), "clusterIP", 2) != "None":
        raise SourceError(f"{rel}: Service {headless!r} must be headless (clusterIP: None)")
    cluster_ip = _field(_top_block(services[name], "spec"), "clusterIP", 2)
    if cluster_ip is not None:
        _require_in_service_cidr(rel, name, cluster_ip)

    return Seat(
        aud=aud,
        provider=provider,
        account=account,
        deployment=name,
        service=name,
        headless=headless,
        cluster_ip=cluster_ip,
        barrier=raw_barrier == "true",
        source=rel,
    )


def _require_in_service_cidr(rel: str, service: str, ip: str) -> None:
    octets = ip.split(".")
    if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
        raise SourceError(f"{rel}: Service {service!r} clusterIP {ip!r} is not an IPv4 address")
    packed = int(octets[0]) << 24 | int(octets[1]) << 16 | int(octets[2]) << 8 | int(octets[3])
    if not _SERVICE_CIDR_LO <= packed <= _SERVICE_CIDR_HI:
        raise SourceError(
            f"{rel}: Service {service!r} clusterIP {ip} is OUTSIDE the cluster Service CIDR "
            f"{SERVICE_CIDR} — the apiserver would refuse to allocate it"
        )


def load_seats() -> list[Seat]:
    # The generated inventory lives in the same directory and matches the same glob; it is an
    # OUTPUT, never a seat. Excluded by identity rather than by pattern so a real seat can never
    # be skipped by a naming coincidence.
    paths = sorted(p for p in BROKER_DIR.glob("broker-*.yaml") if p != INVENTORY)
    if not paths:
        raise SourceError(f"no broker-*.yaml under {BROKER_DIR} — refusing to derive an empty estate")
    seats = sorted((parse_seat(p) for p in paths), key=lambda s: s.aud)

    for field in ("aud", "deployment"):
        seen: dict[str, str] = {}
        for seat in seats:
            value = getattr(seat, field)
            if value in seen:
                raise SourceError(f"duplicate {field} {value!r} in {seen[value]} and {seat.source}")
            seen[value] = seat.source

    pinned: dict[str, str] = {}
    for seat in seats:
        if seat.cluster_ip is None:
            continue
        if seat.cluster_ip in pinned:
            raise SourceError(
                f"ClusterIP {seat.cluster_ip} is pinned by BOTH {pinned[seat.cluster_ip]} and "
                f"{seat.source} — a collision the apiserver will reject"
            )
        pinned[seat.cluster_ip] = seat.source

    if not any(s.barrier for s in seats):
        raise SourceError(
            "no seat is in the capability-kid barrier — an empty barrier silently disables the "
            "activation gate that proves a broker serves a public kid before its private half is "
            "committed. Refusing (fail closed)."
        )
    return seats


# ---------------------------------------------------------------------------
# derived artefact rendering
# ---------------------------------------------------------------------------


def _folded_json_map(pairs: list[tuple[str, str]], indent: int) -> str:
    """Render `{k:v, ...}` as the repo's `>-` folded JSON block.

    Continuation lines carry ONE extra space so they align under the opening
    brace. That makes them "more indented" than the block, so YAML keeps the
    newlines literally rather than folding them — harmless, since JSON ignores
    whitespace between tokens, and it is the shape already committed.
    """
    pad = " " * indent
    body = [f'{pad}{{{json.dumps(pairs[0][0])}:{json.dumps(pairs[0][1])},']
    for key, value in pairs[1:-1]:
        body.append(f"{pad} {json.dumps(key)}:{json.dumps(value)},")
    last_key, last_value = pairs[-1]
    body.append(f"{pad} {json.dumps(last_key)}:{json.dumps(last_value)}}}")
    if len(pairs) == 1:
        body = [f"{pad}{{{json.dumps(last_key)}:{json.dumps(last_value)}}}"]
    return "\n".join(body)


def _replace_env_span(text: str, path_label: str, var: str, new_span: str) -> str:
    """Replace the whole `env:` entry declaring `var` with `new_span`.

    Handles both shapes the repo uses: the flow-style one-liner
    `- { name: X, value: "..." }` and the block form `- name: X` + `value:`
    (with any continuation lines). Raises if the variable is absent — a
    derived value that has gone missing is drift, not a no-op.
    """
    lines = text.splitlines(keepends=True)
    flow = re.compile(rf"^([ ]*)-[ ]*\{{[ ]*name:[ ]*{re.escape(var)}[ ]*,.*\}}[ \t]*$")
    block = re.compile(rf"^([ ]*)-[ ]*name:[ ]*{re.escape(var)}[ \t]*$")

    for i, line in enumerate(lines):
        m = flow.match(line.rstrip("\n"))
        if m:
            return "".join(lines[:i]) + new_span + "\n" + "".join(lines[i + 1 :])
        m = block.match(line.rstrip("\n"))
        if m:
            dash_indent = len(m.group(1))
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() == "":
                    break
                indent = len(nxt) - len(nxt.lstrip(" "))
                # the next sibling list item, or a dedent out of this entry
                if indent <= dash_indent:
                    break
                j += 1
            return "".join(lines[:i]) + new_span + "\n" + "".join(lines[j:])

    raise SourceError(f"{path_label}: env var {var} not found — cannot verify a derived value that is absent")


def render_cp_deployment(seats: list[Seat]) -> str:
    text = CP_DEPLOY.read_text(encoding="utf-8")
    label = CP_DEPLOY.relative_to(REPO).as_posix()

    readyz = _folded_json_map([(s.aud, f"{s.base_url}/readyz") for s in seats], 16)
    sandbox = _folded_json_map([(s.aud, s.base_url) for s in seats], 16)

    text = _replace_env_span(
        text,
        label,
        "AFP_BROKER_READYZ_URLS",
        "            - name: AFP_BROKER_READYZ_URLS\n              value: >-\n" + readyz,
    )
    text = _replace_env_span(
        text,
        label,
        "AFP_SANDBOX_BROKER_URLS",
        "            - name: AFP_SANDBOX_BROKER_URLS\n              value: >-\n" + sandbox,
    )
    return text


def render_provisioner_deployment(seats: list[Seat]) -> str:
    text = PROVISIONER_DEPLOY.read_text(encoding="utf-8")
    label = PROVISIONER_DEPLOY.relative_to(REPO).as_posix()

    barrier = [s for s in seats if s.barrier]
    urls = ",".join(s.headless_url for s in barrier)
    readyz_map = _folded_json_map([(s.aud, s.headless_url) for s in seats], 16)

    text = _replace_env_span(
        text,
        label,
        "AF_PROVISIONER_BROKER_READYZ_URLS",
        f'            - {{ name: AF_PROVISIONER_BROKER_READYZ_URLS, value: "{urls}" }}',
    )
    text = _replace_env_span(
        text,
        label,
        "AF_PROVISIONER_BROKER_READYZ_MAP",
        "            - name: AF_PROVISIONER_BROKER_READYZ_MAP\n              value: >-\n" + readyz_map,
    )
    return text


def kv_gc_enabled() -> bool:
    """True when the provisioner mounts a seeds file, which switches the KV GC ON."""
    text = PROVISIONER_DEPLOY.read_text(encoding="utf-8")
    return re.search(r"(?m)^[^#\n]*AF_PROVISIONER_KV_GC_SEEDS_FILE", text) is not None


# ---------------------------------------------------------------------------
# the inventory artefact (+ the KV-GC seed-coverage record)
# ---------------------------------------------------------------------------


def seeds_document_sha256() -> str:
    """SHA-256 of the SOPS-encrypted operator seeds document, byte for byte off disk.

    Hashes CIPHERTEXT. Nothing is decrypted, so this runs anywhere — which is the
    whole point: it is the one property of the seeds document CI can compute for
    itself, and therefore the only thing that can tie the out-of-band coverage
    record to the document it claims to describe.
    """
    if not OPERATOR_SEEDS.exists():
        raise SourceError(
            f"{OPERATOR_SEEDS.relative_to(REPO).as_posix()} is missing — the seed-coverage record "
            "describes a document that is not in the tree. Refusing to report a coverage state "
            "derived from nothing (fail closed)."
        )
    return hashlib.sha256(OPERATOR_SEEDS.read_bytes()).hexdigest()


@dataclass(frozen=True)
class SeedRecord:
    """What the committed inventory RECORDS about the encrypted seeds document.

    Written only by `--refresh-seed-coverage`, from one read of the decrypted
    document. `--write` copies it forward verbatim: a generator that could
    re-stamp `document_sha256` on its own would let a changed document be
    re-blessed without anyone re-reading its contents, which is exactly the
    guarantee the stamp exists to provide.
    """

    recorded_at: str = ""
    #: SHA-256 of the ENCRYPTED seeds document at the moment the paths below were read.
    document_sha256: str = ""
    #: Every top-level logical KV path the document declares (NAMES ONLY).
    seed_paths: list[str] = field(default_factory=list)
    #: The subset of `seed_paths` that are broker oauth paths — what the KV-GC gate reads.
    oauth_paths: list[str] = field(default_factory=list)


def _read_path_list(text: str, key: str) -> list[str]:
    """Read one `  <key>:` list of scalars out of the committed seedCoverage block.

    STRICTLY the named block. Matching `operator/.../oauth` file-wide would also
    sweep up missingOauthPaths — which made the previously-missing paths read back
    as declared and the record oscillate between two states on successive runs.
    """
    block = re.search(rf"(?m)^[ ]{{2}}{re.escape(key)}:\n((?:[ ]{{4}}[-\[].*\n)*)", text)
    if not block:
        return []
    return [
        v.strip('"')
        for v in re.findall(r"(?m)^[ ]{4}-[ ]*(\S+)[ \t]*$", block.group(1))
    ]


def _read_seed_coverage() -> SeedRecord:
    """Recover the recorded seed-coverage block from the committed inventory.

    The record is refreshed ONLY by `--refresh-seed-coverage`, which reads the
    decrypted seeds document and keeps key NAMES only — never a value. A missing
    record is an error: the KV GC coverage gate cannot be evaluated from an
    absent inventory.
    """
    if not INVENTORY.exists():
        return SeedRecord()
    text = INVENTORY.read_text(encoding="utf-8")
    at = re.search(r"(?m)^[ ]*recordedAt:[ ]*\"([^\"]*)\"", text)
    digest = re.search(r"(?m)^[ ]*seedsDocumentSha256:[ ]*\"([^\"]*)\"", text)
    return SeedRecord(
        recorded_at=at.group(1) if at else "",
        document_sha256=digest.group(1) if digest else "",
        seed_paths=_read_path_list(text, "declaredSeedPaths"),
        oauth_paths=_read_path_list(text, "declaredOauthPaths"),
    )


def render_inventory(seats: list[Seat], record: SeedRecord) -> str:
    required = [s.kv_path(OAUTH_OBJECT) for s in seats]
    missing = [p for p in required if p not in set(record.oauth_paths)]
    ready = not missing

    out: list[str] = []
    add = out.append
    add("# GENERATED by scripts/gen-broker-inventory.py — DO NOT EDIT BY HAND.")
    add("#")
    add("# The broker seat inventory, derived from the broker-*.yaml manifests in this directory")
    add("# (each Deployment's AF_BROKER_AUDIENCE is the seat identity). This file is a REPORT, not")
    add("# an input: nothing reads it at runtime. It exists so the derived values scattered across")
    add("# the control-plane and provisioner Deployments have one reviewable place to disagree with,")
    add("# and so `.gitea/workflows/broker-inventory.yaml` can fail a PR that half-adds a seat.")
    add("#")
    add("# Regenerate:  python scripts/gen-broker-inventory.py --write")
    add("# Verify:      python scripts/gen-broker-inventory.py            # exits 1 on drift")
    add("#")
    add(f"# Broker ClusterIPs are allocated from the cluster Service CIDR {SERVICE_CIDR}")
    add("# (kube-apiserver --service-cluster-ip-range). See docs/network-plan.md before pinning a")
    add("# new one by hand — the apiserver refuses an address outside it, and a duplicate collides.")
    add("---")
    add(f"serviceCIDR: \"{SERVICE_CIDR}\"")
    add(f"namespace: {BROKER_NS}")
    add("seats:")
    for seat in seats:
        add(f"  - aud: \"{seat.aud}\"")
        add(f"    source: {seat.source}")
        add(f"    deployment: {seat.deployment}")
        add(f"    service: {seat.service}")
        add(f"    headless: {seat.headless}")
        add(f"    clusterIP: {seat.cluster_ip if seat.cluster_ip else 'null  # unpinned — allocated dynamically'}")
        add(f"    kidBarrier: {'true' if seat.barrier else 'false'}")
        add("    kvPaths:")
        for obj in CRED_OBJECTS:
            add(f"      - {seat.kv_path(obj)}")
    add("")
    add("# The KV garbage collector (agentforge #69) REFUSES TO START unless the mounted operator")
    add("# seeds file names every broker oauth path the estate declares. That refusal is fail-closed")
    add("# and correct, but it lands at provisioner startup; this record moves it into the PR.")
    add("#")
    add("# `declaredSeedPaths` / `declaredOauthPaths` are a KEY-NAME-ONLY record of the SOPS-encrypted")
    add("# operator seeds document named by `seedsDocument` below. CI cannot decrypt it — and must not")
    add("# be able to — so the record is refreshed out of band by an operator who can, and the check")
    add("# then verifies the record against the seats above:")
    add("#")
    add("#   sops -d --extract '[\"stringData\"][\"seeds.json\"]' \\")
    add(f"#     {SEEDS_LABEL} \\")
    add("#     | python scripts/gen-broker-inventory.py --refresh-seed-coverage")
    add("#")
    add("# NO VALUE is ever read, printed or stored — only the logical KV path names. Since")
    add("# agentforge #276/#277 the operator seeds are create-if-absent, so this document's remaining")
    add("# job is to be WHAT A WIPED VAULT RESTORES FROM: its completeness is the DR guarantee, and")
    add("# `declaredSeedPaths` is the plaintext shadow of it — a path dropped from the seeds shows up")
    add("# as a removed line here, in the very PR that drops it.")
    add("#")
    add("# `seedsDocumentSha256` is the SHA-256 of that file's CIPHERTEXT, exactly as it sits on disk.")
    add("# It is not a secret and reveals nothing about the plaintext; it is the one property of the")
    add("# document CI can compute for itself. Without it the record could go STALE IN SILENCE — the")
    add("# seeds could gain a path, lose one, or have a credential re-cut, and this record would keep")
    add("# asserting what it asserted before while `--check` stayed green. `--check` re-hashes the")
    add("# file and FAILS when it differs from the stamp; `--write` copies the stamp forward untouched,")
    add("# so only a real re-read of the document (the command above) can re-bless it.")
    add("#")
    add("# The paths above come from the SAME FILE the digest is taken from. Piping the live cluster")
    add("# Secret in instead (`kubectl get secret openbao-operator-seeds`) looks equivalent and is not:")
    add("# the two diverge whenever the working tree is behind or ahead of what Flux has reconciled, and")
    add("# the refresh would stamp one document while recording another's paths — wrong, and green. On a")
    add("# checkout one commit behind main that combination stamped a seeds document still carrying a")
    add("# REVOKED dispatcher PAT against paths read from the corrected live Secret. Same-file sourcing")
    add("# makes that divergence unrepresentable, so the recipe above is the only supported one.")
    add("seedCoverage:")
    add(f"  recordedAt: \"{record.recorded_at}\"")
    add(f"  seedsDocumentSha256: \"{record.document_sha256}\"")
    add(f"  seedsDocument: {SEEDS_LABEL}")
    add("  recordedForAuds:")
    for seat in seats:
        add(f"    - \"{seat.aud}\"")
    add("  declaredSeedPaths:")
    if record.seed_paths:
        for path in sorted(record.seed_paths):
            add(f"    - \"{path}\"")
    else:
        add("    []")
    add("  declaredOauthPaths:")
    if record.oauth_paths:
        for path in sorted(record.oauth_paths):
            add(f"    - {path}")
    else:
        add("    []")
    add("  missingOauthPaths:")
    if missing:
        for path in missing:
            add(f"    - {path}")
    else:
        add("    []")
    add(f"  kvGcReady: {'true' if ready else 'false'}")
    return "\n".join(out) + "\n"


def _recorded_for_auds() -> list[str]:
    if not INVENTORY.exists():
        return []
    text = INVENTORY.read_text(encoding="utf-8")
    m = re.search(r"(?m)^  recordedForAuds:\n((?:    - .*\n)*)", text)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def render_all(seats: list[Seat]) -> dict[Path, str]:
    return {
        CP_DEPLOY: render_cp_deployment(seats),
        PROVISIONER_DEPLOY: render_provisioner_deployment(seats),
        INVENTORY: render_inventory(seats, _read_seed_coverage()),
    }


def _coverage_gate(seats: list[Seat]) -> list[str]:
    """The seed-coverage assertions. Returns a list of failure messages."""
    failures: list[str] = []
    record = _read_seed_coverage()
    recorded_auds = _recorded_for_auds()
    current_auds = [s.aud for s in seats]

    if not INVENTORY.exists():
        return ["broker-inventory.yaml is missing — run --write"]

    if recorded_auds != current_auds:
        failures.append(
            "seedCoverage.recordedForAuds is STALE: recorded against "
            f"{recorded_auds} but the estate now declares {current_auds}. The seed-coverage "
            "record was taken against a different seat list, so it cannot be evidence that the "
            "current seats are seeded. Re-record it with --refresh-seed-coverage."
        )

    # Ties the record to the DOCUMENT, not just to the seat list. Hashes ciphertext, so it is
    # computable in CI, which cannot (and must not) decrypt anything.
    current_digest = seeds_document_sha256()
    if not record.document_sha256:
        failures.append(
            "seedCoverage.seedsDocumentSha256 is EMPTY: nothing ties this record to the seeds "
            f"document it describes ({SEEDS_LABEL}, which currently hashes to {current_digest}). "
            "An unstamped record cannot be evidence — the document may have gained or lost a path "
            "since it was taken and the record would read the same either way. Re-record it with "
            "--refresh-seed-coverage."
        )
    elif record.document_sha256 != current_digest:
        failures.append(
            "seedCoverage.seedsDocumentSha256 is STALE: recorded against "
            f"{record.document_sha256} but {SEEDS_LABEL} now hashes to {current_digest}. The seeds "
            "document CHANGED after the coverage record was taken — a path may have been dropped, "
            "added, or a credential re-cut — so the record is no longer evidence about the "
            "document. Re-record it with --refresh-seed-coverage."
        )

    missing = [
        s.kv_path(OAUTH_OBJECT) for s in seats if s.kv_path(OAUTH_OBJECT) not in set(record.oauth_paths)
    ]
    if kv_gc_enabled() and missing:
        failures.append(
            "the provisioner sets AF_PROVISIONER_KV_GC_SEEDS_FILE (the KV garbage collector is "
            "ON) but the operator seeds file does not declare "
            f"{', '.join(missing)} — the collector will refuse to start (agentforge #69 "
            "load_kv_gc_inputs). Seed those paths, or unset AF_PROVISIONER_KV_GC_SEEDS_FILE."
        )
    return failures


def cmd_check(seats: list[Seat]) -> int:
    ok = True
    for path, rendered in render_all(seats).items():
        rel = path.relative_to(REPO).as_posix()
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == rendered:
            print(f"OK {rel}")
            continue
        ok = False
        print(f"DRIFT {rel}")
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"{rel} (committed)",
            tofile=f"{rel} (derived from the broker manifests)",
        )
        sys.stdout.writelines(diff)

    rendered_failed = not ok
    coverage_failed = False
    for failure in _coverage_gate(seats):
        ok = False
        coverage_failed = True
        print(f"DRIFT seed-coverage: {failure}")

    if ok:
        print(f"\nOK — {len(seats)} seats, every derived artefact matches the broker manifests.")
        return 0

    # TWO failure classes, TWO remedies, and both are printed when both apply. `--write` re-renders
    # derived artefacts from the broker manifests; it deliberately CANNOT re-stamp the seed-coverage
    # record (that would let a changed seeds document be re-blessed without anyone re-reading it).
    # Printing only the --write remedy under a coverage failure sends the reader to a command that
    # cannot fix it and then reports success — the same misdirection agentforge #277 removed by
    # giving seed drift its own remedy text instead of filing it under permission refusals.
    print("\nFAIL")
    if rendered_failed:
        print("  * a derived value disagrees with the broker manifest set (the source).")
        print("    Run `python scripts/gen-broker-inventory.py --write` and review the diff.")
    if coverage_failed:
        print("  * the seed-coverage record no longer describes the seeds document.")
        print("    Re-record it (`just af-record-seed-coverage`); `--write` cannot re-stamp it.")
    return 1


def cmd_write(seats: list[Seat]) -> int:
    changed = False
    for path, rendered in render_all(seats).items():
        rel = path.relative_to(REPO).as_posix()
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == rendered:
            print(f"unchanged {rel}")
            continue
        path.write_text(rendered, encoding="utf-8", newline="")
        print(f"wrote     {rel}")
        changed = True
    for failure in _coverage_gate(seats):
        print(f"WARN seed-coverage: {failure}")
    if not changed:
        print("\nnothing to do — the tree already matches the broker manifests.")
    return 0


#: A logical KV path name is a bounded, printable, whitespace-free scalar. A key that is not one
#: would corrupt the generated YAML (or read back TRUNCATED at the first space, making the record
#: oscillate), and is far likelier to mean the wrong document is on stdin than to be a real path.
_SEED_PATH = re.compile(r"^[\x21-\x7e]{1,200}$")


def cmd_refresh_seed_coverage(seats: list[Seat], stdin_text: str, recorded_at: str) -> int:
    """Record the seeds file's KEY NAMES. Never reads, prints or stores a value."""
    raw = stdin_text.strip()
    if not raw:
        print("ERROR: no seeds JSON on stdin (fail closed — an empty read is not an empty file)")
        return 1
    try:
        doc = json.loads(raw)
    except ValueError:
        # NEVER surface the payload: it holds credential values.
        print("ERROR: stdin is not valid JSON")
        return 1
    if not isinstance(doc, dict):
        print("ERROR: the seeds document must be a JSON object")
        return 1

    # KEYS ONLY, from here down — doc[key] is never touched.
    seed_paths = sorted(doc)
    if not all(isinstance(k, str) and _SEED_PATH.match(k) for k in seed_paths):
        # Value-free on purpose: an offending key is not printed either, since a document this
        # shape is most likely not the seeds document at all.
        print("ERROR: a top-level seeds key is not a bounded, whitespace-free, printable path name")
        return 1
    oauth_paths = [k for k in seed_paths if re.fullmatch(r"operator/broker/[a-z0-9-]+/[a-z0-9-]+/oauth", k)]
    del doc

    # Stamped from the SAME act that read the paths above: the two are only evidence together.
    record = SeedRecord(
        recorded_at=recorded_at,
        document_sha256=seeds_document_sha256(),
        seed_paths=seed_paths,
        oauth_paths=oauth_paths,
    )
    INVENTORY.write_text(render_inventory(seats, record), encoding="utf-8", newline="")
    rel = INVENTORY.relative_to(REPO).as_posix()
    print(
        f"recorded {len(seed_paths)} seed path(s), {len(oauth_paths)} of them broker oauth, "
        f"into {rel} (key names only)"
    )
    print(f"  stamped  {SEEDS_LABEL} sha256={record.document_sha256}")
    for path in seed_paths:
        print(f"  declared {path}")
    missing = [s.kv_path(OAUTH_OBJECT) for s in seats if s.kv_path(OAUTH_OBJECT) not in set(oauth_paths)]
    for path in missing:
        print(f"  MISSING  {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify (default); exit 1 on drift")
    group.add_argument("--write", action="store_true", help="regenerate the derived artefacts")
    group.add_argument(
        "--refresh-seed-coverage",
        action="store_true",
        help="read the DECRYPTED seeds JSON on stdin and record its KEY NAMES + a ciphertext hash",
    )
    parser.add_argument(
        "--recorded-at",
        default="",
        help="date stamp for --refresh-seed-coverage (default: today, UTC)",
    )
    args = parser.parse_args()

    try:
        seats = load_seats()
        if args.write:
            return cmd_write(seats)
        if args.refresh_seed_coverage:
            # Default to TODAY, never to the previous stamp: `recordedAt` answers "when was this
            # evidence taken", and carrying the old date forward across a fresh read is the same
            # silent staleness `seedsDocumentSha256` exists to stop.
            today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            return cmd_refresh_seed_coverage(seats, sys.stdin.read(), args.recorded_at or today)
        return cmd_check(seats)
    except SourceError as exc:
        # An unreadable or self-inconsistent source is a FAILURE, never a skip:
        # a gate that cannot determine the answer must not report success.
        print(f"ERROR {exc}")
        return 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
