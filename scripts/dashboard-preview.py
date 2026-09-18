#!/usr/bin/env python3
"""Render a generated dashboard in a LOCAL Grafana and check one row of it with Playwright.

    python scripts/dashboard-preview.py serve                       # Grafana on :3033, live Prometheus
    python scripts/dashboard-preview.py sync                        # after regenerating, while serve runs
    python scripts/dashboard-preview.py check --row "PR Reviewers" --shot reviewers.png \\
        --expect realjaynesage@gmail.com --ignore "Reviewer Errors"

`serve` runs the SAME Grafana version the cluster runs (the OSS zip, downloaded once into --home),
anonymous Admin, provisioned with the ConfigMap's dashboard JSON and one Prometheus datasource at
--prometheus (default: the prometheus-lan NodePort), so every panel renders against the live series
without touching the cluster's Grafana or any credential. After an edit: re-run gen-*-dashboard.py,
then `sync` (copies the ConfigMap's JSON into the running instance; the provider re-reads it
within 10s) and reload the page.

`check` opens the dashboard (the local one, or --url https://... with --auth user:pass for HTTP
basic auth), reads the row's panel inventory from the dashboard model, pages through the row so
every lazy panel mounts, waits for the row to settle, and FAILS when a panel of the model never
mounted or never showed content, or when in any panel: the title is ellipsised, a stat value
wraps, a table scrolls sideways, the panel says "No data", or an --expect text is missing from
the row. It writes a screenshot of the whole row. This is the check the 2026-09-18 PR Reviewers
cut would have failed (see gen-reporting-dashboard.py).

Requires: python -m pip install playwright && python -m playwright install chromium
"""
import argparse
import io
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml"
DEFAULT_HOME = pathlib.Path.home() / ".ailab-grafana-preview"
PROM_LAN = "http://192.168.0.41:30090"       # prometheus-lan NodePort (CLAUDE.md inventory)
GRAFANA_VERSION = "13.0.2"                   # docker.io/grafana/grafana tag in kube-prometheus-stack.yaml


# ───────────────────────── serve ─────────────────────────
def _zip_url(version):
    plat = {"Windows": "windows-amd64", "Linux": "linux-amd64", "Darwin": "darwin-amd64"}[platform.system()]
    return f"https://dl.grafana.com/oss/release/grafana-{version}.{plat}.zip"


def _grafana_bin(home, version):
    # The zip's top folder is grafana-<ver>/ (13.x) or grafana-v<ver>/ (older releases).
    root = next((d for d in (home / f"grafana-{version}", home / f"grafana-v{version}") if d.is_dir()),
                home / f"grafana-{version}")
    exe = root / "bin" / ("grafana.exe" if platform.system() == "Windows" else "grafana")
    return root, exe


def _unpack(home, version, zip_path):
    root, exe = _grafana_bin(home, version)
    if exe.exists():
        return root, exe
    if zip_path is None:
        url = _zip_url(version)
        print(f"downloading {url}")
        data = urllib.request.urlopen(url, timeout=900).read()
        zf = zipfile.ZipFile(io.BytesIO(data))
    else:
        zf = zipfile.ZipFile(zip_path)
    print(f"unpacking into {home}")
    home.mkdir(parents=True, exist_ok=True)
    zf.extractall(home)
    if not exe.exists():
        sys.exit(f"unpacked, but {exe} is missing - zip layout changed?")
    if platform.system() != "Windows":
        exe.chmod(0o755)
    return root, exe


def _dashboard_json(path):
    """The ConfigMap's data values. gen-*-dashboard.py writes the ConfigMap as JSON (a valid YAML
    subset), so json.load reads it without a YAML dependency."""
    cm = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in cm["data"].items() if k.endswith(".json")}


def sync(a):
    inst = pathlib.Path(a.home) / "instance"
    (inst / "dashboards").mkdir(parents=True, exist_ok=True)
    for path in a.dashboard:
        for name, body in _dashboard_json(pathlib.Path(path)).items():
            (inst / "dashboards" / name).write_text(body, encoding="utf-8")
            print(f"provisioned {name} from {path}")


def serve(a):
    home = pathlib.Path(a.home)
    root, exe = _unpack(home, a.version, a.zip)
    inst = home / "instance"
    for d in ("data", "logs", "plugins", "provisioning/datasources", "provisioning/dashboards", "dashboards"):
        (inst / d).mkdir(parents=True, exist_ok=True)
    sync(a)
    (inst / "provisioning/datasources/preview.yaml").write_text(
        "apiVersion: 1\ndatasources:\n"
        f"  - {{ name: Prometheus, type: prometheus, uid: preview-prom, url: {a.prometheus}, "
        "access: proxy, isDefault: true }\n", encoding="utf-8")
    (inst / "provisioning/dashboards/preview.yaml").write_text(
        "apiVersion: 1\nproviders:\n"
        "  - name: preview\n    type: file\n    updateIntervalSeconds: 10\n"
        f"    options: {{ path: {(inst / 'dashboards').as_posix()} }}\n", encoding="utf-8")
    ini = inst / "preview.ini"
    ini.write_text(
        f"[server]\nhttp_port = {a.port}\nhttp_addr = 127.0.0.1\n"
        f"[paths]\ndata = {(inst / 'data').as_posix()}\nlogs = {(inst / 'logs').as_posix()}\n"
        f"plugins = {(inst / 'plugins').as_posix()}\nprovisioning = {(inst / 'provisioning').as_posix()}\n"
        "[auth.anonymous]\nenabled = true\norg_role = Admin\n"
        "[auth]\ndisable_login_form = true\n"
        "[analytics]\nreporting_enabled = false\ncheck_for_updates = false\n"
        "[news]\nnews_feed_enabled = false\n", encoding="utf-8")
    print(f"grafana {a.version} on http://127.0.0.1:{a.port}/ (anonymous admin, Ctrl-C to stop)")
    sys.stdout.flush()
    sys.exit(subprocess.call([str(exe), "server", "--homepath", str(root), "--config", str(ini)]))


# ───────────────────────── check ─────────────────────────
JS_ROW_PANELS = """
([rowText]) => {
  // Every mounted panel that sits between this row's title and the next row's title.
  const rows = Array.from(document.querySelectorAll('[data-testid^="data-testid dashboard-row-title-"]'))
    .map(r => ({ r, top: r.getBoundingClientRect().top + window.scrollY }));
  const row = rows.find(x => x.r.textContent.includes(rowText));
  if (!row) return null;
  const next = Math.min(...rows.map(x => x.top).filter(y => y > row.top + 1), Infinity);
  const panels = Array.from(document.querySelectorAll('[data-testid^="data-testid Panel header "]'))
    .map(p => ({ el: p, box: p.getBoundingClientRect() }))
    .filter(p => p.box.height > 0)
    .filter(p => { const y = p.box.top + window.scrollY; return y > row.top && y < next; });
  const overflows = el => Array.from(el.querySelectorAll('*')).some(e => {
    const s = getComputedStyle(e);
    return (s.overflowX === 'auto' || s.overflowX === 'scroll') && e.scrollWidth > e.clientWidth + 2;
  });
  const lines = el => {
    const r = document.createRange(); r.selectNodeContents(el);
    return new Set(Array.from(r.getClientRects()).map(x => Math.round(x.top))).size;
  };
  return {
    rowTop: row.top, nextTop: next === Infinity ? null : next,
    pageBottom: document.documentElement.scrollHeight, viewBottom: window.scrollY + window.innerHeight,
    panels: panels.map(({ el, box }) => {
      const title = el.getAttribute('data-testid').replace('data-testid Panel header ', '');
      const titleEl = Array.from(el.querySelectorAll('h2, h6, [class*="title"]')).find(e => e.textContent.trim() === title.trim());
      const text = el.innerText;
      // What the panel shows BESIDES its title: a stat's number, a table's cells, "No data".
      const content = text.split('\\n').map(l => l.trim()).filter(l => l && l !== title.trim()).join('\\n');
      // A drawn chart counts too: uPlot paints a timeline's rows and labels on a canvas, no DOM text.
      const graphic = Array.from(el.querySelectorAll('canvas')).some(c => c.width > 0 && c.height > 0);
      return {
        title, text, content, graphic,
        titleCut: titleEl ? titleEl.scrollWidth > titleEl.clientWidth + 1 : null,
        // Big text = a stat value. Anything rendered >= 20px that spans two lines has wrapped.
        wrapped: Array.from(el.querySelectorAll('div, span'))
          .filter(e => e.children.length === 0 && e.textContent.trim() && parseFloat(getComputedStyle(e).fontSize) >= 20)
          .filter(e => lines(e) > 1).map(e => e.textContent.trim()),
        headers: Array.from(el.querySelectorAll('[role="columnheader"]')).map(h => h.textContent.trim()),
        rows: Math.max(0, el.querySelectorAll('[role="row"]').length - (el.querySelector('[role="columnheader"]') ? 1 : 0)),
        hscroll: overflows(el),
        loading: !!el.querySelector('[data-testid*="loading" i], [aria-label*="loading" i], [class*="skeleton" i]'),
        box: { x: box.left, y: box.top, w: box.width, h: box.height },
      };
    }),
  };
}
"""


def _row_inventory(page, base, uid, row_text):
    """The titles of every panel the dashboard MODEL puts in this row, so a panel that never
    mounts is a failure rather than an absence (reviewer-codex on ailab#784). A collapsed row
    nests its panels; an expanded one is followed by them in the flat list."""
    r = page.request.get(f"{base}/api/dashboards/uid/{uid}")
    if not r.ok:
        sys.exit(f"cannot read the dashboard model: HTTP {r.status} from {base}/api/dashboards/uid/{uid}")
    titles, inside, collapsed = [], False, False
    for p in r.json()["dashboard"]["panels"]:
        if p.get("type") == "row":
            if inside:
                break
            if row_text in p.get("title", ""):
                inside, collapsed = True, bool(p.get("collapsed"))
                titles += [q["title"] for q in p.get("panels") or []]
            continue
        if inside:
            titles.append(p["title"])
    if not inside:
        sys.exit(f"row {row_text!r} is not in dashboard {uid!r}")
    return titles, collapsed


def check(a):
    from playwright.sync_api import sync_playwright
    base = a.url.rstrip("/")
    url = f"{base}/d/{a.uid}?orgId=1&kiosk"
    creds = None
    if a.auth:
        u, _, pw = a.auth.partition(":")
        creds = {"username": u, "password": pw}
    failures, report = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": a.width, "height": a.height}, http_credentials=creds,
                                  ignore_https_errors=a.insecure)
        page = ctx.new_page()
        page.goto(url, wait_until="load", timeout=a.timeout * 1000)
        expected, collapsed = _row_inventory(page, base, a.uid, a.row)
        row = page.locator('[data-testid^="data-testid dashboard-row-title-"]', has_text=a.row).first
        row.wait_for(timeout=a.timeout * 1000)
        if collapsed:
            row.click()
            page.wait_for_timeout(500)

        def pin():
            # scroll_into_view centres the title; the row must start at the top of the viewport.
            page.evaluate("el => window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top - 8)",
                          row.element_handle())

        # 1. Page through the whole row: panels mount lazily as they enter the viewport, and
        #    the row may be taller than any viewport we start with. Ends at the next row's
        #    title or the bottom of the page; that is the row's extent.
        pin()
        st = page.evaluate(JS_ROW_PANELS, [a.row])
        for _ in range(200):
            end = st["nextTop"] if st["nextTop"] is not None else st["pageBottom"]
            if st["viewBottom"] >= end - 1:
                break
            page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            page.wait_for_timeout(800)
            st = page.evaluate(JS_ROW_PANELS, [a.row])
        extent = end - st["rowTop"]
        # 2. Then the whole row in ONE viewport for the read and the screenshot.
        page.set_viewport_size({"width": a.width, "height": int(min(extent + 80, 8000))})
        pin()
        # 3. Settle: every inventory panel mounted, every panel showing something beyond its
        #    title with no loading marker, and all of that unchanged across two consecutive
        #    polls - not a fixed sleep (reviewer-codex on ailab#784).
        deadline, prev, settled = time.time() + a.timeout, None, False
        while time.time() < deadline:
            st = page.evaluate(JS_ROW_PANELS, [a.row])
            byt = {x["title"]: x for x in st["panels"]}
            ready = (all(t in byt for t in expected)
                     and all((x["content"] or x["graphic"]) and not x["loading"] for x in byt.values()))
            sig = tuple(sorted((t, x["content"]) for t, x in byt.items()))
            if ready and sig == prev:
                settled = True
                break
            prev = sig if ready else None
            time.sleep(1)
        panels = st["panels"]
        if a.shot and panels:
            ys = [x["box"]["y"] for x in panels]
            ye = [x["box"]["y"] + x["box"]["h"] for x in panels]
            top = max(0, min(ys) - 40)
            page.screenshot(path=a.shot, clip={"x": 0, "y": top, "width": a.width,
                                               "height": min(page.viewport_size["height"], max(ye)) - top})
        text = "\n".join(x["text"] for x in panels)
        browser.close()

    titles = [x["title"] for x in panels]
    if not panels:
        failures.append(f"row {a.row!r} found, but no panel mounted under it within {a.timeout}s "
                        f"(model lists {len(expected)}: {expected})")
    for t in expected:
        if t not in titles:
            failures.append(f"panel in the model but not mounted: {t!r}")
    if not settled:
        stuck = [x["title"] for x in panels if not (x["content"] or x["graphic"]) or x["loading"]]
        failures.append(f"row did not settle within {a.timeout}s; unready: {stuck}")
    ignored = [re.compile(i) for i in a.ignore]
    for x in panels:
        t = x["title"]
        if any(i.search(t) for i in ignored):
            report.append(f"  skip  {t}")
            continue
        probs = []
        if x["titleCut"]:
            probs.append("title ellipsised")
        if x["titleCut"] is None:
            probs.append("title element not found")
        if x["wrapped"]:
            probs.append("value wraps: " + " | ".join(x["wrapped"]))
        if x["hscroll"]:
            probs.append("scrolls sideways")
        if re.search(r"\bNo data\b", x["text"]):
            probs.append("shows 'No data'")
        if not (x["content"] or x["graphic"]) or x["loading"]:
            probs.append("no content")
        if x["headers"]:
            report.append(f"        columns: {x['headers']}  rows: {x['rows']}")
        report.append(("  FAIL  " if probs else "  ok    ") + t + ("  <- " + "; ".join(probs) if probs else ""))
        failures += [f"{t}: {p}" for p in probs]
    for e in a.expect:
        if e not in text:
            failures.append(f"expected text missing from row: {e!r}")
    for t in a.panel:
        if not any(t in title for title in titles):
            failures.append(f"panel missing from row: {t!r}")
    print(f"row {a.row!r}: {len(panels)} of {len(expected)} panels mounted, extent {int(extent)}px"
          + (f", screenshot {a.shot}" if a.shot and panels else ""))
    print("\n".join(report))
    if failures:
        print("\nFAILED:\n  " + "\n  ".join(failures))
        sys.exit(1)
    print("\nOK")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run a local Grafana provisioned with the generated dashboard")
    s.add_argument("--dashboard", action="append", default=None, help="ConfigMap YAML (repeatable)")
    s.add_argument("--prometheus", default=PROM_LAN)
    s.add_argument("--home", default=str(DEFAULT_HOME))
    s.add_argument("--version", default=GRAFANA_VERSION)
    s.add_argument("--zip", default=None, help="an already-downloaded grafana-<ver>.<plat>.zip")
    s.add_argument("--port", type=int, default=3033)
    s.set_defaults(fn=serve)
    y = sub.add_parser("sync", help="re-copy the generated dashboard JSON into the running preview")
    y.add_argument("--dashboard", action="append", default=None, help="ConfigMap YAML (repeatable)")
    y.add_argument("--home", default=str(DEFAULT_HOME))
    y.set_defaults(fn=sync)
    c = sub.add_parser("check", help="open the dashboard with Playwright and check one row")
    c.add_argument("--url", default="http://127.0.0.1:3033")
    c.add_argument("--uid", default="ailab-reporting")
    c.add_argument("--row", required=True, help="substring of the row title")
    c.add_argument("--auth", default=os.environ.get("GRAFANA_AUTH"), help="user:password (HTTP basic)")
    c.add_argument("--insecure", action="store_true", help="ignore TLS errors")
    c.add_argument("--expect", action="append", default=[], help="text that must appear in the row (repeatable)")
    c.add_argument("--ignore", action="append", default=[], help="panel title regex to skip (repeatable)")
    c.add_argument("--panel", action="append", default=[], help="panel title (substring) that must be in the row")
    c.add_argument("--shot", default=None, help="write a PNG of the row here")
    c.add_argument("--width", type=int, default=1920)
    c.add_argument("--height", type=int, default=1400, help="viewport height for the first pass; "
                   "the final read resizes it to the row")
    c.add_argument("--timeout", type=int, default=60)
    c.set_defaults(fn=check)
    a = ap.parse_args()
    if a.cmd in ("serve", "sync") and not a.dashboard:
        a.dashboard = [str(DEFAULT_DASHBOARD)]
    a.fn(a)


if __name__ == "__main__":
    main()
