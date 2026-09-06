#!/usr/bin/env python3
"""Evaluate the BrokerSeat ValidatingAdmissionPolicies' CEL against an admit/deny fixture table.

WHY THIS EXISTS. `kubernetes/apps/infrastructure/agentforge-broker/brokerseat-admission.yaml` holds the
two admission guards behind brokerseat-rbac.yaml: `agentforge-cp-brokerseat-guard` (the control plane
may only NAME a seat — provider/account/clusterIP — never own, label, finalize or status it) and
`agentforge-provisioner-seat-objects-guard` (everything the provisioner writes is a seat-shaped object
owned by its BrokerSeat, never a Flux one, structurally bound to what the seat template renders).
Every rule is a CEL expression the apiserver compiles at admission time and NOWHERE else — a client
dry-run parses the YAML and never evaluates it; a server dry-run only proves it COMPILES. This harness
answers the question that matters when that file is edited: *does clause N still admit the shape the
controller renders, and does it still deny the shapes it was written to deny?*

It is scripts/check-tenant-guard-cel.py's evaluator GENERALISED (that one is hard-wired to
tenant-guard.yaml and its CP PodMonitor render; it is left byte-for-byte untouched):
  * `--policy` may hold SEVERAL ValidatingAdmissionPolicy documents; each case names the one it targets.
  * the admission REQUEST is modelled, not just the object: operation (CREATE/UPDATE), userInfo.username,
    namespace, kind -> resource (subresources included), oldObject on UPDATE. The policy's
    matchConstraints.resourceRules and matchConditions are evaluated first, so a case can also assert
    that a policy does NOT fire ("skip") — the SA gate is a property the table pins, not an assumption.
  * `spec.variables` are evaluated LAZILY and memoised, as the apiserver does: only the variables an
    expression references (transitively) are computed, so a Deployment-only variable never errors on a
    Service. An expression error is a DENY by that clause (failurePolicy: Fail).
  * `--fixtures <json>` replaces the built-in table with an external one in the format `--dump-fixtures`
    writes, so the same cases can be replayed elsewhere (the kind-based pre-flip check, plan F18).

THE BASELINE is not hand-typed: it is the NEWEST Flux-managed git seat whose stem is already the
mechanical broker-<provider>-<account> (broker-anthropic-claude-max-3.yaml today), loaded from the
tree, RE-KEYED to a hypothetical controller account (`<account>-x`: the same stem/aud substitution
the renderer performs, so every name and OpenBao path follows — a git seat's own audience is exactly
what validation 13 refuses) and stamped exactly as the controller's render_seat_objects will stamp
its 8 objects (agentforge.io/broker-seat=<stem>, ONE controller+blockOwnerDeletion ownerReference to
BrokerSeat <stem>, the render-digest annotation) with the pinned Service's clusterIP set. The git
seats and the CP's broker templates agree byte-for-byte (the CP's golden suite pins it), so this is
the closest in-repo artefact to what the provisioner will submit; if the template shape ever changes,
the seat files change with it and THIS table says whether the guard still admits the new shape. Every
deny case is that baseline with ONE property changed, and pins WHICH clause denies it (not just
"denied"), so a case cannot pass because some other clause happened to reject the fixture. The
un-re-keyed objects (the git seat under its own stem) are the fixtures for validation 13.

FAIL CLOSED on what it cannot model: a policy with matchConstraints.namespaceSelector /
objectSelector / excludeResourceRules / matchPolicy, or a binding with matchResources / paramRef /
validationActions other than [Deny], is refused outright (exit 1) rather than evaluated as if those
narrowings were absent — a selector could otherwise disable enforcement while this table stays green.
resourceRules ARE modelled (apiGroups, apiVersions, resources incl. subresources, operations).

WHAT IT IS NOT. A fidelity approximation, not the apiserver: cel-python 0.5.0 (the `_==_`/`_!=_`
null overloads and the strings-extension `split` are re-supplied below, as in the tenant harness);
no CRD structural-schema validation (an out-of-enum field, a two-source volume) and no defaulting
(`type: ClusterIP`, `protocol: TCP`) — cases that must hold in production spell defaulted values out.
Two apiserver steps that run BEFORE validating admission are not modelled either, and each removed
a fixture when the table was replayed against a real 1.31.4 apiserver (kind, 2026-09-06): a CREATE's
`status` is stripped by PrepareForCreate (so the CP guard's `!has(object.status)` can only ever
bite on the /status subresource — the only status fixture is that one), and a Service's clusterIP
is ALLOCATED by the registry before the policy sees the object (so an "unpinned" pinned Service
reaches the VAP already carrying an in-CIDR address — pinning is the CRD's `spec.clusterIP`
contract, the Service clause pins the SHAPE: None for -headless, in-CIDR for <stem>). Likewise a
request whose namespace differs from its object's is a 400 before admission, so validation 1 is
exercised only with request == object namespace. And a field behind a disabled feature gate is
DROPPED before admission (1.31 default: `procMount` without ProcMountType), so `dep-ctr-procmount-
unmasked` denies here but admits — harmlessly, the field is gone — on such a cluster; the predicate
stays for clusters where the gate exists.

USAGE:  python scripts/check-seat-guard-cel.py            (run in .gitea/workflows/tenant-guard-cel.yaml)
        python scripts/check-seat-guard-cel.py -v         list every case + its verdict
        python scripts/check-seat-guard-cel.py --dump-fixtures out.json
        python scripts/check-seat-guard-cel.py --policy <candidate.yaml> [--fixtures cases.json]
Exit 0 = every case matched its expectation. Exit 1 = a mismatch (the table names which).
"""

from __future__ import annotations

import argparse
import copy
import json
import operator
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

try:
    import celpy
    from celpy import celtypes
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "check-seat-guard-cel.py needs cel-python (written against 0.5.0): "
        "pip install 'cel-python==0.5.0'"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
BROKER_DIR = REPO_ROOT / "kubernetes/apps/infrastructure/agentforge-broker"
POLICY = BROKER_DIR / "brokerseat-admission.yaml"
INVENTORY = BROKER_DIR / "broker-inventory.yaml"

CP_GUARD = "agentforge-cp-brokerseat-guard"
OBJECTS_GUARD = "agentforge-provisioner-seat-objects-guard"
CP_SA = "system:serviceaccount:agentforge:agentforge-platform"
PROVISIONER_SA = "system:serviceaccount:openbao:agentforge-provisioner"
FLUX_SA = "system:serviceaccount:flux-system:kustomize-controller"
BROKER_NS = "agentforge-broker"

# kind -> (group, version, plural) for every kind the two policies can be asked about.
RESOURCES: dict[str, tuple[str, str, str]] = {
    "BrokerSeat": ("agentforge.io", "v1alpha1", "brokerseats"),
    "Deployment": ("apps", "v1", "deployments"),
    "Service": ("", "v1", "services"),
    "PodDisruptionBudget": ("policy", "v1", "poddisruptionbudgets"),
    "ExternalSecret": ("external-secrets.io", "v1", "externalsecrets"),
    "CiliumNetworkPolicy": ("cilium.io", "v2", "ciliumnetworkpolicies"),
}

# A fixed owner uid: the harness never talks to an apiserver, so the BrokerSeat's uid is whatever the
# fixture says it is. UPDATE cases that must deny a re-parented object use ANOTHER_UID.
OWNER_UID = "6f1c2a3e-5ea7-4b0b-9c1d-000000000c0de"
ANOTHER_UID = "0b1d0b1d-0000-4000-8000-0000deadbeef"
DIGEST = "sha256:" + "0" * 64
PINNED_IP = "10.96.0.200"
OTHER_STEM = "broker-anthropic-other"  # a hypothetical OTHER controller seat
GIT_STEM = "broker-anthropic-max1"  # a hand-named Flux-managed git seat (KNOWN_STEMS) ...
GIT_ALIAS = "broker-anthropic-claude-max-1"  # ... and the mechanical alias of its audience anthropic/claude-max-1
UNSUPPORTED_MATCH = ("namespaceSelector", "objectSelector", "excludeResourceRules", "matchPolicy")

SKIP = "skip"


@dataclass
class Case:
    name: str
    policy: str
    object: dict[str, Any] | None
    expect: int | str | None  # None = ADMIT, int = DENY by that 1-based validation, "skip" = not matched
    operation: str = "CREATE"
    username: str = PROVISIONER_SA
    old_object: dict[str, Any] | None = None
    namespace: str | None = None  # defaults to the object's metadata.namespace
    subresource: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "policy": self.policy,
            "operation": self.operation,
            "username": self.username,
            "namespace": self.namespace,
            "subresource": self.subresource,
            "object": self.object,
            "oldObject": self.old_object,
            "expect": self.expect,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Case":
        return cls(
            name=d["name"],
            policy=d["policy"],
            object=d.get("object"),
            expect=d.get("expect"),
            operation=d.get("operation", "CREATE"),
            username=d.get("username", PROVISIONER_SA),
            old_object=d.get("oldObject"),
            namespace=d.get("namespace"),
            subresource=d.get("subresource", ""),
        )


# -- the baseline: a real git seat, stamped as the controller stamps ------------------------------


@dataclass(frozen=True)
class Baseline:
    stem: str
    provider: str
    account: str
    source: str
    git_stem: str  # the source git seat's own stem (what validation 13 refuses)
    git_account: str
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)  # slot -> object, re-keyed
    git_objects: dict[str, dict[str, Any]] = field(default_factory=dict)  # slot -> object, git stem

    @property
    def aud(self) -> str:
        return f"{self.provider}/{self.account}"

    def kv(self, kind: str) -> str:
        return f"operator/broker/{self.aud}/{kind}"

    def rekeyed(self, slot: str, stem: str, account: str, provider: str | None = None) -> dict[str, Any]:
        """The slot's object as the renderer would emit it for another stem/account (names, labels,
        selectors, ownerReference, OpenBao paths all follow); `provider` defaults to the baseline's."""
        text = json.dumps(self.objects[slot])
        text = text.replace(f"{self.provider}/{self.account}", f"{provider or self.provider}/{account}")
        text = text.replace(self.stem, stem)
        return json.loads(text)


def _audience_of(deployment: dict[str, Any]) -> str | None:
    for c in deployment["spec"]["template"]["spec"].get("containers", []):
        for e in c.get("env", []):
            if e.get("name") == "AF_BROKER_AUDIENCE":
                return e.get("value")
    return None


def _slots(docs: list[dict[str, Any]], stem: str) -> dict[str, dict[str, Any]]:
    """Map the 8 documents of one seat file to fixed slot names, failing closed on any other shape."""
    want = {
        ("Deployment", stem): "deployment",
        ("PodDisruptionBudget", stem): "pdb",
        ("Service", f"{stem}-headless"): "headless",
        ("Service", stem): "service",
        ("ExternalSecret", f"{stem}-oauth"): "es-oauth",
        ("ExternalSecret", f"{stem}-kids"): "es-kids",
        ("ExternalSecret", f"{stem}-ledger"): "es-ledger",
        ("CiliumNetworkPolicy", stem): "cnp",
    }
    out: dict[str, dict[str, Any]] = {}
    for d in docs:
        key = (d.get("kind"), d.get("metadata", {}).get("name"))
        if key not in want:
            raise SystemExit(f"seat file carries an unexpected document {key}; refusing to build a baseline from it")
        out[want[key]] = d
    missing = set(want.values()) - set(out)
    if missing:
        raise SystemExit(f"seat file is missing {sorted(missing)}; refusing to build a baseline from it")
    return out


def stamp(obj: dict[str, Any], stem: str, uid: str = OWNER_UID) -> dict[str, Any]:
    """What render_seat_objects adds AFTER template parity is taken (design D1/D5)."""
    meta = obj["metadata"]
    meta.setdefault("labels", {})["agentforge.io/broker-seat"] = stem
    meta.setdefault("annotations", {})["agentforge.io/render-digest"] = DIGEST
    meta["ownerReferences"] = [
        {
            "apiVersion": "agentforge.io/v1alpha1",
            "kind": "BrokerSeat",
            "name": stem,
            "uid": uid,
            "controller": True,
            "blockOwnerDeletion": True,
        }
    ]
    return obj


def load_baseline(seat_file: Path | None = None) -> Baseline:
    """The first git seat (sorted) whose Deployment name IS broker-<provider>-<account>, stamped."""
    candidates = [seat_file] if seat_file else sorted(
        p for p in BROKER_DIR.glob("broker-*.yaml") if p != INVENTORY
    )
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        docs = [d for d in yaml.safe_load_all(text) if d]
        deps = [d for d in docs if d.get("kind") == "Deployment"]
        if len(deps) != 1:
            continue
        aud = _audience_of(deps[0])
        if not aud or "/" not in aud:
            continue
        provider, git_account = aud.split("/", 1)
        git_stem = deps[0]["metadata"]["name"]
        if git_stem != f"broker-{provider}-{git_account}":
            if seat_file:
                raise SystemExit(f"{path}: stem {git_stem} is not the mechanical broker-{provider}-{git_account}")
            continue
        # Re-key to a hypothetical controller account of the same provider: the renderer's own
        # substitution (aud first, then stem — the two strings never overlap: '/' vs '-').
        account = f"{git_account}-x"
        stem = f"broker-{provider}-{account}"
        rekeyed_text = text.replace(f"{provider}/{git_account}", f"{provider}/{account}").replace(git_stem, stem)

        def _stamped(src: str, s: str) -> dict[str, dict[str, Any]]:
            slots = _slots([d for d in yaml.safe_load_all(src) if d], s)
            for obj in slots.values():
                if obj.get("kind") != "Deployment":
                    obj["metadata"].pop("annotations", None)
                stamp(obj, s)
            # The git seat may not be pinned yet (claude-max-3 isn't); the controller ALWAYS pins.
            slots["service"]["spec"].setdefault("clusterIP", PINNED_IP)
            return slots

        return Baseline(stem=stem, provider=provider, account=account,
                        source=path.relative_to(REPO_ROOT).as_posix(),
                        git_stem=git_stem, git_account=git_account,
                        objects=_stamped(rekeyed_text, stem), git_objects=_stamped(text, git_stem))
    raise SystemExit("no git seat with a mechanical broker-<provider>-<account> stem found under "
                     f"{BROKER_DIR} — pass --seat-file explicitly")


# -- the case table ---------------------------------------------------------------------------

Mutator = Callable[[dict[str, Any]], Any]


def cp_seat(provider: str = "anthropic", account: str = "claude-max-9", ip: str = "10.96.0.201",
            name: str | None = None) -> dict[str, Any]:
    """EXACTLY the body the CP's KubeBrokerSeats.create submits (design D8): apiVersion, kind,
    metadata.name(+namespace), spec — nothing else."""
    return {
        "apiVersion": "agentforge.io/v1alpha1",
        "kind": "BrokerSeat",
        "metadata": {"name": name or f"broker-{provider}-{account}", "namespace": BROKER_NS},
        "spec": {"provider": provider, "account": account, "clusterIP": ip},
    }


def cp(name: str, expect: int | str | None, mutate: Mutator | None = None, *, operation: str = "CREATE",
       username: str = CP_SA, subresource: str = "", old: dict[str, Any] | None = None) -> Case:
    obj = cp_seat()
    if mutate:
        mutate(obj)
    return Case(name=name, policy=CP_GUARD, object=obj, expect=expect, operation=operation,
                username=username, old_object=old, subresource=subresource)


def cp_cases() -> list[Case]:
    adopted = cp_seat()
    adopted["metadata"]["finalizers"] = ["agentforge.io/brokerseat"]
    return [
        # ---- the CP's own create must stay admitted ---------------------------------------------
        cp("cp-bare-seat-admitted", None),
        cp("cp-bare-openai-seat-admitted", None,
           lambda o: (o["spec"].update(provider="openai", account="codex-9"),
                      o["metadata"].update(name="broker-openai-codex-9"))),
        # ---- (1)-(4) the CP may not fake ownership / readiness / labels -------------------------
        cp("cp-finalizer-denied", 1, lambda o: o["metadata"].update(finalizers=["agentforge.io/brokerseat"])),
        cp("cp-ownerreference-denied", 2, lambda o: o["metadata"].update(ownerReferences=[
            {"apiVersion": "v1", "kind": "ConfigMap", "name": "x", "uid": ANOTHER_UID}])),
        cp("cp-label-denied", 3, lambda o: o["metadata"].update(labels={"app.kubernetes.io/name": "x"})),
        cp("cp-flux-label-denied", 3, lambda o: o["metadata"].update(
            labels={"kustomize.toolkit.fluxcd.io/name": "agentforge-broker"})),
        cp("cp-annotation-denied", 4, lambda o: o["metadata"].update(
            annotations={"agentforge.io/render-digest": DIGEST})),
        # ---- (5) the mechanical name -----------------------------------------------------------
        cp("cp-hand-named-git-stem-denied", 5, lambda o: (
            o["metadata"].update(name=GIT_STEM), o["spec"].update(account="claude-max-1"))),
        cp("cp-name-other-provider-denied", 5, lambda o: o["metadata"].update(name="broker-openai-claude-max-9")),
        cp("cp-name-other-account-denied", 5, lambda o: o["metadata"].update(name="broker-anthropic-claude-max-1")),
        cp("cp-name-no-prefix-denied", 5, lambda o: o["metadata"].update(name="anthropic-claude-max-9")),
        # ---- (6) never a status (the apiserver strips status on CREATE before admission, so the
        #          clause is meaningful only on the /status subresource — see the docstring) ---------
        cp("cp-status-subresource-denied", 6, lambda o: o.update(status={"phase": "Ready"}),
           operation="UPDATE", subresource="status", old=cp_seat()),
        # ---- UPDATE of an adopted CR (carries the provisioner's finalizer) is refused by (1) ------
        cp("cp-update-adopted-cr-denied", 1, lambda o: o["metadata"].update(
            finalizers=["agentforge.io/brokerseat"]), operation="UPDATE", old=adopted),
        # ---- the SA gate: the provisioner's own CR writes are NOT this policy's business ----------
        cp("cp-guard-skips-provisioner", SKIP, lambda o: o["metadata"].update(
            finalizers=["agentforge.io/brokerseat"]), operation="UPDATE", username=PROVISIONER_SA, old=adopted),
    ]


def _dep_spec(o: dict[str, Any]) -> dict[str, Any]:
    return o["spec"]["template"]["spec"]


def _ctr(o: dict[str, Any]) -> dict[str, Any]:
    return _dep_spec(o)["containers"][0]


def _vol(o: dict[str, Any], name: str) -> dict[str, Any]:
    return next(v for v in _dep_spec(o)["volumes"] if v["name"] == name)


def _rule(o: dict[str, Any], direction: str, idx: int) -> dict[str, Any]:
    return o["spec"][direction][idx]


def _psc(o: dict[str, Any]) -> dict[str, Any]:
    return _dep_spec(o)["securityContext"]


def _csc(o: dict[str, Any]) -> dict[str, Any]:
    return _ctr(o)["securityContext"]


def _deny(o: dict[str, Any], key: str) -> dict[str, Any]:
    return next(r for r in o["spec"]["egressDeny"] if key in r)


def objects_cases(b: Baseline) -> list[Case]:
    S = b.stem

    def seat(name: str, slot: str, expect: int | str | None, mutate: Mutator | None = None, *,
             operation: str = "CREATE", old_mutate: Mutator | None = None, username: str = PROVISIONER_SA,
             namespace: str | None = None) -> Case:
        obj = copy.deepcopy(b.objects[slot])
        if mutate:
            mutate(obj)
        old = None
        if operation == "UPDATE":
            old = copy.deepcopy(b.objects[slot])
            if old_mutate:
                old_mutate(old)
        return Case(name=name, policy=OBJECTS_GUARD, object=obj, expect=expect, operation=operation,
                    username=username, old_object=old, namespace=namespace)

    def relabel(stem: str) -> Mutator:
        return lambda o: o["metadata"]["labels"].update({"agentforge.io/broker-seat": stem})

    def unstamp(o: dict[str, Any]) -> None:
        """Back to the git object as Flux applies it (its labels + no ownership)."""
        o["metadata"]["labels"].pop("agentforge.io/broker-seat")
        o["metadata"].pop("ownerReferences")
        o["metadata"]["labels"].update({
            "kustomize.toolkit.fluxcd.io/name": "agentforge-broker",
            "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
        })

    cases: list[Case] = []
    # ---- every stamped seat object stays admitted, on CREATE and on the drift-correcting UPDATE ----
    for slot in b.objects:
        cases.append(seat(f"{slot}-as-rendered-admitted", slot, None))
        cases.append(seat(f"{slot}-replace-in-place-admitted", slot, None, operation="UPDATE"))
    cases += [
        # ---- (1) namespace ------------------------------------------------------------------------
        seat("dep-other-namespace", "deployment", 1,
             lambda o: o["metadata"].update(namespace="agentforge-sandbox"), namespace="agentforge-sandbox"),
        seat("cnp-other-namespace", "cnp", 1,
             lambda o: o["metadata"].update(namespace="openbao"), namespace="openbao"),
        # ---- (2) name + ownership label ---------------------------------------------------------
        seat("dep-name-not-stem", "deployment", 2, lambda o: o["metadata"].update(name=f"{S}-x")),
        seat("dep-name-without-broker-prefix", "deployment", 2, lambda o: (
            o["metadata"].update(name="seat-anthropic-x"), relabel("seat-anthropic-x")(o))),
        seat("dep-label-missing", "deployment", 2, lambda o: o["metadata"]["labels"].pop("agentforge.io/broker-seat")),
        seat("dep-label-other-stem", "deployment", 2, relabel(OTHER_STEM)),
        seat("dep-label-empty", "deployment", 2, relabel("")),
        seat("dep-stem-unknown-provider", "deployment", 2, lambda o: (
            o["metadata"].update(name="broker-google-x"), relabel("broker-google-x")(o))),
        seat("dep-stem-too-long", "deployment", 2, lambda o: (
            o["metadata"].update(name="broker-anthropic-" + "a" * 36),
            relabel("broker-anthropic-" + "a" * 36)(o))),
        seat("svc-name-unknown-suffix", "service", 2, lambda o: o["metadata"].update(name=f"{S}-metrics")),
        seat("es-name-unknown-suffix", "es-oauth", 2, lambda o: o["metadata"].update(name=f"{S}-token")),
        seat("pdb-name-headless-suffix", "pdb", 2, lambda o: o["metadata"].update(name=f"{S}-headless")),
        seat("cnp-name-other-seat", "cnp", 2, lambda o: o["metadata"].update(name=OTHER_STEM)),
        # ---- (3) ownerReferences ----------------------------------------------------------------
        seat("dep-no-ownerreference", "deployment", 3, lambda o: o["metadata"].pop("ownerReferences")),
        seat("dep-two-ownerreferences", "deployment", 3, lambda o: o["metadata"]["ownerReferences"].append(
            {"apiVersion": "agentforge.io/v1alpha1", "kind": "BrokerSeat", "name": OTHER_STEM, "uid": ANOTHER_UID})),
        seat("dep-ownerref-other-seat", "deployment", 3, lambda o: o["metadata"]["ownerReferences"][0].update(name=OTHER_STEM)),
        seat("dep-ownerref-not-controller", "deployment", 3, lambda o: o["metadata"]["ownerReferences"][0].update(controller=False)),
        seat("dep-ownerref-controller-absent", "deployment", 3, lambda o: o["metadata"]["ownerReferences"][0].pop("controller")),
        seat("dep-ownerref-no-blockownerdeletion", "deployment", 3, lambda o: o["metadata"]["ownerReferences"][0].pop("blockOwnerDeletion")),
        seat("dep-ownerref-wrong-kind", "deployment", 3, lambda o: o["metadata"]["ownerReferences"][0].update(kind="Deployment")),
        seat("dep-ownerref-wrong-apiversion", "deployment", 3, lambda o: o["metadata"]["ownerReferences"][0].update(apiVersion="agentforge.io/v1beta1")),
        seat("dep-ownerref-empty-uid", "deployment", 3, lambda o: o["metadata"]["ownerReferences"][0].update(uid="")),
        # ---- (4) never a Flux object; UPDATE only over this stem's own object ----------------------
        seat("dep-flux-name-label", "deployment", 4, lambda o: o["metadata"]["labels"].update(
            {"kustomize.toolkit.fluxcd.io/name": "agentforge-broker"})),
        seat("dep-flux-namespace-label", "deployment", 4, lambda o: o["metadata"]["labels"].update(
            {"kustomize.toolkit.fluxcd.io/namespace": "flux-system"})),
        seat("dep-update-over-git-seat-object", "deployment", 4, operation="UPDATE", old_mutate=unstamp),
        seat("svc-update-over-flux-labelled-object", "service", 4, operation="UPDATE",
             old_mutate=lambda o: o["metadata"]["labels"].update({"kustomize.toolkit.fluxcd.io/name": "agentforge-broker"})),
        seat("dep-update-changes-owner-uid", "deployment", 4, operation="UPDATE",
             old_mutate=lambda o: o["metadata"]["ownerReferences"][0].update(uid=ANOTHER_UID)),
        seat("cnp-update-over-other-stem-object", "cnp", 4, operation="UPDATE", old_mutate=relabel(OTHER_STEM)),
        seat("es-update-over-unowned-object", "es-kids", 4, operation="UPDATE",
             old_mutate=lambda o: o["metadata"].pop("ownerReferences")),
        # ---- (5) Deployment pod shape -------------------------------------------------------------
        seat("dep-second-container", "deployment", 5, lambda o: _dep_spec(o)["containers"].append(
            {"name": "sidecar", "image": _ctr(o)["image"]})),
        seat("dep-init-container", "deployment", 5, lambda o: _dep_spec(o).update(initContainers=[
            {"name": "init", "image": _ctr(o)["image"]}])),
        seat("dep-ephemeral-container", "deployment", 5, lambda o: _dep_spec(o).update(ephemeralContainers=[
            {"name": "debug", "image": _ctr(o)["image"]}])),
        seat("dep-image-tag-not-digest", "deployment", 5, lambda o: _ctr(o).update(
            image="registry.chifor.me/agentforge/orchestrator:latest")),
        seat("dep-image-other-repository", "deployment", 5, lambda o: _ctr(o).update(
            image="registry.chifor.me/agentforge/p1-worker@sha256:" + "a" * 64)),
        seat("dep-image-other-registry", "deployment", 5, lambda o: _ctr(o).update(
            image="docker.io/library/orchestrator@sha256:" + "a" * 64)),
        seat("dep-image-short-digest", "deployment", 5, lambda o: _ctr(o).update(
            image="registry.chifor.me/agentforge/orchestrator@sha256:" + "a" * 63)),
        seat("dep-other-serviceaccount", "deployment", 5, lambda o: _dep_spec(o).update(serviceAccountName="af-broker-eso")),
        seat("dep-serviceaccount-absent", "deployment", 5, lambda o: _dep_spec(o).pop("serviceAccountName")),
        seat("dep-automount-token", "deployment", 5, lambda o: _dep_spec(o).update(automountServiceAccountToken=True)),
        seat("dep-hostnetwork", "deployment", 5, lambda o: _dep_spec(o).update(hostNetwork=True)),
        # ---- (6) Deployment identity --------------------------------------------------------------
        seat("dep-selector-other-seat", "deployment", 6, lambda o: o["spec"]["selector"]["matchLabels"].update(
            {"app.kubernetes.io/name": GIT_STEM})),
        seat("dep-selector-matchexpressions", "deployment", 6, lambda o: o["spec"]["selector"].update(
            matchExpressions=[{"key": "app.kubernetes.io/component", "operator": "In", "values": ["broker"]}])),
        seat("dep-pod-label-other-seat", "deployment", 6, lambda o: o["spec"]["template"]["metadata"]["labels"].update(
            {"app.kubernetes.io/name": GIT_STEM})),
        seat("dep-pod-component-not-broker", "deployment", 6, lambda o: o["spec"]["template"]["metadata"]["labels"].update(
            {"app.kubernetes.io/component": "sandbox"})),
        # ---- (7) Deployment secret sources --------------------------------------------------------
        seat("dep-other-seat-secret-volume", "deployment", 7, lambda o: _vol(o, "broker-oauth")["secret"].update(
            secretName=f"{GIT_STEM}-oauth")),
        seat("dep-secret-plus-emptydir-other-seat", "deployment", 7, lambda o: _vol(o, "tmp").update(
            secret={"secretName": f"{GIT_STEM}-oauth"})),
        seat("dep-hostpath-volume", "deployment", 7, lambda o: _dep_spec(o)["volumes"].append(
            {"name": "host", "hostPath": {"path": "/var/run"}})),
        seat("dep-projected-token-volume", "deployment", 7, lambda o: _dep_spec(o)["volumes"].append(
            {"name": "tok", "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}})),
        seat("dep-configmap-volume", "deployment", 7, lambda o: _dep_spec(o)["volumes"].append(
            {"name": "cm", "configMap": {"name": "agentforge-broker-env"}})),
        seat("dep-pvc-volume", "deployment", 7, lambda o: _dep_spec(o)["volumes"].append(
            {"name": "pvc", "persistentVolumeClaim": {"claimName": "x"}})),
        # the explicit per-type denies are the belt BEHIND the positive secret|emptyDir shape: a
        # two-source volume (which the apiserver's own validation refuses) must trip them on its own
        seat("dep-volume-emptydir-plus-hostpath", "deployment", 7, lambda o: _vol(o, "tmp").update(hostPath={"path": "/var/run"})),
        seat("dep-volume-emptydir-plus-projected", "deployment", 7, lambda o: _vol(o, "tmp").update(
            projected={"sources": [{"serviceAccountToken": {"path": "token"}}]})),
        seat("dep-volume-emptydir-plus-configmap", "deployment", 7, lambda o: _vol(o, "tmp").update(configMap={"name": "agentforge-broker-env"})),
        seat("dep-volume-emptydir-plus-pvc", "deployment", 7, lambda o: _vol(o, "tmp").update(persistentVolumeClaim={"claimName": "x"})),
        seat("dep-env-secretkeyref-other-seat", "deployment", 7, lambda o: _ctr(o)["env"].append(
            {"name": "X", "valueFrom": {"secretKeyRef": {"name": f"{GIT_STEM}-oauth", "key": "CLAUDE_CODE_OAUTH_TOKEN"}}})),
        seat("dep-env-secretkeyref-shared-secret", "deployment", 7, lambda o: _ctr(o)["env"].append(
            {"name": "X", "valueFrom": {"secretKeyRef": {"name": "openbao-tls", "key": "ca.crt"}}})),
        seat("dep-envfrom-secretref-other-seat", "deployment", 7, lambda o: _ctr(o)["envFrom"].append(
            {"secretRef": {"name": f"{OTHER_STEM}-ledger"}})),
        seat("dep-env-secretkeyref-own-admitted", "deployment", None, lambda o: _ctr(o)["env"].append(
            {"name": "X", "valueFrom": {"secretKeyRef": {"name": f"{S}-kids", "key": "registry.json"}}})),
        seat("dep-envfrom-secretref-own-admitted", "deployment", None, lambda o: _ctr(o)["envFrom"].append(
            {"secretRef": {"name": f"{S}-ledger"}})),
        # ---- (8) Services -----------------------------------------------------------------------
        seat("svc-selector-other-seat", "service", 8, lambda o: o["spec"].update(selector={"app.kubernetes.io/name": GIT_STEM})),
        seat("svc-selector-empty", "service", 8, lambda o: o["spec"].update(selector={})),
        seat("svc-selector-extra-label", "headless", 8, lambda o: o["spec"]["selector"].update({"agentforge.io/pool": "planner"})),
        seat("svc-type-nodeport", "service", 8, lambda o: o["spec"].update(type="NodePort")),
        seat("svc-type-loadbalancer", "service", 8, lambda o: o["spec"].update(type="LoadBalancer")),
        seat("svc-externalname", "service", 8, lambda o: o["spec"].update(type="ExternalName", externalName="attacker.example")),
        seat("svc-externalname-field-only", "service", 8, lambda o: o["spec"].update(externalName="attacker.example")),
        seat("svc-externalips", "service", 8, lambda o: o["spec"].update(externalIPs=["192.168.0.50"])),
        seat("svc-extra-port", "service", 8, lambda o: o["spec"]["ports"].append(
            {"name": "ssh", "port": 22, "targetPort": 22, "protocol": "TCP"})),
        seat("svc-nodeport-field", "service", 8, lambda o: o["spec"]["ports"][0].update(nodePort=30870)),
        seat("svc-headless-with-clusterip", "headless", 8, lambda o: o["spec"].update(clusterIP=PINNED_IP)),
        seat("svc-pinned-clusterip-none", "service", 8, lambda o: o["spec"].update(clusterIP="None")),
        seat("svc-pinned-outside-service-cidr", "service", 8, lambda o: o["spec"].update(clusterIP="10.42.0.5")),
        # ---- (9) PDB -----------------------------------------------------------------------------
        seat("pdb-selector-other-seat", "pdb", 9, lambda o: o["spec"]["selector"]["matchLabels"].update(
            {"app.kubernetes.io/name": GIT_STEM})),
        seat("pdb-selector-matchexpressions", "pdb", 9, lambda o: o["spec"]["selector"].update(
            matchExpressions=[{"key": "app.kubernetes.io/component", "operator": "Exists"}])),
        # ---- (10) ExternalSecret store / target -----------------------------------------------------
        seat("es-foreign-store", "es-oauth", 10, lambda o: o["spec"]["secretStoreRef"].update(name="af-eso-platform-dev")),
        seat("es-clustersecretstore", "es-oauth", 10, lambda o: o["spec"]["secretStoreRef"].update(kind="ClusterSecretStore")),
        seat("es-target-other-seat-secret", "es-oauth", 10, lambda o: o["spec"]["target"].update(name=f"{GIT_STEM}-oauth")),
        seat("es-target-other-kind-of-own-seat", "es-oauth", 10, lambda o: o["spec"]["target"].update(name=f"{S}-kids")),
        seat("es-creationpolicy-merge", "es-oauth", 10, lambda o: o["spec"]["target"].update(creationPolicy="Merge")),
        seat("es-target-template", "es-oauth", 10, lambda o: o["spec"]["target"].update(
            template={"data": {"CLAUDE_CODE_OAUTH_TOKEN": "{{ .token }}"}})),
        seat("es-target-templatefrom-other-seat-secret", "es-oauth", 10, lambda o: o["spec"]["target"].update(
            template={"templateFrom": [{"secret": {"name": f"{GIT_STEM}-oauth", "items": [{"key": "CLAUDE_CODE_OAUTH_TOKEN"}]}}]})),
        seat("es-datafrom-find", "es-oauth", 10, lambda o: o["spec"].update(dataFrom=[{"find": {"path": "operator/broker"}}])),
        seat("es-datafrom-sourceref", "es-oauth", 10, lambda o: o["spec"]["dataFrom"][0].update(
            sourceRef={"storeRef": {"name": "af-eso-platform-dev", "kind": "SecretStore"}})),
        seat("es-datafrom-generatorref", "es-oauth", 10, lambda o: o["spec"]["dataFrom"][0].update(
            generatorRef={"apiVersion": "generators.external-secrets.io/v1alpha1", "kind": "Password", "name": "x"})),
        seat("es-datafrom-rewrite", "es-oauth", 10, lambda o: o["spec"]["dataFrom"][0].update(
            rewrite=[{"regexp": {"source": "(.*)", "target": "x_$1"}}])),
        seat("es-data-sourceref", "es-kids", 10, lambda o: o["spec"]["data"][0].update(
            sourceRef={"storeRef": {"name": "af-eso-platform-dev", "kind": "SecretStore"}})),
        # ---- (11) ExternalSecret sources ---------------------------------------------------------
        seat("es-oauth-key-other-audience", "es-oauth", 11, lambda o: o["spec"]["dataFrom"][0]["extract"].update(
            key="operator/broker/anthropic/claude-max-1/oauth")),
        seat("es-oauth-key-other-provider-same-account", "es-oauth", 11, lambda o: o["spec"]["dataFrom"][0]["extract"].update(
            key=f"operator/broker/openai/{b.account}/oauth")),
        seat("es-oauth-key-tenant-path", "es-oauth", 11, lambda o: o["spec"]["dataFrom"][0]["extract"].update(
            key="tenants/tenant-zero/platform-dev/orchestrator")),
        seat("es-oauth-key-other-operator-doc", "es-oauth", 11, lambda o: o["spec"]["dataFrom"][0]["extract"].update(
            key="operator/dispatcher/webhook")),
        seat("es-oauth-key-traversal", "es-oauth", 11, lambda o: o["spec"]["dataFrom"][0]["extract"].update(
            key=f"operator/broker/anthropic/{b.account}/../claude-max-1/oauth")),
        seat("es-oauth-key-kids-doc", "es-oauth", 11, lambda o: o["spec"]["dataFrom"][0]["extract"].update(key=b.kv("kids"))),
        seat("es-oauth-two-extracts", "es-oauth", 11, lambda o: o["spec"]["dataFrom"].append(
            {"extract": {"key": b.kv("oauth")}})),
        seat("es-oauth-plus-data", "es-oauth", 11, lambda o: o["spec"].update(data=[
            {"secretKey": "x", "remoteRef": {"key": b.kv("oauth"), "property": "x"}}])),
        seat("es-oauth-via-data", "es-oauth", 11, lambda o: (o["spec"].pop("dataFrom"), o["spec"].update(data=[
            {"secretKey": "CLAUDE_CODE_OAUTH_TOKEN", "remoteRef": {"key": b.kv("oauth"), "property": "CLAUDE_CODE_OAUTH_TOKEN"}}]))),
        seat("es-kids-via-datafrom", "es-kids", 11, lambda o: (o["spec"].pop("data"), o["spec"].update(
            dataFrom=[{"extract": {"key": b.kv("kids")}}]))),
        seat("es-kids-key-other-audience", "es-kids", 11, lambda o: o["spec"]["data"][0]["remoteRef"].update(
            key="operator/broker/anthropic/claude-max-1/kids")),
        seat("es-kids-key-oauth-doc", "es-kids", 11, lambda o: o["spec"]["data"][0]["remoteRef"].update(key=b.kv("oauth"))),
        seat("es-kids-property-other", "es-kids", 11, lambda o: o["spec"]["data"][0]["remoteRef"].update(property="private.pem")),
        seat("es-kids-secretkey-other", "es-kids", 11, lambda o: o["spec"]["data"][0].update(secretKey="auth.json")),
        seat("es-ledger-key-oauth-doc", "es-ledger", 11, lambda o: o["spec"]["data"][0]["remoteRef"].update(key=b.kv("oauth"))),
        seat("es-ledger-key-other-audience", "es-ledger", 11, lambda o: o["spec"]["data"][0]["remoteRef"].update(
            key="operator/broker/openai/codex-pro/ledger")),
        seat("es-ledger-two-entries", "es-ledger", 11, lambda o: o["spec"]["data"].append(
            {"secretKey": "PGPASSWORD", "remoteRef": {"key": b.kv("ledger"), "property": "AF_BROKER_LEDGER_DSN"}})),
        seat("es-ledger-property-other", "es-ledger", 11, lambda o: o["spec"]["data"][0]["remoteRef"].update(property="ADMIN_DSN")),
        # ---- (12) CiliumNetworkPolicy --------------------------------------------------------------
        seat("cnp-endpointselector-git-seat", "cnp", 12, lambda o: o["spec"].update(
            endpointSelector={"matchLabels": {"app.kubernetes.io/name": GIT_STEM}})),
        seat("cnp-endpointselector-other-controller-seat", "cnp", 12, lambda o: o["spec"].update(
            endpointSelector={"matchLabels": {"app.kubernetes.io/name": OTHER_STEM}})),
        seat("cnp-endpointselector-empty", "cnp", 12, lambda o: o["spec"].update(endpointSelector={})),
        seat("cnp-endpointselector-component-broker", "cnp", 12, lambda o: o["spec"].update(
            endpointSelector={"matchLabels": {"app.kubernetes.io/component": "broker"}})),
        seat("cnp-endpointselector-matchexpressions", "cnp", 12, lambda o: o["spec"].update(
            endpointSelector={"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "Exists"}]})),
        seat("cnp-specs-list", "cnp", 12, lambda o: o.update(specs=[copy.deepcopy(o["spec"])])),
        seat("cnp-nodeselector", "cnp", 12, lambda o: o["spec"].update(nodeSelector={"matchLabels": {}})),
        seat("cnp-ingressdeny", "cnp", 12, lambda o: o["spec"].update(ingressDeny=[{"fromEntities": ["world"]}])),
        # a peer widener ADDED to a well-formed rule (a rule without fromEndpoints is refused by the
        # structural predicate; these trip the explicit fromEntities/fromCIDR denies on their own)
        seat("cnp-ingress-fromentities-world", "cnp", 12, lambda o: _rule(o, "ingress", 0).update(fromEntities=["world"])),
        seat("cnp-ingress-fromcidr", "cnp", 12, lambda o: _rule(o, "ingress", 0).update(fromCIDR=["0.0.0.0/0"])),
        seat("cnp-ingress-rule-without-fromendpoints", "cnp", 12, lambda o: o["spec"]["ingress"].append(
            {"fromEntities": ["world"], "toPorts": [{"ports": [{"port": "8700", "protocol": "TCP"}]}]})),
        seat("cnp-ingress-fromendpoints-empty-selector", "cnp", 12, lambda o: o["spec"]["ingress"].append(
            {"fromEndpoints": [{}], "toPorts": [{"ports": [{"port": "8700", "protocol": "TCP"}]}]})),
        seat("cnp-ingress-fromendpoints-no-namespace", "cnp", 12, lambda o: o["spec"]["ingress"].append(
            {"fromEndpoints": [{"matchLabels": {"agentforge.io/trust-class": "agent"}}],
             "toPorts": [{"ports": [{"port": "8700", "protocol": "TCP"}]}]})),
        seat("cnp-ingress-other-namespace", "cnp", 12, lambda o: _rule(o, "ingress", 0)["fromEndpoints"][0]["matchLabels"].update(
            {"k8s:io.kubernetes.pod.namespace": "databases"})),
        seat("cnp-ingress-extra-port", "cnp", 12, lambda o: _rule(o, "ingress", 0)["toPorts"][0]["ports"].append(
            {"port": "22", "protocol": "TCP"})),
        seat("cnp-ingress-udp-port", "cnp", 12, lambda o: _rule(o, "ingress", 0)["toPorts"][0]["ports"][0].update(protocol="UDP")),
        seat("cnp-ingress-no-toports", "cnp", 12, lambda o: _rule(o, "ingress", 0).pop("toPorts")),
        seat("cnp-ingress-l7-rules", "cnp", 12, lambda o: _rule(o, "ingress", 0)["toPorts"][0].update(
            rules={"http": [{"method": "GET"}]})),
        # a destination widener ADDED to a well-formed toEndpoints rule (same reasoning as ingress)
        seat("cnp-egress-toentities-world", "cnp", 12, lambda o: _rule(o, "egress", 1).update(toEntities=["world"])),
        seat("cnp-egress-tocidr", "cnp", 12, lambda o: _rule(o, "egress", 1).update(toCIDR=["0.0.0.0/0"])),
        seat("cnp-egress-toservices", "cnp", 12, lambda o: _rule(o, "egress", 1).update(
            toServices=[{"k8sService": {"serviceName": "openbao", "namespace": "openbao"}}])),
        seat("cnp-egress-rule-without-destination", "cnp", 12, lambda o: o["spec"]["egress"].append(
            {"toEntities": ["world"], "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]})),
        seat("cnp-egress-fqdn-not-allowlisted", "cnp", 12, lambda o: _rule(o, "egress", 2)["toFQDNs"].append(
            {"matchName": "attacker.example"})),
        seat("cnp-egress-fqdn-matchpattern", "cnp", 12, lambda o: _rule(o, "egress", 2)["toFQDNs"].append(
            {"matchPattern": "*.anthropic.com"})),
        seat("cnp-egress-fqdn-other-provider", "cnp", 12, lambda o: _rule(o, "egress", 2)["toFQDNs"].append(
            {"matchName": "chatgpt.com"})),
        seat("cnp-egress-fqdn-port-80", "cnp", 12, lambda o: _rule(o, "egress", 2)["toPorts"][0]["ports"].append(
            {"port": "80", "protocol": "TCP"})),
        seat("cnp-egress-fqdn-no-toports", "cnp", 12, lambda o: _rule(o, "egress", 2).pop("toPorts")),
        seat("cnp-egress-toendpoints-openbao", "cnp", 12, lambda o: o["spec"]["egress"].append(
            {"toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "openbao"}}],
             "toPorts": [{"ports": [{"port": "5432", "protocol": "TCP"}]}]})),
        seat("cnp-egress-toendpoints-any", "cnp", 12, lambda o: o["spec"]["egress"].append(
            {"toEndpoints": [{}], "toPorts": [{"ports": [{"port": "53", "protocol": "UDP"}]}]})),
        seat("cnp-egress-toendpoints-other-port", "cnp", 12, lambda o: _rule(o, "egress", 1)["toPorts"][0]["ports"].append(
            {"port": "8200", "protocol": "TCP"})),
        seat("cnp-egress-toendpoints-plus-fqdns", "cnp", 12, lambda o: _rule(o, "egress", 1).update(
            toFQDNs=[{"matchName": "api.anthropic.com"}])),
        seat("cnp-egress-dns-http-rule", "cnp", 12, lambda o: _rule(o, "egress", 0)["toPorts"][0]["rules"].update(
            http=[{"method": "GET"}])),
        seat("cnp-egressdeny-missing", "cnp", 12, lambda o: o["spec"].pop("egressDeny")),
        seat("cnp-egressdeny-without-private-cidrs", "cnp", 12, lambda o: o["spec"].update(
            egressDeny=[r for r in o["spec"]["egressDeny"] if "toCIDR" not in r])),
        seat("cnp-egressdeny-without-node-entities", "cnp", 12, lambda o: o["spec"].update(
            egressDeny=[r for r in o["spec"]["egressDeny"] if "toEntities" not in r])),
        # the belt must carry EVERY range: the three RFC1918 ones alone (no link-local metadata,
        # loopback, IPv6) or everything but ::/0 is not the template's belt
        seat("cnp-egressdeny-belt-rfc1918-only", "cnp", 12, lambda o: _deny(o, "toCIDR").update(
            toCIDR=["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"])),
        seat("cnp-egressdeny-belt-without-ipv6", "cnp", 12, lambda o: _deny(o, "toCIDR").update(
            toCIDR=["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8"])),
        # ---- (13) never a git-managed audience (review round 1, F1) ------------------------------
        # the baseline's own source seat under its real stem — every one of its 8 objects is refused
        # (an ExternalSecret there would sync the git seat's credential into a controller Secret) ...
        *[Case(name=f"{slot}-git-seat-own-stem", policy=OBJECTS_GUARD, object=copy.deepcopy(obj), expect=13)
          for slot, obj in b.git_objects.items()],
        # ... and so is the mechanical ALIAS of a hand-named git seat's audience, for every kind
        *[Case(name=f"{slot}-git-aud-alias", policy=OBJECTS_GUARD,
               object=b.rekeyed(slot, GIT_ALIAS, "claude-max-1"), expect=13)
          for slot in ("deployment", "pdb", "headless", "service", "es-oauth", "es-kids", "es-ledger", "cnp")],
        # ... and the hand-named stem itself (its child names collide with the git objects)
        Case(name="es-oauth-hand-named-git-stem", policy=OBJECTS_GUARD,
             object=b.rekeyed("es-oauth", GIT_STEM, "max1"), expect=13),
        # while another provider's account under a slug that merely LOOKS like a git one is fine
        Case(name="es-oauth-other-provider-same-slug-admitted", policy=OBJECTS_GUARD,
             object=b.rekeyed("es-oauth", "broker-openai-claude-max-1", "claude-max-1", provider="openai"), expect=None),
        # ---- (5) host namespaces, each on its own (review round 1, F8) ---------------------------
        seat("dep-hostpid", "deployment", 5, lambda o: _dep_spec(o).update(hostPID=True)),
        seat("dep-hostipc", "deployment", 5, lambda o: _dep_spec(o).update(hostIPC=True)),
        # ---- (12) TLS interception / listener (F2), enableDefaultDeny + deny-rule shape (F3), ports (F4)
        seat("cnp-ingress-terminating-tls", "cnp", 12, lambda o: _rule(o, "ingress", 0)["toPorts"][0].update(
            terminatingTLS={"secret": {"name": f"{GIT_STEM}-oauth", "namespace": "agentforge-broker"}})),
        seat("cnp-egress-originating-tls", "cnp", 12, lambda o: _rule(o, "egress", 2)["toPorts"][0].update(
            originatingTLS={"secret": {"name": "openbao-tls", "namespace": "agentforge-broker"}})),
        seat("cnp-egress-db-originating-tls", "cnp", 12, lambda o: _rule(o, "egress", 1)["toPorts"][0].update(
            originatingTLS={"secret": {"name": "openbao-tls"}})),
        seat("cnp-ingress-listener", "cnp", 12, lambda o: _rule(o, "ingress", 0)["toPorts"][0].update(
            listener={"envoyConfig": {"name": "x"}, "name": "l"})),
        seat("cnp-egress-fqdn-servernames", "cnp", 12, lambda o: _rule(o, "egress", 2)["toPorts"][0].update(
            serverNames=["attacker.example"])),
        seat("cnp-enabledefaultdeny", "cnp", 12, lambda o: o["spec"].update(enableDefaultDeny={"egress": False})),
        seat("cnp-egressdeny-belt-narrowed-by-toports", "cnp", 12, lambda o: _deny(o, "toCIDR").update(
            toPorts=[{"ports": [{"port": "1", "protocol": "TCP"}]}])),
        seat("cnp-egressdeny-entities-narrowed-by-toports", "cnp", 12, lambda o: _deny(o, "toEntities").update(
            toPorts=[{"ports": [{"port": "1", "protocol": "TCP"}]}])),
        seat("cnp-egressdeny-two-selectors", "cnp", 12, lambda o: _deny(o, "toEntities").update(
            toEndpoints=[{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "openbao"}}])),
        seat("cnp-egressdeny-extra-key", "cnp", 12, lambda o: _deny(o, "toCIDR").update(
            toRequires=[{"matchLabels": {"x": "y"}}])),
        seat("cnp-egressdeny-only-fqdns", "cnp", 12, lambda o: o["spec"].update(egressDeny=[
            {"toFQDNs": [{"matchName": "x.example"}]}])),
        seat("cnp-ingress-endport", "cnp", 12, lambda o: _rule(o, "ingress", 0)["toPorts"][0]["ports"][0].update(endPort=9464)),
        seat("cnp-ingress-port-without-protocol", "cnp", 12, lambda o: _rule(o, "ingress", 0)["toPorts"][0]["ports"][0].pop("protocol")),
        seat("cnp-egress-dns-endport", "cnp", 12, lambda o: _rule(o, "egress", 0)["toPorts"][0]["ports"][0].update(endPort=65535)),
        seat("cnp-egress-db-port-without-protocol", "cnp", 12, lambda o: _rule(o, "egress", 1)["toPorts"][0]["ports"][0].pop("protocol")),
        seat("cnp-egress-fqdn-port-without-protocol", "cnp", 12, lambda o: _rule(o, "egress", 2)["toPorts"][0]["ports"][0].pop("protocol")),
        seat("cnp-egress-fqdn-endport", "cnp", 12, lambda o: _rule(o, "egress", 2)["toPorts"][0]["ports"][0].update(endPort=65535)),
        seat("cnp-egress-fqdn-toports-extra-key", "cnp", 12, lambda o: _rule(o, "egress", 2)["toPorts"][0].update(
            rules={"http": [{}]})),
        seat("cnp-ingress-fromendpoints-matchexpressions", "cnp", 12, lambda o: _rule(o, "ingress", 0)["fromEndpoints"][0].update(
            matchExpressions=[{"key": "x", "operator": "Exists"}])),
        seat("cnp-egress-toendpoints-matchexpressions", "cnp", 12, lambda o: _rule(o, "egress", 1)["toEndpoints"][0].update(
            matchExpressions=[{"key": "x", "operator": "Exists"}])),
        seat("cnp-egress-fqdn-entry-extra-key", "cnp", 12, lambda o: _rule(o, "egress", 2)["toFQDNs"][0].update(
            matchPattern="*.anthropic.com")),
        seat("cnp-egress-dns-rules-plus-l7", "cnp", 12, lambda o: _rule(o, "egress", 0)["toPorts"][0]["rules"].update(
            l7proto="x")),
        # ---- (14) pod / container isolation (review round 1, F6) ---------------------------------
        seat("dep-pod-sc-absent", "deployment", 14, lambda o: _dep_spec(o).pop("securityContext")),
        seat("dep-pod-runasnonroot-false", "deployment", 14, lambda o: _psc(o).update(runAsNonRoot=False)),
        seat("dep-pod-runasuser-0", "deployment", 14, lambda o: _psc(o).update(runAsUser=0)),
        seat("dep-pod-runasgroup-0", "deployment", 14, lambda o: _psc(o).update(runAsGroup=0)),
        seat("dep-pod-fsgroup-0", "deployment", 14, lambda o: _psc(o).update(fsGroup=0)),
        seat("dep-pod-seccomp-unconfined", "deployment", 14, lambda o: _psc(o).update(seccompProfile={"type": "Unconfined"})),
        seat("dep-pod-seccomp-absent", "deployment", 14, lambda o: _psc(o).pop("seccompProfile")),
        seat("dep-pod-sysctls", "deployment", 14, lambda o: _psc(o).update(sysctls=[{"name": "net.ipv4.ip_forward", "value": "1"}])),
        seat("dep-ctr-sc-absent", "deployment", 14, lambda o: _ctr(o).pop("securityContext")),
        seat("dep-ctr-privileged", "deployment", 14, lambda o: _csc(o).update(privileged=True)),
        seat("dep-ctr-allowprivesc", "deployment", 14, lambda o: _csc(o).update(allowPrivilegeEscalation=True)),
        seat("dep-ctr-cap-add", "deployment", 14, lambda o: _csc(o)["capabilities"].update(add=["NET_ADMIN"])),
        seat("dep-ctr-cap-drop-not-all", "deployment", 14, lambda o: _csc(o)["capabilities"].update(drop=["NET_RAW"])),
        seat("dep-ctr-cap-absent", "deployment", 14, lambda o: _csc(o).pop("capabilities")),
        seat("dep-ctr-rootfs-writable", "deployment", 14, lambda o: _csc(o).update(readOnlyRootFilesystem=False)),
        seat("dep-ctr-runasnonroot-false", "deployment", 14, lambda o: _csc(o).update(runAsNonRoot=False)),
        seat("dep-ctr-seccomp-unconfined", "deployment", 14, lambda o: _csc(o).update(seccompProfile={"type": "Unconfined"})),
        seat("dep-ctr-seccomp-absent", "deployment", 14, lambda o: _csc(o).pop("seccompProfile")),
        seat("dep-ctr-procmount-unmasked", "deployment", 14, lambda o: _csc(o).update(procMount="Unmasked")),
        seat("dep-ctr-runasuser-0", "deployment", 14, lambda o: _csc(o).update(runAsUser=0)),
        seat("dep-ctr-runasgroup-0", "deployment", 14, lambda o: _csc(o).update(runAsGroup=0)),
        seat("dep-ctr-command-override", "deployment", 14, lambda o: _ctr(o).update(command=["/bin/sh", "-c", "id"])),
        seat("dep-ctr-args-other", "deployment", 14, lambda o: _ctr(o).update(args=["agentforge", "provisioner"])),
        seat("dep-ctr-args-absent", "deployment", 14, lambda o: _ctr(o).pop("args")),
        seat("dep-ctr-hostport", "deployment", 14, lambda o: _ctr(o)["ports"][0].update(hostPort=8700)),
        seat("dep-ctr-stdin", "deployment", 14, lambda o: _ctr(o).update(stdin=True)),
        seat("dep-ctr-tty", "deployment", 14, lambda o: _ctr(o).update(tty=True)),
        seat("dep-ctr-volumedevices", "deployment", 14, lambda o: _ctr(o).update(volumeDevices=[{"name": "tmp", "devicePath": "/dev/x"}])),
        seat("dep-nodename", "deployment", 14, lambda o: _dep_spec(o).update(nodeName="ai-agent-1")),
        seat("dep-hostaliases", "deployment", 14, lambda o: _dep_spec(o).update(hostAliases=[
            {"ip": "10.0.0.1", "hostnames": ["api.anthropic.com"]}])),
        seat("dep-shareprocessnamespace", "deployment", 14, lambda o: _dep_spec(o).update(shareProcessNamespace=True)),
        seat("dep-imagepullsecrets", "deployment", 14, lambda o: _dep_spec(o).update(imagePullSecrets=[{"name": f"{GIT_STEM}-oauth"}])),
        seat("dep-scheduler-other", "deployment", 14, lambda o: _dep_spec(o).update(schedulerName="attacker")),
        seat("dep-pod-sc-defaulted-scheduler-admitted", "deployment", None, lambda o: _dep_spec(o).update(schedulerName="default-scheduler")),
        # ---- (15) probes / lifecycle (review round 1, F5) ------------------------------------------
        seat("dep-readiness-host", "deployment", 15, lambda o: _ctr(o)["readinessProbe"]["httpGet"].update(host="10.96.0.1")),
        seat("dep-liveness-host", "deployment", 15, lambda o: _ctr(o)["livenessProbe"]["httpGet"].update(host="169.254.169.254")),
        seat("dep-readiness-tcpsocket", "deployment", 15, lambda o: (_ctr(o)["readinessProbe"].pop("httpGet"),
            _ctr(o)["readinessProbe"].update(tcpSocket={"host": "10.96.0.1", "port": 443}))),
        seat("dep-readiness-exec", "deployment", 15, lambda o: (_ctr(o)["readinessProbe"].pop("httpGet"),
            _ctr(o)["readinessProbe"].update(exec={"command": ["/bin/sh", "-c", "true"]}))),
        seat("dep-liveness-grpc", "deployment", 15, lambda o: (_ctr(o)["livenessProbe"].pop("httpGet"),
            _ctr(o)["livenessProbe"].update(grpc={"port": 8700}))),
        seat("dep-readiness-exec-plus-httpget", "deployment", 15, lambda o: _ctr(o)["readinessProbe"].update(
            exec={"command": ["true"]})),
        seat("dep-liveness-exec-plus-httpget", "deployment", 15, lambda o: _ctr(o)["livenessProbe"].update(
            exec={"command": ["true"]})),
        seat("dep-readiness-path-other", "deployment", 15, lambda o: _ctr(o)["readinessProbe"]["httpGet"].update(path="/healthz")),
        seat("dep-liveness-path-other", "deployment", 15, lambda o: _ctr(o)["livenessProbe"]["httpGet"].update(path="/readyz")),
        seat("dep-readiness-port-other", "deployment", 15, lambda o: _ctr(o)["readinessProbe"]["httpGet"].update(port="metrics")),
        seat("dep-liveness-port-number", "deployment", 15, lambda o: _ctr(o)["livenessProbe"]["httpGet"].update(port=8700)),
        seat("dep-readiness-httpheaders", "deployment", 15, lambda o: _ctr(o)["readinessProbe"]["httpGet"].update(
            httpHeaders=[{"name": "Host", "value": "attacker.example"}])),
        seat("dep-readiness-scheme-https", "deployment", 15, lambda o: _ctr(o)["readinessProbe"]["httpGet"].update(scheme="HTTPS")),
        seat("dep-readiness-scheme-http-admitted", "deployment", None, lambda o: _ctr(o)["readinessProbe"]["httpGet"].update(scheme="HTTP")),
        seat("dep-startup-probe", "deployment", 15, lambda o: _ctr(o).update(startupProbe={"httpGet": {"path": "/", "port": "http"}})),
        seat("dep-lifecycle-poststart", "deployment", 15, lambda o: _ctr(o).update(lifecycle={"postStart": {"httpGet": {"host": "10.96.0.1", "path": "/", "port": 443}}})),
        seat("dep-lifecycle-prestop-exec", "deployment", 15, lambda o: _ctr(o).update(lifecycle={"preStop": {"exec": {"command": ["true"]}}})),
        seat("dep-no-readiness-probe", "deployment", 15, lambda o: _ctr(o).pop("readinessProbe")),
        seat("dep-no-liveness-probe", "deployment", 15, lambda o: _ctr(o).pop("livenessProbe")),
        # ---- the SA gate: other identities writing the same kinds are NOT this policy's business ---
        seat("objects-guard-skips-cp-sa", "deployment", SKIP, unstamp, username=CP_SA),
        seat("objects-guard-skips-flux-sa", "deployment", SKIP, unstamp, username=FLUX_SA),
    ]
    return cases


def builtin_cases(baseline: Baseline) -> list[Case]:
    return cp_cases() + objects_cases(baseline)


# -- evaluation -------------------------------------------------------------------------------

# cel-python 0.5.0 has no `_==_`/`_!=_` overload for (message, null) — the apiserver's CEL does.
# Re-supply exactly that overload and delegate every other comparison to the stock operator.
def _cel_eq(a: Any, b: Any) -> celtypes.BoolType:
    if a is None or b is None:
        return celtypes.BoolType(a is None and b is None)
    return celtypes.BoolType(bool(operator.eq(a, b)))


def _cel_ne(a: Any, b: Any) -> celtypes.BoolType:
    if a is None or b is None:
        return celtypes.BoolType(not (a is None and b is None))
    return celtypes.BoolType(bool(operator.ne(a, b)))


# cel-python 0.5.0 ships none of the CEL *strings extension*, which Kubernetes DOES enable; the
# objects guard re-derives a seat's audience path with `split`. Supply that one member.
def _cel_split(text: Any, sep: Any) -> celtypes.ListType:
    return celtypes.ListType([celtypes.StringType(p) for p in str(text).split(str(sep))])


EXTRA_FUNCTIONS = {"_==_": _cel_eq, "_!=_": _cel_ne, "split": _cel_split}

_VAR_REF = re.compile(r"\bvariables\.([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Verdict:
    kind: str  # "admit" | "deny" | "skip"
    detail: int | str | None = None  # deny: 1-based validation index; skip: why

    def __str__(self) -> str:
        if self.kind == "admit":
            return "ADMIT"
        if self.kind == "deny":
            return f"DENY by validation ({self.detail})"
        return f"SKIP (not matched: {self.detail})"

    def expected_by(self, expect: int | str | None) -> bool:
        if expect is None:
            return self.kind == "admit"
        if expect == SKIP:
            return self.kind == "skip"
        return self.kind == "deny" and self.detail == expect


def load_policies(path: Path = POLICY) -> dict[str, dict[str, Any]]:
    """The policies of `path`, each REQUIRED to be bound the only way this harness can model:
    exactly one binding, validationActions [Deny], no matchResources/paramRef, and no policy-level
    namespaceSelector/objectSelector/excludeResourceRules/matchPolicy. Anything else is refused
    (a selector could disable enforcement while the table stays green)."""
    policies: dict[str, dict[str, Any]] = {}
    bindings: dict[str, list[dict[str, Any]]] = {}
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "ValidatingAdmissionPolicy":
            policies[doc["metadata"]["name"]] = doc
        elif doc and doc.get("kind") == "ValidatingAdmissionPolicyBinding":
            bindings.setdefault(doc["spec"]["policyName"], []).append(doc)
    if not policies:
        raise SystemExit(f"{path}: no ValidatingAdmissionPolicy document")
    for name, pol in policies.items():
        mc = pol["spec"].get("matchConstraints", {})
        for key in UNSUPPORTED_MATCH:
            if key in mc:
                raise SystemExit(f"{path}: policy {name} sets matchConstraints.{key}, which this harness "
                                 "cannot model — refusing to evaluate as if it were absent")
        bs = bindings.get(name, [])
        if len(bs) != 1:
            raise SystemExit(f"{path}: policy {name} needs exactly one binding, found {len(bs)}")
        spec = bs[0]["spec"]
        if spec.get("validationActions") != ["Deny"]:
            raise SystemExit(f"{path}: binding for {name} must have validationActions [Deny], has {spec.get('validationActions')}")
        for key in ("matchResources", "paramRef"):
            if key in spec:
                raise SystemExit(f"{path}: binding for {name} sets {key}, which this harness cannot model")
    return policies


def _rule_matches(rule: dict[str, Any], group: str, version: str, resource: str, operation: str) -> bool:
    groups = rule.get("apiGroups", [])
    versions = rule.get("apiVersions", [])
    ops = rule.get("operations", [])
    resources = rule.get("resources", [])
    if "*" not in groups and group not in groups:
        return False
    if "*" not in versions and version not in versions:
        return False
    if "*" not in ops and operation not in ops:
        return False
    main, _, sub = resource.partition("/")
    for r in resources:
        if r == resource or r == "*/*":
            return True
        if r == "*" and not sub:
            return True
        if r == f"{main}/*" and sub:
            return True
    return False


_ENV = celpy.Environment()
_PROGRAMS: dict[str, Any] = {}


def _program(expression: str) -> Any:
    """Compile each distinct expression once (the table evaluates the same ~30 expressions for
    every case; compiling is the slow half of cel-python)."""
    prog = _PROGRAMS.get(expression)
    if prog is None:
        prog = _ENV.program(_ENV.compile(expression), functions=EXTRA_FUNCTIONS)
        _PROGRAMS[expression] = prog
    return prog


class _Evaluator:
    """One policy + one request; variables are memoised and computed on first reference."""

    def __init__(self, policy: dict[str, Any], base: dict[str, Any]) -> None:
        self.base = base
        self.decls = {v["name"]: v["expression"] for v in policy["spec"].get("variables", [])}
        self.order = [v["name"] for v in policy["spec"].get("variables", [])]
        self.memo: dict[str, Any] = {}

    def _needed(self, expression: str) -> list[str]:
        needed: set[str] = set()
        todo = list(_VAR_REF.findall(expression))
        while todo:
            n = todo.pop()
            if n in needed or n not in self.decls:
                continue
            needed.add(n)
            todo.extend(_VAR_REF.findall(self.decls[n]))
        return [n for n in self.order if n in needed]  # declaration order, as the apiserver

    def _activation(self) -> dict[str, Any]:
        act = dict(self.base)
        act["variables"] = celtypes.MapType(
            {celtypes.StringType(k): v for k, v in self.memo.items()}
        )
        return act

    def _run(self, expression: str) -> Any:
        result = _program(expression).evaluate(self._activation())
        if isinstance(result, celpy.CELEvalError):
            raise result
        return result

    def eval(self, expression: str) -> Any:
        for name in self._needed(expression):
            if name not in self.memo:
                self.memo[name] = self._run(self.decls[name])
        return self._run(expression)


def evaluate(policy: dict[str, Any], case: Case) -> Verdict:
    """Mirror the apiserver: resourceRules -> matchConditions -> validations in order (first DENY wins;
    an expression error is a DENY by that clause under failurePolicy: Fail)."""
    obj = case.object if case.object is not None else case.old_object
    if obj is None:
        raise SystemExit(f"{case.name}: a case needs an object or an oldObject")
    kind = obj["kind"]
    if kind not in RESOURCES:
        raise SystemExit(f"{case.name}: unknown kind {kind}; add it to RESOURCES")
    group, version, plural = RESOURCES[kind]
    resource = plural + (f"/{case.subresource}" if case.subresource else "")
    rules = policy["spec"].get("matchConstraints", {}).get("resourceRules", [])
    if not any(_rule_matches(r, group, version, resource, case.operation) for r in rules):
        return Verdict("skip", "resourceRules")

    ns = case.namespace if case.namespace is not None else obj.get("metadata", {}).get("namespace", "")
    base = {
        "object": celpy.json_to_cel(case.object),
        "oldObject": celpy.json_to_cel(case.old_object),
        "params": celpy.json_to_cel(None),
        "request": celpy.json_to_cel(
            {
                "kind": {"group": group, "version": version, "kind": kind},
                "resource": {"group": group, "version": version, "resource": plural},
                "subResource": case.subresource,
                "name": obj.get("metadata", {}).get("name", ""),
                "namespace": ns,
                "operation": case.operation,
                "userInfo": {"username": case.username, "groups": ["system:serviceaccounts"]},
            }
        ),
        "namespaceObject": celpy.json_to_cel(
            {"metadata": {"name": ns, "labels": {"kubernetes.io/metadata.name": ns}}} if ns else None
        ),
    }
    ev = _Evaluator(policy, base)
    for mc in policy["spec"].get("matchConditions", []):
        if not ev.eval(mc["expression"]):
            return Verdict("skip", mc["name"])
    for idx, val in enumerate(policy["spec"]["validations"], 1):
        try:
            ok = ev.eval(val["expression"])
        except Exception:  # an eval error DENIES (failurePolicy: Fail)
            return Verdict("deny", idx)
        if not ok:
            return Verdict("deny", idx)
    return Verdict("admit")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--policy", type=Path, default=POLICY, metavar="PATH",
                    help="a candidate copy of brokerseat-admission.yaml (default: the tree's)")
    ap.add_argument("--fixtures", type=Path, metavar="JSON",
                    help="evaluate this case table INSTEAD of the built-in one (format: --dump-fixtures)")
    ap.add_argument("--seat-file", type=Path, metavar="PATH",
                    help="the git seat to build the baseline from (default: first mechanical-stem seat)")
    ap.add_argument("--dump-fixtures", type=Path, metavar="JSON",
                    help="write the built-in case table as JSON and exit")
    args = ap.parse_args()

    policies = load_policies(args.policy)
    if args.fixtures:
        cases = [Case.from_json(c) for c in json.loads(args.fixtures.read_text(encoding="utf-8"))["cases"]]
        source = str(args.fixtures)
    else:
        baseline = load_baseline(args.seat_file)
        cases = builtin_cases(baseline)
        source = f"baseline {baseline.source} (stem {baseline.stem}, aud {baseline.aud})"
    if args.dump_fixtures:
        args.dump_fixtures.write_text(json.dumps({"cases": [c.to_json() for c in cases]}, indent=2) + "\n",
                                      encoding="utf-8")
        print(f"wrote {len(cases)} cases to {args.dump_fixtures}")
        return 0

    names = [c.name for c in cases]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise SystemExit(f"duplicate case names: {dupes}")

    failures: list[str] = []
    per_policy: dict[str, int] = {}
    for case in cases:
        if case.policy not in policies:
            raise SystemExit(f"{case.name}: policy {case.policy} not in {args.policy}")
        got = evaluate(policies[case.policy], case)
        ok = got.expected_by(case.expect)
        per_policy[case.policy] = per_policy.get(case.policy, 0) + 1
        if args.verbose or not ok:
            want = ("ADMIT" if case.expect is None else "SKIP" if case.expect == SKIP
                    else f"DENY by validation ({case.expect})")
            print(f"{'ok  ' if ok else 'FAIL'} {case.name}: {got}" + ("" if ok else f"  [want {want}]"))
        if not ok:
            failures.append(case.name)

    print(f"\n{source}")
    for name, n in sorted(per_policy.items()):
        print(f"  {name}: {n} cases")
    print(f"{len(cases) - len(failures)}/{len(cases)} seat-guard CEL cases passed")
    if failures:
        print("FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
