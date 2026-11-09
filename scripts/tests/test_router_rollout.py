import importlib.util
import pathlib
import unittest
from unittest.mock import patch
import urllib.error
import io
import tempfile

SPEC = importlib.util.spec_from_file_location('router_rollout', pathlib.Path(__file__).parents[1] / 'check-router-rollout.py')
rollout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollout)
IMAGE = 'registry.chifor.me/llm-router/router@sha256:' + 'a' * 64
POD = 'router-fixture'


def row(value=1, **labels):
    return {'metric': labels, 'value': [123, str(value)]}


class RolloutTest(unittest.TestCase):
    def setUp(self):
        self.data = {
            'kube_pod_container_info': [row(pod=POD, image_id=IMAGE)],
            'kube_pod_status_ready': [row(pod=POD)],
            'kube_pod_init_container_info': [row(container='backup'), row(container='migration')],
            'kube_pod_init_container_status_terminated_reason': [row(container='backup'), row(container='migration')],
            'kube_job_status_failed': [row(0)],
            'kube_job_failed': [row(0)],
            'kube_job_status_succeeded': [row()],
        }

    def check(self, init=('backup', 'migration'), job='canary'):
        return rollout.check(lambda expr: self.data.get(expr.split('{')[0], []), IMAGE, list(init), job)

    def test_expected_release_and_successful_checks(self):
        self.assertEqual(self.check()['completedCanaryJob'], 'canary')

    def test_old_healthy_release_does_not_pass(self):
        self.data['kube_pod_container_info'][0]['metric']['image_id'] = 'old-image'
        with self.assertRaises(rollout.Pending): self.check()

    def test_missing_or_not_ready_does_not_pass(self):
        self.data['kube_pod_status_ready'] = []
        with self.assertRaises(rollout.Pending): self.check()
        self.data['kube_pod_status_ready'] = [row(0, pod=POD)]
        with self.assertRaises(rollout.Pending): self.check()

    def test_multiple_ready_replicas_do_not_pass(self):
        self.data['kube_pod_container_info'].append(row(pod='other', image_id=IMAGE))
        self.data['kube_pod_status_ready'].append(row(pod='other'))
        with self.assertRaises(rollout.Pending): self.check()

    def test_missing_init_or_canary_does_not_pass(self):
        self.data['kube_pod_init_container_status_terminated_reason'].pop()
        with self.assertRaises(rollout.Pending): self.check()
        self.data['kube_pod_init_container_info'] = []
        self.data['kube_job_status_succeeded'] = []
        with self.assertRaises(rollout.Pending): self.check(init=())

    def test_failed_canary_is_terminal_even_if_success_is_present(self):
        self.data['kube_job_status_failed'] = [row()]
        with self.assertRaisesRegex(RuntimeError, 'canary failed'): self.check()

    def test_deadline_failure_with_no_failed_pods_is_terminal(self):
        self.data['kube_job_failed'] = [row(condition='true')]
        self.data['kube_job_status_succeeded'] = []
        with self.assertRaisesRegex(RuntimeError, 'canary failed'): self.check()

    def test_cleanup_rollout_checks_image_without_requiring_removed_resources(self):
        self.data['kube_pod_init_container_info'] = []
        self.assertIsNone(self.check(init=(), job=None)['completedCanaryJob'])

    def test_cleanup_waits_for_old_ready_pod_with_same_image_to_be_replaced(self):
        with self.assertRaises(rollout.Pending): self.check(init=(), job=None)

    def test_checked_in_expectation(self):
        image, names, job = rollout.expectation()
        self.assertIn('@sha256:', image)
        self.assertEqual(len(image.rsplit(':', 1)[1]), 64)

    def test_canary_expectation_requires_one_job_on_the_same_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / 'kubernetes/apps/apps/llm-router'
            folder.mkdir(parents=True)
            (folder / 'router.yaml').write_text('image: ' + IMAGE + '\n')
            canary = folder / 'plugin-settings-canary.yaml'
            canary.write_text('metadata:\n  name: router-plugin-settings-fixture\nimage: ' + IMAGE + '\n')
            self.assertEqual(rollout.expectation(root)[2], 'router-plugin-settings-fixture')
            canary.write_text('metadata:\n  name: router-plugin-settings-fixture\nimage: old\n')
            with self.assertRaisesRegex(ValueError, 'same pinned release'): rollout.expectation(root)
            canary.write_text('image: ' + IMAGE + '\n')
            with self.assertRaisesRegex(ValueError, 'declare'): rollout.expectation(root)
            (folder / 'other-canary.yaml').write_text('')
            with self.assertRaisesRegex(ValueError, 'at most one'): rollout.expectation(root)

    def test_init_expectation_excludes_nested_names_and_later_volumes(self):
        init = '''      initContainers:
        - name: backup-before-worker-budgets
          image: IMAGE
          env:
            - name: ROUTER_DATA_DIR
              value: /data
          volumeMounts:
            - name: data
              mountPath: /data
            - name: tmp
              mountPath: /tmp
'''.replace('IMAGE', IMAGE)
        containers = '      containers:\n        - name: router\n          image: ' + IMAGE + '\n'
        volumes = '      volumes:\n        - name: data\n          emptyDir: {}\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / 'kubernetes/apps/apps/llm-router'
            folder.mkdir(parents=True)
            for blocks in (init + containers + volumes, containers + init + volumes):
                with self.subTest(init_before_containers=blocks.startswith(init)):
                    (folder / 'router.yaml').write_text(blocks)
                    self.assertEqual(rollout.expectation(root), (IMAGE, ['backup-before-worker-budgets'], None))

    def test_unpinned_init_image_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / 'kubernetes/apps/apps/llm-router'
            folder.mkdir(parents=True)
            (folder / 'router.yaml').write_text('image: ' + IMAGE + '\nimage: VERIFIED_ROUTER_IMAGE_AT_SHA256\n')
            with self.assertRaisesRegex(ValueError, 'digest-pinned router image'): rollout.expectation(root)

    def test_commented_init_images_are_validated_with_horizontal_whitespace(self):
        other = 'registry.chifor.me/llm-router/router@sha256:' + 'b' * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / 'kubernetes/apps/apps/llm-router'
            folder.mkdir(parents=True)
            for value in (IMAGE + ' # verified', '"' + IMAGE + '"\t# verified', "'" + IMAGE + "' # verified"):
                with self.subTest(accepted=value):
                    (folder / 'router.yaml').write_text('image: ' + IMAGE + '\nimage\t: \t' + value + '\t\n')
                    self.assertEqual(rollout.expectation(root), (IMAGE, [], None))
            for value in ('old:latest # stale init', other + ' # mismatched init', ' # no value', '\n  ' + IMAGE):
                with self.subTest(rejected=value):
                    (folder / 'router.yaml').write_text('image: ' + IMAGE + '\nimage\t: \t' + value + '\n')
                    with self.assertRaisesRegex(ValueError, 'digest-pinned router image'): rollout.expectation(root)

    def test_quoted_image_keys_and_list_entries_cannot_hide_a_bad_image(self):
        for key in ('"image"', "'image'", '- image'):
            with self.subTest(key=key):
                self.assertEqual(rollout.pinned_images(key + ': ' + IMAGE + ' # valid'), {IMAGE})
                with self.assertRaisesRegex(ValueError, 'digest-pinned router image'):
                    rollout.pinned_images('image: ' + IMAGE + '\n' + key + ': latest # invalid')

    def test_canary_cannot_hide_commented_unpinned_or_mismatched_image(self):
        other = 'registry.chifor.me/llm-router/router@sha256:' + 'b' * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / 'kubernetes/apps/apps/llm-router'
            folder.mkdir(parents=True)
            (folder / 'router.yaml').write_text('image: ' + IMAGE + '\n')
            canary = folder / 'fixture-canary.yaml'
            prefix = 'metadata:\n  name: router-fixture\nimage: ' + IMAGE + ' # application\n'
            canary.write_text(prefix)
            self.assertEqual(rollout.expectation(root)[2], 'router-fixture')
            for value in ('latest', other):
                with self.subTest(value=value):
                    canary.write_text(prefix + 'image: ' + value + ' # init\n')
                    with self.assertRaisesRegex(ValueError, 'same pinned release'): rollout.expectation(root)

    def test_public_check_requires_authentication_refusal(self):
        for refused in (True, False):
            def fetch(request, **kwargs):
                if request.full_url.endswith('/admin/v1/config'):
                    if refused: raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, None)
                    return io.BytesIO(b'{}')
                return io.BytesIO(b'{"status":"ready"}' if request.full_url.endswith('/ready') else b'{"status":"ok"}')
            with patch.object(rollout.urllib.request, 'urlopen', side_effect=fetch):
                if refused: rollout.public_check()
                else:
                    with self.assertRaises(RuntimeError): rollout.public_check()


if __name__ == '__main__': unittest.main()
