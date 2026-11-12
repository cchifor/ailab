#!/usr/bin/env python3
"""Prepare a pinned, inactive VM maintenance render input from one Relay commit."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

RENDER = Path(__file__).resolve().parents[1] / 'kubernetes/optional/relay-provenance-journal/render.py'
SPEC = importlib.util.spec_from_file_location('relay_provenance_renderer', RENDER)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)
FILES = {
    'schema008Repair': '008-authority-completion.sql',
    'vmSchema011': '011-kubevirt-original-registration.sql',
    'vmSchema012': '012-kubevirt-connector.sql',
    'vmSchema013': '013-kubevirt-lease-renewal.sql',
    'vmSchema014': '014-vm-backing-effect-lookups.sql',
}
PREFIX = 'deploy/worker-provenance-journal/'


def prepare(repo, commit, output, expected=None, git=None):
    expected = expected or renderer.REVIEWED_SCHEMA_SHA256
    git = git or (lambda *args: subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL))
    repo = Path(repo)
    output = Path(output)
    if not repo.is_absolute() or repo.resolve() != repo or not repo.is_dir() or not output.is_absolute() or output.parent.resolve() != output.parent:
        raise ValueError('path-invalid')
    if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit) or commit == '0' * 40:
        raise ValueError('commit-invalid')
    if git('rev-parse', 'HEAD').decode().strip() != commit:
        raise ValueError('checkout-commit-mismatch')
    config = {'version': 1, 'relaySourceCommit': commit, 'fromVersion': 11}
    for key, basename in FILES.items():
        path = repo / PREFIX / basename
        digest = expected[key]
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('source-file-mismatch')
        if git('show', commit + ':' + PREFIX + basename) != path.read_bytes():
            raise ValueError('commit-file-mismatch')
        renderer.pinned({'path': str(path), 'sha256': digest}, 262144)
        config[key] = {'path': str(path), 'sha256': digest}
    raw = (json.dumps(config, separators=(',', ':')) + '\n').encode()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
    finally:
        os.close(fd)
    return hashlib.sha256(raw).hexdigest()


if __name__ == '__main__':
    try:
        if len(sys.argv) != 4:
            raise ValueError('usage-invalid')
        print(prepare(*sys.argv[1:]))
    except (ValueError, OSError, subprocess.CalledProcessError, renderer.Refusal):
        print('vm-upgrade-input-refused', file=sys.stderr)
        sys.exit(78)
