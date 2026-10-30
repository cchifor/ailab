#!/usr/bin/env python3
"""Revoke ONE legacy capability kid from ONE broker kid registry, with the provisioner's own code.

Run through scripts/af-revoke-legacy-kid.sh, which pipes this file into the agentforge-provisioner pod
(`kubectl exec -i ... -- python - <args>`), so the DEPLOYED image's code and the provisioner's own
OpenBao identity (AF_PROVISIONER_OPENBAO_URL / _TOKEN, already allowed to write
af/operator/broker/*/kids) do the work. No token touches the workstation.

The removal itself is agentforge's `capability_kid._cas_remove_from_registry`: it deletes the kid
only when its public_key matches (another incarnation is `superseded` and left alone), treats a read
error as an error and a missing kid as `absent`, preserves every sibling and every other kid,
validates the WHOLE result with the broker's parse_kid_registry, CAS-writes with a bounded re-read,
and emits a `revoked` audit record. This script only adds the pre-checks around it.

Pre-checks, on every run (dry or not), via `precheck()`, which is pure so scripts/tests can cover it:
  * the retired kid's record matches the expected identity: the sha256 prefix of its public key,
    its allowed_tenants and its allowed_workspaces. Anything else is a hard stop.
  * the kid that must SURVIVE (platform-dev's) is present with its expected public-key prefix.
Output is kid names, KV versions and key fingerprints only. With --execute it also prints one
`ROLLBACK <json>` line: the removed PUBLIC record plus aud and version, which is public-key material
only. The wrapper saves it out of the repo.

Written for the 2026-10-30 playground retirement (plans/2026-10-30-retire-playground-plan.md, Phase 3).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from typing import Any


class PrecheckError(Exception):
    """A pre-check failed: nothing may be written."""


def pk_sha256(record: Mapping[str, Any]) -> str:
    """sha256 hex of the record's stripped PEM public key (the value brokers report as `digest`)."""
    return hashlib.sha256(str(record.get("public_key", "")).strip().encode()).hexdigest()


def precheck(
    registry: Mapping[str, Any],
    *,
    kid: str,
    expect_pk: str,
    expect_tenants: list[str],
    expect_workspaces: list[str],
    keep_kid: str,
    keep_pk: str,
) -> dict[str, Any] | None:
    """Validate a parsed registry.json before a removal. Returns the retired kid's record, or None
    when it is already absent (a resumed run). Raises PrecheckError on any mismatch."""
    kids = registry.get("kids")
    if not isinstance(kids, dict):
        raise PrecheckError("registry has no 'kids' object")
    if len(expect_pk) < 12 or len(keep_pk) < 12:
        raise PrecheckError("fingerprint prefixes must be at least 12 hex characters")
    keep = kids.get(keep_kid)
    if not isinstance(keep, dict):
        raise PrecheckError(f"the surviving kid {keep_kid!r} is not in this registry")
    if not pk_sha256(keep).startswith(keep_pk):
        raise PrecheckError(f"the surviving kid {keep_kid!r} has an unexpected public key")
    record = kids.get(kid)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise PrecheckError(f"kid {kid!r}: entry is not an object")
    if not pk_sha256(record).startswith(expect_pk):
        raise PrecheckError(f"kid {kid!r}: public key does not match the expected fingerprint")
    if record.get("allowed_tenants") != expect_tenants:
        raise PrecheckError(f"kid {kid!r}: allowed_tenants is {record.get('allowed_tenants')!r}")
    if record.get("allowed_workspaces") != expect_workspaces:
        raise PrecheckError(f"kid {kid!r}: allowed_workspaces is {record.get('allowed_workspaces')!r}")
    return dict(record)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--aud", required=True, help="<provider>/<account>, e.g. anthropic/claude-max-1")
    p.add_argument("--kid", required=True)
    p.add_argument("--expect-pk", required=True, help="sha256 hex prefix of the kid's public key")
    p.add_argument("--expect-tenant", action="append", required=True)
    p.add_argument("--expect-workspace", action="append", required=True)
    p.add_argument("--keep-kid", required=True, help="a kid that must survive, untouched")
    p.add_argument("--keep-pk", required=True, help="sha256 hex prefix of that kid's public key")
    p.add_argument("--execute", action="store_true", help="remove it (default: dry run)")
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:  # pragma: no cover - runs inside the provisioner pod
    import os

    from prometheus_client import CollectorRegistry

    from agentforge.provisioner.audit import ProvisionerAudit, ProvisionerMetrics
    from agentforge.provisioner.capability_kid import (
        OPERATOR_REGISTRY_KEY,
        _cas_remove_from_registry,
        operator_kids_registry_path,
    )
    from agentforge.provisioner.openbao import HvacOpenBaoClient

    client = HvacOpenBaoClient(
        url=os.environ["AF_PROVISIONER_OPENBAO_URL"],
        token=os.environ["AF_PROVISIONER_OPENBAO_TOKEN"],
        verify=os.environ.get("REQUESTS_CA_BUNDLE") or True,
    )
    path = operator_kids_registry_path(args.aud)

    async def read() -> tuple[dict[str, Any], int]:
        doc, version = await client.read_secret_versioned(path)
        if doc is None:
            raise PrecheckError(f"{path}: no document")
        return json.loads(doc[OPERATOR_REGISTRY_KEY]), version

    checks = dict(kid=args.kid, expect_pk=args.expect_pk, expect_tenants=args.expect_tenant,
                  expect_workspaces=args.expect_workspace, keep_kid=args.keep_kid, keep_pk=args.keep_pk)
    registry, version = await read()
    record = precheck(registry, **checks)
    keep_before = registry["kids"][args.keep_kid]
    print(f"{args.aud}: version={version} kids={sorted(registry['kids'])}")
    if record is None:
        print(f"{args.aud}: {args.kid} already absent - nothing to do")
        return 0
    print(f"{args.aud}: {args.kid} matches (pk sha256 {pk_sha256(record)[:16]}, "
          f"tenants={record['allowed_tenants']}, workspaces={record['allowed_workspaces']})")
    if not args.execute:
        print(f"{args.aud}: DRY RUN - pass --execute to remove it")
        return 0
    print("ROLLBACK " + json.dumps({"aud": args.aud, "version": version, "kid": args.kid, "record": record}))
    outcome = await _cas_remove_from_registry(
        client, args.aud, args.kid,
        org=args.expect_tenant[0], ws=args.expect_workspace[0],
        expected_public_key=record["public_key"],
        audit=ProvisionerAudit(ProvisionerMetrics(registry=CollectorRegistry())),
    )
    print(f"{args.aud}: outcome={outcome}")
    after, after_version = await read()
    if args.kid in after["kids"]:
        raise PrecheckError(f"{args.aud}: {args.kid} is still present after outcome={outcome}")
    if after["kids"].get(args.keep_kid) != keep_before:
        raise PrecheckError(f"{args.aud}: {args.keep_kid}'s record CHANGED - investigate now")
    print(f"{args.aud}: version={after_version} kids={sorted(after['kids'])} "
          f"({args.keep_kid} unchanged)")
    return 0 if outcome in ("removed", "absent") else 1


def main(argv: list[str]) -> int:  # pragma: no cover - runs inside the provisioner pod
    import asyncio

    try:
        return asyncio.run(_run(_parse_args(argv)))
    except PrecheckError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
