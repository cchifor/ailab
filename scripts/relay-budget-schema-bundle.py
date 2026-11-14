#!/usr/bin/env python3
"""Export a public, immutable ledger schema bundle from one reviewed Relay commit.

This renders files only. It neither fetches source nor contacts Kubernetes or a
database. The source commit is the single reviewed pin in
kubernetes/optional/relay-budget-authority/initialize/relay-source.env; both
standalone Jobs refuse a bundle from any other commit. `--validation-commit`
renders an UNPINNED bundle for local validation only (receipt `pinned: false`).
The caller must verify review/merge status and pin the printed manifest digest
in the standalone initialization or upgrade Job before applying it.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = ROOT / "kubernetes/optional/relay-budget-authority/initialize/relay-source.env"
SCHEMAS = (
    "001-authority.sql", "002-preflight.sql", "003-scoped-requests.sql",
    "004-preflight-errors.sql", "005-provenance-purpose.sql",
)
SCHEMA_VERSION = 5


def pinned_commit(path: Path = PIN_FILE) -> str:
    """The reviewed pin, or "" when unset. Same grammar the Jobs' verify-schema.sh accepts."""
    values = []
    for line in path.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = re.fullmatch(r"RELAY_BUDGET_SOURCE_COMMIT=([0-9a-f]{40})?", line)
        if not match:
            raise ValueError("source pin has an unexpected line")
        values.append(match.group(1) or "")
    if len(values) != 1:
        raise ValueError("source pin must assign RELAY_BUDGET_SOURCE_COMMIT exactly once")
    return values[0]


def render(repository: Path, commit: str, output: Path, pin: str = "") -> dict:
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
    return {"sourceCommit": commit, "pinned": commit == pin, "schemaVersion": SCHEMA_VERSION,
            "manifestSha256": manifest_digest, "qualification": "not-evaluated"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relay_repository", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--validation-commit",
                        help="render this commit instead of the pin; the Jobs refuse the result")
    args = parser.parse_args()
    try:
        pin = pinned_commit()
        commit = args.validation_commit or pin
        if not commit:
            raise ValueError("no reviewed source pin is set")
        receipt = render(args.relay_repository, commit, args.output_directory, pin)
    except (ValueError, OSError, subprocess.CalledProcessError):
        parser.exit(1, "Schema bundle refused: check the source pin, files and fresh output directory\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
