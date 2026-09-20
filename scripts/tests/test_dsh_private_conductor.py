#!/usr/bin/env python3
"""
test_dsh_private_conductor.py

Tests for stage-private-conductor.mjs - operator-only verified private Gitea tarball staging.

Tests execute the ACTUAL Node.js helper with temporary files using Python unittest.
Covers:
- Successful copy/reuse
- Hash mismatch with no output
- Oversized/nonregular source refusal (no FIFO hang)
- Corrupt existing destination refusal
- Symlink destination refusal
- Safe behavior with spaces in paths
- Dangling symlink handling
- Hardlinked destination rejection
- Linked destination directory rejection
- Inert module import
- Bounded static errors (no interpolated paths)
- Concurrent differing-output no-clobber
"""

import json
import os
import subprocess
import tempfile
import hashlib
import unittest
import stat
import shutil
import threading
import time
from pathlib import Path


# Static error codes (must match implementation)
ERR_INVALID_ARGS = 'INVALID_ARGS'
ERR_INVALID_HASH_FORMAT = 'INVALID_HASH_FORMAT'
ERR_SOURCE_NOT_REGULAR = 'SOURCE_NOT_REGULAR'
ERR_SOURCE_TOO_LARGE = 'SOURCE_TOO_LARGE'
ERR_SOURCE_READ_FAILED = 'SOURCE_READ_FAILED'
ERR_HASH_MISMATCH = 'HASH_MISMATCH'
ERR_DEST_DIR_CREATE_FAILED = 'DEST_DIR_CREATE_FAILED'
ERR_DEST_IS_SYMLINK = 'DEST_IS_SYMLINK'
ERR_DEST_NOT_REGULAR = 'DEST_NOT_REGULAR'
ERR_DEST_HARDLINKED = 'DEST_HARDLINKED'
ERR_DEST_HASH_MISMATCH = 'DEST_HASH_MISMATCH'
ERR_TEMP_CREATE_FAILED = 'TEMP_CREATE_FAILED'
ERR_WRITE_FAILED = 'WRITE_FAILED'
ERR_SYNC_FAILED = 'SYNC_FAILED'
ERR_LINK_FAILED = 'LINK_FAILED'
ERR_UNLINK_FAILED = 'UNLINK_FAILED'


class TestDshPrivateConductor(unittest.TestCase):
    """Test suite for stage-private-conductor.mjs"""

    SCRIPT_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'kubernetes', 'apps', 'apps', 'dsh', 'stage-private-conductor.mjs'
    )

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix='dsh_private_conductor_test_')

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_stager(self, source_path, expected_hash, dest_root):
        """Run the stager script and return (returncode, stdout, stderr)"""
        cmd = ['node', self.SCRIPT_PATH, source_path, expected_hash, dest_root]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode, result.stdout, result.stderr

    def compute_sha256(self, data):
        """Compute SHA256 hash of bytes"""
        return hashlib.sha256(data).hexdigest().lower()

    def create_test_file(self, name, content):
        """Create a test file with given content"""
        filepath = os.path.join(self.temp_dir, name)
        with open(filepath, 'wb') as f:
            f.write(content)
        return filepath

    def test_successful_copy(self):
        """Test successful copy of a valid tarball"""
        content = b'Hello, World!'
        source_path = self.create_test_file('source.tar.gz', content)
        expected_hash = self.compute_sha256(content)

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertEqual(returncode, 0, f"Expected success, got: {stderr}")
        result = json.loads(stdout)
        self.assertTrue(result['success'])
        self.assertEqual(result['hash'], expected_hash)
        self.assertTrue(result['destination'].endswith(f'{expected_hash}/dsh-team-conductor-0.1.0.tgz'))
        self.assertTrue(os.path.exists(result['destination']))
        with open(result['destination'], 'rb') as f:
            self.assertEqual(f.read(), content)

    def test_successful_reuse(self):
        """Test reuse of existing matching destination"""
        content = b'Reuse Test'
        source_path = self.create_test_file('source.tar.gz', content)
        expected_hash = self.compute_sha256(content)

        dest_dir = os.path.join(self.temp_dir, 'dest', expected_hash)
        os.makedirs(dest_dir)
        dest_path = os.path.join(dest_dir, 'dsh-team-conductor-0.1.0.tgz')
        with open(dest_path, 'wb') as f:
            f.write(content)

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertEqual(returncode, 0, f"Expected success, got: {stderr}")
        result = json.loads(stdout)
        self.assertTrue(result['success'])
        self.assertEqual(result['hash'], expected_hash)
        self.assertEqual(result['destination'], dest_path)

    def test_hash_mismatch_no_output(self):
        """Test hash mismatch - no output written"""
        content = b'Actual Content'
        source_path = self.create_test_file('source.tar.gz', content)
        wrong_hash = 'a' * 64

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, wrong_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_HASH_MISMATCH)

        expected_dest = os.path.join(dest_root, wrong_hash, 'dsh-team-conductor-0.1.0.tgz')
        self.assertFalse(os.path.exists(expected_dest))

    def test_oversized_source_refusal(self):
        """Test oversized source (>1MiB) is refused"""
        oversized_content = b'x' * (1024 * 1024 + 1)
        source_path = self.create_test_file('oversized.tar.gz', oversized_content)
        expected_hash = self.compute_sha256(oversized_content)

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_SOURCE_TOO_LARGE)

    def test_fifo_source_refusal(self):
        """Test FIFO source is refused (no hang)"""
        fifo_path = os.path.join(self.temp_dir, 'source.fifo')
        os.mkfifo(fifo_path)

        expected_hash = self.compute_sha256(b'')

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(fifo_path, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_SOURCE_NOT_REGULAR)

    def test_corrupt_existing_destination_refusal(self):
        """Test corrupt existing destination (hash mismatch) is refused"""
        content = b'New Content'
        source_path = self.create_test_file('source.tar.gz', content)
        expected_hash = self.compute_sha256(content)

        dest_dir = os.path.join(self.temp_dir, 'dest', expected_hash)
        os.makedirs(dest_dir)
        dest_path = os.path.join(dest_dir, 'dsh-team-conductor-0.1.0.tgz')
        with open(dest_path, 'wb') as f:
            f.write(b'Old Corrupt Content')

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_DEST_HASH_MISMATCH)

        with open(dest_path, 'rb') as f:
            self.assertEqual(f.read(), b'Old Corrupt Content')

    def test_symlink_destination_refusal(self):
        """Test symlink destination is refused"""
        content = b'Symlink Dest Test'
        source_path = self.create_test_file('source.tar.gz', content)
        expected_hash = self.compute_sha256(content)

        dest_dir = os.path.join(self.temp_dir, 'dest', expected_hash)
        os.makedirs(dest_dir)
        dest_path = os.path.join(dest_dir, 'dsh-team-conductor-0.1.0.tgz')

        real_file = os.path.join(self.temp_dir, 'real.tar.gz')
        with open(real_file, 'wb') as f:
            f.write(b'Real File')

        os.symlink(real_file, dest_path)

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_DEST_IS_SYMLINK)

    def test_spaces_in_paths(self):
        """Test safe behavior with spaces in paths"""
        space_dir = os.path.join(self.temp_dir, 'path with spaces')
        os.makedirs(space_dir)
        source_path = os.path.join(space_dir, 'source file.tar.gz')
        content = b'Spaces Test'
        with open(source_path, 'wb') as f:
            f.write(content)

        expected_hash = self.compute_sha256(content)
        dest_root = os.path.join(self.temp_dir, 'dest root with spaces')

        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertEqual(returncode, 0, f"Expected success, got: {stderr}")
        result = json.loads(stdout)
        self.assertTrue(result['success'])
        self.assertEqual(result['hash'], expected_hash)
        self.assertTrue(os.path.exists(result['destination']))

    def test_invalid_hash_format(self):
        """Test invalid hash format error"""
        source_path = self.create_test_file('source.tar.gz', b'Test')
        invalid_hash = 'not-a-valid-hash'

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, invalid_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_INVALID_HASH_FORMAT)

    def test_uppercase_hash_rejected(self):
        """Test uppercase hash is rejected (must be lowercase)"""
        content = b'Test'
        source_path = self.create_test_file('source.tar.gz', content)
        actual_hash = self.compute_sha256(content)
        uppercase_hash = actual_hash.upper()

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, uppercase_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_INVALID_HASH_FORMAT)

    def test_empty_source_refusal(self):
        """Test empty source file is refused"""
        source_path = self.create_test_file('empty.tar.gz', b'')
        expected_hash = self.compute_sha256(b'')

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_SOURCE_READ_FAILED)

    def test_directory_source_refusal(self):
        """Test directory source is refused"""
        dir_path = os.path.join(self.temp_dir, 'not-a-file')
        os.makedirs(dir_path)

        expected_hash = 'a' * 64

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(dir_path, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_SOURCE_NOT_REGULAR)

    def test_preserves_unrelated_artifacts(self):
        """Test that unrelated artifacts in destination are preserved"""
        content = b'Preserve Test'
        source_path = self.create_test_file('source.tar.gz', content)
        expected_hash = self.compute_sha256(content)

        dest_dir = os.path.join(self.temp_dir, 'dest', expected_hash)
        os.makedirs(dest_dir)
        unrelated_file = os.path.join(dest_dir, 'unrelated.txt')
        with open(unrelated_file, 'wb') as f:
            f.write(b'Unrelated Content')

        other_hash = 'b' * 64
        other_dir = os.path.join(self.temp_dir, 'dest', other_hash)
        os.makedirs(other_dir)
        other_file = os.path.join(other_dir, 'dsh-team-conductor-0.1.0.tgz')
        with open(other_file, 'wb') as f:
            f.write(b'Other Slot Content')

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertEqual(returncode, 0, f"Expected success, got: {stderr}")

        self.assertTrue(os.path.exists(unrelated_file))
        with open(unrelated_file, 'rb') as f:
            self.assertEqual(f.read(), b'Unrelated Content')

        self.assertTrue(os.path.exists(other_file))
        with open(other_file, 'rb') as f:
            self.assertEqual(f.read(), b'Other Slot Content')

    def test_1mb_exact_limit(self):
        """Test exactly 1MiB file is accepted"""
        content = b'x' * (1024 * 1024)
        source_path = self.create_test_file('exactly-1mb.tar.gz', content)
        expected_hash = self.compute_sha256(content)

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertEqual(returncode, 0, f"Expected success for exactly 1MiB, got: {stderr}")
        result = json.loads(stdout)
        self.assertTrue(result['success'])

    def test_dangling_symlink_source(self):
        """Test dangling source symlink is refused"""
        # Create symlink to non-existent target
        symlink_path = os.path.join(self.temp_dir, 'dangling.tar.gz')
        os.symlink('/nonexistent/path/file.tar.gz', symlink_path)

        expected_hash = 'a' * 64

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(symlink_path, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        # Should fail with SOURCE_NOT_REGULAR since target doesn't exist
        # (or WRITE_FAILED if caught in general handler)
        self.assertIn(result['error'], [ERR_SOURCE_NOT_REGULAR, ERR_WRITE_FAILED])

    def test_hardlinked_destination_refusal(self):
        """Test hardlinked destination is refused"""
        content = b'Hardlink Test'
        source_path = self.create_test_file('source.tar.gz', content)
        expected_hash = self.compute_sha256(content)

        # Create destination directory and file
        dest_dir = os.path.join(self.temp_dir, 'dest', expected_hash)
        os.makedirs(dest_dir)
        dest_path = os.path.join(dest_dir, 'dsh-team-conductor-0.1.0.tgz')
        with open(dest_path, 'wb') as f:
            f.write(content)

        # Create a hard link to the destination
        hardlink_path = os.path.join(self.temp_dir, 'hardlink.tar.gz')
        os.link(dest_path, hardlink_path)

        # Now destination has nlink=2
        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], ERR_DEST_HARDLINKED)

    def test_linked_destination_directory(self):
        """Test when destination directory is a symlink (should fail)"""
        content = b'Dir Symlink Test'
        source_path = self.create_test_file('source.tar.gz', content)
        expected_hash = self.compute_sha256(content)

        # Create a real directory elsewhere
        real_dir = os.path.join(self.temp_dir, 'real_dest_dir')
        os.makedirs(real_dir)

        # Create symlink named as the hash directory
        hash_dir = os.path.join(self.temp_dir, 'dest', expected_hash)
        os.makedirs(os.path.dirname(hash_dir))
        os.symlink(real_dir, hash_dir)

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(source_path, expected_hash, dest_root)

        # Should fail because destination dir path contains a symlink component
        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])
        # Error should be DEST_DIR_CREATE_FAILED (symlink in path) or similar
        self.assertIn(result['error'], [ERR_DEST_DIR_CREATE_FAILED, ERR_DEST_IS_SYMLINK])

    def test_inert_module_import(self):
        """Actually import from another entry with the same basename."""
        launcher = os.path.join(self.temp_dir, 'stage-private-conductor.mjs')
        with open(launcher, 'w') as f:
            f.write("import {pathToFileURL} from 'node:url';\nawait import(pathToFileURL(" + json.dumps(self.SCRIPT_PATH) + "));\nconsole.log('imported');\n")
        result = subprocess.run(['node', launcher], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'imported')
        self.assertEqual(os.listdir(self.temp_dir), ['stage-private-conductor.mjs'])

    def test_projected_script_entry(self):
        """A symlinked ConfigMap-style entry must really execute."""
        entry = os.path.join(self.temp_dir, 'projected-entry.mjs')
        os.symlink(self.SCRIPT_PATH, entry)
        content = b'projected-script'
        source = self.create_test_file('source.tgz', content)
        result = subprocess.run(['node', entry, source, self.compute_sha256(content), os.path.join(self.temp_dir, 'dest')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        with open(output['destination'], 'rb') as f:
            self.assertEqual(f.read(), content)

    def test_raced_destination_is_not_replaced(self):
        """Inject a real differing file immediately before the actual link syscall."""
        content = b'approved'
        source = self.create_test_file('source.tgz', content)
        expected = self.compute_sha256(content)
        dest = os.path.join(self.temp_dir, 'dest')
        code = """import fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';
import {pathToFileURL} from 'node:url';
const link = fs.linkSync;
fs.linkSync = (src,dst) => { fs.writeFileSync(dst,'competing-content',{flag:'wx'}); return link(src,dst); };
syncBuiltinESMExports();
const m = await import(pathToFileURL(SCRIPT));
console.log(JSON.stringify(m.stagePrivateConductor(...process.argv.slice(1))));
""".replace('SCRIPT', json.dumps(self.SCRIPT_PATH))
        result = subprocess.run(['node', '--input-type=module', '-e', code, source, expected, dest], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['success'])
        folder = os.path.join(dest, expected)
        output = os.path.join(folder, 'dsh-team-conductor-0.1.0.tgz')
        with open(output, 'rb') as f:
            self.assertEqual(f.read(), b'competing-content')
        self.assertEqual(os.listdir(folder), ['dsh-team-conductor-0.1.0.tgz'])

    def test_dangling_destination_link(self):
        content = b'approved'
        source = self.create_test_file('source.tgz', content)
        expected = self.compute_sha256(content)
        dest = os.path.join(self.temp_dir, 'dest')
        folder = os.path.join(dest, expected)
        os.makedirs(folder)
        output = os.path.join(folder, 'dsh-team-conductor-0.1.0.tgz')
        os.symlink('absent-target', output)
        rc, out, err = self.run_stager(source, expected, dest)
        self.assertNotEqual(rc, 0)
        self.assertEqual(json.loads(out)['error'], ERR_DEST_IS_SYMLINK)
        self.assertTrue(os.path.islink(output))
        self.assertEqual(os.readlink(output), 'absent-target')

    def test_bounded_static_errors(self):
        """Test that error messages are static (no interpolated paths or error.message)"""
        # Test with non-existent source - error should be static
        non_existent = os.path.join(self.temp_dir, 'nonexistent', 'path', 'file.tar.gz')
        expected_hash = 'a' * 64

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(non_existent, expected_hash, dest_root)

        self.assertNotEqual(returncode, 0)
        result = json.loads(stdout)
        self.assertFalse(result['success'])

        # Error should be a static code, not contain the path
        error = result['error']
        self.assertIsInstance(error, str)
        # Should NOT contain path components or error messages
        self.assertNotIn(non_existent, error)
        self.assertNotIn('/tmp', error)
        self.assertNotIn('ENOENT', error)
        # Should be a known static error code (SOURCE_NOT_REGULAR or WRITE_FAILED)
        self.assertIn(error, [ERR_INVALID_ARGS, ERR_SOURCE_NOT_REGULAR, ERR_SOURCE_READ_FAILED, ERR_WRITE_FAILED])

    def test_concurrent_distinct_artifacts(self):
        """Concurrent distinct digests retain their separate output files."""
        content1 = b'Content for thread 1'
        content2 = b'Content for thread 2'

        hash1 = self.compute_sha256(content1)
        hash2 = self.compute_sha256(content2)

        # Create two source files with different content/hashes
        source1 = self.create_test_file('source1.tar.gz', content1)
        source2 = self.create_test_file('source2.tar.gz', content2)

        dest_root = os.path.join(self.temp_dir, 'dest')
        results = []
        errors = []

        def run_stager1():
            try:
                rc, out, err = self.run_stager(source1, hash1, dest_root)
                results.append(('thread1', rc, json.loads(out)))
            except Exception as e:
                errors.append(('thread1', str(e)))

        def run_stager2():
            try:
                rc, out, err = self.run_stager(source2, hash2, dest_root)
                results.append(('thread2', rc, json.loads(out)))
            except Exception as e:
                errors.append(('thread2', str(e)))

        # Run concurrently
        t1 = threading.Thread(target=run_stager1)
        t2 = threading.Thread(target=run_stager2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(results), 2)

        # Both should succeed (different hashes = different destination dirs)
        for thread_name, rc, result in results:
            self.assertEqual(rc, 0, f"{thread_name} failed: {result}")
            self.assertTrue(result['success'])

        # Verify both files exist at their respective destinations
        dest1 = os.path.join(dest_root, hash1, 'dsh-team-conductor-0.1.0.tgz')
        dest2 = os.path.join(dest_root, hash2, 'dsh-team-conductor-0.1.0.tgz')
        self.assertTrue(os.path.exists(dest1))
        self.assertTrue(os.path.exists(dest2))
        with open(dest1, 'rb') as f:
            self.assertEqual(f.read(), content1)
        with open(dest2, 'rb') as f:
            self.assertEqual(f.read(), content2)

    def test_legitimate_source_symlink(self):
        """Test legitimate source symlink is allowed and resolved"""
        content = b'Real Source Content'
        real_path = self.create_test_file('real.tar.gz', content)

        # Create symlink to real file
        symlink_path = os.path.join(self.temp_dir, 'symlink.tar.gz')
        os.symlink(real_path, symlink_path)

        expected_hash = self.compute_sha256(content)

        dest_root = os.path.join(self.temp_dir, 'dest')
        returncode, stdout, stderr = self.run_stager(symlink_path, expected_hash, dest_root)

        self.assertEqual(returncode, 0, f"Expected success with symlink, got: {stderr}")
        result = json.loads(stdout)
        self.assertTrue(result['success'])
        self.assertEqual(result['hash'], expected_hash)


class TestDshPrivateConductorErrorCodes(unittest.TestCase):
    """Test that all error codes are static and bounded"""

    SCRIPT_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'kubernetes', 'apps', 'apps', 'dsh', 'stage-private-conductor.mjs'
    )

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix='dsh_error_test_')

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_stager(self, source_path, expected_hash, dest_root):
        cmd = ['node', self.SCRIPT_PATH, source_path, expected_hash, dest_root]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout, result.stderr

    def test_invalid_args_error(self):
        """Test INVALID_ARGS is returned for wrong argument count"""
        # Run with 4 arguments instead of 3
        dest_root = os.path.join(self.temp_dir, 'dest')
        cmd = ['node', self.SCRIPT_PATH, 'source', 'hash', dest_root, 'extra']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        self.assertNotEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output['error'], ERR_INVALID_ARGS)

    def test_all_errors_are_static(self):
        """Verify all error codes don't contain interpolated data"""
        valid_errors = {
            ERR_INVALID_ARGS, ERR_INVALID_HASH_FORMAT, ERR_SOURCE_NOT_REGULAR,
            ERR_SOURCE_TOO_LARGE, ERR_SOURCE_READ_FAILED, ERR_HASH_MISMATCH,
            ERR_DEST_DIR_CREATE_FAILED, ERR_DEST_IS_SYMLINK, ERR_DEST_NOT_REGULAR,
            ERR_DEST_HARDLINKED, ERR_DEST_HASH_MISMATCH, ERR_TEMP_CREATE_FAILED,
            ERR_WRITE_FAILED, ERR_SYNC_FAILED, ERR_LINK_FAILED, ERR_UNLINK_FAILED
        }

        # Run various error scenarios and verify error codes are in valid set
        content = b'Test'
        source = self.create_test_file('test.tar.gz', content)

        # Invalid hash format
        rc, out, _ = self.run_stager(source, 'invalid', self.temp_dir)
        result = json.loads(out)
        self.assertIn(result['error'], valid_errors)

        # Hash mismatch
        rc, out, _ = self.run_stager(source, 'a' * 64, self.temp_dir)
        result = json.loads(out)
        self.assertIn(result['error'], valid_errors)

        # Oversized
        oversized = self.create_test_file('big.tar.gz', b'x' * (1024 * 1024 + 1))
        rc, out, _ = self.run_stager(oversized, self.compute_sha256(b'x' * (1024 * 1024 + 1)), self.temp_dir)
        result = json.loads(out)
        self.assertIn(result['error'], valid_errors)

    def create_test_file(self, name, content):
        filepath = os.path.join(self.temp_dir, name)
        with open(filepath, 'wb') as f:
            f.write(content)
        return filepath

    def compute_sha256(self, data):
        return hashlib.sha256(data).hexdigest().lower()


if __name__ == '__main__':
    unittest.main(verbosity=2)
