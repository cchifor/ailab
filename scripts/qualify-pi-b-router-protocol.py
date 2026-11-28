#!/usr/bin/env python3
"""Explicit offline B management-protocol qualification; never auto-discovered.

Release-gate invocation (all paths are supplied by the qualification job):
  python scripts/qualify-pi-b-router-protocol.py \
    --router-checkout /qualified/router \
    --dependencies-volume qualified-router-dependencies --output /new/proof

The existing Docker volume must contain package.json, pnpm-lock.yaml and
node_modules. The checkout must
already contain the manifest's exact commit. No fetch, image pull, installation,
credential, live service or default skip is provided. Missing inputs fail.

The current pin is the locally qualified integration candidate, not a published
release. CI must explicitly supply its verified checkout/cache; later release
qualification updates the pins through review. Cache lock equality is checked,
but is not a cryptographic audit of installed dependency contents. Existing
0bc/6c796 proof remains historical; each invocation writes a new proof directory.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/qualification/pi-b-router-protocol.json"
TEST_NAME = "relay-b-fence-control-protocol.test.ts"

PREFLIGHT = r"""
const fs = require('node:fs'), crypto = require('node:crypto'), assert = require('node:assert/strict');
const pins = JSON.parse(fs.readFileSync('/input/pins.json'));
const sha = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
assert.equal(process.version, pins.nodeVersion);
assert.equal(process.getuid(), 1000);
assert.equal(sha('/input/router.tar'), pins.routerArchiveSha256);
assert.equal(sha('/work/package.json'), pins.packageSha256);
assert.equal(sha('/work/pnpm-lock.yaml'), pins.lockSha256);
for (const [path, hash] of Object.entries(pins.files)) {
  const name = path.split('/').at(-1);
  const actual = name.endsWith('.test.ts') ? '/work/tests/' + name : '/input/' + name;
  assert.equal(sha(actual), hash);
}
console.log(JSON.stringify({source: pins.routerCommit, image: pins.image, node: process.version, uid: process.getuid(), inputHashesVerified: true}));
"""


def need(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(checkout, *args):
    result = subprocess.run(
        ["git", "-C", str(checkout), *args], capture_output=True, timeout=30,
    )
    need(result.returncode == 0, "Required local Router Git input is unavailable")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--router-checkout", type=Path, required=True)
    parser.add_argument("--dependencies-volume", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pins_bytes = MANIFEST.read_bytes()
    pins = json.loads(pins_bytes)
    need(pins["version"] == 1, "Unsupported protocol qualification pins")
    docker = shutil.which("docker")
    need(docker is not None and shutil.which("git") is not None, "Docker and Git are required; no skip")
    checkout = args.router_checkout.resolve(strict=True)
    need(git(checkout, "rev-parse", "HEAD").decode().strip() == pins["routerCommit"], "Router HEAD differs from the qualified commit")
    need(not git(checkout, "status", "--porcelain", "--untracked-files=no").strip(), "Router tracked files must be clean")
    file_manifest = git(checkout, "ls-tree", "-r", "--full-tree", pins["routerCommit"])
    need(sha(file_manifest) == pins["routerFileManifestSha256"], "Router file manifest differs")
    for name, pin in [("package.json", "packageSha256"), ("pnpm-lock.yaml", "lockSha256")]:
        need(sha(git(checkout, "show", pins["routerCommit"] + ":" + name)) == pins[pin], "Router package/lock differs")
    snapshots = {}
    for path, expected in pins["files"].items():
        data = (ROOT / path).read_bytes()
        need(sha(data) == expected, "Reviewed fixture/helper source differs: " + path)
        snapshots[Path(path).name] = data
    need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.dependencies_volume), "Invalid existing cache volume name")
    dep_mount = "type=volume,source=" + args.dependencies_volume + ",target=/deps,readonly,volume-nocopy"
    check = subprocess.run([docker, "volume", "inspect", args.dependencies_volume], capture_output=True, timeout=30)
    need(check.returncode == 0, "Dependency volume must already exist; no empty cache creation")
    image = subprocess.run([docker, "image", "inspect", pins["image"], "--format", "{{.Id}}"], capture_output=True, text=True, timeout=30)
    need(image.returncode == 0 and image.stdout.strip() == pins["image"], "Exact cached image is required; no pull")
    archive = git(checkout, "archive", "--format=tar", pins["routerCommit"])
    need(sha(archive) == pins["routerArchiveSha256"], "Exact local Router Git archive differs")
    # Refuse links and special entries entirely, rather than relying on tar to
    # resolve them safely. Every extraction target stays under the private root.
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as table:
        for member in table:
            need(member.isfile() or member.isdir(), "Archive links/special files are forbidden")
            need(not member.name.startswith("/") and "\\" not in member.name
                 and all(part not in (".", "..") for part in member.name.rstrip("/").split("/")), "Archive path escapes private root")
    output = args.output.resolve()
    need("," not in str(output), "Docker output path cannot contain a comma")
    need(not output.exists(), "Output must be new; existing evidence is never overwritten")
    output.mkdir(parents=True, mode=0o700)
    inputs = output / "inputs"
    inputs.mkdir(mode=0o755)
    inputs.chmod(0o755)  # Synthetic source inputs must be readable by UID 1000.
    evidence = output / "evidence"
    evidence.mkdir(mode=0o777)
    evidence.chmod(0o777)  # Synthetic evidence only, below the private output root.
    for name, data in snapshots.items():
        (inputs / name).write_bytes(data)
    (inputs / "pins.json").write_bytes(pins_bytes)
    (inputs / "preflight.cjs").write_text(PREFLIGHT, encoding="utf-8")
    (inputs / "router-file-manifest.txt").write_bytes(file_manifest)
    (inputs / "router.tar").write_bytes(archive)
    run_id = uuid.uuid4().hex
    volume, container = "pi-b-protocol-" + run_id, "pi-b-protocol-run-" + run_id
    created = False
    outcome = {"passed": False, "routerCommit": pins["routerCommit"], "manifestSha256": sha(pins_bytes), "inputFiles": {name: sha(data) for name, data in snapshots.items()}, "scope": pins["scope"]}

    def invoke(label, argv, timeout=180, required=True):
        (output / (label + "-argv.json")).write_text(json.dumps(argv, indent=2) + "\n", encoding="utf-8")
        with (output / (label + ".log")).open("wb") as log:
            result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        (output / (label + "-result.json")).write_text(json.dumps({"exitCode": result.returncode}) + "\n", encoding="utf-8")
        if required:
            need(result.returncode == 0, label + " failed; see retained log")
        return result.returncode

    common = [docker, "run", "--rm", "--init", "--pull=never", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,mode=1777", "--env", "HOME=/tmp", "--mount", "type=volume,source=" + volume + ",target=/work,volume-nocopy"]
    try:
        created = True  # Also clean up an uncertain create reply for this UUID.
        invoke("create-volume", [docker, "volume", "create", "--label", "qualification=" + run_id, volume], 30)
        invoke("prepare-volume", common + ["--name", container, pins["image"], "sh", "-c", 'test -z "$(ls -A /work)" && chmod 0777 /work'], 30)
        # All shell text is fixed. User paths enter structured Docker mount args.
        command = (
            "tar --no-same-owner -xf /input/router.tar -C /work && "
            "cp --no-preserve=ownership /input/" + TEST_NAME + " /work/tests/" + TEST_NAME + " && "
            "cmp /work/package.json /deps/package.json && cmp /work/pnpm-lock.yaml /deps/pnpm-lock.yaml && "
            "cp -a --no-preserve=ownership /deps/node_modules /work/node_modules && "
            "cmp /work/pnpm-lock.yaml /work/node_modules/.pnpm/lock.yaml && mkdir /work/data && "
            "node /input/preflight.cjs && node node_modules/typescript/bin/tsc --noEmit && "
            "node node_modules/vitest/vitest.mjs run tests/" + TEST_NAME
        )
        invoke("protocol", common + ["--name", container, "--user", "1000:1000", "--mount", dep_mount,
            "--mount", "type=bind,source=" + str(inputs) + ",target=/input,readonly",
            "--mount", "type=bind,source=" + str(evidence) + ",target=/evidence",
            "--env", "B_FENCE_HELPER=/input/owner-phase-b-fence.cjs", "--env", "B_FENCE_EVIDENCE=/evidence",
            "--workdir", "/work", pins["image"], "sh", "-c", command])
        for name in ["positive.json", "wrong-receipt-digest.json"]:
            record = json.loads((evidence / name).read_bytes())
            need(record["source"] == pins["routerCommit"] and record["providerOpens"] == 0, "Protocol evidence scope differs")
        outcome["passed"] = True
    finally:
        # Unique names are created only by this invocation. No cache is removed.
        if created:
            try:
                invoke("cleanup-container", [docker, "rm", "-f", container], 30, required=False)
                removed = invoke("cleanup-volume", [docker, "volume", "rm", volume], 30, required=False)
                outcome["privateVolumeRemoved"] = removed == 0
                if removed != 0:
                    outcome["passed"] = False
            except (OSError, subprocess.TimeoutExpired):
                outcome["privateVolumeRemoved"] = False
                outcome["passed"] = False
        outcome["evidence"] = {str(path.relative_to(output)): sha(path.read_bytes()) for path in output.rglob("*") if path.is_file() and path.name != "qualification.json"}
        (output / "qualification.json").write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    need(outcome["passed"], "Qualification or cleanup failed; see retained proof")
    print(json.dumps({"passed": True, "proof": str(output / "qualification.json"), "routerCommit": pins["routerCommit"]}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print("Protocol qualification failed: " + str(error), file=sys.stderr)
        sys.exit(1)
