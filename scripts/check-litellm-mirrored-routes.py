#!/usr/bin/env python3
"""check-litellm-mirrored-routes.py — keep litellm-local's DIAGNOSTIC routes a verbatim copy of
the main gateway's, so a reproduction through litellm-local means what it claims.

WHY THIS EXISTS. litellm-local.yaml carries `qwen3.8-27b-vllm-cloud` (added 2026-09-22) for one
reason: a dev worker holding the fleet's diagnostic virtual key can replay a production LLM
failure THROUGH a gateway instead of against a bare backend. That is only evidence if the route
is the same route production uses on the main `litellm` gateway — same `model`, `api_base`,
sampling params, `extra_body`, `model_info`, same deployment order. The lockstep was first
enforced by a comment ("keep the two files in lockstep"), which reviewer-claude on ailab #822
correctly called out: an edit to the main route that is not mirrored silently turns "which
gateway answered" back into a behavioural question, and nothing in CI would notice.

WHAT IT CHECKS, per mirrored model_name:
  * the main gateway's deployments for that name, in order, equal litellm-local's, in order,
    AFTER dropping litellm-local's synthetic `*_cost_per_token` fields (those exist only so the
    diagnostic key's budget meters; they are never sent upstream);
  * litellm-local's copies DO carry both cost fields — a route without them meters no spend, so
    the key's budget would never enforce on it (the litellm-local.yaml header explains why the
    fields must sit under litellm_params).

WHAT IT DOES NOT CHECK (stated so nobody over-reads a green run): the gateway-LEVEL settings.
The main gateway runs `litellm_settings` (drop_params, request_timeout, the chatgpt provider map)
and `router_settings` (least-busy, num_retries, allowed_fails/cooldown, and a FALLBACK from
qwen3.8-27b-vllm-cloud to qwen3.8-27b-ailab); litellm-local runs neither. A repro that passes
here while production fails should look there next — in particular the fallback, which can hand
a llama.cpp response to a caller that asked for the vLLM route.

FAIL-CLOSED, the same shape as check-vkey-models.py: a missing file, an unparseable document, a
mirrored name absent from either side, or a deployment-count mismatch is a non-zero exit.

Run: `python scripts/check-litellm-mirrored-routes.py [MAIN_YAML LOCAL_YAML]` (paths are
optional and exist for the test harness; CI runs it against the repo files).
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
MAIN = REPO / "kubernetes" / "apps" / "apps" / "ai" / "litellm.yaml"
LOCAL = REPO / "kubernetes" / "apps" / "apps" / "ai" / "litellm-local.yaml"
MAIN_CM = "litellm-config"
LOCAL_CM = "litellm-local-config"

# model_names that litellm-local carries as verbatim mirrors of the main gateway.
MIRRORED = ("qwen3.8-27b-vllm-cloud",)
COST_FIELDS = ("input_cost_per_token", "output_cost_per_token")


class CheckError(Exception):
    """Anything that stops us PROVING the mirror. Never a warning."""


def _model_list(path: pathlib.Path, cm_name: str) -> list[dict]:
    import yaml  # imported here so a missing PyYAML is a CheckError, not a traceback

    if not path.exists():
        raise CheckError(f"{path}: file not found")
    try:
        docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    except Exception as exc:  # noqa: BLE001 — any parse failure is fatal by design
        raise CheckError(f"{path}: unparseable YAML: {exc}") from exc
    for doc in docs:
        if doc.get("kind") != "ConfigMap" or (doc.get("metadata") or {}).get("name") != cm_name:
            continue
        raw = (doc.get("data") or {}).get("config.yaml")
        if raw is None:
            raise CheckError(f"{path}: ConfigMap/{cm_name} has no config.yaml key")
        try:
            cfg = yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001
            raise CheckError(f"{path}: embedded config.yaml is unparseable: {exc}") from exc
        entries = (cfg or {}).get("model_list")
        if not entries:
            raise CheckError(f"{path}: config.yaml has an empty or missing model_list")
        return entries
    raise CheckError(f"{path}: no ConfigMap named {cm_name}")


def _deployments(entries: list[dict], name: str) -> list[dict]:
    return [e for e in entries if (e or {}).get("model_name") == name]


def _comparable(entry: dict) -> dict:
    params = dict(entry.get("litellm_params") or {})
    for f in COST_FIELDS:
        params.pop(f, None)
    return {"litellm_params": params, "model_info": entry.get("model_info")}


def check(main_path: pathlib.Path, local_path: pathlib.Path) -> list[str]:
    """Return a list of problems (empty == the mirror holds)."""
    main = _model_list(main_path, MAIN_CM)
    local = _model_list(local_path, LOCAL_CM)
    problems: list[str] = []
    for name in MIRRORED:
        m = _deployments(main, name)
        l = _deployments(local, name)
        if not m:
            problems.append(f"{name}: not served by the main gateway ({main_path.name}) — nothing to mirror")
            continue
        if not l:
            problems.append(f"{name}: absent from {local_path.name} — the diagnostic route is gone")
            continue
        if len(m) != len(l):
            problems.append(f"{name}: {len(m)} deployment(s) on the main gateway, {len(l)} on litellm-local")
            continue
        for i, (a, b) in enumerate(zip(m, l)):
            for f in COST_FIELDS:
                if f not in (b.get("litellm_params") or {}):
                    problems.append(f"{name}[{i}] on litellm-local lacks litellm_params.{f} — its spend would never meter")
            ca, cb = _comparable(a), _comparable(b)
            if ca != cb:
                diff = _describe_diff(ca, cb)
                problems.append(f"{name}[{i}] differs from the main gateway's deployment {i} (modulo cost): {diff}")
    return problems


def _describe_diff(a: dict, b: dict) -> str:
    out = []
    for section in ("litellm_params", "model_info"):
        da, db = a.get(section) or {}, b.get(section) or {}
        if not isinstance(da, dict) or not isinstance(db, dict):
            if da != db:
                out.append(f"{section}: main={json.dumps(da, sort_keys=True)} local={json.dumps(db, sort_keys=True)}")
            continue
        for key in sorted(set(da) | set(db)):
            if da.get(key) != db.get(key):
                out.append(
                    f"{section}.{key}: main={json.dumps(da.get(key), sort_keys=True)} "
                    f"local={json.dumps(db.get(key), sort_keys=True)}"
                )
    return "; ".join(out) or "(unequal, no field-level detail)"


USAGE = (
    "usage: check-litellm-mirrored-routes.py            # the repo's litellm.yaml + litellm-local.yaml\n"
    "       check-litellm-mirrored-routes.py MAIN LOCAL # explicit paths (test harness / drift hunting)\n"
    "exactly zero or two paths — one path would silently check the repo files instead of yours"
)


def main() -> int:
    args = sys.argv[1:]
    if len(args) == 0:
        main_path, local_path = MAIN, LOCAL
    elif len(args) == 2:
        main_path, local_path = pathlib.Path(args[0]), pathlib.Path(args[1])
    else:
        # Fail closed on a malformed invocation: a single path used to be ignored and the run
        # then reported OK about files the caller never named (reviewer-claude, ailab #822).
        print(f"ERROR expected 0 or 2 arguments, got {len(args)}\n{USAGE}", file=sys.stderr)
        return 2
    try:
        problems = check(main_path, local_path)
    except CheckError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    if problems:
        for p in problems:
            print(f"ERROR {p}", file=sys.stderr)
        return 1
    print(
        f"OK {local_path.name}: {', '.join(MIRRORED)} mirrors {main_path.name} deployment-for-deployment "
        "(modulo synthetic cost)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
