#!/usr/bin/env python3
"""LLM carve-vs-GTT tok/s benchmark for the Strix Halo heavyweight models.

Non-disruptive: drives the *running* llama-server on each node via its native
``/completion`` endpoint and reads the ``timings`` block, so it measures the exact
serving path users hit and can be re-run identically before/after the BIOS UMA
carve change. Each result self-documents its memory regime (VRAM carve size +
VRAM/GTT split, read from the Proxmox host over SSH) so the delta is interpretable.

See docs/superpowers/specs/2026-07-06-llm-carve-vs-gtt-benchmark-design.md

Usage:
    python scripts/bench-llm.py run --label before-bios          # node2 gpt-oss + node3 122B
    python scripts/bench-llm.py run --targets node3 --label before-bios
    python scripts/bench-llm.py compare bench/results/before-bios-*.json bench/results/after-bios-*.json

Stdlib only. Runs on the Windows host (reaches 192.168.0.0/24); shells out to
scripts/node-ssh.py for the per-node VRAM/GTT sysfs read (same access path as the
rest of the repo tooling).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TOOL_VERSION = "1.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_OUT = REPO_ROOT / "bench" / "results"

# LXC endpoint <-> Proxmox host map (mgmt LAN). card0 sysfs lives on the host.
NODES = {
    "node1": {"host": "192.168.0.2", "lxc": "192.168.0.44"},
    "node2": {"host": "192.168.0.3", "lxc": "192.168.0.45"},
    "node3": {"host": "192.168.0.4", "lxc": "192.168.0.46"},
}
DEFAULT_TARGETS = ["node2", "node3"]  # the two on-demand heavyweights
DEFAULT_SIZES = [512, 4096, 16384]    # prompt tokens; auto-skipped if > n_ctx
GIB = 1024 ** 3


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #
def _request(url: str, payload=None, timeout: float = 600.0):
    """GET (payload=None) or POST JSON. Returns parsed JSON, even for HTTP-error
    bodies (llama-server returns 4xx with a JSON ``error`` field)."""
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {exc.code} {exc.reason}"}
    except Exception as exc:  # URLError, timeout, JSON decode
        return {"error": f"{type(exc).__name__}: {exc}"}


def health_ok(endpoint: str, timeout: float) -> bool:
    r = _request(endpoint + "/health", timeout=min(timeout, 30))
    return isinstance(r, dict) and r.get("status") == "ok"


def _dig(d, *path):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


def extract_n_ctx(props: dict):
    if not isinstance(props, dict):
        return None
    for path in (
        ("default_generation_settings", "n_ctx"),
        ("default_generation_settings", "params", "n_ctx"),
        ("n_ctx",),
    ):
        val = _dig(props, *path)
        if isinstance(val, int) and val > 0:
            return val
    return None


def get_model_id(endpoint: str, props: dict, timeout: float):
    r = _request(endpoint + "/v1/models", timeout=min(timeout, 30))
    mid = _dig(r, "data")
    if isinstance(mid, list) and mid and isinstance(mid[0], dict) and mid[0].get("id"):
        return mid[0]["id"]
    mpath = _dig(props, "model_path") or _dig(props, "default_generation_settings", "model")
    if isinstance(mpath, str):
        return os.path.basename(mpath)
    return "unknown"


# --------------------------------------------------------------------------- #
# Memory-regime capture (VRAM carve + VRAM/GTT split) via node-ssh.py
# --------------------------------------------------------------------------- #
def read_mem(host: str):
    remote = (
        'for c in /sys/class/drm/card*/device; do '
        '[ -e "$c/mem_info_vram_total" ] || continue; '
        'echo "vram_total=$(cat $c/mem_info_vram_total)"; '
        'echo "vram_used=$(cat $c/mem_info_vram_used)"; '
        'echo "gtt_total=$(cat $c/mem_info_gtt_total)"; '
        'echo "gtt_used=$(cat $c/mem_info_gtt_used)"; '
        'break; done; '
        'echo "cmdline=$(cat /proc/cmdline)"'
    )
    try:
        out = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "node-ssh.py"), host, remote],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        return {"error": f"ssh failed: {exc}"}
    if out.returncode != 0:
        return {"error": f"node-ssh rc={out.returncode}: {out.stderr.strip()[:200]}"}
    mem = {}
    for line in out.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in ("vram_total", "vram_used", "gtt_total", "gtt_used"):
            try:
                mem[key] = int(val.strip())
            except ValueError:
                pass
        elif key == "cmdline":
            mem["cmdline"] = val.strip()
    return mem or {"error": "no mem_info found"}


# --------------------------------------------------------------------------- #
# Deterministic prompt construction
# --------------------------------------------------------------------------- #
def build_corpus(min_chars: int = 140_000) -> str:
    """A fixed, deterministic corpus large enough to slice the biggest prompt from.
    Same text every run => byte-identical prompts before/after the BIOS change."""
    base = (
        "In a quiet home lab three compact machines hum while a language model weighs "
        "a handful of unlikely options and settles on a measured answer that trades a "
        "little speed for a little more certainty about the shape of the problem. "
    )
    parts, total, i = [], 0, 0
    while total < min_chars:
        chunk = f"Section {i}. " + base
        parts.append(chunk)
        total += len(chunk)
        i += 1
    return "".join(parts)


def build_prompt(endpoint: str, n_tokens: int, corpus: str, timeout: float):
    """Return (prompt_text, method). Prefer an exact-token prompt via the server's
    own tokenizer (/tokenize -> slice -> /detokenize); fall back to a char heuristic."""
    tok = _request(endpoint + "/tokenize", {"content": corpus}, timeout=min(timeout, 60))
    tokens = tok.get("tokens") if isinstance(tok, dict) else None
    if isinstance(tokens, list) and len(tokens) >= n_tokens:
        # tokens may be ints or {"id":..} depending on build; normalize to ints
        ids = [t["id"] if isinstance(t, dict) else t for t in tokens[:n_tokens]]
        det = _request(endpoint + "/detokenize", {"tokens": ids}, timeout=min(timeout, 60))
        text = det.get("content") if isinstance(det, dict) else None
        if isinstance(text, str) and text.strip():
            return text, "tokenize"
    return corpus[: int(n_tokens * 3.8)], "chars"


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
def measure_once(endpoint: str, prompt: str, n_predict: int, timeout: float):
    payload = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0.0,   # greedy => deterministic
        "seed": 0,
        "cache_prompt": False,  # force a real prefill every run
        "ignore_eos": True,     # always generate exactly n_predict => clean decode rate
        "stream": False,
    }
    r = _request(endpoint + "/completion", payload, timeout=timeout)
    if not isinstance(r, dict) or "error" in r:
        return {"error": (r.get("error") if isinstance(r, dict) else "bad response")}
    t = r.get("timings") or {}
    return {
        "prefill_tps": t.get("prompt_per_second"),
        "decode_tps": t.get("predicted_per_second"),
        "prompt_n": t.get("prompt_n"),
        "predicted_n": t.get("predicted_n"),
        "prompt_ms": t.get("prompt_ms"),
        "predicted_ms": t.get("predicted_ms"),
    }


def _stats(vals):
    vals = [v for v in vals if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return None
    return {
        "median": round(statistics.median(vals), 2),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "n": len(vals),
    }


def run_target(name: str, port: int, sizes, n_predict: int, runs: int,
               warmup: int, timeout: float):
    node = NODES[name]
    endpoint = f"http://{node['lxc']}:{port}"
    log(f"\n=== {name} @ {endpoint} ===")
    res = {"node": name, "endpoint": endpoint}

    if not health_ok(endpoint, timeout):
        log(f"  ! /health not ok — skipping {name}")
        res["error"] = "health not ok"
        return res

    props = _request(endpoint + "/props", timeout=min(timeout, 30))
    n_ctx = extract_n_ctx(props)
    res["model_id"] = get_model_id(endpoint, props, timeout)
    res["n_ctx"] = n_ctx
    res["total_slots"] = _dig(props, "total_slots")
    res["model_path"] = _dig(props, "model_path")
    res["server_info"] = props.get("build_info") or props.get("system_info") if isinstance(props, dict) else None
    log(f"  model={res['model_id']}  n_ctx={n_ctx}  slots={res['total_slots']}")

    res["mem_before"] = read_mem(node["host"])
    _log_mem("  mem before:", res["mem_before"])

    corpus = build_corpus()
    sweep = []
    for size in sizes:
        row = {"prompt_size": size}
        if n_ctx and size + n_predict + 64 > n_ctx:
            row["skipped"] = f"size+{n_predict}+64 > n_ctx({n_ctx})"
            log(f"  [{size:>6} tok] skipped: exceeds n_ctx {n_ctx}")
            sweep.append(row)
            continue

        prompt, method = build_prompt(endpoint, size, corpus, timeout)
        row["prompt_method"] = method
        log(f"  [{size:>6} tok] prompt via {method}; warmup x{warmup}, measure x{runs}")

        for w in range(warmup):
            m = measure_once(endpoint, prompt, n_predict, timeout)
            if "error" in m:
                log(f"      warmup {w+1} error: {m['error']}")

        measured = []
        for k in range(runs):
            m = measure_once(endpoint, prompt, n_predict, timeout)
            measured.append(m)
            if "error" in m:
                log(f"      run {k+1} error: {m['error']}")
            else:
                pf, dc = m.get("prefill_tps"), m.get("decode_tps")
                pf_s = f"{pf:.1f}" if isinstance(pf, (int, float)) else str(pf)
                dc_s = f"{dc:.1f}" if isinstance(dc, (int, float)) else str(dc)
                log(f"      run {k+1}: prefill {pf_s} tok/s  "
                    f"decode {dc_s} tok/s  (prompt_n={m.get('prompt_n')})")

        ok = [m for m in measured if "error" not in m]
        row["prompt_n"] = ok[0]["prompt_n"] if ok else None
        row["decode_tps"] = _stats([m["decode_tps"] for m in ok])
        row["prefill_tps"] = _stats([m["prefill_tps"] for m in ok])
        row["runs"] = measured
        sweep.append(row)

    res["mem_after"] = read_mem(node["host"])
    _log_mem("  mem after: ", res["mem_after"])
    res["sweep"] = sweep
    return res


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def _gib(n):
    return f"{n / GIB:.1f}" if isinstance(n, (int, float)) else "?"


def _log_mem(prefix, mem):
    if not isinstance(mem, dict) or "error" in mem:
        log(f"{prefix} {mem.get('error') if isinstance(mem, dict) else mem}")
        return
    log(f"{prefix} carve(VRAM total)={_gib(mem.get('vram_total'))}GiB  "
        f"vram_used={_gib(mem.get('vram_used'))}GiB  "
        f"gtt_total={_gib(mem.get('gtt_total'))}GiB  gtt_used={_gib(mem.get('gtt_used'))}GiB")


def print_run_summary(result):
    log("\n----- summary -----")
    for tgt in result["targets"]:
        if tgt.get("error"):
            log(f"{tgt['node']}: ERROR {tgt['error']}")
            continue
        mb = tgt.get("mem_before", {})
        log(f"\n{tgt['node']}  {tgt['model_id']}  (carve {_gib(mb.get('vram_total'))}GiB, "
            f"gtt_used {_gib(mb.get('gtt_used'))}GiB)")
        log(f"  {'ctx':>7} | {'prefill tok/s':>16} | {'decode tok/s':>14}")
        for row in tgt.get("sweep", []):
            if row.get("skipped"):
                log(f"  {row['prompt_size']:>7} | {'(skipped: > n_ctx)':>16} |")
                continue
            pf = row.get("prefill_tps") or {}
            dc = row.get("decode_tps") or {}
            log(f"  {row['prompt_size']:>7} | {pf.get('median', '?'):>16} | {dc.get('median', '?'):>14}")


def _index_by_model(data):
    return {t["model_id"]: t for t in data.get("targets", []) if not t.get("error")}


def cmd_compare(files):
    loaded = []
    for f in files:
        for path in sorted(glob.glob(f)) or [f]:
            with open(path) as fh:
                d = json.load(fh)
            d["_file"] = os.path.basename(path)
            loaded.append(d)
    if len(loaded) < 2:
        log("compare needs >= 2 result files")
        return 1

    base = loaded[0]
    base_idx = _index_by_model(base)
    print(f"\nBaseline: {base['_file']}  (label={base.get('label')})")
    for other in loaded[1:]:
        oidx = _index_by_model(other)
        print(f"Compared: {other['_file']}  (label={other.get('label')})\n")
        for model, bt in base_idx.items():
            ot = oidx.get(model)
            if not ot:
                print(f"  {model}: not present in {other['_file']}\n")
                continue
            mb, ma = bt.get("mem_before", {}), ot.get("mem_before", {})
            print(f"  == {model} ==")
            print(f"     regime: carve {_gib(mb.get('vram_total'))}GiB -> {_gib(ma.get('vram_total'))}GiB | "
                  f"gtt_used {_gib(mb.get('gtt_used'))}GiB -> {_gib(ma.get('gtt_used'))}GiB")
            print(f"     {'ctx':>7} | {'prefill tok/s (a->b, d%)':>30} | {'decode tok/s (a->b, d%)':>30}")
            brows = {r["prompt_size"]: r for r in bt.get("sweep", [])}
            orows = {r["prompt_size"]: r for r in ot.get("sweep", [])}
            for size in sorted(set(brows) | set(orows)):
                print(f"     {size:>7} | {_cell(brows.get(size), orows.get(size), 'prefill_tps'):>30} | "
                      f"{_cell(brows.get(size), orows.get(size), 'decode_tps'):>30}")
            print()
    return 0


def _cell(brow, orow, key):
    def med(row):
        if not row or row.get("skipped"):
            return None
        s = row.get(key) or {}
        return s.get("median")
    a, b = med(brow), med(orow)
    if a is None and b is None:
        return "-"
    if a is None or b is None:
        return f"{a} -> {b}"
    delta = (b - a) / a * 100 if a else 0.0
    return f"{a:.1f} -> {b:.1f} ({delta:+.1f}%)"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_run(args):
    targets = args.targets or DEFAULT_TARGETS
    for t in targets:
        if t not in NODES:
            log(f"unknown target '{t}' (known: {', '.join(NODES)})")
            return 1
    sizes = [int(s) for s in args.sizes.split(",")] if args.sizes else DEFAULT_SIZES

    result = {
        "tool_version": TOOL_VERSION,
        "label": args.label,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "params": {"sizes": sizes, "n_predict": args.n_predict,
                   "runs": args.runs, "warmup": args.warmup, "port": args.port},
        "targets": [],
    }
    for name in targets:
        result["targets"].append(
            run_target(name, args.port, sizes, args.n_predict,
                       args.runs, args.warmup, args.timeout)
        )

    print_run_summary(result)

    out_dir = Path(args.out) if args.out else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{args.label}-{stamp}.json"
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2)
    log(f"\nwrote {out_path}")
    print(str(out_path))  # stdout: the result path (scriptable)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="LLM carve-vs-GTT tok/s benchmark")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the benchmark against target nodes")
    r.add_argument("--targets", nargs="*", help=f"node names (default: {' '.join(DEFAULT_TARGETS)})")
    r.add_argument("--port", type=int, default=8080)
    r.add_argument("--label", default="before-bios")
    r.add_argument("--sizes", help=f"comma prompt sizes (default: {','.join(map(str, DEFAULT_SIZES))})")
    r.add_argument("--n-predict", type=int, default=256, dest="n_predict")
    r.add_argument("--runs", type=int, default=3, help="measured runs per size")
    r.add_argument("--warmup", type=int, default=1)
    r.add_argument("--timeout", type=float, default=600.0, help="per-request seconds")
    r.add_argument("--out", help=f"output dir (default: {DEFAULT_OUT})")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="compare >=2 result JSON files")
    c.add_argument("files", nargs="+", help="result JSON files (globs ok)")
    c.set_defaults(func=lambda a: cmd_compare(a.files))

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
