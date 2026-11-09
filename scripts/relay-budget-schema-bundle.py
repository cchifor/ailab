#!/usr/bin/env python3
"""Export a public, immutable ledger schema bundle from one reviewed Relay commit.

This renders files only. It neither fetches source nor contacts Kubernetes or a
database. The caller must verify review/merge status and pin the printed manifest
digest in the standalone initialization or upgrade Job before applying it.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

SCHEMAS = (
    "001-authority.sql", "002-preflight.sql", "003-scoped-requests.sql",
    "004-preflight-errors.sql",
)


def render(repository: Path, commit: str, output: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source must be a full lowercase commit SHA")
    resolved = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", commit + "^{commit}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if resolved != commit:
        raise ValueError("source must identify a commit, not a tag object")
    data = {"source-commit": commit + "\n"}
    for name in SCHEMAS:
        value = subprocess.run(
            ["git", "-C", str(repository), "show",
             f"{commit}:deploy/worker-budget-ledger/{name}"],
            check=True, capture_output=True,
        ).stdout.decode("utf-8")
        if not value or "\x00" in value:
            raise ValueError("schema file is empty or not text")
        data[name] = value
    if sum(len(value.encode()) for value in data.values()) > 900_000:
        raise ValueError("schema bundle exceeds the ConfigMap size budget")
    data["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(value.encode()).hexdigest()}  {name}\n"
        for name, value in data.items()
    )
    manifest_digest = hashlib.sha256(data["SHA256SUMS"].encode()).hexdigest()
    manifest = {
        "apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
        "metadata": {
            "name": "relay-budget-reviewed-schema-once",
            "namespace": "relay-budget-authority",
            "labels": {"velero.io/exclude-from-backup": "true"},
            "annotations": {"relay.chifor.me/schema-source-commit": commit,
                            "relay.chifor.me/schema-manifest-sha256": manifest_digest},
        },
        "data": data,
    }
    output.mkdir(mode=0o700)  # Never overwrite an earlier reviewed bundle.
    for name, value in data.items():
        (output / name).write_bytes(value.encode())
    (output / "configmap.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"sourceCommit": commit, "schemaVersion": 4,
            "manifestSha256": manifest_digest, "qualification": "not-evaluated"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relay_repository", type=Path)
    parser.add_argument("reviewed_commit")
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        receipt = render(args.relay_repository, args.reviewed_commit, args.output_directory)
    except (ValueError, OSError, subprocess.CalledProcessError):
        parser.exit(1, "Schema bundle refused: check source commit, files and fresh output directory\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
