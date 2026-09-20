"""Actual operator provisioning helpers; synthetic bytes, no cluster mutations."""
import base64
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/'provision-dsh-conductor-artifact.py'
spec=importlib.util.spec_from_file_location('conductor_artifact',path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ArtifactProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.payload=b'fixed reviewed fixture bytes'
        self.digest=hashlib.sha256(self.payload).hexdigest()
        self.constants=patch.multiple(m,SHA=self.digest,SIZE=len(self.payload),NAME='dsh-conductor-artifact-'+self.digest[:16])
        self.constants.start();self.addCleanup(self.constants.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.source=Path(self.temp.name)/'artifact';self.source.write_bytes(self.payload)

    def test_verified_buffer_and_manifest(self):
        payload=m.read_verified(self.source);value=m.manifest(payload)
        self.assertEqual(base64.b64decode(value['binaryData'][m.KEY]),self.payload)
        self.assertIs(value['immutable'],True)
        m.verify_existing(value,payload)

    def test_symlink_refused(self):
        link=Path(self.temp.name)/'link';link.symlink_to(self.source)
        with self.assertRaises(OSError):m.read_verified(link)

    def test_fifo_refused_without_read(self):
        fifo=Path(self.temp.name)/'fifo';os.mkfifo(fifo)
        with self.assertRaises(ValueError):m.read_verified(fifo)

    def test_wrong_size_refused(self):
        self.source.write_bytes(self.payload+b'x')
        with self.assertRaises(ValueError):m.read_verified(self.source)

    def test_wrong_checksum_refused(self):
        self.source.write_bytes(b'x'*len(self.payload))
        with self.assertRaises(ValueError):m.read_verified(self.source)
        with self.assertRaises(ValueError):m.manifest(b'x'*len(self.payload))

    def test_existing_shape_drift_refused(self):
        valid=m.manifest(self.payload)
        mutations=[lambda x:x.update(immutable=False),lambda x:x.update(data={'conductor.runtime.json':'override'}),lambda x:x['binaryData'].update(extra=''),lambda x:x['metadata'].update(namespace='other'),lambda x:x['metadata'].update(ownerReferences=[{'uid':'ephemeral'}]),lambda x:x['metadata'].update(deletionTimestamp='pending')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value=copy.deepcopy(valid);mutate(value)
                with self.assertRaises(ValueError):m.verify_existing(value,self.payload)

    def test_existing_byte_drift_refused(self):
        value=m.manifest(self.payload);value['binaryData'][m.KEY]=base64.b64encode(b'x'*len(self.payload)).decode()
        with self.assertRaises(ValueError):m.verify_existing(value,self.payload)

    def test_existing_verification_never_mutates(self):
        value=m.manifest(self.payload);before=copy.deepcopy(value)
        m.verify_existing(value,self.payload);self.assertEqual(value,before)

if __name__=='__main__':unittest.main()
