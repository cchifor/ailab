#!/usr/bin/env python3
"""Unit tests for image-pin.py using unittest.mock. No Docker/SSH/live cluster."""

import hashlib
import importlib.util
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Load image_pin module using importlib with relative path resolution
BASE_DIR = Path(__file__).resolve().parents[2]
IMAGE_PIN_PATH = BASE_DIR / 'ansible' / 'roles' / 'dsh_validator_pin' / 'files' / 'image-pin.py'
spec = importlib.util.spec_from_file_location('image_pin', str(IMAGE_PIN_PATH))
image_pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_pin)
sys.modules['image_pin'] = image_pin


def make_valid_container(running=True):
    """Return a container dict that passes all validation."""
    return {
        'Image': image_pin.IMAGE,
        'Config': {
            'Labels': {image_pin.LABEL: '0.1.0'},
            'User': '65532:65532',
            'Entrypoint': ['/bin/sleep'],
            'Cmd': ['2147483647'],
        },
        'HostConfig': {
            'NetworkMode': 'none',
            'ReadonlyRootfs': True,
            'Privileged': False,
            'Memory': image_pin.MEMORY,
            'MemorySwap': image_pin.MEMORY,
            'NanoCpus': 10000000,
            'PidsLimit': 4,
            'CapDrop': ['ALL'],
            'SecurityOpt': ['no-new-privileges'],
            'RestartPolicy': {'Name': 'unless-stopped'},
            'Binds': None,
            'PortBindings': None,
            'PidMode': '',
            'IpcMode': 'private',
            'LogConfig': {'Type': 'none'},
            'UTSMode': '',
            'DeviceRequests': [],
            'Devices': [],
        },
        'NetworkSettings': {'Networks': {'none': {}}},
        'Mounts': [],
        'State': {'Running': running},
    }


class TestInspectFunction(unittest.TestCase):
    """Test the inspect() function for container and image inspection."""

    def test_inspect_found_returns_object(self):
        mock_result = MagicMock(returncode=0, stdout=json.dumps([{'Id': 'abc'}]).encode())
        with patch('image_pin.docker', return_value=mock_result):
            result = image_pin.inspect('container', 'test')
            self.assertEqual(result['Id'], 'abc')

    def test_inspect_not_found_returns_none(self):
        for stderr in [b'No such container', b'no such image']:
            with self.subTest(stderr=stderr):
                mock_result = MagicMock(returncode=1, stdout=b'', stderr=stderr)
                with patch('image_pin.docker', return_value=mock_result):
                    self.assertIsNone(image_pin.inspect('container', 'x'))

    def test_inspect_unavailable_raises(self):
        mock_result = MagicMock(returncode=1, stdout=b'', stderr=b'Docker daemon error')
        with patch('image_pin.docker', return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                image_pin.inspect('container', 'test')
            self.assertIn('Docker inspection unavailable', str(ctx.exception))

    def test_inspect_unexpected_shape_raises(self):
        mock_result = MagicMock(returncode=0, stdout=json.dumps([]).encode())
        with patch('image_pin.docker', return_value=mock_result):
            with self.assertRaises(RuntimeError):
                image_pin.inspect('container', 'test')


class TestArchiveBytesFunction(unittest.TestCase):
    """Test the archive_bytes() function for secure archive reading."""

    def test_archive_bytes_success(self):
        """Test successful archive reading with valid checksum."""
        data = b'x' * image_pin.ARCHIVE_SIZE
        mock_stat = MagicMock(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_size=image_pin.ARCHIVE_SIZE)
        with patch('os.open', return_value=3):
            with patch('os.fdopen') as m:
                mf = MagicMock(); mf.fileno.return_value = 3; mf.read.return_value = data
                m.return_value.__enter__.return_value = mf
                with patch('os.fstat', return_value=mock_stat):
                    # Patch hashlib.sha256 in the image_pin module namespace
                    with patch('image_pin.hashlib.sha256') as h:
                        h.return_value.hexdigest.return_value = image_pin.ARCHIVE_SHA
                        result = image_pin.archive_bytes()
                        self.assertEqual(result, data)

    def test_archive_bytes_rejects_non_regular_file(self):
        mock_stat = MagicMock(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_size=image_pin.ARCHIVE_SIZE)
        with patch('os.open', return_value=3):
            with patch('os.fdopen') as m:
                mf = MagicMock(); mf.fileno.return_value = 3; mf.read.return_value = b'x' * image_pin.ARCHIVE_SIZE
                m.return_value.__enter__.return_value = mf
                with patch('os.fstat', return_value=mock_stat):
                    with self.assertRaises(RuntimeError) as ctx:
                        image_pin.archive_bytes()
                    self.assertIn('Unsafe archive', str(ctx.exception))

    def test_archive_bytes_rejects_wrong_owner(self):
        mock_stat = MagicMock(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_size=image_pin.ARCHIVE_SIZE)
        with patch('os.open', return_value=3):
            with patch('os.fdopen') as m:
                mf = MagicMock(); mf.fileno.return_value = 3; mf.read.return_value = b'x' * image_pin.ARCHIVE_SIZE
                m.return_value.__enter__.return_value = mf
                with patch('os.fstat', return_value=mock_stat):
                    with self.assertRaises(RuntimeError) as ctx:
                        image_pin.archive_bytes()
                    self.assertIn('Unsafe archive', str(ctx.exception))

    def test_archive_bytes_rejects_world_writable(self):
        mock_stat = MagicMock(st_mode=stat.S_IFREG | 0o666, st_uid=0, st_size=image_pin.ARCHIVE_SIZE)
        with patch('os.open', return_value=3):
            with patch('os.fdopen') as m:
                mf = MagicMock(); mf.fileno.return_value = 3; mf.read.return_value = b'x' * image_pin.ARCHIVE_SIZE
                m.return_value.__enter__.return_value = mf
                with patch('os.fstat', return_value=mock_stat):
                    with self.assertRaises(RuntimeError) as ctx:
                        image_pin.archive_bytes()
                    self.assertIn('Unsafe archive', str(ctx.exception))

    def test_archive_bytes_rejects_checksum_mismatch(self):
        data = b'x' * image_pin.ARCHIVE_SIZE
        mock_stat = MagicMock(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_size=image_pin.ARCHIVE_SIZE)
        with patch('os.open', return_value=3):
            with patch('os.fdopen') as m:
                mf = MagicMock(); mf.fileno.return_value = 3; mf.read.return_value = data
                m.return_value.__enter__.return_value = mf
                with patch('os.fstat', return_value=mock_stat):
                    with patch('hashlib.sha256') as h:
                        h.return_value.hexdigest.return_value = 'wronghash'
                        with self.assertRaises(RuntimeError) as ctx:
                            image_pin.archive_bytes()
                        self.assertIn('Archive checksum mismatch', str(ctx.exception))


class TestValidateContainerFunction(unittest.TestCase):
    """Test validate_container() rejects deviations from expected config."""

    def test_valid_container_passes(self):
        # Should not raise
        image_pin.validate_container(make_valid_container())

    def test_rejects_wrong_image(self):
        c = make_valid_container()
        c['Image'] = 'sha256:wrong'
        with self.assertRaises(RuntimeError) as ctx:
            image_pin.validate_container(c)
        self.assertIn('mismatch', str(ctx.exception))

    def test_rejects_missing_label(self):
        c = make_valid_container()
        c['Config']['Labels'] = {}
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_wrong_user(self):
        c = make_valid_container()
        c['Config']['User'] = 'root'
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_network_not_none(self):
        c = make_valid_container()
        c['HostConfig']['NetworkMode'] = 'bridge'
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_not_readonly(self):
        c = make_valid_container()
        c['HostConfig']['ReadonlyRootfs'] = False
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_privileged(self):
        c = make_valid_container()
        c['HostConfig']['Privileged'] = True
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_wrong_memory(self):
        c = make_valid_container()
        c['HostConfig']['Memory'] = 128 * 1024 * 1024
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_wrong_nano_cpus(self):
        c = make_valid_container()
        c['HostConfig']['NanoCpus'] = 100000000
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_wrong_pids_limit(self):
        c = make_valid_container()
        c['HostConfig']['PidsLimit'] = 100
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_caps_not_dropped(self):
        c = make_valid_container()
        c['HostConfig']['CapDrop'] = []
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_has_mounts(self):
        c = make_valid_container()
        c['Mounts'] = [{'Source': '/host'}]
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_has_binds(self):
        c = make_valid_container()
        c['HostConfig']['Binds'] = ['/host:/ctr']
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_added_capabilities(self):
        c = make_valid_container()
        c['HostConfig']['CapAdd'] = ['NET_ADMIN']
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_devices(self):
        c = make_valid_container()
        c['HostConfig']['Devices'] = [{'PathOnHost': '/dev/null'}]
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_device_requests(self):
        c = make_valid_container()
        c['HostConfig']['DeviceRequests'] = [{'Driver': 'nvidia'}]
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_device_cgroup_rules(self):
        c = make_valid_container()
        c['HostConfig']['DeviceCgroupRules'] = ['c *:* rm']
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_nonprivate_ipc(self):
        for mode in ['', 'shareable', 'host']:
            with self.subTest(mode=mode):
                c = make_valid_container()
                c['HostConfig']['IpcMode'] = mode
                with self.assertRaises(RuntimeError):
                    image_pin.validate_container(c)

    def test_rejects_host_uts_namespace(self):
        c = make_valid_container()
        c['HostConfig']['UTSMode'] = 'host'
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_network_settings_not_exactly_none(self):
        c = make_valid_container()
        c['NetworkSettings'] = {'Networks': {'bridge': {}}}
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)

    def test_rejects_network_settings_missing(self):
        c = make_valid_container()
        del c['NetworkSettings']
        with self.assertRaises(RuntimeError):
            image_pin.validate_container(c)


class TestEnsureFunction(unittest.TestCase):
    """Test ensure() container creation and management."""

    def test_rejects_non_root(self):
        with patch('os.geteuid', return_value=1000):
            with self.assertRaises(RuntimeError) as ctx:
                image_pin.ensure()
            self.assertIn('Root operator required', str(ctx.exception))

    def test_idempotent_running_container(self):
        """Existing valid running container: no mutation, returns created=False."""
        c = make_valid_container(running=True)
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[c, c]):
                with patch('image_pin.docker') as mock_docker:
                    result = image_pin.ensure()
                    self.assertEqual(result, {'created': False, 'started': False, 'imageRestored': False})
                    mock_docker.assert_not_called()

    def test_starts_stopped_owned_container(self):
        """Stopped owned container: starts it, returns started=True."""
        c_stopped = make_valid_container(running=False)
        c_running = make_valid_container(running=True)
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[c_stopped, c_running]):
                with patch('image_pin.docker') as mock_docker:
                    mock_docker.return_value = MagicMock(returncode=0, stdout=b'', stderr=b'')
                    result = image_pin.ensure()
                    self.assertEqual(result, {'created': False, 'started': True, 'imageRestored': False})
                    mock_docker.assert_any_call('start', image_pin.NAME)

    def test_start_failure_raises(self):
        c_stopped = make_valid_container(running=False)
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', return_value=c_stopped):
                with patch('image_pin.docker') as mock_docker:
                    mock_docker.return_value = MagicMock(returncode=1, stdout=b'', stderr=b'fail')
                    with self.assertRaises(RuntimeError) as ctx:
                        image_pin.ensure()
                    self.assertIn('Could not start owned retention container', str(ctx.exception))

    def test_container_disappears_raises(self):
        c_stopped = make_valid_container(running=False)
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[c_stopped, None]):
                with patch('image_pin.docker', return_value=MagicMock(returncode=0)):
                    with self.assertRaises(RuntimeError) as ctx:
                        image_pin.ensure()
                    self.assertIn('Retention container disappeared', str(ctx.exception))

    def test_restores_missing_image(self):
        """Missing image: loads from archive, creates container."""
        container = make_valid_container(running=True)
        # inspect sequence: container=None, image=None (restored=True), image=valid (after load), container=created, container=validated
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, None, {'Id': image_pin.IMAGE, 'Config': {}}, container, container]):
                with patch('image_pin.archive_bytes', return_value=b'archive_data') as mock_archive:
                    with patch('image_pin.docker') as mock_docker:
                        mock_docker.side_effect = [
                            MagicMock(returncode=0),  # load
                            MagicMock(returncode=0, stdout=b'cid'),  # run
                        ]
                        result = image_pin.ensure()
                        self.assertEqual(result, {'created': True, 'started': True, 'imageRestored': True})
                        mock_archive.assert_called_once()
                        mock_docker.assert_any_call('load', input=b'archive_data')

    def test_image_restore_failure_raises(self):
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, None]):
                with patch('image_pin.archive_bytes', return_value=b'data'):
                    with patch('image_pin.docker', return_value=MagicMock(returncode=1)):
                        with self.assertRaises(RuntimeError) as ctx:
                            image_pin.ensure()
                        self.assertIn('Verified image restore failed', str(ctx.exception))

    def test_rejects_wrong_image_id(self):
        wrong_image = {'Id': 'sha256:wrongid', 'Config': {}}
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, wrong_image]):
                with patch('image_pin.archive_bytes', return_value=b'data'):
                    with patch('image_pin.docker', return_value=MagicMock(returncode=0)):
                        with self.assertRaises(RuntimeError) as ctx:
                            image_pin.ensure()
                        self.assertIn('Pinned image identity', str(ctx.exception))

    def test_rejects_image_with_volumes(self):
        image_with_volumes = {'Id': image_pin.IMAGE, 'Config': {'Volumes': {'/data': {}}}}
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, image_with_volumes]):
                with patch('image_pin.archive_bytes', return_value=b'data'):
                    with patch('image_pin.docker', return_value=MagicMock(returncode=0)):
                        with self.assertRaises(RuntimeError) as ctx:
                            image_pin.ensure()
                        self.assertIn('volume declaration mismatch', str(ctx.exception))

    def test_creates_container_with_isolation_params(self):
        """Verify docker run is called with isolation parameters."""
        container = make_valid_container(running=True)
        valid_image = {'Id': image_pin.IMAGE, 'Config': {}}
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, valid_image, container, container]):
                with patch('image_pin.archive_bytes', return_value=b'data'):
                    with patch('image_pin.docker') as mock_docker:
                        mock_docker.side_effect = [
                            MagicMock(returncode=0),  # load
                            MagicMock(returncode=0, stdout=b'cid'),  # run
                        ]
                        image_pin.ensure()
                        # Find the run call
                        run_args = None
                        for call in mock_docker.call_args_list:
                            if call[0][0] == 'run':
                                run_args = call[0]
                                break
                        self.assertIsNotNone(run_args)
                        # Check key isolation flags
                        self.assertIn('--network=none', run_args)
                        self.assertIn('--ipc=private', run_args)
                        self.assertIn('--memory-swap=16m', run_args)
                        self.assertIn('--read-only', run_args)
                        self.assertIn('--cap-drop=ALL', run_args)
                        self.assertIn('--security-opt=no-new-privileges', run_args)
                        self.assertIn('--pids-limit=4', run_args)
                        self.assertIn('--memory=16m', run_args)
                        self.assertIn('--cpus=0.01', run_args)
                        self.assertIn('--pull=never', run_args)
                        self.assertIn(image_pin.NAME, run_args)

    def test_no_broad_cleanup_calls(self):
        """ensure() does not call prune, rm, rmi, or system commands."""
        c = make_valid_container(running=True)
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', return_value=c):
                with patch('image_pin.docker') as mock_docker:
                    mock_docker.return_value = MagicMock(returncode=0)
                    image_pin.ensure()
                    cleanup_cmds = {'prune', 'rm', 'rmi', 'system', 'stop'}
                    for call in mock_docker.call_args_list:
                        self.assertNotIn(call[0][0], cleanup_cmds)

    def test_reserved_name_mismatch_refused_no_mutation(self):
        """Container with reserved name but wrong image: refused without mutation."""
        bad_container = make_valid_container()
        bad_container['Image'] = 'sha256:wrongimage'
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', return_value=bad_container):
                with patch('image_pin.docker') as mock_docker:
                    with self.assertRaises(RuntimeError) as ctx:
                        image_pin.ensure()
                    self.assertIn('mismatch', str(ctx.exception))
                    mock_docker.assert_not_called()


class TestEnsureContainerCreationFailure(unittest.TestCase):
    """Test error handling when container creation fails."""

    def test_creation_fails_raises(self):
        # inspect sequence: container=None, image=None (restored=True), image=valid (after load), container=None (after failed run)
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, None, {'Id': image_pin.IMAGE, 'Config': {}}, None, None]):
                with patch('image_pin.archive_bytes', return_value=b'data'):
                    with patch('image_pin.docker') as mock_docker:
                        mock_docker.side_effect = [
                            MagicMock(returncode=0),  # load
                            MagicMock(returncode=1, stderr=b'fail'),  # run
                        ]
                        with self.assertRaises(RuntimeError) as ctx:
                            image_pin.ensure()
                        self.assertIn('Retention creation did not confirm success', str(ctx.exception))


class TestEnsureContainerNotRunningAfterStart(unittest.TestCase):
    """Test error when container is still not running after start attempt."""

    def test_not_running_after_start_raises(self):
        c_stopped = make_valid_container(running=False)
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[c_stopped, c_stopped]):
                with patch('image_pin.docker', return_value=MagicMock(returncode=0)):
                    with self.assertRaises(RuntimeError) as ctx:
                        image_pin.ensure()
                    self.assertIn('Retention container is not running', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
