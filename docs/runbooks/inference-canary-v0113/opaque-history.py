#!/usr/bin/env python3
"""Read-only local opaque-block evidence; this does NOT prove upstream replay.

Run only after completed hub control/permission receipts and a quiescent canary.
Supply native UUID from the completed hub receipt, not from local history:
  python3 opaque-history.py snapshot PROVIDER SOURCE_RELAY_UUID NATIVE_UUID
  python3 opaque-history.py compare PROVIDER SOURCE_RELAY_UUID REPLACEMENT_RELAY_UUID NATIVE_UUID
CLI fixes the state root and UID. Redirect sanitized stdout to fresh evidence paths
outside holder state. No credentials, history text, signatures or ciphertext are
printed. Local receipts bind files but are not independent hub acceptance evidence.
Comparison covers the exact imported JSONL prefix recorded by native-recovery.json;
Pi may append startup/turn entries afterwards. It does not establish which blocks
were sent upstream, visible reasoning, successful inference, or tool-effect safety.
A missing/nonunique imported hash boundary is refused, never guessed. Compacted or
selected branches without opaque blocks return NOT_EXERCISED, never PASS.
Source schemas: Relay packages/pi-launcher/history.ts (v2 owned recovery / v3
JSONL) and holder/src/native.rs retained_receipt. No live reads in offline tests.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

ROOT = Path("/home/c4/.local/state/relay-holder")
PROFILES = {"codex": "1d9a7d6e-4b26-49fd-9053-7b0a22e1baae",
            "qwen": "f25c187b-1e50-4c7c-860e-52947bfdf39e",
            "claude": "fdb56ca5-908b-40ee-94b3-aac735dd8622"}
HISTORY_LIMIT = 16 * 1024 * 1024
META_LIMIT = 16384
SCOPE = "Selected-ancestry opaque field hashes and local import preservation only; upstream replay is unproven."


class Refused(Exception):
    pass


def require(condition):
    if not condition:
        raise Refused("Unsafe, inconsistent or unsupported local evidence")


def canonical_uuid(value):
    try:
        require(isinstance(value, str) and str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError, TypeError):
        raise Refused("Invalid receipt identifier") from None
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def exact(value, keys):
    return isinstance(value, dict) and set(value) == set(keys.split())


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda _: require(False))


def fingerprint(s):
    return (s.st_dev, s.st_ino, s.st_uid, s.st_gid, s.st_mode, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class PrivateTree:
    """Keep all traversed descriptors open and recheck each pathname at completion."""
    def __init__(self, root, uid):
        require(root.is_absolute() and str(root) == os.path.normpath(str(root)))
        self.root, self.uid, self.handles = root, uid, []
        self.directories = {}
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.handles.append((fd, None, None, os.fstat(fd), False))
            for component in root.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                self.handles.append((child, fd, component, os.fstat(child), False))
                fd = child
            self.private(os.fstat(fd), True)
            self.handles[-1] = (*self.handles[-1][:4], True)
            self.directories[""] = fd
        except BaseException:
            self.close()
            raise

    def private(self, s, directory):
        require(s.st_uid == self.uid and stat.S_IMODE(s.st_mode) == (0o700 if directory else 0o600))
        require(stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode) and s.st_nlink == 1)

    def relative(self, path):
        require(isinstance(path, (str, Path)))
        path = Path(path)
        require(path.is_absolute() and str(path) == os.path.normpath(str(path)))
        return path.relative_to(self.root)

    def directory(self, path):
        relative = self.relative(path)
        fd, parts = self.directories[""], []
        for component in relative.parts:
            require(component not in (".", "..", ""))
            parts.append(component)
            key = "/".join(parts)
            if key not in self.directories:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                s = os.fstat(child)
                self.handles.append((child, fd, component, s, True))
                self.private(s, True)
                self.directories[key] = child
            fd = self.directories[key]
        return os.fstat(fd)

    def identity(self, path):
        s = self.directory(path)
        return {"path": str(path), "device": str(s.st_dev), "inode": str(s.st_ino), "uid": str(s.st_uid)}

    def read(self, path, limit):
        path = Path(path)
        self.relative(path)
        self.directory(path.parent)
        parent = self.directories["" if path.parent == self.root else path.parent.relative_to(self.root).as_posix()]
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        self.handles.append((fd, parent, path.name, before, True))
        self.private(before, False)
        require(before.st_size <= limit)
        parts, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
        require(size <= limit and size == before.st_size and fingerprint(before) == fingerprint(os.fstat(fd)))
        return b"".join(parts)

    def stable(self):
        for fd, parent, name, before, private in self.handles:
            after = os.fstat(fd)
            require((fingerprint(before) == fingerprint(after)) if private else
                    (before.st_dev, before.st_ino, before.st_mode) == (after.st_dev, after.st_ino, after.st_mode))
            if parent is not None:
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                require(fingerprint(linked) == fingerprint(after))

    def close(self):
        for fd, *_ in reversed(self.handles):
            os.close(fd)
        self.handles.clear()


def opaque_fields(data, native_id, workspace, provider):
    require(len(data) <= HISTORY_LIMIT and data.endswith(b"\n"))
    lines = data.splitlines()
    require(2 <= len(lines) <= 20000 and all(0 < len(line) <= 1024 * 1024 for line in lines))
    header, *entries = [decode(line) for line in lines]
    require(exact(header, "type version id timestamp cwd") and header["type"] == "session" and
            header["version"] == 3 and header["id"] == native_id and header["cwd"] == str(workspace))
    by_id = {}
    for entry in entries:
        require(isinstance(entry, dict) and isinstance(entry.get("id"), str) and
                re.fullmatch(r"[a-f0-9]{8}", entry["id"]) and entry["id"] not in by_id and
                "parentId" in entry and (entry["parentId"] is None or entry["parentId"] in by_id))
        by_id[entry["id"]] = entry
    branch, leaf = [], entries[-1]
    while leaf is not None:
        branch.append(leaf)
        leaf = by_id[leaf["parentId"]] if leaf["parentId"] is not None else None
    result = []
    for entry in reversed(branch):
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        require(isinstance(message, dict))
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        require(isinstance(content, list) and len(content) <= 1024)
        for block_index, block in enumerate(content):
            require(isinstance(block, dict))
            if block.get("type") != "thinking" or "thinkingSignature" not in block:
                continue
            signature = block["thinkingSignature"]
            require(isinstance(signature, str) and isinstance(block.get("redacted", False), bool))
            if not signature:
                continue
            record = {"entryId": entry["id"], "blockIndex": block_index,
                      "field": "thinkingSignature", "redacted": block.get("redacted", False),
                      "sha256": sha(signature.encode("utf-8"))}
            result.append(record)
            if provider == "codex":
                item = decode(signature.encode("utf-8"))
                require(isinstance(item, dict) and item.get("type") == "reasoning")
                encrypted = item.get("encrypted_content")
                if encrypted is not None:
                    require(isinstance(encrypted, str))
                    if encrypted:
                        result.append({"entryId": entry["id"], "blockIndex": block_index,
                                       "field": "encrypted_content", "sha256": sha(encrypted.encode("utf-8"))})
    return result


def source_evidence(tree, provider, source, native_id):
    home = tree.root / "native-homes" / source
    identity = tree.identity(home)
    workspace = tree.root / "workspaces" / PROFILES[provider]
    workspace_identity = tree.identity(workspace)
    receipt_bytes = tree.read(home / "relay-retained-state.json", META_LIMIT)
    receipt = decode(receipt_bytes)
    require(exact(receipt, "version admission containment home metadata") and receipt["version"] == 1 and
            receipt["admission"] == "blocked" and receipt["containment"] == "owned-process-group-only")
    require(receipt["home"] == {key: int(identity[key]) for key in ("device", "inode", "uid")})
    metadata = receipt["metadata"]
    require(exact(metadata, "sessionId profileDigest nativeSessionId workspace outcome history") and
            metadata["sessionId"] == source and metadata["nativeSessionId"] == native_id and
            digest(metadata["profileDigest"]) and metadata["workspace"] == workspace_identity and
            metadata["outcome"] in ("closed", "interrupted"))
    history = metadata["history"]
    require(exact(history, "path sha256") and digest(history["sha256"]))
    path = Path(history["path"])
    require(path.is_relative_to(home) and path != home)
    data = tree.read(path, HISTORY_LIMIT)
    require(sha(data) == history["sha256"])
    fields = opaque_fields(data, native_id, workspace, provider)
    return {"sourceSessionId": source, "nativeSessionId": native_id,
            "profileDigest": metadata["profileDigest"], "retainedReceiptSha256": sha(receipt_bytes),
            "historySha256": sha(data), "opaqueFields": fields}, identity, workspace_identity, history


def imported_prefix(data, expected):
    require(digest(expected) and len(data) <= HISTORY_LIMIT and data.endswith(b"\n"))
    lines = data.splitlines(keepends=True)
    require(2 <= len(lines) <= 20000 and all(len(line) <= 1024 * 1024 + 1 for line in lines))
    hasher, size, boundaries = hashlib.sha256(), 0, []
    for line in lines:
        hasher.update(line)
        size += len(line)
        if hasher.hexdigest() == expected:
            boundaries.append(size)
    require(len(boundaries) == 1)
    # The SHA binds exact bytes and newline boundary, not a search for matching
    # opaque fields in an arbitrary prefix. Parsing below also validates ancestry.
    return data[:boundaries[0]]


def observe(root, uid, provider, source, native_id, replacement=None):
    require(provider in PROFILES)
    canonical_uuid(source)
    canonical_uuid(native_id)
    if replacement is not None:
        canonical_uuid(replacement)
        require(source != replacement)
    tree = PrivateTree(root, uid)
    try:
        report, identity, workspace, history = source_evidence(tree, provider, source, native_id)
        report.update(version=1, provider=provider, profileId=PROFILES[provider], scope=SCOPE,
                      upstreamReplayProven=False)
        if replacement is not None:
            home = root / "native-homes" / replacement
            tree.identity(home)
            descriptor = decode(tree.read(home / "holder-recovery.json", META_LIMIT))
            required = "version runtimeManifestSha256 nativeSessionId workspace historyFile historySha256 admission sourceSessionId ownerSessionId retainedReceiptSha256 nativeHome"
            require(isinstance(descriptor, dict) and set(descriptor) in
                    (set(required.split()), set(required.split()) | {"acknowledgeUnknownToolEffects"}))
            require(descriptor.get("acknowledgeUnknownToolEffects", True) is True and descriptor["version"] == 2 and
                    descriptor["admission"] == "holder-owned" and digest(descriptor["runtimeManifestSha256"]) and
                    descriptor["nativeSessionId"] == native_id and descriptor["sourceSessionId"] == source and
                    descriptor["ownerSessionId"] == replacement and descriptor["nativeHome"] == identity and
                    descriptor["workspace"] == workspace and descriptor["historyFile"] == history["path"] and
                    descriptor["historySha256"] == report["historySha256"] and
                    descriptor["retainedReceiptSha256"] == report["retainedReceiptSha256"])
            recovery = decode(tree.read(home / "native-recovery.json", META_LIMIT))
            require(exact(recovery, "version nativeSessionId runtimeManifestSha256 sourceSha256 importedSha256 workspace admission sourceSessionId ownerSessionId retainedReceiptSha256 nativeHome"))
            for field in ("version", "nativeSessionId", "runtimeManifestSha256", "workspace", "admission",
                          "sourceSessionId", "ownerSessionId", "retainedReceiptSha256", "nativeHome"):
                require(recovery[field] == descriptor[field])
            require(recovery["sourceSha256"] == report["historySha256"] and digest(recovery["importedSha256"]))
            data = tree.read(home / "resumed-session.jsonl", HISTORY_LIMIT)
            prefix = imported_prefix(data, recovery["importedSha256"])
            imported = opaque_fields(prefix, native_id, Path(workspace["path"]), provider)
            equal = report["opaqueFields"] == imported
            report.update(replacementSessionId=replacement, importedSha256=sha(prefix),
                          importedOpaqueFields=imported, orderedOpaqueFieldsEqual=equal)
        else:
            equal = True
        fields = report["opaqueFields"]
        exercised = bool(fields) and (provider != "codex" or any(x["field"] == "encrypted_content" for x in fields))
        report.update(opaqueFieldCount=len(fields), status=("MISMATCH" if not equal else
                      "NOT_EXERCISED" if not exercised else "PRESERVED" if replacement else "OBSERVED"))
        tree.stable()
        return report
    finally:
        tree.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("snapshot", "compare"):
        command = sub.add_parser(action)
        command.add_argument("provider", choices=PROFILES)
        command.add_argument("source")
        if action == "compare":
            command.add_argument("replacement")
        command.add_argument("native", help="Native UUID from the completed hub receipt")
    args = parser.parse_args()
    try:
        require(os.getuid() == 1000)
        report = observe(ROOT, 1000, args.provider, args.source, args.native, getattr(args, "replacement", None))
    except (Refused, OSError, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        # Never print exception text: JSON parse errors and paths can contain content.
        report = {"version": 1, "status": "REFUSED", "scope": SCOPE, "upstreamReplayProven": False}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in ("OBSERVED", "PRESERVED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
