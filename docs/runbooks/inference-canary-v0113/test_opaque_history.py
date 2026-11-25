"""Offline Linux fixtures only: python3 -B -m unittest discover -s THIS_DIRECTORY -p test_opaque_history.py"""
import importlib.util
import contextlib
import io
import sys
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("opaque_history", Path(__file__).with_name("opaque-history.py"))
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
SOURCE = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
NATIVE = "33333333-3333-4333-8333-333333333333"
STAMP = "2026-11-25T00:00:00.000Z"


def write(path, value):
    data = value if isinstance(value, bytes) else (json.dumps(value) + "\n").encode()
    path.write_bytes(data)
    path.chmod(0o600)
    return data


def jsonl(entries):
    return b"".join((json.dumps(entry) + "\n").encode() for entry in entries)


class OpaqueHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.uid = os.getuid()
        for directory in ["native-homes", "workspaces", "native-homes/" + SOURCE, "native-homes/" + OWNER]:
            (self.root / directory).mkdir(mode=0o700)
        self.source = self.root / "native-homes" / SOURCE
        self.owner = self.root / "native-homes" / OWNER
        self.build("claude")

    def build(self, provider, signatures=True):
        self.provider = provider
        self.workspace = self.root / "workspaces" / observer.PROFILES[provider]
        self.workspace.mkdir(mode=0o700, exist_ok=True)
        self.header = {"type":"session", "version":3, "id":NATIVE, "timestamp":STAMP, "cwd":str(self.workspace)}
        signature = json.dumps({"type":"reasoning", "id":"opaque-id", "encrypted_content":"fixture-ciphertext"}) if provider == "codex" else "fixture-signature-secret"
        blocks = [{"type":"thinking", "thinking":"PRIVATE MODEL TEXT", "thinkingSignature":signature}]
        if provider == "claude":
            blocks.append({"type":"thinking", "thinking":"[Reasoning redacted]", "thinkingSignature":"fixture-redacted-secret", "redacted":True})
        if not signatures:
            blocks = [{"type":"text", "text":"PRIVATE MESSAGE"}]
        self.first = {"type":"message", "id":"aaaaaaaa", "parentId":None, "timestamp":STAMP,
                      "message":{"role":"assistant", "content":blocks, "stopReason":"stop", "timestamp":1}}
        abandoned = {"type":"message", "id":"bbbbbbbb", "parentId":"aaaaaaaa", "timestamp":STAMP,
                     "message":{"role":"assistant", "content":[{"type":"thinking", "thinking":"abandoned", "thinkingSignature":signature + "-ABANDONED"}], "stopReason":"stop", "timestamp":2}}
        self.last = {"type":"message", "id":"cccccccc", "parentId":"aaaaaaaa", "timestamp":STAMP,
                     "message":{"role":"user", "content":[{"type":"text", "text":"PRIVATE USER TEXT"}], "timestamp":3}}
        self.source_bytes = jsonl([self.header, self.first, abandoned, self.last])
        self.imported_bytes = jsonl([self.header, self.first, self.last])
        self.history = self.source / "history.jsonl"
        write(self.history, self.source_bytes)
        def identity(path):
            s = path.stat()
            return {"path":str(path), "device":str(s.st_dev), "inode":str(s.st_ino), "uid":str(s.st_uid)}
        self.home_identity = identity(self.source)
        self.workspace_identity = identity(self.workspace)
        self.receipt = {"version":1, "admission":"blocked", "containment":"owned-process-group-only",
                        "home":{key:int(self.home_identity[key]) for key in ("device", "inode", "uid")},
                        "metadata":{"sessionId":SOURCE,"profileDigest":"a"*64,"nativeSessionId":NATIVE,
                                    "workspace":self.workspace_identity,"outcome":"closed",
                                    "history":{"path":str(self.history),"sha256":observer.sha(self.source_bytes)}}}
        receipt_bytes = write(self.source / "relay-retained-state.json", self.receipt)
        self.descriptor = {"version":2,"runtimeManifestSha256":"b"*64,"nativeSessionId":NATIVE,
                           "workspace":self.workspace_identity,"historyFile":str(self.history),
                           "historySha256":observer.sha(self.source_bytes),"admission":"holder-owned",
                           "sourceSessionId":SOURCE,"ownerSessionId":OWNER,
                           "retainedReceiptSha256":observer.sha(receipt_bytes),"nativeHome":self.home_identity}
        write(self.owner / "holder-recovery.json", self.descriptor)
        self.recovery = {key:value for key,value in self.descriptor.items() if key not in ("historyFile","historySha256")}
        self.recovery.update(sourceSha256=observer.sha(self.source_bytes),importedSha256=observer.sha(self.imported_bytes))
        write(self.owner / "native-recovery.json", self.recovery)
        # Actual Pi appends startup custom entries after the exact imported bytes.
        startup = {"type":"custom","id":"dddddddd","parentId":"cccccccc","timestamp":STAMP,"customType":"relay-inference-session-v1","data":{}}
        write(self.owner / "resumed-session.jsonl", self.imported_bytes + jsonl([startup]))

    def observe(self, replacement=OWNER, native=NATIVE):
        return observer.observe(self.root,self.uid,self.provider,SOURCE,native,replacement)

    def test_messages_selected_ancestry_and_exact_import_prefix(self):
        report = self.observe()
        self.assertEqual(report["status"],"PRESERVED")
        self.assertEqual(report["opaqueFieldCount"],2)
        self.assertTrue(report["orderedOpaqueFieldsEqual"])
        self.assertFalse(report["upstreamReplayProven"])
        self.assertTrue(report["opaqueFields"][1]["redacted"])
        text = json.dumps(report)
        for forbidden in ("PRIVATE", "fixture-signature-secret", "fixture-redacted-secret", "ABANDONED"):
            self.assertNotIn(forbidden,text)
        self.assertEqual(self.observe(None)["status"],"OBSERVED")

    def test_codex_hashes_signature_and_encrypted_field(self):
        self.build("codex")
        report = self.observe()
        self.assertEqual(report["status"],"PRESERVED")
        self.assertEqual([x["field"] for x in report["opaqueFields"]],["thinkingSignature","encrypted_content"])
        self.assertNotIn("fixture-ciphertext",json.dumps(report))

    def test_absence_is_not_exercised(self):
        self.build("qwen",False)
        self.assertEqual(self.observe()["status"],"NOT_EXERCISED")

    def test_external_hub_native_uuid_is_mandatory_binding(self):
        with self.assertRaises(observer.Refused):
            self.observe(native=OWNER)

    def test_ordered_hash_mismatch_not_false_success(self):
        changed = json.loads(json.dumps(self.first))
        changed["message"]["content"].reverse()
        imported = jsonl([self.header,changed,self.last])
        write(self.owner / "resumed-session.jsonl",imported)
        self.recovery["importedSha256"] = observer.sha(imported)
        write(self.owner / "native-recovery.json",self.recovery)
        self.assertEqual(self.observe()["status"],"MISMATCH")

    def test_hash_boundary_cannot_be_guessed(self):
        self.recovery["importedSha256"] = "f"*64
        write(self.owner / "native-recovery.json",self.recovery)
        with self.assertRaises(observer.Refused):
            self.observe()

    def test_local_receipt_and_descriptor_binding(self):
        for key,value in (("ownerSessionId",SOURCE),("sourceSessionId",OWNER),("retainedReceiptSha256","c"*64),("historySha256","c"*64)):
            with self.subTest(key=key):
                write(self.owner / "holder-recovery.json",{**self.descriptor,key:value})
                with self.assertRaises(observer.Refused):
                    self.observe()
        write(self.owner / "holder-recovery.json",self.descriptor)
        self.receipt["metadata"]["workspace"]["inode"] = "0"
        write(self.source / "relay-retained-state.json",self.receipt)
        with self.assertRaises(observer.Refused):
            self.observe()

    def test_unsafe_files_and_bounds(self):
        original = self.history.read_bytes()
        for kind in ("symlink","hardlink","fifo","mode","oversize"):
            with self.subTest(kind=kind):
                self.history.unlink()
                target = self.source / "target"
                if target.exists():
                    target.unlink()
                if kind in ("symlink","hardlink"):
                    write(target,original)
                    self.history.symlink_to(target) if kind == "symlink" else os.link(target,self.history)
                elif kind == "fifo":
                    os.mkfifo(self.history,0o600)
                else:
                    write(self.history,original)
                    if kind == "mode":
                        self.history.chmod(0o644)
                    else:
                        with self.history.open("r+b") as f:
                            f.truncate(observer.HISTORY_LIMIT+1)
                with self.assertRaises((observer.Refused,OSError)):
                    self.observe()
        self.history.unlink()
        write(self.history,original)

    def test_wrong_uid_and_symlinked_directory_refused(self):
        with self.assertRaises(observer.Refused):
            observer.observe(self.root,self.uid+1,self.provider,SOURCE,NATIVE)
        moved = self.root / "saved-source"
        self.source.rename(moved)
        self.source.symlink_to(moved,target_is_directory=True)
        with self.assertRaises((observer.Refused,OSError)):
            self.observe()

    def test_file_change_during_observation_refused(self):
        original = observer.PrivateTree.read
        def mutate(tree,path,limit):
            data = original(tree,path,limit)
            if Path(path).name == "resumed-session.jsonl":
                with self.history.open("ab") as f:
                    f.write(b" ")
            return data
        with patch.object(observer.PrivateTree,"read",mutate):
            with self.assertRaises(observer.Refused):
                self.observe()

    def test_replaced_parent_directory_refused(self):
        original = observer.PrivateTree.read
        def replace(tree,path,limit):
            data = original(tree,path,limit)
            if Path(path).name == "resumed-session.jsonl":
                self.source.rename(self.root / "saved-source")
                self.source.mkdir(mode=0o700)
            return data
        with patch.object(observer.PrivateTree,"read",replace):
            with self.assertRaises(observer.Refused):
                self.observe()

    def test_invalid_ancestry_and_duplicate_json_refused(self):
        invalid = {**self.first,"parentId":"ffffffff"}
        with self.assertRaises(observer.Refused):
            observer.opaque_fields(jsonl([self.header,invalid]),NATIVE,self.workspace,self.provider)
        with self.assertRaises(observer.Refused):
            observer.decode(b'{"version":1,"version":2}')

    def test_private_parent_mode_and_source_digest_refused(self):
        self.source.chmod(0o755)
        with self.assertRaises(observer.Refused):
            self.observe()
        self.source.chmod(0o700)
        write(self.history,self.source_bytes.replace(b"PRIVATE MODEL TEXT",b"ALTERED MODEL TEXT"))
        with self.assertRaises(observer.Refused):
            self.observe()

    def test_cli_never_prints_private_exception_contents(self):
        output = io.StringIO()
        with patch.object(sys,"argv",["opaque-history.py","snapshot","claude",SOURCE,NATIVE]), \
             patch.object(observer.os,"getuid",return_value=1000), \
             patch.object(observer,"observe",side_effect=ValueError("PRIVATE CIPHERTEXT")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(observer.main(),1)
        self.assertEqual(json.loads(output.getvalue())["status"],"REFUSED")
        self.assertNotIn("PRIVATE",output.getvalue())


if __name__ == "__main__":
    unittest.main()
