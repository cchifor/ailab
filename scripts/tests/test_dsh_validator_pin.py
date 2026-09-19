#!/usr/bin/env python3
"""Unit tests for image-pin.py using unittest.mock. No Docker/SSH/live cluster."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

# Load image_pin module using importlib with relative path resolution
BASE_DIR = Path(__file__).resolve().parents[2]
IMAGE_PIN_PATH = BASE_DIR / 'ansible' / 'roles' / 'dsh_validator_pin' / 'files' / 'image-pin.py'
ROLE_TASKS_PATH = BASE_DIR / 'ansible' / 'roles' / 'dsh_validator_pin' / 'tasks' / 'main.yml'
RUNNER_DEFAULTS_PATH = BASE_DIR / 'ansible' / 'roles' / 'gitea_runner' / 'defaults' / 'main.yml'
HOST_VARS_PATH = BASE_DIR / 'ansible' / 'host_vars' / 'ci-runner-1.yml'
GITEA_CLEANUP_PATH = BASE_DIR / 'ansible' / 'roles' / 'gitea_runner' / 'files' / 'gitea-runner-cleanup.sh'
GITHUB_RECLAIM_TEMPLATE = BASE_DIR / 'ansible' / 'roles' / 'github_runner' / 'templates' / 'runner-reclaim.sh.j2'
GITHUB_RECLAIM_DEFAULTS = BASE_DIR / 'ansible' / 'roles' / 'github_runner' / 'defaults' / 'main.yml'
PIN_UNIT_DIR = BASE_DIR / 'ansible' / 'roles' / 'dsh_validator_pin' / 'files'
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

    def test_archive_bytes_rejects_group_or_world_readable(self):
        """The contract is root-only 0600, not merely 'nobody else can write'."""
        for mode in (0o644, 0o640, 0o700):
            with self.subTest(mode=oct(mode)):
                mock_stat = MagicMock(st_mode=stat.S_IFREG | mode, st_uid=0, st_size=image_pin.ARCHIVE_SIZE)
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
                with patch('image_pin.archive_bytes', return_value=b'data') as mock_archive:
                    with patch('image_pin.docker') as mock_docker:
                        # the image is present, so the ONLY docker call on this path is `run`
                        mock_docker.side_effect = [MagicMock(returncode=0, stdout=b'cid')]
                        image_pin.ensure()
                        mock_archive.assert_not_called()
                        self.assertEqual([c[0][0] for c in mock_docker.call_args_list], ['run'])
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
                        # The flags whose absence would NOT fail any other test: the image's default
                        # user is not 65532, a container without a restart policy is gone after the
                        # first daemon restart, and a real entrypoint would run the validator.
                        for flag in ('--user=65532:65532', '--entrypoint=/bin/sleep', '--restart=unless-stopped',
                                     '--log-driver=none', '--detach', '--name', image_pin.IMAGE, '2147483647'):
                            self.assertIn(flag, run_args)
                        self.assertEqual(run_args.index('--name') + 1, run_args.index(image_pin.NAME))

    def test_no_broad_cleanup_calls(self):
        """No path of ensure() issues anything but inspect/start/load/run — checked on EVERY
        argument of EVERY call, across the running, stopped and create-with-restore paths, so a
        `docker image prune` or `docker rm` slipped in anywhere would fail here."""
        running = make_valid_container(running=True)
        stopped = make_valid_container(running=False)
        valid_image = {'Id': image_pin.IMAGE, 'Config': {}}
        # (inspect answers, docker answers, the docker verbs the path is expected to issue)
        paths = {
            'running': ([running, running], [], []),
            'stopped': ([stopped, running], [MagicMock(returncode=0)], ['start']),
            'restore': ([None, None, valid_image, running, running],
                        [MagicMock(returncode=0), MagicMock(returncode=0, stdout=b'cid')], ['load', 'run']),
        }
        forbidden = {'prune', 'rm', 'rmi', 'system', 'stop', 'kill', 'volume', 'network', 'builder', 'image', 'container'}
        allowed_first = {'inspect', 'start', 'load', 'run'}
        for name, (inspects, dockers, expected) in paths.items():
            with self.subTest(path=name):
                with patch('os.geteuid', return_value=0):
                    with patch('image_pin.inspect', side_effect=inspects):
                        with patch('image_pin.archive_bytes', return_value=b'data'):
                            with patch('image_pin.docker', side_effect=dockers) as mock_docker:
                                image_pin.ensure()
                                self.assertEqual([c[0][0] for c in mock_docker.call_args_list], expected)
                                for call in mock_docker.call_args_list:
                                    self.assertIn(call[0][0], allowed_first, call)
                                    self.assertFalse(forbidden & set(call[0]), call)

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
    """Error handling when container creation fails — and the ONE failure that is retried."""

    def test_creation_fails_with_image_present_raises_without_retry(self):
        """`docker run` fails while the image is still there (a name taken meanwhile): refused at
        once, no second run, no restore."""
        valid_image = {'Id': image_pin.IMAGE, 'Config': {}}
        # inspect: container=None, image=valid, image=valid (the post-failure check)
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, valid_image, valid_image]) as mock_inspect:
                with patch('image_pin.archive_bytes', return_value=b'data') as mock_archive:
                    with patch('image_pin.docker', return_value=MagicMock(returncode=1, stderr=b'fail')) as mock_docker:
                        with self.assertRaises(RuntimeError) as ctx:
                            image_pin.ensure()
                        self.assertIn('Retention creation did not confirm success', str(ctx.exception))
                        self.assertEqual([c[0][0] for c in mock_docker.call_args_list], ['run'])
                        mock_archive.assert_not_called()
                        self.assertEqual(mock_inspect.call_count, 3)

    def test_creation_fails_because_image_was_pruned_restores_and_retries_once(self):
        """The cleanup's `image prune -af` removed the still-unreferenced image between the
        inspection and the run: restore from the archive and run again, exactly once."""
        container = make_valid_container(running=True)
        valid_image = {'Id': image_pin.IMAGE, 'Config': {}}
        # inspect: container=None, image=valid, image=None (gone after the failed run),
        #          image=None (attempt 2 -> restore), image=valid (after load), container, container
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, valid_image, None, None, valid_image, container, container]):
                with patch('image_pin.archive_bytes', return_value=b'archive_data') as mock_archive:
                    with patch('image_pin.docker') as mock_docker:
                        mock_docker.side_effect = [
                            MagicMock(returncode=1, stderr=b'No such image'),  # run (attempt 1)
                            MagicMock(returncode=0),                          # load
                            MagicMock(returncode=0, stdout=b'cid'),           # run (attempt 2)
                        ]
                        result = image_pin.ensure()
                        self.assertEqual(result, {'created': True, 'started': True, 'imageRestored': True})
                        mock_archive.assert_called_once()
                        self.assertEqual([c[0][0] for c in mock_docker.call_args_list], ['run', 'load', 'run'])

    def test_creation_fails_twice_raises(self):
        """A second failed run is not retried again, whatever inspect says about the image."""
        valid_image = {'Id': image_pin.IMAGE, 'Config': {}}
        with patch('os.geteuid', return_value=0):
            with patch('image_pin.inspect', side_effect=[None, valid_image, None, None, valid_image, None]):
                with patch('image_pin.archive_bytes', return_value=b'archive_data'):
                    with patch('image_pin.docker') as mock_docker:
                        mock_docker.side_effect = [
                            MagicMock(returncode=1, stderr=b'fail'),  # run (attempt 1)
                            MagicMock(returncode=0),                  # load
                            MagicMock(returncode=1, stderr=b'fail'),  # run (attempt 2)
                        ]
                        with self.assertRaises(RuntimeError) as ctx:
                            image_pin.ensure()
                        self.assertIn('Retention creation did not confirm success', str(ctx.exception))
                        self.assertEqual([c[0][0] for c in mock_docker.call_args_list], ['run', 'load', 'run'])


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


class TestCleanupExclusionDrift(unittest.TestCase):
    """host_vars/ci-runner-1.yml pins a LITERAL copy of the gitea_runner role default plus the pin's
    own alternative. That is deliberate (section M of test-cleanup.sh reads role defaults as literal
    scalars, so the default cannot be a Jinja composition), and it means a later change to the
    fleet-wide default would silently never reach ci-runner-1 — unless this test fails."""

    PIN_ALTERNATIVE = '| /dsh-conductor-image-pin$'

    def test_host_var_extends_the_current_role_default(self):
        default = yaml.safe_load(RUNNER_DEFAULTS_PATH.read_text(encoding='utf-8'))['gitea_runner_cleanup_infra_exclude_re']
        host = yaml.safe_load(HOST_VARS_PATH.read_text(encoding='utf-8'))['gitea_runner_cleanup_infra_exclude_re']
        self.assertEqual(host, default + self.PIN_ALTERNATIVE,
                         'the gitea_runner default changed: update ansible/host_vars/ci-runner-1.yml to match')

    def test_role_assert_pins_the_same_literal(self):
        host = yaml.safe_load(HOST_VARS_PATH.read_text(encoding='utf-8'))['gitea_runner_cleanup_infra_exclude_re']
        tasks = yaml.safe_load(ROLE_TASKS_PATH.read_text(encoding='utf-8'))
        asserts = [cond for task in tasks for cond in (task.get('ansible.builtin.assert') or {}).get('that', [])]
        self.assertIn("gitea_runner_cleanup_infra_exclude_re == '%s'" % host, asserts)

    def test_pin_alternative_matches_only_the_exact_container_name(self):
        """Both reclaim loops match `{{.Config.Image}} {{.Name}}` case-insensitively; the anchored,
        space-prefixed name must hit the pin and nothing that merely shares the prefix."""
        host_vars = yaml.safe_load(HOST_VARS_PATH.read_text(encoding='utf-8'))
        for var in ('gitea_runner_cleanup_infra_exclude_re', 'github_runner_reclaim_keep_re'):
            with self.subTest(var=var):
                pattern = re.compile(host_vars[var], re.IGNORECASE)
                self.assertTrue(pattern.search(image_pin.IMAGE + ' /' + image_pin.NAME))
                self.assertFalse(pattern.search(image_pin.IMAGE + ' /dsh-validation-1234'))
                self.assertFalse(pattern.search('node:24 /' + image_pin.NAME + '-old'))

    def test_github_reclaim_keeps_the_same_container_and_nothing_fleet_wide(self):
        host_vars = yaml.safe_load(HOST_VARS_PATH.read_text(encoding='utf-8'))
        self.assertEqual(host_vars['github_runner_reclaim_keep_re'], self.PIN_ALTERNATIVE.lstrip('|'))
        defaults = yaml.safe_load(GITHUB_RECLAIM_DEFAULTS.read_text(encoding='utf-8'))
        self.assertEqual(defaults['github_runner_reclaim_keep_re'], '', 'the keep-list must stay a per-host opt-in')

    def test_both_reclaim_loops_match_against_the_same_metadata_shape(self):
        """The exemption regexes are written for `<image> <name>`; if either script changes the
        inspect format the anchored name stops matching and the pin is silently reaped again."""
        meta_format = "'{{.Config.Image}} {{.Name}}'"
        self.assertIn(meta_format, GITEA_CLEANUP_PATH.read_text(encoding='utf-8'))
        rendered = GITHUB_RECLAIM_TEMPLATE.read_text(encoding='utf-8').replace('{% raw %}', '').replace('{% endraw %}', '')
        self.assertIn(meta_format, rendered)

    def test_pin_service_is_a_plain_oneshot_with_a_timer(self):
        """RemainAfterExit would leave the unit 'active (exited)' and a timer cannot re-run an
        active unit — the whole self-heal would be a no-op. The timer must exist and be installed."""
        service = (PIN_UNIT_DIR / 'dsh-conductor-image-pin.service').read_text(encoding='utf-8')
        self.assertIn('Type=oneshot', service)
        self.assertNotIn('RemainAfterExit', service)
        timer = (PIN_UNIT_DIR / 'dsh-conductor-image-pin.timer').read_text(encoding='utf-8')
        self.assertIn('OnUnitInactiveSec=', timer)
        self.assertIn('Unit=dsh-conductor-image-pin.service', timer)
        tasks = yaml.safe_load(ROLE_TASKS_PATH.read_text(encoding='utf-8'))
        systemd = [t.get('ansible.builtin.systemd_service') for t in tasks if t.get('ansible.builtin.systemd_service')]
        self.assertIn({'name': 'dsh-conductor-image-pin.timer', 'enabled': True, 'state': 'started'}, systemd)
        looped = [t.get('loop', []) for t in tasks if t.get('ansible.builtin.copy')]
        self.assertTrue(any('dsh-conductor-image-pin.timer' in items for items in looped), looped)

    def test_env_guard_counts_every_shell_assignment_form(self):
        """The rewrite replaces the LAST line matching the key while the value check sees only the
        canonical double-quoted form; the counter must therefore see every form the sourced shell
        file would honour - tab- or space-indented, `export`-prefixed with either whitespace,
        single-quoted - or a duplicate slips past both assertions and overrides the exemption."""
        text = ROLE_TASKS_PATH.read_text(encoding='utf-8')
        patterns = re.findall(r"regex_findall\('((?:[^'\\]|\\.)*)'\)", text)
        self.assertEqual(len(patterns), 2, patterns)
        counter, canonical = (p.encode().decode('unicode_escape') for p in patterns)
        base = 'GITEA_CLEANUP_INFRA_EXCLUDE_RE="buildkit|buildx"\n'
        self.assertEqual(len(re.findall(counter, base)), 1)
        self.assertEqual(re.findall(canonical, base), ['buildkit|buildx'])
        for dup in ("\tGITEA_CLEANUP_INFRA_EXCLUDE_RE='custom'\n", "  GITEA_CLEANUP_INFRA_EXCLUDE_RE='custom'\n",
                    "export GITEA_CLEANUP_INFRA_EXCLUDE_RE='custom'\n", "export\tGITEA_CLEANUP_INFRA_EXCLUDE_RE='custom'\n",
                    "\texport \tGITEA_CLEANUP_INFRA_EXCLUDE_RE=custom\n"):
            with self.subTest(dup=dup):
                self.assertEqual(len(re.findall(counter, base + '# comment\n' + dup)), 2, 'a second assignment went uncounted')
        # a comment mentioning the key is not an assignment
        self.assertEqual(len(re.findall(counter, base + '# GITEA_CLEANUP_INFRA_EXCLUDE_RE=old\n')), 1)

    def test_pin_play_refuses_until_the_github_reclaim_keeps_the_pin(self):
        tasks = yaml.safe_load(ROLE_TASKS_PATH.read_text(encoding='utf-8'))
        slurps = [t['ansible.builtin.slurp']['src'] for t in tasks if t.get('ansible.builtin.slurp')]
        self.assertIn('/usr/local/bin/runner-reclaim.sh', slurps)
        asserts = [cond for t in tasks for cond in (t.get('ansible.builtin.assert') or {}).get('that', [])]
        self.assertTrue(any('KEEP_RE=' in c and 'github_runner_reclaim_keep_re' in c for c in asserts), asserts)
        # and the assert comes before anything is installed
        first_assert = next(i for i, t in enumerate(tasks) if 'KEEP_RE=' in str((t.get('ansible.builtin.assert') or {}).get('that', '')))
        first_copy = next(i for i, t in enumerate(tasks) if t.get('ansible.builtin.copy'))
        self.assertLess(first_assert, first_copy)

    def test_exemption_task_runs_after_the_unit_validated_the_container(self):
        """A foreign container squatting on the reserved name must fail the unit BEFORE the
        cleanup exemption is written, or the refusal hands the squatter a permanent exemption."""
        tasks = yaml.safe_load(ROLE_TASKS_PATH.read_text(encoding='utf-8'))
        names = [t['name'] for t in tasks]
        unit = next(i for i, t in enumerate(tasks) if (t.get('ansible.builtin.systemd_service') or {}).get('name') == 'dsh-conductor-image-pin.service')
        exemption = next(i for i, t in enumerate(tasks) if t.get('ansible.builtin.lineinfile'))
        self.assertLess(unit, exemption, names)


@unittest.skipUnless(sys.platform != 'win32' and shutil.which('bash') and shutil.which('timeout'),
                     'needs a POSIX bash + coreutils timeout (the rest of this suite is POSIX-only too)')
class TestGithubReclaimKeepList(unittest.TestCase):
    """runner-reclaim.sh (github_runner role) `docker rm -f`s every container at every boot and every
    ephemeral cycle of the co-located GitHub agent. Only its docker block is exercised: the script's
    tail writes a live node-exporter beacon and chowns the runner home, neither of which a unit test
    may touch on a CI runner. The block is everything up to the first top-level `fi`."""

    IDS = ['aaa111', 'bbb222', 'ccc333']
    META = {
        'aaa111': 'node:24 /platform-e2e-web-1',
        'bbb222': image_pin.IMAGE + ' /' + image_pin.NAME,
        'ccc333': 'moby/buildkit:buildx-stable-1 /buildx_buildkit_builder0',
    }

    def render_docker_block(self, keep_re, home):
        text = GITHUB_RECLAIM_TEMPLATE.read_text(encoding='utf-8')
        values = {'github_runner_dir': home + '/actions-runner', 'github_runner_home': home,
                  'github_runner_reclaim_keep_re': keep_re}
        text = text.replace('{% raw %}', '').replace('{% endraw %}', '')
        text = re.sub(r'\{\{\s*(\w+)\s*\}\}', lambda m: values.get(m.group(1), m.group(0)), text)
        lines = text.splitlines()
        end = next(i for i, ln in enumerate(lines) if ln == 'fi')
        block = '\n'.join(lines[:end + 1]) + '\nexit 0\n'
        self.assertIn('docker rm -f', block, 'the docker block moved; teach this test where it went')
        # Go templates ({{.Config.Image}}) legitimately survive; an ansible variable ({{ name }}) must not.
        self.assertIsNone(re.search(r'\{\{\s*\w+\s*\}\}', block), 'an unrendered template variable survived')
        return block

    def run_block(self, keep_re, failing_inspect=()):
        with tempfile.TemporaryDirectory() as tmp:
            binp = Path(tmp) / 'bin'; binp.mkdir()
            log = Path(tmp) / 'docker.log'
            meta_case = '\n'.join('    %s) %s ;;' % (cid, 'exit 1' if cid in failing_inspect else 'printf %%s "%s"' % m)
                                  for cid, m in self.META.items())
            fake = ('#!/usr/bin/env bash\n'
                    'printf "%s\\n" "$*" >> "' + str(log).replace('\\', '/') + '"\n'
                    'case "$1 $2" in\n'
                    '  "ps -aq") printf "%s\\n" ' + ' '.join(self.IDS) + ' ;;\n'
                    '  "inspect -f") case "$4" in\n' + meta_case + '\n    *) exit 1 ;;\n  esac ;;\n'
                    'esac\nexit 0\n')
            (binp / 'docker').write_text(fake, encoding='utf-8')
            os.chmod(binp / 'docker', 0o755)
            script = Path(tmp) / 'reclaim.sh'
            script.write_text(self.render_docker_block(keep_re, tmp), encoding='utf-8')
            env = dict(os.environ, PATH=str(binp) + os.pathsep + os.environ.get('PATH', ''))
            proc = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return log.read_text(encoding='utf-8').splitlines() if log.exists() else []

    def test_pin_survives_and_everything_else_is_still_removed(self):
        calls = self.run_block(' /dsh-conductor-image-pin$')
        rm = [c for c in calls if c.startswith('rm -f ')]
        self.assertEqual(len(rm), 1, calls)
        removed = set(rm[0].split()[2:])
        self.assertEqual(removed, {'aaa111', 'ccc333'}, calls)

    def test_inspect_failure_keeps_rather_than_removes(self):
        """A failed inspect yields no metadata to match against; that container is kept this cycle,
        never defaulted into the rm -f list (the pin's whole protection would hinge on one inspect)."""
        calls = self.run_block(' /dsh-conductor-image-pin$', failing_inspect=('aaa111',))
        rm = [c for c in calls if c.startswith('rm -f ')]
        self.assertEqual(len(rm), 1, calls)
        self.assertEqual(set(rm[0].split()[2:]), {'ccc333'}, calls)

    def test_empty_keep_list_is_the_unchanged_ephemeral_contract(self):
        calls = self.run_block('')
        rm = [c for c in calls if c.startswith('rm -f ')]
        self.assertEqual(len(rm), 1, calls)
        self.assertEqual(set(rm[0].split()[2:]), set(self.IDS), calls)
        self.assertFalse([c for c in calls if c.startswith('inspect ')], 'no per-container inspect when nothing is kept')


if __name__ == '__main__':
    unittest.main()
