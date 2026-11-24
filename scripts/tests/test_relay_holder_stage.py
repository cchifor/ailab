import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'ansible/roles/relay_holder/files/stage_archive.py'
spec = importlib.util.spec_from_file_location('holder_stage', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class StageTests(unittest.TestCase):
    def run_archive(self, members, digest=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        archive = root / 'release.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for info, content in members:
                tar.addfile(info, io.BytesIO(content) if content else None)
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        return root, archive, digest or sha

    def test_exact_retry_preserves_existing_files_for_installer_verification(self):
        info = tarfile.TarInfo('manifest.json')
        info.size = 2
        root, archive, sha = self.run_archive([(info, b'{}')])
        self.assertTrue(module.stage(archive, sha, root / 'out'))
        (root / 'out/manifest.json').write_text('changed')
        self.assertFalse(module.stage(archive, sha, root / 'out'))
        self.assertEqual((root / 'out/manifest.json').read_text(), 'changed')

    def test_digest_failure_writes_nothing(self):
        root, archive, _ = self.run_archive([])
        with self.assertRaises(ValueError):
            module.stage(archive, '0' * 64, root / 'out')
        self.assertFalse((root / 'out').exists())

    def test_escape_and_special_entries_are_refused(self):
        for kind, name, target in [(tarfile.REGTYPE, '../escape', ''),
                                   (tarfile.REGTYPE, '/escape', ''),
                                   (tarfile.SYMTYPE, 'link', '../escape'),
                                   (tarfile.SYMTYPE, 'link', '/etc/passwd'),
                                   (tarfile.LNKTYPE, 'link', 'manifest.json'),
                                   (tarfile.FIFOTYPE, 'pipe', '')]:
            with self.subTest(kind=kind, name=name, target=target):
                info = tarfile.TarInfo(name)
                info.type, info.linkname = kind, target
                root, archive, sha = self.run_archive([(info, None)])
                with self.assertRaises((ValueError, tarfile.FilterError)):
                    module.stage(archive, sha, root / 'out')
                self.assertFalse((root / 'out').exists())
                self.assertEqual(list(root.glob('.extract-*')), [])

    def test_relative_internal_link_survives(self):
        file = tarfile.TarInfo('node')
        file.size, file.mode = 1, 0o755
        link = tarfile.TarInfo('bin/node')
        link.type, link.linkname = tarfile.SYMTYPE, '../node'
        root, archive, sha = self.run_archive([(file, b'x'), (link, None)])
        module.stage(archive, sha, root / 'out')
        self.assertTrue((root / 'out/bin/node').is_symlink())
        self.assertEqual((root / 'out/bin/node').read_bytes(), b'x')

    def test_destination_ancestry_rejects_symlink_and_shared_parent(self):
        spec = importlib.util.spec_from_file_location('holder_paths', SOURCE.with_name('check_paths.py'))
        paths = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(paths)
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            root = Path(temp).resolve()
            paths.check(os.getuid(), [str(root / 'new/child')])
            (root / 'link').symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                paths.check(os.getuid(), [str(root / 'link/new')])
            root.chmod(0o777)
            with self.assertRaises(ValueError):
                paths.check(os.getuid(), [str(root / 'new')])
            root.chmod(0o700)

    def test_exact_connector_digest_directory_must_be_checked(self):
        spec = importlib.util.spec_from_file_location('holder_paths', SOURCE.with_name('check_paths.py'))
        paths = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(paths)
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            root = Path(temp).resolve()
            connectors = root / 'connectors'
            connectors.mkdir()
            outside = root / 'unrelated'
            outside.mkdir()
            digest = connectors / ('a' * 64)
            digest.symlink_to(outside, target_is_directory=True)
            # Checking only the parent misses the redirection at the digest leaf.
            paths.check(os.getuid(), [str(connectors)])
            with self.assertRaises(ValueError):
                paths.check(os.getuid(), [str(digest)])
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
