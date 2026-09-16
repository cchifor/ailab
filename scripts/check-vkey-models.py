#!/usr/bin/env python3
"""check-vkey-models.py — verify every per-tenant virtual-key model whitelist in
litellm-vkeys.yaml names a model that litellm-local.yaml actually serves.

WHY THIS EXISTS. litellm-vkeys.yaml's `orgs.json` carries, per tenant, a `models`
whitelist that LiteLLM stores on the generated key. That list is a cross-reference
into a DIFFERENT file — litellm-local.yaml's `model_list` — with nothing keeping the
two honest:

  * litellm-vkeys.yaml is deliberately NOT Flux-applied (it is operator-run; a
    tenant-provisioning Job under the wait:true `apps` Kustomization would wedge the
    whole apps layer), so no reconcile ever revisits it after a model is renamed.
  * gen-litellm-consumers.py does not cover it. That generator derives Open WebUI and
    the dsh seed from litellm.yaml — the MAIN proxy. Tenants live on the separate
    local-only gateway, whose model_list it never reads.
  * A dangling whitelist entry does not fail loudly at seed time. The key is created
    with whatever names it was given; the breakage surfaces later, per request, as a
    model-not-found on a key that authenticates perfectly.

This is not hypothetical. Removing qwen3.6-35b-a3b on 2026-09-16 left `example`
whitelisting a model that no longer existed anywhere, and litellm-local's model_list
had gone from that one model to qwen3.8-27b-ailab. Nothing in CI noticed. Nothing live
broke only because the seeding Job had never been run and `example` carries the
40-zero PLACEHOLDER key that seed.py skips by design — luck about deployment state,
not a property of the check surface.

FAIL-CLOSED, the same shape as check-inline-hashes.py and gen-litellm-consumers.py
--check: a missing file, an unparseable document, a whitelist that is not a list of
strings, or an org that names no models at all is a non-zero exit, never a skip. An
empty `models` list is rejected on purpose -- in LiteLLM an empty whitelist means
"all models this key's team can reach", which is the opposite of what this file's
whole point is (see its header: local-only, enforced three ways).

Run: `python scripts/check-vkey-models.py`
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
VKEYS = REPO / "kubernetes" / "apps" / "apps" / "ai" / "litellm-vkeys.yaml"
LOCAL = REPO / "kubernetes" / "apps" / "apps" / "ai" / "litellm-local.yaml"
SPEC_CM = "litellm-vkeys-spec"


class CheckError(Exception):
    """Anything that stops us PROVING the whitelist is a subset. Never a warning."""


def _docs(path: pathlib.Path):
    import yaml  # imported here so a missing PyYAML is a CheckError, not a traceback

    if not path.exists():
        raise CheckError(f"{path.relative_to(REPO)}: file not found")
    try:
        return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    except Exception as exc:  # noqa: BLE001 — any parse failure is fatal by design
        raise CheckError(f"{path.relative_to(REPO)}: unparseable YAML: {exc}") from exc


def served_model_names() -> set[str]:
    """The model_names litellm-local actually serves.

    Folded to a SET on purpose: litellm-local carries one model_name per DEPLOYMENT,
    and qwen3.8-27b-ailab is deliberately two (node2 + node3). A whitelist names the
    model_name, never a deployment, so duplicates here are expected and not an error.
    """
    import yaml

    for doc in _docs(LOCAL):
        if doc.get("kind") != "ConfigMap":
            continue
        raw = (doc.get("data") or {}).get("config.yaml")
        if raw is None:
            continue
        try:
            cfg = yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001
            raise CheckError(f"litellm-local.yaml: embedded config.yaml is unparseable: {exc}") from exc
        entries = (cfg or {}).get("model_list")
        if not entries:
            raise CheckError("litellm-local.yaml: config.yaml has an empty or missing model_list")
        names = set()
        for i, m in enumerate(entries):
            name = (m or {}).get("model_name")
            if not name:
                raise CheckError(f"litellm-local.yaml: model_list[{i}] has no model_name")
            names.add(name)
        return names
    raise CheckError("litellm-local.yaml: no ConfigMap carrying a config.yaml key")


def declared_orgs() -> list[dict]:
    for doc in _docs(VKEYS):
        if doc.get("kind") != "ConfigMap":
            continue
        if (doc.get("metadata") or {}).get("name") != SPEC_CM:
            continue
        raw = (doc.get("data") or {}).get("orgs.json")
        if raw is None:
            raise CheckError(f"litellm-vkeys.yaml: ConfigMap/{SPEC_CM} has no orgs.json key")
        try:
            orgs = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise CheckError(f"litellm-vkeys.yaml: orgs.json is not valid JSON: {exc}") from exc
        if not isinstance(orgs, list):
            raise CheckError("litellm-vkeys.yaml: orgs.json must be a JSON array")
        return orgs
    raise CheckError(f"litellm-vkeys.yaml: no ConfigMap named {SPEC_CM}")


def main() -> int:
    try:
        served = served_model_names()
        orgs = declared_orgs()
    except CheckError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1

    problems: list[str] = []
    for i, org in enumerate(orgs):
        where = f"orgs.json[{i}]"
        if not isinstance(org, dict):
            problems.append(f"{where} is not an object")
            continue
        name = org.get("org", f"<unnamed {i}>")
        models = org.get("models")
        if models is None:
            problems.append(f"{where} org={name!r} has no `models` whitelist")
            continue
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            problems.append(f"{where} org={name!r} `models` must be a list of strings, got {models!r}")
            continue
        if not models:
            problems.append(
                f"{where} org={name!r} has an EMPTY `models` list. In LiteLLM that means "
                f"'every model this key can reach', not 'none' — name the models explicitly."
            )
            continue
        dangling = [m for m in models if m not in served]
        if dangling:
            problems.append(
                f"{where} org={name!r} whitelists {dangling} which litellm-local.yaml does not serve. "
                f"Its model_list is {sorted(served)}. A key minted from this reaches LiteLLM fine and "
                f"then fails EVERY request with model-not-found."
            )

    if problems:
        for p in problems:
            print(f"ERROR {p}", file=sys.stderr)
        return 1

    print(
        f"OK litellm-vkeys.yaml: {len(orgs)} org(s), every whitelisted model served by "
        f"litellm-local.yaml ({', '.join(sorted(served))})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
