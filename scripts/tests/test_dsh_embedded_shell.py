#!/usr/bin/env python3
"""Syntax-check every shell script embedded in the dsh manifests.

WHY THIS EXISTS. The dsh Deployment and install Job carry substantial /bin/sh
programs inside YAML `args`. `kustomize build` and `kubeconform` both validate
the YAML around them and neither looks inside the string, so a shell syntax
error renders, lints, deploys -- and then aborts an initContainer. On a
1-replica Recreate Deployment that is an outage.

This is not hypothetical: a comment edit once replaced text inside a quoted
`echo` with a replacement containing a newline, leaving the string unterminated:

    echo "WARNING: ... (dsh-tool-subagent registers
    echo "WARNING: it only while its provider exists), NOT listed as broken."

which dash rejects with `Syntax error: ")" unexpected`. Every repo gate passed;
only running the script found it.

`sh -n` parses without executing, so this is safe and fast. It needs a real
/bin/sh, which the CI runner has.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DSH = ROOT / "kubernetes" / "apps" / "apps" / "dsh"

# Containers whose command is a shell wrapping an inline program. The pair is
# (file, kind) so a failure names where to look.
MANIFESTS = ["deployment.yaml", "install-job.yaml", "searxng.yaml"]


def _load(path):
    try:
        import yaml
    except ImportError:                                    # pragma: no cover
        raise unittest.SkipTest("PyYAML unavailable")
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _pod_specs(doc):
    kind = doc.get("kind")
    if kind in ("Deployment", "Job", "StatefulSet", "DaemonSet"):
        yield doc["spec"]["template"]["spec"]
    elif kind == "Pod":
        yield doc["spec"]


def shell_programs():
    """(label, script) for every inline shell program in the dsh manifests."""
    out = []
    for name in MANIFESTS:
        path = DSH / name
        if not path.exists():
            continue
        for doc in _load(path):
            for spec in _pod_specs(doc):
                for group in ("initContainers", "containers"):
                    for c in spec.get(group) or []:
                        cmd = c.get("command") or []
                        args = c.get("args") or []
                        # The shape used throughout: command: [sh, -c] + args: [<program>]
                        if not any(str(x).endswith("sh") for x in cmd[:1]):
                            continue
                        if "-c" not in [str(x) for x in cmd]:
                            continue
                        for i, a in enumerate(args):
                            if isinstance(a, str) and "\n" in a:
                                out.append((f"{name}:{c['name']}[{i}]", a))
    return out


class EmbeddedShell(unittest.TestCase):
    def test_every_inline_script_parses(self):
        programs = shell_programs()
        # A zero-length result would make this test vacuously green if the
        # manifests were ever restructured, so assert we actually found some.
        self.assertGreater(len(programs), 0, "found no inline shell to check -- has the shape changed?")
        failures = []
        for label, script in programs:
            r = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
            if r.returncode != 0:
                failures.append(f"{label}: {r.stderr.strip()}")
        self.assertEqual(failures, [], "\n  " + "\n  ".join(failures))


if __name__ == "__main__":
    unittest.main()
