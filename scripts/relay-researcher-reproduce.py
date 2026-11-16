#!/usr/bin/env python3
"""Re-run step I4 and the I5 import from the pinned release and require byte-identical results.

docs/runbooks/relay-researcher-installation.md commits the outputs of two tool runs:

  I4  scripts/relay-researcher-binding.mts (with the pinned Relay checkout's code) writes
      docs/relay-workers/researcher-i4/ (binding.json, definition.json, summary.json,
      template-preparation-input.json);
  I5  Relay's scripts/prepare-pod-installation.ts turns that definition into a bundle, and
      scripts/relay-researcher-bundle.py import writes kubernetes/apps/relay-researcher/pins/pins.yaml
      and kubernetes/apps/relay-researcher/agent/config.json from it.

This script re-runs all three against the COMMITTED tree of <ref> (git archive, so uncommitted edits
neither help nor hurt) and exits 1 on any byte difference. It never touches the working tree:

  1. exports <ref> to a private temporary directory;
  2. runs that tree's generator with the identity recorded in researcher-binding-inputs.json
     (`installation`; the generator itself checks the Relay checkout, the asset sha256 pins, the
     fingerprint and Relay's preparation) and compares every file with the committed
     docs/relay-workers/researcher-i4/ (the same file set, byte for byte);
  3. runs Relay's prepare-pod-installation.ts on the regenerated definition and that tree's
     `relay-researcher-bundle.py check --chart` on the bundle;
  4. if the committed pins are filled (step I5 has landed), runs `import` in the exported tree and
     compares the two files it writes with the committed ones, byte for byte. While the pins are
     placeholders (before I5) there is nothing to compare; the script says so and checks the rest.

  relay-researcher-reproduce.py --relay <pinned relay checkout> --assets <release-asset dir> [--ref HEAD]

The Relay checkout must be at the pinned sourceCommit with `npm ci` done, Node 22.21+ on PATH, and
helm on PATH (for --chart). The assets are the five files researcher-binding-inputs.json pins, from
Gitea release 2142 (private: download them with your own token). Stdlib only; reads no secret.
`just relay-researcher-reproduce` wraps it.

CI does not run this: ailab's runners hold no credential for the private cchifor/relay repository
or its release assets. scripts/tests/test_relay_researcher_reproduction.py checks in CI that the
committed outputs, pins, config and provenance agree with each other and with the pinned inputs.
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = "docs/relay-workers/researcher-binding-inputs.json"
I4_OUTPUT = "docs/relay-workers/researcher-i4"
I4_FILES = ("binding.json", "definition.json", "summary.json", "template-preparation-input.json")
IMPORTED = ("kubernetes/apps/relay-researcher/pins/pins.yaml", "kubernetes/apps/relay-researcher/agent/config.json")


class Difference(Exception):
    pass


def run(argv: list[str], cwd: Path | None = None) -> str:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        raise Difference(f"{Path(argv[0]).name} {' '.join(argv[1:3])} failed ({done.returncode}): "
                         f"{(done.stderr or done.stdout).strip()[-2000:]}")
    return done.stdout


def export(ref: str, destination: Path) -> str:
    """The committed tree of `ref` (its commit id is returned), extracted without git metadata."""
    commit = run(["git", "-C", str(ROOT), "rev-parse", "--verify", f"{ref}^{{commit}}"]).strip()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", commit],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    return commit


def compare_files(label: str, produced: Path, committed: Path, names) -> list[str]:
    """Byte comparison; the produced directory must hold exactly `names` when it is a directory."""
    differences = []
    for name in names:
        a, b = produced / name, committed / name
        if not a.is_file() or not b.is_file():
            differences.append(f"{label}: {name} is missing ({'produced' if not a.is_file() else 'committed'})")
        elif not filecmp.cmp(a, b, shallow=False):
            differences.append(f"{label}: {name} differs from the committed file")
    return differences


def pins_are_placeholders(tree: Path) -> bool:
    """True only while EVERY pin is still the reviewed placeholder (before step I5). Anything else,
    malformed pins included, is compared with a fresh import, never skipped."""
    spec = importlib.util.spec_from_file_location("relay_researcher_bundle", tree / "scripts/relay-researcher-bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return all("operator-fills" in str(value) for value in module.pins().values())


def reproduce(relay: Path, assets: Path, ref: str) -> list[str]:
    relay, assets = relay.resolve(), assets.resolve()
    tsx = relay / "node_modules/.bin/tsx"
    if not tsx.is_file():
        raise Difference(f"{tsx} is missing: run `npm ci` in the pinned Relay checkout")
    with tempfile.TemporaryDirectory(prefix="relay-researcher-reproduce-") as tmp:
        work = Path(tmp)  # mkdtemp: 0700, as prepare-pod-installation.ts requires of its input's parents
        tree = work / "tree"
        tree.mkdir()
        commit = export(ref, tree)
        print(f"reproducing {ref} ({commit}) with Relay {run(['git', '-C', str(relay), 'rev-parse', 'HEAD']).strip()}")
        identity = json.loads((tree / INPUTS).read_text(encoding="utf-8"))["installation"]
        # After step I10 the committed definition carries the live fence UID; regenerate with it.
        fence = json.loads((tree / I4_OUTPUT / "definition.json").read_text(encoding="utf-8"))["identity"].get(
            "fenceObjectUid")

        # Step I4: the generator of the exported tree, so the committed generator and inputs are what run.
        regenerated = work / "i4"
        run([str(tsx), str(tree / "scripts/relay-researcher-binding.mts"), "--relay", str(relay),
             "--assets", str(assets), "--installation-id", identity["installationId"],
             "--installation-public-key", identity["installationPublicKey"], "--out", str(regenerated),
             *(["--fence-object-uid", fence] if fence else [])])
        produced = sorted(p.name for p in regenerated.iterdir())
        committed = sorted(p.name for p in (tree / I4_OUTPUT).iterdir())
        differences = [] if produced == committed == sorted(I4_FILES) else [
            f"I4: file sets differ: produced {produced}, committed {committed}"]
        differences += compare_files("I4", regenerated, tree / I4_OUTPUT, I4_FILES)
        summary = json.loads((regenerated / "summary.json").read_text(encoding="utf-8"))
        print(f"I4: {I4_OUTPUT}/ regenerated, bindingDigest {summary['bindingDigest']}: "
              + ("byte-identical" if not differences else "DIFFERENT"))

        # Step I5: Relay's preparation, the reviewed check with the chart render, then the import.
        definition = work / "in/definition.json"
        definition.parent.mkdir(mode=0o700)
        shutil.copyfile(regenerated / "definition.json", definition)
        definition.chmod(0o644)
        digest = hashlib.sha256(definition.read_bytes()).hexdigest()
        bundle = work / "bundle"
        run([str(tsx), "scripts/prepare-pod-installation.ts", str(definition), digest, str(bundle)], cwd=relay)
        run([sys.executable, str(tree / "scripts/relay-researcher-bundle.py"), "check", str(bundle), "--chart", str(relay)])
        print("I5: prepare-pod-installation.ts and relay-researcher-bundle.py check --chart pass")
        if not pins_are_placeholders(tree):
            before = work / "committed"
            for path in IMPORTED:
                (before / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tree / path, before / path)
            run([sys.executable, str(tree / "scripts/relay-researcher-bundle.py"), "import", str(bundle),
                 "--chart", str(relay)])
            imported = compare_files("I5", tree, before, IMPORTED)
            differences += imported
            print("I5: import rewrote pins.yaml and agent/config.json: "
                  + ("byte-identical" if not imported else "DIFFERENT"))
        else:
            print("I5: the committed pins are placeholders (step I5 has not landed): no import to compare")
        return differences


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--relay", type=Path, required=True, help="pinned Relay checkout, `npm ci` done")
    parser.add_argument("--assets", type=Path, required=True, help="directory with the five pinned release assets")
    parser.add_argument("--ref", default="HEAD", help="committed ailab state to reproduce (default HEAD)")
    args = parser.parse_args(argv)
    try:
        differences = reproduce(args.relay, args.assets, args.ref)
    except Difference as error:
        print(f"relay-researcher-reproduce: {error}", file=sys.stderr)
        return 1
    for difference in differences:
        print(f"relay-researcher-reproduce: {difference}", file=sys.stderr)
    print("reproduced: every committed output is byte-identical" if not differences else "NOT reproduced")
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
