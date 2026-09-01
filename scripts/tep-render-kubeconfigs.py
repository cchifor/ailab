#!/usr/bin/env python3
"""Generate ansible/secrets/tep-tokens.sops.yaml from the live tep-dwN token Secrets.

Run by the operator (admin kubectl + SOPS age key) AFTER the testpool tree is merged and
Flux-applied — the Secrets kubernetes/apps/infrastructure/testpool/tep-access.yaml creates must
exist. Then `just dev-workers` renders ~/.tep/kubeconfig on every worker (roles/dev_worker
tasks/tep.yml). Re-run after any token rotation (delete+recreate the Secret, re-run this, re-run
the role).

    export SOPS_AGE_KEY_FILE=kubernetes/infra/_out/age.agekey   # Windows sops ignores %APPDATA%
    python scripts/tep-render-kubeconfigs.py
"""
import base64
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "ansible/secrets/tep-tokens.sops.yaml"
WORKERS = [f"dev-worker-{i}" for i in range(1, 7)]
KC = ["kubectl", "--context", "admin@ai", "-n", "testpool"]


def get_secret(name: str) -> dict:
    r = subprocess.run(KC + ["get", "secret", name, "-o", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"cannot read secret {name} (is the testpool tree applied?): {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)["data"]


def main() -> None:
    lines = []
    ca = None
    for w in WORKERS:
        sa = "tep-" + w.replace("dev-worker-", "dw")
        data = get_secret(f"{sa}-token")
        token = base64.b64decode(data["token"]).decode()
        ca = data["ca.crt"]  # already base64; identical across workers
        lines.append(f"  {w}: {token}")
    plaintext = "tep_cluster_ca_b64: " + ca + "\ntep_tokens:\n" + "\n".join(lines) + "\n"

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, dir=str(OUT.parent), newline="\n") as f:
        f.write(plaintext)
        tmp = f.name
    try:
        enc = subprocess.run(["sops", "--encrypt", tmp], capture_output=True, text=True)
        if enc.returncode != 0:
            sys.exit(f"sops encrypt failed (SOPS_AGE_KEY_FILE set? .sops.yaml rule matches?): {enc.stderr.strip()[:300]}")
        OUT.write_text(enc.stdout, encoding="utf-8")
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)
    print(f"wrote {OUT} ({len(WORKERS)} tokens; run `just dev-workers` to distribute)")


if __name__ == "__main__":
    main()
