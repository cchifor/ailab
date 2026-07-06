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
DEFAULT_SIZES = [512, 4096, 7680]     # prompt tokens; matches the committed baseline (n_ctx 8192)
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
def build_corpus(min_chars: int) -> str:
    """A fixed, deterministic corpus large enough to slice the biggest prompt from.
    Same text every run => byte-identical prompts before/after the BIOS change.
    Callers size min_chars to the largest sweep point so no prompt is under-built."""
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


def tokenize_corpus(endpoint: str, corpus: str, timeout: float):
    """Tokenize the corpus ONCE per target; return a normalized list of int ids, or
    None if the server's /tokenize is unavailable (caller falls back to the char path)."""
    tok = _request(endpoint + "/tokenize", {"content": corpus}, timeout=min(timeout, 60))
    tokens = tok.get("tokens") if isinstance(tok, dict) else None
    if not isinstance(tokens, list):
        return None
    # tokens may be ints or {"id":..} depending on build; normalize to ints
    return [t["id"] if isinstance(t, dict) else t for t in tokens]


def build_prompt(endpoint, corpus, corpus_tokens, n_tokens, timeout):
    """Return (prompt_text|None, method). Prefer an exact-token prompt by slicing the
    pre-tokenized corpus and /detokenize-ing it; fall back to a char heuristic if the
    tokenizer is unavailable. Returns (None, 'corpus_too_short') if neither path can
    reach n_tokens — the caller marks the row failed rather than silently under-sizing
    and mislabelling it as the requested size."""
    if corpus_tokens is not None:
        if len(corpus_tokens) < n_tokens:
            return None, "corpus_too_short"
        det = _request(endpoint + "/detokenize",
                       {"tokens": corpus_tokens[:n_tokens]}, timeout=min(timeout, 60))
        text = det.get("content") if isinstance(det, dict) else None
        if isinstance(text, str) and text.strip():
            return text, "tokenize"
    approx = int(n_tokens * 3.8)
    if approx > len(corpus):
        return None, "corpus_too_short"
    return corpus[:approx], "chars"


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

    if n_ctx is None:
        log("  ! /props gave no n_ctx — cannot verify prompts fit the context window; "
            "sizes run as-is and any truncation is flagged per row")
        res["n_ctx_unknown"] = True

    # Build + tokenize the corpus ONCE per target, sized to the largest sweep point.
    max_size = max(sizes) if sizes else 0
    corpus = build_corpus(max(140_000, max_size * 6))
    corpus_tokens = tokenize_corpus(endpoint, corpus, timeout)
    sweep = []
    for size in sizes:
        row = {"prompt_size": size}
        if n_ctx and size + n_predict + 64 > n_ctx:
            row["skipped"] = f"size+{n_predict}+64 > n_ctx({n_ctx})"
            log(f"  [{size:>6} tok] skipped: exceeds n_ctx {n_ctx}")
            sweep.append(row)
            continue

        prompt, method = build_prompt(endpoint, corpus, corpus_tokens, size, timeout)
        row["prompt_method"] = method
        if prompt is None:
            row["error"] = f"could not build a {size}-token prompt ({method})"
            log(f"  [{size:>6} tok] ERROR: {row['error']}")
            sweep.append(row)
            continue
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
        row["prompt_n"] = ok[0].get("prompt_n") if ok else None
        row["decode_tps"] = _stats([m["decode_tps"] for m in ok])
        row["prefill_tps"] = _stats([m["prefill_tps"] for m in ok])
        row["runs"] = measured
        # Sanity: prompt_n should track the requested size. A large shortfall means the
        # server truncated the prompt (it exceeded the real n_ctx) or the char fallback
        # mis-sized it — flag the row rather than trust a measurement of the wrong length.
        pn = row["prompt_n"]
        if isinstance(pn, int) and abs(pn - size) > max(64, int(0.1 * size)):
            row["warning"] = f"prompt_n {pn} != requested {size} (possible truncation/undersize)"
            log(f"      ! {row['warning']}")
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


def _index_by_node(data):
    # Key by node: the stable identity across before/after runs (the same node reloads
    # the same model). model_id is displayed and a change between runs is flagged.
    return {t["node"]: t for t in data.get("targets", []) if not t.get("error")}


def cmd_compare(files):
    loaded, seen = [], set()
    for f in files:
        matches = sorted(glob.glob(f))
        if not matches and os.path.isfile(f):
            matches = [f]
        if not matches:
            log(f"no files match '{f}' — skipping")   # don't open a literal missing path
            continue
        for path in matches:
            if path in seen:
                continue
            seen.add(path)
            with open(path) as fh:
                d = json.load(fh)
            d["_file"] = os.path.basename(path)
            loaded.append(d)
    if len(loaded) < 2:
        log("compare needs >= 2 result files")
        return 1

    # Baseline = earliest run by started_at, independent of arg/glob order.
    loaded.sort(key=lambda d: (d.get("started_at") or "", d["_file"]))
    base = loaded[0]
    base_idx = _index_by_node(base)
    print(f"\nBaseline: {base['_file']}  (label={base.get('label')}, started {base.get('started_at')})")
    for other in loaded[1:]:
        oidx = _index_by_node(other)
        print(f"Compared: {other['_file']}  (label={other.get('label')}, started {other.get('started_at')})\n")
        for node in sorted(set(base_idx) | set(oidx)):
            bt, ot = base_idx.get(node), oidx.get(node)
            if not bt or not ot:
                where = "baseline only" if bt else "compared only"
                print(f"  {node}: present in {where} — skipped\n")
                continue
            bmodel, omodel = bt.get("model_id"), ot.get("model_id")
            hdr = f"  == {node}: {bmodel} =="
            if bmodel != omodel:
                hdr += f"  WARN model changed: {bmodel} -> {omodel}"
            print(hdr)
            mb, ma = bt.get("mem_before", {}), ot.get("mem_before", {})
            print(f"     regime: carve {_gib(mb.get('vram_total'))}GiB -> {_gib(ma.get('vram_total'))}GiB | "
                  f"gtt_used {_gib(mb.get('gtt_used'))}GiB -> {_gib(ma.get('gtt_used'))}GiB")
            print(f"     {'ctx':>7} | {'prefill tok/s (a->b, d%)':>30} | {'decode tok/s (a->b, d%)':>30}")
            brows = {r["prompt_size"]: r for r in bt.get("sweep", [])}
            orows = {r["prompt_size"]: r for r in ot.get("sweep", [])}
            for size in sorted(set(brows) | set(orows)):
                brow, orow = brows.get(size), orows.get(size)
                warn = _prompt_mismatch(brow, orow)
                print(f"     {size:>7} | {_cell(brow, orow, 'prefill_tps'):>30} | "
                      f"{_cell(brow, orow, 'decode_tps'):>30}{warn}")
            print()
    return 0


def _prompt_mismatch(brow, orow):
    """Flag when the two runs did not use a comparable prompt (different tokenizer path
    or drifted prompt_n) — a % delta across mismatched prompts is apples-to-oranges."""
    if not brow or not orow or brow.get("skipped") or orow.get("skipped"):
        return ""
    bm, om = brow.get("prompt_method"), orow.get("prompt_method")
    if bm != om:
        return f"   WARN method {bm} vs {om}"
    bn, on = brow.get("prompt_n"), orow.get("prompt_n")
    if isinstance(bn, int) and isinstance(on, int) and abs(bn - on) > max(8, int(0.02 * max(bn, on))):
        return f"   WARN prompt_n {bn} vs {on}"
    return ""


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
    if args.sizes:
        try:
            sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
        except ValueError as exc:
            log(f"invalid --sizes '{args.sizes}': {exc}")
            return 1
        if not sizes:
            log("no valid sizes in --sizes")
            return 1
    else:
        sizes = DEFAULT_SIZES
    expects = [e.strip() for e in args.expect.split(",")] if args.expect else []

    result = {
        "tool_version": TOOL_VERSION,
        "label": args.label,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "params": {"sizes": sizes, "n_predict": args.n_predict,
                   "runs": args.runs, "warmup": args.warmup, "port": args.port},
        "targets": [],
    }
    for i, name in enumerate(targets):
        res = run_target(name, args.port, sizes, args.n_predict,
                         args.runs, args.warmup, args.timeout)
        if expects and not res.get("error"):
            exp = expects[i] if i < len(expects) else expects[-1]
            mid = res.get("model_id") or ""
            if exp and exp.lower() not in mid.lower():
                log(f"  WARN {name}: loaded model '{mid}' does not match --expect '{exp}'")
                res["expect_mismatch"] = {"expected": exp, "got": mid}
        result["targets"].append(res)

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
    # Runs on the Windows host (cp1252 console/pipe) — force UTF-8 so non-ASCII output
    # never raises UnicodeEncodeError and tees/pipes are consistent.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
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
    r.add_argument("--expect", help="comma list aligned to --targets (last value reused); "
                   "warn if a node's model_id lacks its token — guards against benchmarking "
                   "the wrong model (e.g. the daily driver instead of a heavyweight)")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="compare >=2 result JSON files")
    c.add_argument("files", nargs="+", help="result JSON files (globs ok)")
    c.set_defaults(func=lambda a: cmd_compare(a.files))

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
