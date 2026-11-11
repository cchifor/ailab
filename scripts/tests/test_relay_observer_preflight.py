"""The optional observer preflight emits an allowlisted, never-ready report."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'kubernetes/optional/relay-provenance-journal/observer-preflight.py'
NODE_UID = '11223344-5566-4788-99aa-bbccddeeff00'
POD_UID = '11223344-5566-4788-99aa-bbccddeeff01'


class ObserverPreflightTest(unittest.TestCase):
    def test_allowlist_and_unknown_host_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            kubectl = Path(directory) / 'kubectl'
            kubectl.write_text('''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[:2] == ['get', 'node']:
    value = {'kind':'Node','metadata':{'name':'worker-a','uid':'%s',
        'annotations':{'private':'DO-NOT-PRINT'}},
        'status':{'nodeInfo':{'containerRuntimeVersion':'containerd://2.1.0'}}}
elif args[:2] == ['get', 'pod']:
    value = {'kind':'Pod','metadata':{'name':'work-a','namespace':'research','uid':'%s'},
        'spec':{'nodeName':'worker-a','containers':[{'env':[{'value':'DO-NOT-PRINT'}]}]},
        'status':{'phase':'Running'}}
else:
    value = {'kind':'SelfSubjectReview','status':{'userInfo':{'username':'operator-a'}}}
print(json.dumps(value))
''' % (NODE_UID, POD_UID))
            kubectl.chmod(0o755)
            result = subprocess.run(['python3', '-B', str(SCRIPT), '--node', 'worker-a',
                                     '--namespace', 'research', '--pod', 'work-a',
                                     '--kubectl', str(kubectl)], capture_output=True, text=True,
                                    timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('DO-NOT-PRINT', result.stdout)
            report = json.loads(result.stdout)
            self.assertEqual(report['nodeUid'], NODE_UID)
            self.assertEqual(report['podUid'], POD_UID)
            self.assertTrue(report['podRunningOnNode'])
            self.assertEqual(report['criSocketPath'], 'unknown')
            self.assertEqual(report['checkpointFeatureAndRbac'], 'unknown')
            self.assertFalse(report['readyForObserver'])


if __name__ == '__main__':
    unittest.main()
