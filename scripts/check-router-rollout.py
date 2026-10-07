#!/usr/bin/env python3
"""Read-only CI acceptance using the estate's existing LAN monitoring APIs.

Deployment is CI review/merge -> GitHub mirror -> Flux. No kubeconfig or router
credential is used here; the one-shot GitOps Job keeps its credential in-cluster.
"""
import argparse
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROM = 'http://192.168.0.41:30090'


class Pending(Exception):
    pass


def expectation(root=ROOT):
    folder = root / 'kubernetes/apps/apps/llm-router'
    manifest = (folder / 'router.yaml').read_text()
    images = set(re.findall(r'image: (registry\.chifor\.me/llm-router/router@sha256:[a-f0-9]{64})', manifest))
    if len(images) != 1:
        raise ValueError('Expected one consistent, digest-pinned router image')
    init = manifest.split('      initContainers:\n', 1)
    names = re.findall(r'        - name: ([-a-z0-9]+)', init[1].split('      containers:\n')[0]) if len(init) == 2 else []
    canary = folder / 'shared-routing-canary.yaml'
    job = re.search(r'\n  name: (router-shared-routing-[-a-z0-9]+)\n', canary.read_text()).group(1) if canary.exists() else None
    if job and not re.search(r'image: ' + re.escape(next(iter(images))) + r'\s', canary.read_text()):
        raise ValueError('Canary must use the same pinned release as the router')
    return images.pop(), names, job


def query(expr):
    # Prometheus instant responses otherwise return the evaluation time even
    # when the underlying sample predates a rollout. Reject old scrape samples.
    fresh = f'({expr}) and (time() - timestamp({expr}) < 90)'
    url = PROM + '/api/v1/query?' + urllib.parse.urlencode({'query': fresh})
    with urllib.request.urlopen(url, timeout=10) as response:
        body = json.load(response)
    if body.get('status') != 'success':
        raise RuntimeError('Monitoring query failed')
    return body['data']['result']


def check(query_fn, image, init_names, job):
    namespace = 'namespace="llm-router"'
    infos = query_fn(f'kube_pod_container_info{{{namespace},container="router"}}')
    ready = {r['metric']['pod'] for r in query_fn(f'kube_pod_status_ready{{{namespace},condition="true"}}') if float(r['value'][1]) == 1}
    serving = [r['metric'] for r in infos if r['metric']['pod'] in ready]
    if len(serving) != 1 or serving[0].get('image_id') != image:
        raise Pending('Waiting for one Ready router pod on the expected image')
    pod = serving[0]['pod']
    if init_names:
        done = {r['metric']['container'] for r in query_fn(f'kube_pod_init_container_status_terminated_reason{{{namespace},pod={json.dumps(pod)},reason="Completed"}}') if float(r['value'][1]) == 1}
        if not set(init_names) <= done:
            raise Pending('Waiting for successful backup/migration init checks')
    if job:
        selector = f'{namespace},job_name={json.dumps(job)}'
        if any(float(r['value'][1]) > 0 for r in query_fn(f'kube_job_status_failed{{{selector}}}')):
            raise RuntimeError('Live canary failed; inspect its narrowly scoped Loki logs')
        if not any(float(r['value'][1]) == 1 for r in query_fn(f'kube_job_status_succeeded{{{selector}}}')):
            raise Pending('Waiting for the one-shot Claude/Codex canary')
    return {'image': image, 'pod': pod, 'completedInitChecks': init_names, 'completedCanaryJob': job}


def public_check():
    for path, expected in [('health', 'ok'), ('ready', 'ready')]:
        req = urllib.request.Request('https://router.chifor.me/' + path, headers={'User-Agent': 'curl/8.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            if json.load(response).get('status') != expected:
                raise Pending('Public router is not ready')
    try:
        req = urllib.request.Request('https://router.chifor.me/admin/v1/config', headers={'User-Agent': 'curl/8.0'})
        with urllib.request.urlopen(req, timeout=10):
            raise RuntimeError('Unauthenticated management request was accepted')
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise Pending('Expected unauthenticated management refusal') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=1200)
    args = parser.parse_args()
    image, init_names, job = expectation()
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            result = check(query, image, init_names, job)
            public_check()
            print(json.dumps({'status': 'passed', **result}), flush=True)
            return
        except (Pending, urllib.error.URLError, TimeoutError) as error:
            if time.monotonic() >= deadline:
                raise SystemExit('Rollout acceptance timed out: ' + str(error))
            print(str(error), flush=True)
            time.sleep(20)


if __name__ == '__main__':
    main()
