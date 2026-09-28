#!/usr/bin/env python3
"""Fake-kubectl tests for kubernetes/apps/databases/cnpg-lost-slot-reclone.sh.

    python scripts/tests/cnpg-lost-slot-reclone-mock.py

A fake `kubectl` (this file, re-invoked with FAKE_KUBECTL=1) is put first on PATH. It serves the exact
jsonpath lookups the script makes from a JSON state file, records every mutating call, and mutates the
state the way the API server would (annotate writes annotations; delete removes objects). The probe's
output (slots.tsv) is a fixture per scenario. Each scenario asserts the exit code, the mutating calls
(none for every guard), and the marker protocol.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "kubernetes" / "apps" / "databases" / "cnpg-lost-slot-reclone.sh"
BASH = next((b for b in (r"C:/Program Files/Git/usr/bin/bash.exe", r"C:/Program Files/Git/bin/bash.exe")
             if os.path.exists(b)), "sh")
HEALTHY = "Cluster in healthy state"


# ----------------------------------------------------------------------------------------------------
# fake kubectl
# ----------------------------------------------------------------------------------------------------
def fake_kubectl(argv):
    state_path = os.environ["FAKE_STATE"]
    st = json.load(open(state_path))
    log = open(os.environ["FAKE_LOG"], "a")
    args = list(argv)
    if args[:2] == ["-n", st["ns"]]:
        args = args[2:]
    verb = args[0]

    def save():
        json.dump(st, open(state_path, "w"))

    def out(s):
        sys.stdout.write(s)
        return 0

    if verb == "get":
        kind, name = args[1], args[2] if len(args) > 2 and not args[2].startswith("-") else None
        rest = args[3:] if name else args[2:]
        jp = next((a.split("=", 1)[1] for a in rest if a.startswith("jsonpath=")), None)
        if jp is None and "-o" in rest:
            i = rest.index("-o")
            jp = rest[i + 1].split("=", 1)[1] if rest[i + 1].startswith("jsonpath=") else rest[i + 1]
        if kind == "cluster":
            c = st["clusters"].get(name)
            if not c:
                sys.stderr.write("not found\n"); return 1
            # the "state changes before mutation" scenario: flip the primary on the Nth read
            if st.get("flip_primary_after") is not None:
                st["cluster_reads"] = st.get("cluster_reads", 0) + 1
                if st["cluster_reads"] > st["flip_primary_after"]:
                    c["currentPrimary"] = st["flip_to"]
                save()
            m = {"{.status.phase}": c["phase"], "{.status.currentPrimary}": c["currentPrimary"],
                 "{.status.instanceNames[*]}": " ".join(c["instances"]), "{.spec.instances}": str(c["want"]),
                 "{.spec.replicationSlots.highAvailability.slotPrefix}": c.get("prefix", ""),
                 "{.metadata.annotations.ailab\\.io/reclone-in-progress}": c["annotations"].get("ailab.io/reclone-in-progress", ""),
                 "{.metadata.annotations.ailab\\.io/last-reclone}": c["annotations"].get("ailab.io/last-reclone", "")}
            return out(m[jp])
        if kind == "pod":
            p = st["pods"].get(name)
            if not p:
                sys.stderr.write("Error from server (NotFound)\n"); return 1
            if jp == "name":
                return out(f"pod/{name}\n")
            return out(p["role"])
        if kind == "pvc":
            names = [a for a in ([name] + rest) if a and not a.startswith("-") and not a.startswith("jsonpath") and a != "--ignore-not-found"]
            names = [n for n in names if n in st["pvcs"] or n in ("x",)]
            if jp == "{.metadata.uid}":
                p = st["pvcs"].get(name)
                if not p:
                    sys.stderr.write("NotFound\n"); return 1
                return out(p["uid"])
            return out("".join(f"{n}={st['pvcs'][n]['uid']}={st['pvcs'][n]['pv']}\n" for n in names))
        return 1
    if verb == "annotate":
        name = args[2]
        c = st["clusters"][name]
        for a in args[3:]:
            if a.startswith("--"):
                continue
            if a.endswith("-"):
                c["annotations"].pop(a[:-1], None)
            else:
                k, v = a.split("=", 1); c["annotations"][k] = v
        log.write("annotate " + " ".join(args[3:]) + "\n"); save(); return out("annotated\n")
    if verb == "delete":
        kind = args[1]
        names = [a for a in args[2:] if not a.startswith("-")]
        log.write(f"delete {kind} {' '.join(names)}\n")
        if st.get("deletes_hang"):
            return out("")
        for n in names:
            if kind == "pvc":
                st["pvcs"].pop(n, None)
            else:
                st["pods"].pop(n, None)
        save(); return out("")
    sys.stderr.write(f"fake kubectl: unhandled {args}\n"); return 2


# ----------------------------------------------------------------------------------------------------
# harness
# ----------------------------------------------------------------------------------------------------
def base_state(**over):
    st = {"ns": "databases",
          "clusters": {"infra-pg": {"phase": HEALTHY, "currentPrimary": "infra-pg-2", "instances": ["infra-pg-2", "infra-pg-5"],
                                    "want": 2, "prefix": "_cnpg_", "annotations": {}}},
          "pods": {"infra-pg-2": {"role": "primary"}, "infra-pg-5": {"role": "replica"}},
          "pvcs": {"infra-pg-2": {"uid": "u2", "pv": "pvc-aaa"}, "infra-pg-5": {"uid": "u5", "pv": "pvc-bbb"},
                   "infra-pg-5-wal": {"uid": "u5w", "pv": "pvc-ccc"}}}
    st.update(over)
    return st


# the real probe concatenates booleans into text, so they read true/false (seen live on the drill);
# the script also accepts bare psql t/f - both shapes are exercised below
SLOTS_OK = "in_recovery=false\n_cnpg_infra_pg_5|physical|true|reserved|1083465728|\n"
SLOTS_LOST = "in_recovery=false\n_cnpg_infra_pg_5|physical|false|lost||wal_removed\n"
SLOTS_LOST_TF = "in_recovery=f\n_cnpg_infra_pg_5|physical|f|lost||wal_removed\n"


def run(name, state, slots, env_over=None):
    d = pathlib.Path(tempfile.mkdtemp(prefix="reclone-"))
    (d / "slots.tsv").write_text(slots)
    (d / "state.json").write_text(json.dumps(state))
    (d / "calls.log").write_text("")
    shim = d / "kubectl"
    shim.write_text('#!/bin/sh\nFAKE_KUBECTL=1 exec python "%s" "$@"\n' % str(pathlib.Path(__file__).resolve()).replace("\\", "/"))
    shim.chmod(0o755)
    env = dict(os.environ)
    env.update({"PATH": str(d) + os.pathsep + env["PATH"], "FAKE_STATE": str(d / "state.json"), "FAKE_LOG": str(d / "calls.log"),
                "NAMESPACE": "databases", "CLUSTERS": "infra-pg", "SLOTS_FILE": str(d / "slots.tsv"), "DRY_RUN": "false",
                "DELETE_WAIT_SECONDS": "3", "POLL_SECONDS": "1", "MIN_INTERVAL_SECONDS": "21600", "STUCK_AFTER_SECONDS": "2700"})
    env.update(env_over or {})
    p = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    calls = (d / "calls.log").read_text().splitlines()
    final = json.load(open(d / "state.json"))
    return p.returncode, p.stdout + p.stderr, calls, final


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def main():
    failures = 0

    def check(name, cond, detail=""):
        nonlocal failures
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
        if not cond:
            failures += 1

    # healthy: nothing to do
    rc, out, calls, _ = run("healthy", base_state(), SLOTS_OK)
    check("healthy exit 0, no calls", rc == 0 and not calls and "no lost slot" in out, out)

    # bare t/f booleans (psql -At without concatenation) are accepted too
    rc, out, calls, _ = run("live-tf", base_state(), SLOTS_LOST_TF)
    check("t/f booleans accepted", rc == 0 and len(calls) == 3, str(calls) + out)

    # lost + dry run: decision logged, no mutation
    rc, out, calls, _ = run("dry", base_state(), SLOTS_LOST, {"DRY_RUN": "true"})
    check("dry exit 0, no calls, would-delete", rc == 0 and not calls and "DRY_RUN - would" in out, out)

    # lost + live: marker first, PVCs, pod, wait completes, exit 0, PVs logged
    rc, out, calls, final = run("live", base_state(), SLOTS_LOST)
    check("live exit 0", rc == 0, out)
    check("live order marker -> pvc -> pod", [c.split()[0] for c in calls] == ["annotate", "delete", "delete"]
          and calls[0].startswith("annotate ailab.io/reclone-in-progress=") and calls[0].split("=", 1)[1].split()[0].endswith("/infra-pg-5")
          and calls[1] == "delete pvc infra-pg-5 infra-pg-5-wal" and calls[2] == "delete pod infra-pg-5", str(calls))
    check("live marker kept, pvcs gone, PV names logged", "ailab.io/reclone-in-progress" in final["clusters"]["infra-pg"]["annotations"]
          and "infra-pg-5" not in final["pvcs"] and "pvc-bbb" in out and "pvc-ccc" in out, out)

    # lost + live, but deletes never complete: exit 1, marker left
    rc, out, calls, final = run("hang", base_state(deletes_hang=True), SLOTS_LOST)
    check("hang exit 1, marker left", rc == 1 and "still present" in out and "ailab.io/reclone-in-progress" in final["clusters"]["infra-pg"]["annotations"], out)

    # lost slot belongs to the primary => refuse
    st = base_state(); st["clusters"]["infra-pg"]["currentPrimary"] = "infra-pg-5"; st["pods"]["infra-pg-5"]["role"] = "primary"
    rc, out, calls, _ = run("primary", st, SLOTS_LOST)
    check("primary refused, exit 1, no calls", rc == 1 and not calls and "PRIMARY" in out, out)

    # pod missing / not a replica => refuse
    st = base_state(); del st["pods"]["infra-pg-5"]
    rc, out, calls, _ = run("nopod", st, SLOTS_LOST)
    check("missing pod refused", rc == 1 and not calls and "pod missing" in out, out)

    # phase not healthy => not acting
    st = base_state(); st["clusters"]["infra-pg"]["phase"] = "Switchover in progress"
    rc, out, calls, _ = run("phase", st, SLOTS_LOST)
    check("phase not healthy: exit 0, no calls", rc == 0 and not calls and "not acting" in out, out)

    # probe saw a standby => exit 3
    rc, out, calls, _ = run("recovery", base_state(), SLOTS_LOST.replace("in_recovery=false", "in_recovery=true"))
    check("in_recovery exit 3, no calls", rc == 3 and not calls, out)

    # lost but ACTIVE slot => ignored; slot with a foreign prefix => ignored; logical slot => ignored
    rc, out, calls, _ = run("active-lost", base_state(), "in_recovery=f\n_cnpg_infra_pg_5|physical|t|lost||\n")
    check("active lost ignored", rc == 0 and not calls and "ACTIVE - ignoring" in out, out)
    rc, out, calls, _ = run("unmapped", base_state(), "in_recovery=f\n_cnpg_infra_pg_9|physical|f|lost||\nother_slot|physical|f|lost||\n")
    check("unmapped slots ignored", rc == 0 and not calls and "maps to 0 instances" in out, out)
    rc, out, calls, _ = run("logical", base_state(), "in_recovery=f\n_cnpg_infra_pg_5|logical|f|lost||\n")
    check("logical slot ignored", rc == 0 and not calls and "not physical" in out, out)

    # custom prefix honoured
    st = base_state(); st["clusters"]["infra-pg"]["prefix"] = "_ha_"
    rc, out, calls, _ = run("prefix", st, "in_recovery=f\n_ha_infra_pg_5|physical|f|lost||\n")
    check("custom prefix maps and acts", rc == 0 and calls and calls[1] == "delete pvc infra-pg-5 infra-pg-5-wal", str(calls) + out)

    # last-reclone too recent => budget refusal
    st = base_state(); st["clusters"]["infra-pg"]["annotations"]["ailab.io/last-reclone"] = iso(time.time() - 600) + "/infra-pg-4"
    rc, out, calls, _ = run("budget", st, SLOTS_LOST)
    check("recent last-reclone refused", rc == 1 and not calls and "budget" in out, out)
    st["clusters"]["infra-pg"]["annotations"]["ailab.io/last-reclone"] = iso(time.time() - 30000) + "/infra-pg-4"
    rc, out, calls, _ = run("budget-ok", st, SLOTS_LOST)
    check("old last-reclone allows", rc == 0 and len(calls) == 3, str(calls) + out)

    # marker present, replacement verified => marker cleared, last-reclone stamped; no deletes
    st = base_state(); st["clusters"]["infra-pg"]["annotations"]["ailab.io/reclone-in-progress"] = iso(time.time() - 900) + "/infra-pg-4"
    rc, out, calls, final = run("verify", st, SLOTS_OK)
    ann = final["clusters"]["infra-pg"]["annotations"]
    check("marker verified and cleared", rc == 0 and calls == [calls[0]] and calls[0].startswith("annotate ailab.io/last-reclone=")
          and "ailab.io/reclone-in-progress" not in ann and ann.get("ailab.io/last-reclone", "").endswith("/infra-pg-4"), str(calls) + out)

    # marker present, still lost (join not done), young => observe only
    st = base_state(); st["clusters"]["infra-pg"]["annotations"]["ailab.io/reclone-in-progress"] = iso(time.time() - 300) + "/infra-pg-5"
    st["clusters"]["infra-pg"]["phase"] = "Creating a new replica"
    rc, out, calls, _ = run("observe", st, SLOTS_LOST)
    check("young marker: observe, no calls", rc == 0 and not calls and "observing" in out, out)

    # marker present, old and unverified => exit 1, no calls
    st["clusters"]["infra-pg"]["annotations"]["ailab.io/reclone-in-progress"] = iso(time.time() - 4000) + "/infra-pg-5"
    rc, out, calls, _ = run("stuck", st, SLOTS_LOST)
    check("stuck marker exit 1, no calls", rc == 1 and not calls and "operator needed" in out, out)

    # marker present with a second lost slot: never acts while a marker exists
    st = base_state(); st["clusters"]["infra-pg"]["annotations"]["ailab.io/reclone-in-progress"] = iso(time.time() - 300) + "/infra-pg-4"
    rc, out, calls, _ = run("marker-blocks", st, SLOTS_LOST)
    check("marker blocks new action", not calls, str(calls))

    # state changes between the guards and the mutation => abort
    # 7 cluster reads happen before the guards; the 8th is the pre-mutation re-read of currentPrimary
    st = base_state(flip_primary_after=7, flip_to="infra-pg-5")
    rc, out, calls, _ = run("race", st, SLOTS_LOST)
    check("primary flip before mutation aborts", rc == 1 and not calls and "state changed" in out, str(calls) + out)

    # separate WAL PVC absent (single PVC) still works
    st = base_state(); del st["pvcs"]["infra-pg-5-wal"]
    rc, out, calls, _ = run("nowal", st, SLOTS_LOST)
    check("no wal pvc: deletes the one claim", rc == 0 and calls[1] == "delete pvc infra-pg-5", str(calls) + out)

    # malformed probe output => exit 1
    rc, out, calls, _ = run("malformed", base_state(), "garbage\n")
    check("malformed probe exit 1", rc == 1 and not calls, out)

    print(f"\n{'ALL PASSED' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if os.environ.get("FAKE_KUBECTL") == "1":
        sys.exit(fake_kubectl(sys.argv[1:]))
    sys.exit(main())
