import importlib.util
import pathlib
import unittest
from unittest.mock import patch
import urllib.error
import io

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
            'kube_pod_init_container_status_terminated_reason': [row(container='backup'), row(container='migration')],
            'kube_job_status_failed': [row(0)],
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
        self.data['kube_job_status_succeeded'] = []
        with self.assertRaises(rollout.Pending): self.check(init=())

    def test_failed_canary_is_terminal_even_if_success_is_present(self):
        self.data['kube_job_status_failed'] = [row()]
        with self.assertRaisesRegex(RuntimeError, 'canary failed'): self.check()

    def test_cleanup_rollout_checks_image_without_requiring_removed_resources(self):
        self.assertIsNone(self.check(init=(), job=None)['completedCanaryJob'])

    def test_checked_in_expectation(self):
        image, names, job = rollout.expectation()
        self.assertIn('@sha256:', image)
        self.assertEqual(len(image.rsplit(':', 1)[1]), 64)

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
