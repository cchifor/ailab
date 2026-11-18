#!/usr/bin/env python3
"""Prepare the installation runbook's agent-only update from operator-downloaded assets.

Uses the runtime release's Relay checkout (with node_modules), Node 22.21+ on PATH,
and the existing bundle tool (PyYAML and helm required).
All generation and import checks run in a private temporary copy inside this tree;
only validated inputs, I4 output and the agentImage pin are installed. No downloads,
credentials or live operations. Provenance prose is printed for manual review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = "docs/relay-workers/researcher-binding-inputs.json"
I4 = "docs/relay-workers/researcher-i4"
TREE = "kubernetes/apps/relay-researcher"
PINS = f"{TREE}/pins/pins.yaml"
PROVENANCE = "docs/relay-workers/researcher-i5-provenance.md"
I4_FILES = {"binding.json", "definition.json", "summary.json", "template-preparation-input.json"}
# Explicit public inputs of the generator, bundle checker and its campaign overlay.
STAGED = (
    INPUTS, I4, TREE, PROVENANCE,
    "scripts/relay-researcher-binding.mts", "scripts/relay-researcher-bundle.py",
    "scripts/relay-candidate-run.py", "docs/relay-workers/researcher-installation-definition.json",
    "docs/relay-workers/researcher-runtime-ca.crt", "kubernetes/apps/relay-internal-tls/terminators.yaml",
    "kubernetes/optional/relay-candidate-journal/render.py", "kubernetes/infra/agent-nodes/relay-workers.tf",
    "kubernetes/components/relay-worker-candidate/run", "kubernetes/apps/apps/relay/relay.yaml",
)
TAG = r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
RECEIPT = r"worker-image-infrastructure-agent-[0-9]+\.json"


class Refusal(Exception):
    pass


def require(condition, message: str) -> None:
    if not condition:
        raise Refusal(message)


def field(value, key: str, kind: type, context: str):
    require(isinstance(value, dict), f"{context} must be an object")
    require(key in value, f"{context} is missing {key}")
    require(isinstance(value[key], kind), f"{context}.{key} must be {kind.__name__}")
    return value[key]


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise Refusal(f"invalid JSON constant: {value}")


DECODER = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=invalid_constant)


def object_json(text: str | bytes) -> dict:
    value = DECODER.decode(text.decode("utf-8") if isinstance(text, bytes) else text)
    require(isinstance(value, dict), "expected a JSON object")
    return value


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version(tag: str) -> tuple[int, ...]:
    match = re.fullmatch(TAG, tag) if isinstance(tag, str) else None
    require(match, f"{tag!r} is not a vMAJOR.MINOR.PATCH release tag")
    return tuple(map(int, match.groups()))


def check_agent_tag(tag: str, current: dict, *, allow_downgrade: bool) -> None:
    pinned = field(current, "tag", str, "inputs.agentRelease")
    downgrade = version(tag) < version(pinned)
    require(not downgrade or allow_downgrade,
            f"agent tag {tag} is older than pinned agentRelease.tag {pinned}; "
            "use --allow-downgrade only for an intentional rollback")
    if downgrade:
        print(f"WARNING: --allow-downgrade permits agent downgrade from {pinned} to {tag}",
              file=sys.stderr, flush=True)


def check_node(node: str) -> None:
    match = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)", node)
    require(match and tuple(map(int, match.groups()[:2])) >= (22, 21),
            f"Node 22.21+ must be on PATH; found {node}")


def read_agent_release(directory: Path, tag: str, release_id: int, runtime: dict) -> tuple[dict, str, list[str]]:
    require(version(tag) > version(field(runtime, "tag", str, "inputs.release")),
            "agent tag must be newer than the runtime release")
    require(type(release_id) is int and 0 < release_id <= 2**53 - 1, "invalid Gitea release ID")
    receipts = sorted(directory.glob("worker-image-infrastructure-agent-*.json"))
    require(len(receipts) == 1 and re.fullmatch(RECEIPT, receipts[0].name),
            "--agent-assets must contain exactly one worker-image-infrastructure-agent-<run>.json")
    name = receipts[0].name
    bodies = {}
    for asset in ("SHA256SUMS", "manifest.json", name):
        path = directory / asset
        require(path.is_file() and not path.is_symlink(), f"asset must be a regular file: {asset}")
        bodies[asset] = path.read_bytes()
    manifest, receipt = object_json(bodies["manifest.json"]), object_json(bodies[name])
    require(manifest.get("version") == tag, "manifest.json version does not equal --tag")
    source = manifest.get("sourceCommit")
    require(isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source),
            "manifest.json sourceCommit is not a 40-hex commit")
    require(receipt.get("sourceCommit") == source, "receipt and manifest sourceCommit disagree")
    require(receipt.get("kind") == "relay-worker-image" and type(receipt.get("version")) is int
            and receipt["version"] == 1 and receipt.get("component") == "infrastructure-agent"
            and receipt.get("platform") == "linux/amd64" and receipt.get("runtime") is None,
            "not an infrastructure-agent publish receipt for linux/amd64")
    publication = receipt.get("publication")
    require(isinstance(publication, dict), "receipt has no publication object")
    repository = field(runtime, "agentImage", str, "inputs.release").split("@")[0]
    image = publication.get("image")
    require(isinstance(image, str) and re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", image),
            f"publication.image must be a digest-only {repository} image")
    require(publication.get("manifestSha256") == image.rsplit(":", 1)[1],
            "publication.image and manifestSha256 disagree")
    config = receipt.get("imageConfigDigest")
    require(isinstance(config, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", config),
            "receipt has no sha256 imageConfigDigest")
    hashes = {asset: sha256(body) for asset, body in bodies.items()}
    sums = {}
    for line in bodies["SHA256SUMS"].decode("utf-8").splitlines():
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64}) [ *]?(.+)", line)
        require(match, "malformed SHA256SUMS line")
        digest, asset = match.groups()
        require(asset not in sums, f"duplicate SHA256SUMS entry: {asset}")
        sums[asset] = digest
        require(asset not in hashes or hashes[asset] == digest, f"{asset} differs from SHA256SUMS")
    require(sums, "SHA256SUMS is empty")
    return {"tag": tag, "giteaReleaseId": release_id, "sourceCommit": source, "assets": hashes,
            "agentReceipt": name, "agentImage": image}, config, sorted(hashes.keys() & sums.keys())


def members(text: str) -> dict[str, tuple[int, int, int]]:
    """JSON object member spans: key start, value start, value end. Never serialize the object."""
    object_json(text)
    cursor = text.index("{") + 1
    spans = {}
    while True:
        while text[cursor].isspace():
            cursor += 1
        if text[cursor] == "}":
            return spans
        key_start = cursor
        key, cursor = DECODER.raw_decode(text, cursor)
        while text[cursor].isspace():
            cursor += 1
        cursor += 1  # colon; the complete object has already parsed
        while text[cursor].isspace():
            cursor += 1
        value_start = cursor
        _, cursor = DECODER.raw_decode(text, cursor)
        spans[key] = (key_start, value_start, cursor)
        while text[cursor].isspace():
            cursor += 1
        if text[cursor] == ",":
            cursor += 1


def edit(text: str, patches: list[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(patches, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def rewrite_inputs(text: str, agent: dict) -> str:
    """Replace scalar tokens and the receipt's asset key, preserving all other bytes, including
    whitespace inside agentRelease. In _comment change only its release-identification clause;
    the dated owner decisions remain historical. An unknown layout/schema is refused."""
    inputs = object_json(text)
    field(inputs, "_comment", str, "inputs")
    old = field(inputs, "agentRelease", dict, "inputs")
    keys = {"tag", "giteaReleaseId", "sourceCommit", "assets", "agentReceipt", "agentImage"}
    require(isinstance(old, dict) and set(old) == set(agent) == keys,
            "expected an existing agentRelease with exactly its six fields")
    for value in (old, agent):
        for key in ("tag", "sourceCommit", "agentReceipt", "agentImage"):
            field(value, key, str, "agentRelease")
        require(set(field(value, "assets", dict, "agentRelease")) ==
                {"SHA256SUMS", "manifest.json", value["agentReceipt"]},
                "agentRelease.assets must contain exactly the three agent assets")
    outer = members(text)
    _, start, end = outer["agentRelease"]
    block = text[start:end]
    patches = []
    for key, (_, a, b) in members(block).items():
        if key != "assets":
            if old[key] != agent[key]:
                patches.append((a, b, json.dumps(agent[key])))
            continue
        assets = block[a:b]
        asset_patches = []
        for asset, (k, v, e) in members(assets).items():
            renamed = agent["agentReceipt"] if asset == old["agentReceipt"] else asset
            if renamed != asset:
                _, key_end = DECODER.raw_decode(assets, k)
                asset_patches.append((k, key_end, json.dumps(renamed)))
            if old["assets"][asset] != agent["assets"][renamed]:
                asset_patches.append((v, e, json.dumps(agent["assets"][renamed])))
        patches.append((a, b, edit(assets, asset_patches)))
    def clause(release):
        return (f"`agentRelease` pins the Relay {release['tag']} release (Gitea release "
                f"{release['giteaReleaseId']}, source {release['sourceCommit'][:8]})")
    _, c, d = outer["_comment"]
    comment = text[c:d]
    require(comment.count(clause(old)) == 1, "_comment has no single matching agent release clause")
    updated = edit(text, [(start, end, edit(block, patches)),
                          (c, d, comment.replace(clause(old), clause(agent), 1))])
    expected = {**inputs, "agentRelease": agent,
                "_comment": inputs["_comment"].replace(clause(old), clause(agent), 1)}
    require(object_json(updated) == expected, "text rewrite changed unexpected input values")
    return updated


def run(argv, *, cwd: Path, env=None) -> str:
    args = list(map(str, argv))
    print(f"  $ {shlex.join(args)}", flush=True)
    done = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    require(done.returncode == 0, f"command failed ({done.returncode}): "
            f"{(done.stderr or done.stdout).strip()[-4000:]}")
    return done.stdout


def snapshot(root: Path, paths) -> dict[str, bytes]:
    result = {}
    for name in paths:
        path = root / name
        require(path.exists(), f"missing input: {name}")
        for entry in ([path, *sorted(path.rglob("*"))] if path.is_dir() else [path]):
            require(not entry.is_symlink(), f"symlink input refused: {entry}")
            if entry.is_file():
                result[str(entry.relative_to(root))] = entry.read_bytes()
            else:
                require(entry.is_dir(), f"not a regular file or directory: {entry}")
    return result


def check_i4(before: dict[str, bytes], after: dict[str, bytes], agent: dict, config: str) -> None:
    require(set(before) == set(after) == I4_FILES, "I4 file set differs")
    for name in ("binding.json", "template-preparation-input.json"):
        require(before[name] == after[name], f"I4 {name} changed")
    definition = object_json(before["definition.json"])
    require(object_json(after["definition.json"]) == {**definition, "agentImage": agent["agentImage"]},
            "I4 definition differs beyond agentImage")
    expected = object_json(before["summary.json"])
    expected.update(agentImage=agent["agentImage"], agentRelease={
        **{k: v for k, v in agent.items() if k != "agentImage"}, "imageConfigDigest": config})
    files = field(expected, "files", dict, "I4 summary.json")
    field(files, "definition.json", str, "I4 summary.json.files")
    files["definition.json"] = sha256(after["definition.json"])
    require(object_json(after["summary.json"]) == expected,
            "I4 summary changed beyond agentRelease, agentImage and definition hash (including bindingDigest)")


def check_import(before: dict[str, bytes], after: dict[str, bytes], image: str) -> None:
    try:
        import yaml
    except ImportError as error:
        raise Refusal("PyYAML is required to validate the pins.yaml import") from error

    require(set(before) == set(after), "import changed the installation file set")
    require(PINS in before, "installation is missing pins.yaml")

    def pins(body):
        text = body.decode("utf-8")
        try:
            document = yaml.safe_load(text)
            node = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as error:
            raise Refusal(f"invalid pins.yaml: {error}") from error
        data = field(document, "data", dict, "pins.yaml")
        field(data, "agentImage", str, "pins.yaml.data")
        # The parser's scalar span includes its quotes, regardless of indentation/style.
        # Removing only that span retains the byte guard on comments and all other text.
        for key in ("data", "agentImage"):
            require(isinstance(node, yaml.MappingNode), f"pins.yaml {key} parent must be a mapping")
            matches = [value for name, value in node.value if name.value == key]
            require(len(matches) == 1, f"pins.yaml must contain exactly one {key}")
            node = matches[0]
        require(isinstance(node, yaml.ScalarNode), "pins.yaml agentImage must be a scalar")
        return document, text[:node.start_mark.index] + text[node.end_mark.index:]

    expected, original_text = pins(before[PINS])
    actual, imported_text = pins(after[PINS])
    expected["data"]["agentImage"] = image
    require(actual == expected, "import changed pins.yaml values beyond data.agentImage")
    require(imported_text == original_text, "import changed pins.yaml bytes beyond the agentImage scalar")
    for name in before:
        if name != PINS:
            require(after[name] == before[name], f"import changed {name} beyond pins.yaml's agentImage scalar")


def provenance_report(text: str, agent: dict, config: str, runtime: dict) -> str:
    """Quote the exact existing prose to review; offer factual replacements without inventing
    release rationale, compatibility conclusions or a registry readback."""
    lines = text.splitlines()
    paragraph = next((line for line in lines if line.startswith("Agent-only update (`agentRelease`")), None)
    require(paragraph is not None, "provenance has no agent-only update paragraph")
    start = lines.index(paragraph)
    asset = next((line for line in lines[start + 1:] if line.startswith("- `worker-image-infrastructure-agent-")), None)
    rows = [next((line for line in lines if line.startswith(f"| `pins.yaml` `{field}` |")), None)
            for field in ("agentImage", "sourceCommit")]
    require(asset is not None and all(rows), "provenance has no agent receipt or pin rows")
    receipt = agent["agentReceipt"]
    return (f"Manual review in {PROVENANCE} (file not rewritten):\n"
            f"release id: {agent['giteaReleaseId']}\ntag: {agent['tag']}\nsourceCommit: {agent['sourceCommit']}\n"
            f"receipt: {receipt}\nreceipt sha256: {agent['assets'][receipt]}\n"
            f"image: {agent['agentImage']}\nimage digest: {agent['agentImage'].split('@')[1]}\n"
            f"imageConfigDigest: {config}\n\n"
            "Exact current paragraph, receipt entry and rows to review/update:\n"
            + "\n".join([paragraph, asset, *rows]) + "\n\n"
            "New release sentence and receipt entry:\n"
            f"Agent-only update (`agentRelease`): Gitea release {agent['giteaReleaseId']} "
            f"({agent['tag']}, source `{agent['sourceCommit']}`).\n"
            f"- `{receipt}`: `{agent['assets'][receipt]}`\n"
            "New agentImage row (append only independently verified readback/history):\n"
            f"| `pins.yaml` `agentImage` | `{agent['agentImage']}` | the `agentRelease` receipt "
            f"`{receipt}` (`publication.image`), source `{agent['sourceCommit']}`; "
            f"`imageConfigDigest` = `{config}`, recorded in `researcher-i4/summary.json` |\n"
            f"Keep the sourceCommit row's runtime value `{runtime['sourceCommit']}`; review its "
            "agent-version/source comparisons and the paragraph's rationale, RBAC and compatibility claims "
            "against the new release. Registry readback and compatibility review remain operator steps.")


def install(root: Path, original: dict[str, bytes], replacements: dict[str, bytes]) -> None:
    """Stage and fsync all files before replacing any destination; retain failed rollback backups."""
    temporary = []
    retained = set()
    staged, backups = {}, {}
    installed = []

    def prepare(name, body, label):
        destination = root / name
        with tempfile.NamedTemporaryFile(prefix=f".relay-agent-pin-{label}-", dir=destination.parent,
                                         delete=False) as stream:
            path = Path(stream.name)
            temporary.append(path)
            stream.write(body)
            stream.flush()
            os.fchmod(stream.fileno(), destination.stat().st_mode & 0o7777)
            os.fsync(stream.fileno())
        return path

    try:
        for name, body in replacements.items():
            backups[name] = prepare(name, original[name], "backup")
            staged[name] = prepare(name, body, "replacement")
        for name in replacements:
            os.replace(staged[name], root / name)
            installed.append(name)
    except BaseException as error:
        failures = []
        for name in reversed(installed):
            try:
                os.replace(backups[name], root / name)
            except BaseException as restore_error:
                retained.add(backups[name])
                failures.append(f"{name}: {restore_error}; backup retained at {backups[name]}")
        detail = ("rollback could not restore " + "; ".join(failures) if failures
                  else "all replaced destinations restored")
        raise Refusal(f"installation failed: {type(error).__name__}: {error}; {detail}") from error
    finally:
        for path in temporary:
            if path not in retained:
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    print(f"WARNING: could not clean up temporary file {path}: {error}", file=sys.stderr)


def pin(relay: Path, assets: Path, agent_assets: Path, tag: str, release_id: int,
        *, allow_downgrade: bool = False) -> None:
    relay, assets, agent_assets = relay.resolve(), assets.resolve(), agent_assets.resolve()
    print("1/6 Validate Node and the agent receipt/manifest", flush=True)
    node = run(["node", "--version"], cwd=ROOT).strip()
    check_node(node)
    tsx = relay / "node_modules/.bin/tsx"
    require(tsx.is_file(), "--relay needs node_modules/.bin/tsx (install dependencies first)")
    original = snapshot(ROOT, STAGED)
    inputs = object_json(original[INPUTS])
    runtime = field(inputs, "release", dict, "inputs")
    field(runtime, "sourceCommit", str, "inputs.release")
    identity = field(inputs, "installation", dict, "inputs")
    for key in ("installationId", "installationPublicKey"):
        field(identity, key, str, "inputs.installation")
    check_agent_tag(tag, field(inputs, "agentRelease", dict, "inputs"), allow_downgrade=allow_downgrade)
    agent, config, checked = read_agent_release(agent_assets, tag, release_id, runtime)
    print("2/6 Hash the three agent assets", flush=True)
    for name, digest in agent["assets"].items():
        print(f"  {digest}  {name}", flush=True)
    print("  Cross-checked against SHA256SUMS: " + (", ".join(checked) or "none"), flush=True)
    print("3/6 Rewrite agentRelease and its comment clause as text in a temporary copy", flush=True)
    rewritten = rewrite_inputs(original[INPUTS].decode("utf-8"), agent).encode("utf-8")
    report = provenance_report(original[PROVENANCE].decode("utf-8"), agent, config, runtime)
    with tempfile.TemporaryDirectory(prefix=".relay-agent-pin-", dir=ROOT) as tmp:
        work = Path(tmp)
        tree = work / "tree"
        for name, body in original.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        (tree / INPUTS).write_bytes(rewritten)
        # Run children here with a relative TMPDIR: tsx's Unix socket otherwise exceeds the
        # socket path limit in deeply nested worktrees. All temporary files stay in this directory.
        env = {**os.environ, "TMPDIR": ".", "PYTHONDONTWRITEBYTECODE": "1", "TSX_DISABLE_CACHE": "1"}
        generated = work / "i4"
        definition_identity = field(object_json(original[f"{I4}/definition.json"]),
                                    "identity", dict, "I4 definition.json")
        fence = definition_identity.get("fenceObjectUid")
        require(fence is None or isinstance(fence, str), "I4 definition.json.identity.fenceObjectUid must be str")
        print("4/6 Generate I4; require unchanged binding, template, definition except image, and summary invariants",
              flush=True)
        run([tsx, tree / "scripts/relay-researcher-binding.mts", "--relay", relay, "--assets", assets,
             "--agent-assets", agent_assets, "--installation-id", identity["installationId"],
             "--installation-public-key", identity["installationPublicKey"], "--out", generated,
             *(["--fence-object-uid", fence] if fence else [])], cwd=work, env=env)
        before = snapshot(tree / I4, ["."])
        after = snapshot(generated, ["."])
        check_i4(before, after, agent, config)
        shutil.rmtree(tree / I4)
        shutil.copytree(generated, tree / I4)
        print("5/6 Prepare bundle, check --chart, import; require only the agentImage pin to change", flush=True)
        # Relay requires the definition's immediate parent to be private (0700).
        private = work / "in"
        private.mkdir(mode=0o700)
        definition = private / "definition.json"
        definition.write_bytes(after["definition.json"])
        bundle = work / "bundle"
        run([tsx, relay / "scripts/prepare-pod-installation.ts", definition, sha256(definition.read_bytes()), bundle],
            cwd=work, env=env)
        before_import = snapshot(tree, [TREE])
        for command in ("check", "import"):
            run([sys.executable, tree / "scripts/relay-researcher-bundle.py", command, bundle, "--chart", relay],
                cwd=work, env=env)
        check_import(before_import, snapshot(tree, [TREE]), agent["agentImage"])
        require(snapshot(ROOT, STAGED) == original, "inputs changed during verification; retry from a stable tree")
        replacements = {INPUTS: rewritten, **{f"{I4}/{name}": body for name, body in after.items()},
                        PINS: (tree / PINS).read_bytes()}
        changed = [name for name, body in replacements.items() if original[name] != body]
        print(f"6/6 Install verified output ({len(changed)} changed files) and print provenance", flush=True)
        install(ROOT, original, {name: replacements[name] for name in changed})
    print(report)
    print("Agent pin verified; " + ("updated " + ", ".join(changed) if changed else "all files byte-identical"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("agent-assets", "relay", "assets"):
        parser.add_argument(f"--{option}", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--gitea-release-id", type=int, required=True)
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="allow an intentional rollback below the pinned agentRelease.tag (prints a warning)")
    args = parser.parse_args(argv)
    try:
        pin(args.relay, args.assets, args.agent_assets, args.tag, args.gitea_release_id,
            allow_downgrade=args.allow_downgrade)
    except (Refusal, OSError, ValueError) as error:
        print(f"relay-researcher-agent-pin: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
