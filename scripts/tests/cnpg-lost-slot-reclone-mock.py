#!/usr/bin/env python3
"""Fake-kubectl tests for kubernetes/apps/databases/cnpg-lost-slot-reclone.sh, and fake-psql tests for
its probe, kubernetes/apps/databases/cnpg-lost-slot-reclone-probe.sh.

    python scripts/tests/cnpg-lost-slot-reclone-mock.py

A fake `kubectl` (this file, re-invoked with FAKE_KUBECTL=1) is put first on PATH. It serves the calls
the script makes (`get cluster -o json`, pod/pvc jsonpath reads, `annotate` with a resourceVersion
precondition, `delete`) from a JSON state file, records every mutating call, and mutates the state the
way the API server would. The probe's output (slots.tsv) is a fixture per scenario, in the true/false
shape the real probe produces. Each scenario asserts the exit code, the mutating calls (none for every
guard) and the marker protocol.

The probe tests put a fake `psql` (this file again, FAKE_PSQL=1) first on PATH. It exits with the next
code from a per-scenario list and writes the slot output to `-o <file>` only when that code is 0, so the
retry and hand-off behaviour is exercised without a server.
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
PROBE = ROOT / "kubernetes" / "apps" / "databases" / "cnpg-lost-slot-reclone-probe.sh"
BASH = next((b for b in (r"C:/Program Files/Git/usr/bin/bash.exe", r"C:/Program Files/Git/bin/bash.exe")
             if os.path.exists(b)), "sh")
HEALTHY = "Cluster in healthy state"
MARKER = "ailab.io/reclone-in-progress"
LAST = "ailab.io/last-reclone"


# ----------------------------------------------------------------------------------------------------
# fake kubectl
# ----------------------------------------------------------------------------------------------------
def fake_kubectl(argv):
    state_path = os.environ["FAKE_STATE"]
    st = json.load(open(state_path))
    log = open(os.environ["FAKE_LOG"], "a")
    args = [a for a in argv if not a.startswith("--request-timeout")]
    if args[:2] == ["-n", st["ns"]]:
        args = args[2:]
    verb = args[0]

    def save():
        json.dump(st, open(state_path, "w"))

    def out(s):
        sys.stdout.write(s)
        return 0

    def err(s, rc=1):
        sys.stderr.write(s + "\n")
        return rc

    if verb == "get":
        kind = args[1]
        names = [a for a in args[2:] if not a.startswith("-") and not a.startswith("jsonpath")]
        jp = None
        if "-o" in args:
            v = args[args.index("-o") + 1]
            jp = v.split("=", 1)[1] if v.startswith("jsonpath=") else v
        if kind == "cluster":
            name = names[0]
            st["cluster_reads"] = st.get("cluster_reads", 0) + 1
            if st.get("fail_cluster_read"):
                save(); return err("Unable to connect to the server: dial tcp: i/o timeout")
            c = st["clusters"].get(name)
            if not c:
                save(); return err(f'Error from server (NotFound): clusters.postgresql.cnpg.io "{name}" not found')
            n = st["cluster_reads"]
            if st.get("flip_primary_after") is not None and n > st["flip_primary_after"]:
                c["currentPrimary"] = st["flip_to"]
            if st.get("phase_change_after") is not None and n > st["phase_change_after"]:
                c["phase"] = "Switchover in progress"
            if st.get("marker_appears_after") is not None and n > st["marker_appears_after"]:
                c["annotations"][MARKER] = "2026-01-01T00:00:00Z/other-job"
            save()
            return out(json.dumps({"metadata": {"name": name, "annotations": c["annotations"], "resourceVersion": str(c["rv"])},
                                   "spec": {"instances": c["want"], "replicationSlots": {"highAvailability": {"enabled": True, "slotPrefix": c.get("prefix", "_cnpg_")}}},
                                   "status": {"phase": c["phase"], "currentPrimary": c["currentPrimary"], "instanceNames": c["instances"],
                                              "conditions": c.get("conditions", [])}}))
        if kind == "pod":
            name = names[0]
            st["pod_reads"] = st.get("pod_reads", 0) + 1
            p = st["pods"].get(name)
            if st.get("role_flip_after") is not None and p and st["pod_reads"] > st["role_flip_after"]:
                p["role"] = "primary"
            save()
            if st.get("api_error_on_wait") and st.get("deleting"):
                return err("Unable to connect to the server: connection refused")
            if not p:
                return err(f'Error from server (NotFound): pods "{name}" not found')
            if jp == "{.metadata.uid}":
                return out("pod-uid-" + name)
            return out(p["role"])
        if kind == "pvc":
            if jp == "{.metadata.uid}":
                name = names[0]
                if st.get("api_error_on_wait") and st.get("deleting"):
                    return err("Unable to connect to the server: connection refused")
                p = st["pvcs"].get(name)
                if not p:
                    return err(f'Error from server (NotFound): persistentvolumeclaims "{name}" not found')
                return out(p["uid"])
            have = [n for n in names if n in st["pvcs"]]
            return out("".join(f"{n}={st['pvcs'][n]['uid']}={st['pvcs'][n]['pv']}\n" for n in have))
        return err(f"fake kubectl: unhandled get {args}", 2)
    if verb == "annotate":
        name = args[2]
        c = st["clusters"][name]
        rv = next((a.split("=", 1)[1] for a in args if a.startswith("--resource-version=")), None)
        if rv is not None and (st.get("rv_conflict") or rv != str(c["rv"])):
            log.write("annotate-conflict\n"); save()
            return err(f'error: Operation cannot be fulfilled on clusters.postgresql.cnpg.io "{name}": the object has been modified (Conflict)')
        for a in args[3:]:
            if a.startswith("--"):
                continue
            if a.endswith("-"):
                c["annotations"].pop(a[:-1], None)
            else:
                k, v = a.split("=", 1); c["annotations"][k] = v
        c["rv"] += 1
        log.write("annotate " + " ".join(a for a in args[3:] if not a.startswith("--overwrite")) + "\n"); save()
        return out("annotated\n")
    if verb == "delete":
        kind = args[1]
        names = [a for a in args[2:] if not a.startswith("-")]
        log.write(f"delete {kind} {' '.join(names)}\n")
        st["deleting"] = True
        if not st.get("deletes_hang"):
            for n in names:
                (st["pvcs"] if kind == "pvc" else st["pods"]).pop(n, None)
        save(); return out("")
    return err(f"fake kubectl: unhandled {args}", 2)


def fake_psql(argv):
    st_path = os.environ["FAKE_PSQL_STATE"]
    st = json.load(open(st_path))
    st["calls"] = st.get("calls", 0) + 1
    rc = st["rcs"][min(st["calls"], len(st["rcs"])) - 1]
    json.dump(st, open(st_path, "w"))
    out = argv[argv.index("-o") + 1]
    open(out, "w").write("in_recovery=false\n" if rc == 2 else "")  # worst case: partial output
    if rc == 0:
        open(out, "w").write(SLOTS_OK)
        return 0
    sys.stderr.write({1: "ERROR:  relation \"pg_replication_slots\" does not exist\n",
                      2: 'psql: error: connection to server at "infra-pg-rw" (10.0.0.1), port 5432 failed: No route to host\n'}.get(rc, "psql: error\n"))
    return rc


# ----------------------------------------------------------------------------------------------------
# harness
# ----------------------------------------------------------------------------------------------------
def base_state(**over):
    st = {"ns": "databases",
          "clusters": {"infra-pg": {"phase": HEALTHY, "currentPrimary": "infra-pg-2", "instances": ["infra-pg-2", "infra-pg-5"],
                                    "want": 2, "prefix": "_cnpg_", "annotations": {}, "rv": 100,
                                    "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "2026-01-01T00:00:00Z"}]}},
          "pods": {"infra-pg-2": {"role": "primary"}, "infra-pg-5": {"role": "replica"}},
          "pvcs": {"infra-pg-2": {"uid": "u2", "pv": "pvc-aaa"}, "infra-pg-5": {"uid": "u5", "pv": "pvc-bbb"},
                   "infra-pg-5-wal": {"uid": "u5w", "pv": "pvc-ccc"}}}
    st.update(over)
    return st


# the real probe concatenates booleans into text (true/false); bare psql t/f is accepted too
SLOTS_OK = "in_recovery=false\n_cnpg_infra_pg_5|physical|true|reserved|1083465728|\n"
SLOTS_LOST = "in_recovery=false\n_cnpg_infra_pg_5|physical|false|lost||wal_removed\n"
SLOTS_LOST_TF = "in_recovery=f\n_cnpg_infra_pg_5|physical|f|lost||wal_removed\n"


def run(name, state, slots, env_over=None, unreachable=False):
    d = pathlib.Path(tempfile.mkdtemp(prefix="reclone-"))
    if unreachable:  # what the probe leaves when it could not connect: the hand-off file, no slots
        (d / "probe-unreachable").write_text("")
    else:
        (d / "slots.tsv").write_text(slots)
    (d / "state.json").write_text(json.dumps(state))
    (d / "calls.log").write_text("")
    shim = d / "kubectl"
    # the interpreter that launched the test, not a bare `python` (absent on stock Debian/Alpine)
    shim.write_text('#!/bin/sh\nFAKE_KUBECTL=1 exec "%s" "%s" "$@"\n' % (sys.executable.replace("\\", "/"), str(pathlib.Path(__file__).resolve()).replace("\\", "/")))
    shim.chmod(0o755)
    env = dict(os.environ)
    env.update({"PATH": str(d) + os.pathsep + env["PATH"], "FAKE_STATE": str(d / "state.json"), "FAKE_LOG": str(d / "calls.log"),
                "NAMESPACE": "databases", "CLUSTERS": "infra-pg", "SLOTS_FILE": str(d / "slots.tsv"), "DRY_RUN": "false", "WORK": str(d),
                "PROBE_UNREACHABLE_FILE": str(d / "probe-unreachable"),
                "DELETE_WAIT_SECONDS": "3", "POLL_SECONDS": "1", "MIN_INTERVAL_SECONDS": "21600", "STUCK_AFTER_SECONDS": "2700"})
    env.update(env_over or {})
    p = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    calls = (d / "calls.log").read_text().splitlines()
    final = json.load(open(d / "state.json"))
    return p.returncode, p.stdout + p.stderr, calls, final


def run_probe(rcs, pre_existing=False):
    d = pathlib.Path(tempfile.mkdtemp(prefix="reclone-probe-"))
    (d / "psql.json").write_text(json.dumps({"rcs": rcs}))
    if pre_existing:  # a previous run's files must never be read as this run's result
        (d / "slots.tsv").write_text("stale\n")
        (d / "probe-unreachable").write_text("")
    shim = d / "psql"
    shim.write_text('#!/bin/sh\nFAKE_PSQL=1 exec "%s" "%s" "$@"\n' % (sys.executable.replace("\\", "/"), str(pathlib.Path(__file__).resolve()).replace("\\", "/")))
    shim.chmod(0o755)
    env = dict(os.environ)
    env.update({"PATH": str(d) + os.pathsep + env["PATH"], "FAKE_PSQL_STATE": str(d / "psql.json"),
                "SLOTS_FILE": str(d / "slots.tsv"), "PROBE_UNREACHABLE_FILE": str(d / "probe-unreachable"),
                "PROBE_ATTEMPTS": "3", "PROBE_RETRY_SECONDS": "0"})
    p = subprocess.run([BASH, str(PROBE)], env=env, capture_output=True, text=True, timeout=60)
    slots = (d / "slots.tsv").read_text() if (d / "slots.tsv").exists() else None
    calls = json.load(open(d / "psql.json")).get("calls", 0)
    return p.returncode, p.stdout + p.stderr, slots, (d / "probe-unreachable").exists(), calls


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def is_live_sequence(calls):
    return ([c.split()[0] for c in calls] == ["annotate", "delete", "delete"]
            and calls[0].startswith(f"annotate {MARKER}=") and calls[0].split("=", 1)[1].split()[0].endswith("/infra-pg-5")
            and calls[1] == "delete pvc infra-pg-5 infra-pg-5-wal" and calls[2] == "delete pod infra-pg-5")


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

    # lost + dry run: decision logged, no mutation
    rc, out, calls, _ = run("dry", base_state(), SLOTS_LOST, {"DRY_RUN": "true"})
    check("dry exit 0, no calls, would-delete", rc == 0 and not calls and "DRY_RUN - would" in out, out)

    # lost + live: marker (with resourceVersion precondition) -> PVCs -> pod, wait completes, exit 0
    rc, out, calls, final = run("live", base_state(), SLOTS_LOST)
    check("live exit 0", rc == 0, out)
    check("live order marker -> pvc -> pod", is_live_sequence(calls), str(calls))
    check("live marker kept, pvcs gone, PV names logged", MARKER in final["clusters"]["infra-pg"]["annotations"]
          and "infra-pg-5" not in final["pvcs"] and "pvc-bbb" in out and "pvc-ccc" in out, out)
    rc, out, calls, _ = run("live-tf", base_state(), SLOTS_LOST_TF)
    check("t/f booleans accepted", rc == 0 and is_live_sequence(calls), str(calls) + out)

    # deletes never complete: exit 1, marker left
    rc, out, calls, final = run("hang", base_state(deletes_hang=True), SLOTS_LOST)
    check("hang exit 1, marker left", rc == 1 and "not confirmed gone" in out and MARKER in final["clusters"]["infra-pg"]["annotations"], out)
    # API errors while waiting are "unknown", never "gone"
    rc, out, calls, final = run("wait-api-error", base_state(api_error_on_wait=True), SLOTS_LOST)
    check("api error during wait => exit 1, marker left", rc == 1 and "not confirmed gone" in out and MARKER in final["clusters"]["infra-pg"]["annotations"], out)

    # the lost slot is the primary's own => never actionable, no calls
    st = base_state(); st["clusters"]["infra-pg"]["currentPrimary"] = "infra-pg-5"; st["pods"]["infra-pg-5"]["role"] = "primary"
    rc, out, calls, _ = run("primary", st, SLOTS_LOST)
    check("primary's slot never actionable, no calls", not calls and "PRIMARY" in out, str(calls) + out)

    # pod missing / not a replica => refuse
    st = base_state(); del st["pods"]["infra-pg-5"]
    rc, out, calls, _ = run("nopod", st, SLOTS_LOST)
    check("missing pod refused", rc == 1 and not calls and "pod missing" in out, out)

    # no PVC at all => refuse; only the optional WAL PVC missing => acts on the one claim
    st = base_state(); del st["pvcs"]["infra-pg-5"]; del st["pvcs"]["infra-pg-5-wal"]
    rc, out, calls, _ = run("nopvc", st, SLOTS_LOST)
    check("no PVC refused", rc == 1 and not calls and "no PVC" in out, out)
    st = base_state(); del st["pvcs"]["infra-pg-5-wal"]
    rc, out, calls, _ = run("nowal", st, SLOTS_LOST)
    check("no wal pvc: deletes the one claim", rc == 0 and calls[1] == "delete pvc infra-pg-5", str(calls) + out)

    # phase not healthy => not acting
    st = base_state(); st["clusters"]["infra-pg"]["phase"] = "Switchover in progress"
    rc, out, calls, _ = run("phase", st, SLOTS_LOST)
    check("phase not healthy: exit 0, no calls", rc == 0 and not calls and "not acting" in out, out)

    # probe saw a standby => exit 3
    rc, out, calls, _ = run("recovery", base_state(), SLOTS_LOST.replace("in_recovery=false", "in_recovery=true"))
    check("in_recovery exit 3, no calls", rc == 3 and not calls, out)

    # not actionable shapes: active+lost, unreserved, logical; unmapped slots ignored; missing slot
    rc, out, calls, _ = run("active-lost", base_state(), "in_recovery=false\n_cnpg_infra_pg_5|physical|true|lost||\n")
    check("active lost not actionable", rc == 0 and not calls and "not actionable" in out, out)
    rc, out, calls, _ = run("logical", base_state(), "in_recovery=false\n_cnpg_infra_pg_5|logical|false|lost||\n")
    check("logical slot not actionable", rc == 0 and not calls and "not actionable" in out, out)
    rc, out, calls, _ = run("unmapped", base_state(), "in_recovery=false\n_cnpg_infra_pg_5|physical|true|reserved||\n_cnpg_infra_pg_9|physical|false|lost||\nother_slot|physical|false|lost||\n")
    check("unmapped slots ignored", rc == 0 and not calls and "maps to no replica" in out, out)
    rc, out, calls, _ = run("no-slot", base_state(), "in_recovery=false\n")
    check("replica without a slot: logged, no action", rc == 0 and not calls and "has no slot" in out, out)

    # custom prefix honoured
    st = base_state(); st["clusters"]["infra-pg"]["prefix"] = "_ha_"
    rc, out, calls, _ = run("prefix", st, "in_recovery=false\n_ha_infra_pg_5|physical|false|lost||\n")
    check("custom prefix maps and acts", rc == 0 and calls and calls[1] == "delete pvc infra-pg-5 infra-pg-5-wal", str(calls) + out)

    # last-reclone too recent => budget refusal; old enough => allowed
    st = base_state(); st["clusters"]["infra-pg"]["annotations"][LAST] = iso(time.time() - 600) + "/infra-pg-4"
    rc, out, calls, _ = run("budget", st, SLOTS_LOST)
    check("recent last-reclone refused", rc == 1 and not calls and "budget" in out, out)
    st["clusters"]["infra-pg"]["annotations"][LAST] = "garbage/infra-pg-4"
    rc, out, calls, _ = run("budget-corrupt", st, SLOTS_LOST)
    check("unparseable last-reclone refused (fails closed)", rc == 1 and not calls and "no parseable timestamp" in out, out)
    st["clusters"]["infra-pg"]["annotations"][LAST] = iso(time.time() - 30000) + "/infra-pg-4"
    rc, out, calls, _ = run("budget-ok", st, SLOTS_LOST)
    check("old last-reclone allows", rc == 0 and is_live_sequence(calls), str(calls) + out)

    # marker present, replacement verified => marker cleared + last-reclone stamped; no deletes
    st = base_state(); st["clusters"]["infra-pg"]["annotations"][MARKER] = iso(time.time() - 900) + "/infra-pg-4"
    rc, out, calls, final = run("verify", st, SLOTS_OK)
    ann = final["clusters"]["infra-pg"]["annotations"]
    check("marker verified and cleared", rc == 0 and len(calls) == 1 and calls[0].startswith(f"annotate {LAST}=")
          and MARKER not in ann and ann.get(LAST, "").endswith("/infra-pg-4"), str(calls) + out)
    # ... but NOT when the replacement's slot is missing, unreserved, or inactive
    for label, slots in (("missing", "in_recovery=false\n"),
                         ("unreserved", "in_recovery=false\n_cnpg_infra_pg_5|physical|true|unreserved||\n"),
                         ("inactive", "in_recovery=false\n_cnpg_infra_pg_5|physical|false|reserved||\n")):
        st = base_state(); st["clusters"]["infra-pg"]["annotations"][MARKER] = iso(time.time() - 900) + "/infra-pg-4"
        rc, out, calls, final = run("verify-" + label, st, slots)
        check(f"verify refused when replacement slot {label}", rc == 0 and not calls and MARKER in final["clusters"]["infra-pg"]["annotations"] and "observing" in out, str(calls) + out)

    # marker present, join not done, young => observe only; old => exit 1; a second lost slot never acts
    st = base_state(); st["clusters"]["infra-pg"]["annotations"][MARKER] = iso(time.time() - 300) + "/infra-pg-5"
    st["clusters"]["infra-pg"]["phase"] = "Creating a new replica"
    rc, out, calls, _ = run("observe", st, SLOTS_LOST)
    check("young marker: observe, no calls", rc == 0 and not calls and "observing" in out, out)
    st["clusters"]["infra-pg"]["annotations"][MARKER] = iso(time.time() - 4000) + "/infra-pg-5"
    rc, out, calls, _ = run("stuck", st, SLOTS_LOST)
    check("stuck marker exit 1, no calls", rc == 1 and not calls and "operator needed" in out, out)
    st = base_state(); st["clusters"]["infra-pg"]["annotations"][MARKER] = iso(time.time() - 300) + "/infra-pg-4"
    rc, out, calls, _ = run("marker-blocks", st, SLOTS_LOST)
    check("marker blocks new action", not calls, str(calls))

    # the state changes between the guards and the mutation => abort (2 cluster reads: initial + final)
    rc, out, calls, _ = run("race-primary", base_state(flip_primary_after=1, flip_to="infra-pg-5"), SLOTS_LOST)
    check("primary flip before mutation aborts", rc == 1 and not calls and "state changed" in out, str(calls) + out)
    rc, out, calls, _ = run("race-phase", base_state(phase_change_after=1), SLOTS_LOST)
    check("phase change before mutation aborts", rc == 1 and not calls and "state changed" in out, str(calls) + out)
    rc, out, calls, _ = run("race-role", base_state(role_flip_after=1), SLOTS_LOST)
    check("role change before mutation aborts", rc == 1 and not calls and "state changed" in out, str(calls) + out)
    rc, out, calls, _ = run("race-marker", base_state(marker_appears_after=1), SLOTS_LOST)
    check("marker appearing before mutation aborts", rc == 1 and not calls and "state changed" in out, str(calls) + out)
    rc, out, calls, _ = run("rv-conflict", base_state(rv_conflict=True), SLOTS_LOST)
    check("resourceVersion conflict aborts before any delete", rc == 1 and calls == ["annotate-conflict"] and "could not take the marker" in out, str(calls) + out)

    # the Cluster cannot be read => failure, never "no annotations"
    rc, out, calls, _ = run("cluster-read-fails", base_state(fail_cluster_read=True), SLOTS_LOST)
    check("cluster read failure exit 1, no calls", rc == 1 and not calls and "cannot read Cluster" in out, out)

    # malformed probe output => exit 1
    rc, out, calls, _ = run("malformed", base_state(), "garbage\n")
    check("malformed probe exit 1", rc == 1 and not calls, out)

    # the probe could not connect at all (it hands off instead of failing). CNPG restarts the primary in
    # place on every operator/image upgrade and reports the Cluster unhealthy meanwhile: not a fault.
    def unready(phase, since):  # CNPG patches phase and Ready together: Ready=False since `since`
        st = base_state(); st["clusters"]["infra-pg"]["phase"] = phase
        st["clusters"]["infra-pg"]["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": iso(since)}]
        return st
    rc, out, calls, _ = run("unreachable-upgrading", unready("Upgrading cluster", time.time() - 120), "", unreachable=True)
    check("unreachable while Cluster not healthy: exit 0, no calls", rc == 0 and not calls and "not acting" in out, out)
    rc, out, calls, _ = run("unreachable-upgrading-long", unready("Upgrading cluster", time.time() - 7200), "", unreachable=True)
    check("unreachable, transient phase but unhealthy for 2h: exit 1", rc == 1 and not calls and "failing" in out, out)
    st = base_state(); st["clusters"]["infra-pg"]["phase"] = "Upgrading cluster"; st["clusters"]["infra-pg"]["conditions"] = []
    rc, out, calls, _ = run("unreachable-upgrading-no-ready", st, "", unreachable=True)
    check("unreachable, transient phase, no Ready condition: exit 1", rc == 1 and not calls and "failing" in out, out)
    st = unready("Upgrading cluster", time.time() - 120)
    st["clusters"]["infra-pg"]["annotations"][MARKER] = iso(time.time() - 4000) + "/infra-pg-5"
    rc, out, calls, _ = run("unreachable-stuck-marker", st, "", unreachable=True)
    check("unreachable never hides an overdue re-clone marker: exit 1", rc == 1 and not calls and "operator needed" in out, out)
    rc, out, calls, _ = run("unreachable-restart", unready("Primary instance is being restarted without a switchover", time.time() - 60), "", unreachable=True)
    check("unreachable during in-place primary restart: exit 0", rc == 0 and not calls and "not acting" in out, out)
    # stuck / terminal / unknown phases are NOT maintenance: an unreachable primary there must page
    for label, phase in (("unrecoverable", "Cluster is unrecoverable and needs manual intervention"),
                         ("waiting-user", "Waiting for user action"),
                         ("empty", "")):
        rc, out, calls, _ = run("unreachable-" + label, unready(phase, time.time() - 60), "", unreachable=True)
        check(f"unreachable in phase '{phase}': exit 1", rc == 1 and not calls and "failing" in out, out)
    # ... but unreachable while CNPG says healthy is a real fault and must still page
    rc, out, calls, _ = run("unreachable-healthy", base_state(), "", unreachable=True)
    check("unreachable while Cluster healthy: exit 1, no calls", rc == 1 and not calls and "reports healthy" in out, out)
    # ... unless it became healthy moments ago: the restart is finishing (-rw lags the phase by seconds)
    st = base_state(); st["clusters"]["infra-pg"]["conditions"][0]["lastTransitionTime"] = iso(time.time() - 30)
    rc, out, calls, _ = run("unreachable-just-healthy", st, "", unreachable=True)
    check("unreachable, Ready for 30s: exit 0", rc == 0 and not calls and "not acting" in out, out)
    st = base_state(); st["clusters"]["infra-pg"]["conditions"] = []
    rc, out, calls, _ = run("unreachable-healthy-no-ready", st, "", unreachable=True)
    check("unreachable, healthy, no Ready condition: exit 1", rc == 1 and not calls and "reports healthy" in out, out)
    # ... and an unreadable Cluster is never taken as "not healthy"
    rc, out, calls, _ = run("unreachable-cluster-read-fails", base_state(fail_cluster_read=True), "", unreachable=True)
    check("unreachable + Cluster unreadable: exit 1, no calls", rc == 1 and not calls and "cannot read Cluster" in out, out)

    # --- probe (fake psql) --------------------------------------------------------------------------
    rc, out, slots, handoff, n = run_probe([0])
    check("probe ok: exit 0, slots written, no hand-off", rc == 0 and slots == SLOTS_OK and not handoff and n == 1, out)
    rc, out, slots, handoff, n = run_probe([2, 2, 0])
    check("probe retries a connection failure, then succeeds", rc == 0 and slots == SLOTS_OK and not handoff and n == 3, out)
    rc, out, slots, handoff, n = run_probe([2, 2, 2])
    check("probe hands off after PROBE_ATTEMPTS connection failures",
          rc == 0 and slots is None and handoff and n == 3 and "could not connect" in out, out)
    rc, out, slots, handoff, n = run_probe([1])
    check("probe: SQL error in a -c command (psql 1) fails the run, no retry", rc == 1 and not handoff and n == 1, out)
    rc, out, slots, handoff, n = run_probe([3])
    check("probe: any other non-2 exit fails the run, no retry", rc == 3 and not handoff and n == 1, out)
    rc, out, slots, handoff, n = run_probe([0], pre_existing=True)
    check("probe clears a previous run's hand-off file", rc == 0 and slots == SLOTS_OK and not handoff, out)

    print(f"\n{'ALL PASSED' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if os.environ.get("FAKE_KUBECTL") == "1":
        sys.exit(fake_kubectl(sys.argv[1:]))
    if os.environ.get("FAKE_PSQL") == "1":
        sys.exit(fake_psql(sys.argv[1:]))
    sys.exit(main())
