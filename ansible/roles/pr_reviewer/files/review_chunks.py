"""Bounded, complete review snapshots. No network, credentials or approval side effects.

The adapter must acquire an immutable diff and independently check current PR eligibility.
This module cannot turn legacy reviews, skipped files or incomplete parts into approval.
"""
import ast
import hashlib
import json
import re

VERSION = "whole-file-review-v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
PERSONA = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
MAX_RESULT_BYTES = 65536
MAX_SUMMARY_BYTES = 2048


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def valid_binding(binding):
    keys = {"repo", "pr", "base_repo", "base_ref", "base_sha", "head_repo",
            "head_ref", "head_sha", "policy"}
    if type(binding) is not dict or set(binding) != keys:
        raise ValueError("Exact immutable PR binding required")
    if type(binding["pr"]) is not int or binding["pr"] <= 0:
        raise ValueError("Positive PR number required")
    for key in ("repo", "base_repo", "head_repo"):
        if not isinstance(binding[key], str) or not REPO.fullmatch(binding[key]):
            raise ValueError("Exact repository identity required")
        if any(part in (".", "..") for part in binding[key].split("/")):
            raise ValueError("Repository traversal refused")
    if binding["repo"] != binding["base_repo"]:
        raise ValueError("PR must belong to its base repository")
    for key in ("base_sha", "head_sha"):
        if not isinstance(binding[key], str) or not SHA.fullmatch(binding[key]):
            raise ValueError("Full commit SHA required")
    for key in ("base_ref", "head_ref"):
        value = binding[key]
        if (not isinstance(value, str) or not 1 <= len(value.encode()) <= 1024
                or value.startswith("-") or any(ord(c) < 32 for c in value)):
            raise ValueError("Exact bounded branch identity required")
    if not isinstance(binding["policy"], str) or not DIGEST.fullmatch(binding["policy"]):
        raise ValueError("Review policy digest required")
    return dict(binding)


def _path(raw, prefixed=True):
    try:
        if not isinstance(raw, bytes):
            raise ValueError("Diff path must be bytes")
        if raw.startswith(b'"'):
            # Git C-quotes non-ASCII bytes using octal escapes. Never eval code.
            value = ast.literal_eval(raw.decode("ascii"))
            if not isinstance(value, str):
                raise ValueError("Not a quoted git path")
            value = value.encode("latin1").decode("utf-8")
        else:
            value = raw.decode("utf-8")
    except (ValueError, SyntaxError, UnicodeError) as exc:
        raise ValueError("Unreadable diff path") from exc
    if value == "/dev/null":
        return None
    if prefixed:
        if value[:2] not in ("a/", "b/"):
            raise ValueError("Unexpected diff path prefix")
        value = value[2:]
    if (not value or value.startswith("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ValueError("Unsafe diff path")
    return value


def _paths(section):
    # Only headers precede the first hunk; added source cannot impersonate a header.
    header = section.split(b"\n@@ ", 1)[0]
    def lifecycle(a, b):
        if re.search(br"(?m)^new file mode ", header):
            return None, b
        if re.search(br"(?m)^deleted file mode ", header):
            return a, None
        return a, b
    old = re.search(br"(?m)^--- (.+)$", header)
    new = re.search(br"(?m)^\+\+\+ (.+)$", header)
    if old and new:
        # Git appends exactly one literal tab to these headers for unquoted paths with
        # spaces. Strip that separator only here; embedded tabs or timestamp payloads
        # are not part of the immutable `git diff` transport and must fail closed.
        a, b = _path(old[1].removesuffix(b"\t")), _path(new[1].removesuffix(b"\t"))
        if a is None and b is None:
            raise ValueError("Diff section has no path")
        return a, b
    if old or new:
        raise ValueError("Incomplete diff path headers")
    for mode in (b"rename", b"copy"):
        old = re.search(br"(?m)^" + mode + br" from (.+)$", header)
        new = re.search(br"(?m)^" + mode + br" to (.+)$", header)
        if old and new:
            return _path(old[1], False), _path(new[1], False)
        if old or new:
            raise ValueError("Incomplete rename headers")
    first = section.split(b"\n", 1)[0][len(b"diff --git "):]
    if first.startswith(b'"'):
        match = re.fullmatch(br'("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")', first)
        if match:
            a, b = _path(match[1]), _path(match[2])
            if a == b:
                return lifecycle(a, b)
    else:
        # Mode-only changes have equal paths, including unquoted spaces.
        for split in re.finditer(br" b/", first):
            a, b = _path(first[:split.start()]), _path(first[split.start() + 1:])
            if a == b:
                return lifecycle(a, b)
    raise ValueError("Unaccounted diff path")


def _sections(raw):
    if type(raw) is not bytes or not raw.startswith(b"diff --git "):
        raise ValueError("Complete nonempty unified diff required")
    try:
        raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Non-UTF8 diff cannot receive full review") from exc
    if b"\x00" in raw or re.search(br"(?m)^(GIT binary patch|Binary files .* differ)$", raw):
        raise ValueError("Binary content needs an independent disposition")
    starts = [m.start() for m in re.finditer(br"(?m)^diff --git ", raw)]
    result = [raw[start:(starts[i + 1] if i + 1 < len(starts) else len(raw))]
              for i, start in enumerate(starts)]
    if b"".join(result) != raw:
        raise ValueError("Diff byte coverage incomplete")
    for section in result:
        _validate_section(section)
    return result


def _validate_section(section):
    """Reject partial transport or invented sections before any model can call them clean."""
    lines = section.splitlines(keepends=True)
    if any(not line.endswith(b"\n") for line in lines):
        raise ValueError("Truncated unified diff line")
    old_left = new_left = 0
    hunks = False
    metadata = []
    previous_old = previous_new = 0
    data_seen = False
    for line in lines[1:]:
        if line.startswith(b"@@ "):
            match = re.fullmatch(br"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n", line)
            if not match or old_left or new_left:
                raise ValueError("Invalid or truncated unified hunk")
            old_start, new_start = int(match[1]), int(match[3])
            if old_start < previous_old or new_start < previous_new:
                raise ValueError("Overlapping or reordered unified hunks")
            old_left = int(match[2]) if match[2] is not None else 1
            new_left = int(match[4]) if match[4] is not None else 1
            previous_old, previous_new = old_start + old_left, new_start + new_left
            hunks = True
            data_seen = False
        elif hunks:
            if line == b"\\ No newline at end of file\n" and data_seen:
                data_seen = False
                continue
            if not line or line[:1] not in (b" ", b"+", b"-"):
                raise ValueError("Unexpected unified hunk content")
            old_left -= line[:1] in (b" ", b"-")
            new_left -= line[:1] in (b" ", b"+")
            if old_left < 0 or new_left < 0:
                raise ValueError("Unified hunk exceeds declared lines")
            data_seen = True
        else:
            if not re.fullmatch(
                br"(?:index [0-9a-f]+\.\.[0-9a-f]+(?: [0-7]{6})?|"
                br"(?:old|new|new file|deleted file) mode [0-7]{6}|"
                br"(?:dis)?similarity index \d{1,3}%|(?:rename|copy) (?:from|to) .+|"
                br"--- .+|\+\+\+ .+)\n", line
            ):
                raise ValueError("Unknown diff metadata")
            metadata.append(line)
    if old_left or new_left:
        raise ValueError("Truncated unified hunk")
    has_old = any(s.startswith(b"--- ") for s in metadata)
    has_new = any(s.startswith(b"+++ ") for s in metadata)
    if hunks:
        if not (has_old and has_new):
            raise ValueError("Hunk lacks old/new file headers")
    elif has_old or has_new:
        raise ValueError("File headers without hunks")
    elif not (any(s.startswith((b"rename from ", b"copy from ", b"new file mode ",
                               b"deleted file mode ")) for s in metadata)
              or (any(s.startswith(b"old mode ") for s in metadata)
                  and any(s.startswith(b"new mode ") for s in metadata))):
        raise ValueError("Section contains no complete file change")


def make_plan(binding, merge_base, raw, *, part_bytes=400000,
              max_raw_bytes=33554432, max_parts=128):
    binding = valid_binding(binding)
    if not isinstance(merge_base, str) or not SHA.fullmatch(merge_base):
        raise ValueError("Exact merge-base commit required")
    if (type(part_bytes) is not int or not 128 <= part_bytes <= 400000
            or type(max_raw_bytes) is not int or not part_bytes <= max_raw_bytes <= 33554432
            or type(max_parts) is not int or not 1 <= max_parts <= 128):
        raise ValueError("Invalid bounded review limits")
    if type(raw) is not bytes or len(raw) > max_raw_bytes:
        raise ValueError("Snapshot exceeds intake bound")
    parts, files, pending, seen = [], [], b"", set()

    def finish():
        parts.append(dict(index=len(parts), kind="diff", digest=digest(pending),
                          text=pending.decode("utf-8")))

    for section in _sections(raw):
        if len(section) > part_bytes:
            raise ValueError("Whole file section exceeds part bound; no truncation allowed")
        old, new = _paths(section)
        identity = (old, new)
        if identity in seen:
            raise ValueError("Duplicate changed file section")
        seen.add(identity)
        if pending and len(pending) + len(section) > part_bytes:
            finish()
            pending = b""
        files.append(dict(old=old, new=new, sha256=digest(section), bytes=len(section),
                          part=len(parts)))
        pending += section
    if pending:
        finish()
    if not parts or len(parts) > max_parts:
        raise ValueError("Snapshot exceeds total part budget")
    plan = dict(binding=binding, merge_base=merge_base, raw_sha256=digest(raw),
                bytes=len(raw), files=files, parts=parts,
                limits=dict(part_bytes=part_bytes, max_raw_bytes=max_raw_bytes,
                            max_parts=max_parts), version=VERSION)
    identity = {**plan, "parts": [{k: v for k, v in p.items() if k != "text"}
                                  for p in parts]}
    plan["id"] = digest(canonical(identity))
    return plan


def validate_plan(plan):
    try:
        raw = b"".join(p["text"].encode("utf-8") for p in plan["parts"])
        expected = make_plan(plan["binding"], plan["merge_base"], raw, **plan["limits"])
        if canonical(expected) != canonical(plan):
            raise ValueError("Snapshot manifest or part content changed")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid review snapshot") from exc
    return plan


def valid_result(result, paths):
    if type(result) is not dict or set(result) != {"summary", "findings"}:
        raise ValueError("Complete model result required")
    if (not isinstance(result["summary"], str) or not result["summary"].strip()
            or len(result["summary"].encode()) > MAX_SUMMARY_BYTES
            or type(result["findings"]) is not list or len(result["findings"]) > 128
            or len(canonical(result)) > MAX_RESULT_BYTES):
        raise ValueError("Model result exceeds bound or is incomplete")
    for finding in result["findings"]:
        if (type(finding) is not dict
                or set(finding) != {"path", "side", "line", "severity", "confidence", "body"}
                or not isinstance(finding["path"], str) or finding["path"] not in paths
                or finding["side"] not in ("OLD", "NEW")
                or type(finding["line"]) is not int or finding["line"] < 0
                or finding["severity"] not in ("blocker", "important", "nit")
                or finding["confidence"] not in ("high", "medium", "low")
                or not isinstance(finding["body"], str) or not finding["body"].strip()
                or len(finding["body"].encode()) > 8192):
            raise ValueError("Invalid finding; cannot assume clean review")
    if b"<!--" in canonical(result):
        raise ValueError("Model-generated marker refused")
    return json.loads(canonical(result))


class Ledger:
    """Receipts are scoped to one immutable snapshot and one independent persona.

    Existing bot jobs/attempt counters are deliberately untouched. A failed call records
    nothing; the adapter's established retry/quarantine policy remains the only scheduler.
    """
    def __init__(self, connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS chunk_snapshots(
            snapshot TEXT PRIMARY KEY, manifest TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS chunk_results(
            snapshot TEXT NOT NULL, persona TEXT NOT NULL, part INTEGER NOT NULL,
            digest TEXT NOT NULL, result TEXT NOT NULL,
            PRIMARY KEY(snapshot,persona,part))""")
        self.db.commit()

    def _check(self, plan, persona):
        validate_plan(plan)
        if not isinstance(persona, str) or not PERSONA.fullmatch(persona):
            raise ValueError("Exact reviewer persona required")
        row = self.db.execute("SELECT manifest FROM chunk_snapshots WHERE snapshot=?",
                              (plan["id"],)).fetchone()
        if row is None or row[0] != canonical(plan).decode():
            raise ValueError("Unknown or altered snapshot")

    def prepare(self, plan, persona):
        validate_plan(plan)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO chunk_snapshots VALUES(?,?)",
                            (plan["id"], canonical(plan).decode()))
        self._check(plan, persona)

    def _rows(self, plan, persona):
        self._check(plan, persona)
        rows = list(self.db.execute("SELECT part,digest,result FROM chunk_results "
                                    "WHERE snapshot=? AND persona=? ORDER BY part",
                                    (plan["id"], persona)))
        if [r[0] for r in rows] != list(range(len(rows))) or len(rows) > len(plan["parts"]) + 1:
            raise ValueError("Receipt set has missing or unexpected parts")
        verified = []
        for index, claimed_digest, encoded in rows:
            part = (plan["parts"][index] if index < len(plan["parts"])
                    else self._integration(plan, verified))
            if claimed_digest != part["digest"]:
                raise ValueError("Receipt belongs to different part")
            try:
                result = valid_result(json.loads(encoded), self._paths(plan, part))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Invalid persisted result") from exc
            verified.append(result)
        return verified

    @staticmethod
    def _paths(plan, part):
        return {f[k] for f in plan["files"] for k in ("old", "new")
                if f[k] is not None and (part["kind"] == "integration"
                                        or f["part"] == part["index"])}

    @staticmethod
    def _integration(plan, results):
        if len(results) != len(plan["parts"]):
            raise ValueError("Integration requires every diff review")
        # Preserve all findings and summaries. If these cannot fit, fail explicitly;
        # a summary prefix or an arbitrary finding cap would hide unresolved blockers.
        payload = dict(snapshot=plan["id"], binding=plan["binding"],
                       merge_base=plan["merge_base"], raw_sha256=plan["raw_sha256"],
                       part_digests=[p["digest"] for p in plan["parts"]],
                       files=[[(f["new"] if f["old"] == f["new"] else [f["old"], f["new"]]),
                               f["part"]] for f in plan["files"]], reviews=results)
        text = canonical(payload)
        if len(text) > plan["limits"]["part_bytes"]:
            raise ValueError("Complete integration review exceeds bound")
        return dict(index=len(plan["parts"]), kind="integration", digest=digest(text),
                    text=text.decode())

    def next_part(self, plan, persona):
        results = self._rows(plan, persona)
        index = len(results)
        if index < len(plan["parts"]):
            return dict(plan["parts"][index])
        if index == len(plan["parts"]):
            return self._integration(plan, results)
        return None

    def record(self, plan, persona, index, result):
        rows = self._rows(plan, persona)
        if type(index) is not int or not 0 <= index <= len(plan["parts"]):
            raise ValueError("Invalid part ordinal")
        part = (plan["parts"][index] if index < len(plan["parts"])
                else self._integration(plan, rows[:len(plan["parts"])]))
        result = valid_result(result, self._paths(plan, part))
        if index < len(rows):
            if result != rows[index]:
                raise ValueError("Conflicting receipt replay")
            return
        if index != len(rows):
            raise ValueError("Cannot record out-of-order part")
        with self.db:
            self.db.execute("INSERT INTO chunk_results VALUES(?,?,?,?,?)",
                            (plan["id"], persona, index, part["digest"], canonical(result).decode()))

    def aggregate(self, plan, persona):
        results = self._rows(plan, persona)
        if len(results) != len(plan["parts"]) + 1:
            raise ValueError("Full review and integration are incomplete")
        findings = [f for result in results for f in result["findings"]]
        verdict = ("findings" if any(f["severity"] in ("blocker", "important")
                                     for f in findings) else "clean")
        return dict(snapshot=plan["id"], persona=persona, binding=plan["binding"],
                    merge_base=plan["merge_base"], raw_sha256=plan["raw_sha256"],
                    parts=len(plan["parts"]), files=len(plan["files"]), verdict=verdict,
                    findings=findings, summary=results[-1]["summary"])
