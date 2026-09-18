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
basic auth), scrolls the named row into view and FAILS when, in any panel of that row: the title
is ellipsised, a stat value wraps onto a second line, a table scrolls sideways, the panel says
"No data", or an --expect text is missing from the row. It writes a screenshot of the row. This is
the check the 2026-09-18 PR Reviewers cut would have failed (see gen-reporting-dashboard.py).

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
  // Every panel that sits between this row's title and the next row's title, top to bottom.
  const rows = Array.from(document.querySelectorAll('[data-testid^="data-testid dashboard-row-title-"]'));
  const row = rows.find(r => r.textContent.includes(rowText));
  if (!row) return null;
  const top = row.getBoundingClientRect().top + window.scrollY;
  const next = rows.map(r => r.getBoundingClientRect().top + window.scrollY).filter(y => y > top + 1).sort((a, b) => a - b)[0] ?? Infinity;
  const panels = Array.from(document.querySelectorAll('[data-testid^="data-testid Panel header "]'))
    .map(p => ({ el: p, box: p.getBoundingClientRect() }))
    .filter(p => p.box.top + window.scrollY > top && p.box.top + window.scrollY < next && p.box.height > 0);
  const overflows = el => Array.from(el.querySelectorAll('*')).some(e => {
    const s = getComputedStyle(e);
    return (s.overflowX === 'auto' || s.overflowX === 'scroll') && e.scrollWidth > e.clientWidth + 2;
  });
  const lines = el => {
    const r = document.createRange(); r.selectNodeContents(el);
    return new Set(Array.from(r.getClientRects()).map(x => Math.round(x.top))).size;
  };
  return panels.map(({ el, box }) => {
    const title = el.getAttribute('data-testid').replace('data-testid Panel header ', '');
    const titleEl = Array.from(el.querySelectorAll('h2, h6, [class*="title"]')).find(e => e.textContent.trim() === title.trim());
    const titleCut = titleEl ? titleEl.scrollWidth > titleEl.clientWidth + 1 : null;
    // Big text = a stat value. Anything rendered >= 20px that wraps onto two lines is a wrapped value.
    const wrapped = Array.from(el.querySelectorAll('div, span'))
      .filter(e => e.children.length === 0 && e.textContent.trim() && parseFloat(getComputedStyle(e).fontSize) >= 20)
      .filter(e => lines(e) > 1).map(e => e.textContent.trim());
    const headers = Array.from(el.querySelectorAll('[role="columnheader"]')).map(h => h.textContent.trim());
    const rowsN = Array.from(el.querySelectorAll('[role="row"]')).length - (headers.length ? 1 : 0);
    return { title, text: el.innerText, titleCut, wrapped, headers, rows: rowsN > 0 ? rowsN : 0,
             hscroll: overflows(el), loading: !!el.querySelector('[data-testid*="loading"], [aria-label*="loading" i]'),
             box: { x: box.left, y: box.top, w: box.width, h: box.height } };
  });
}
"""


def check(a):
    from playwright.sync_api import sync_playwright
    url = f"{a.url.rstrip('/')}/d/{a.uid}?orgId=1&kiosk"
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
        row = page.locator('[data-testid^="data-testid dashboard-row-title-"]', has_text=a.row).first
        row.wait_for(timeout=a.timeout * 1000)
        row.scroll_into_view_if_needed()
        # scroll_into_view centres the title; pin it to the top so the whole row (up to --height
        # pixels of it) is inside the viewport - panels outside it never mount (lazy rendering).
        page.evaluate("el => window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top - 8)",
                      row.element_handle())
        # Panels are lazy: give the row's panels time to mount and their queries to return, then
        # keep polling until nothing in the row is loading and every panel carries some text.
        deadline = time.time() + a.timeout
        panels = None
        while time.time() < deadline:
            panels = page.evaluate(JS_ROW_PANELS, [a.row])
            if panels and all(not x["loading"] and x["text"].strip() for x in panels):
                time.sleep(1.5)     # one more tick for late gauges / table cells
                panels = page.evaluate(JS_ROW_PANELS, [a.row])
                break
            time.sleep(1)
        if not panels:
            sys.exit(f"row {a.row!r} not found on {url}")
        if a.shot:
            xs = [x["box"]["x"] for x in panels]; ys = [x["box"]["y"] for x in panels]
            xe = [x["box"]["x"] + x["box"]["w"] for x in panels]; ye = [x["box"]["y"] + x["box"]["h"] for x in panels]
            clip = {"x": max(0, min(xs) - 4), "y": max(0, min(ys) - 40), "width": max(xe) - min(xs) + 8,
                    "height": min(a.height, max(ye)) - max(0, min(ys) - 40)}
            page.screenshot(path=a.shot, clip=clip)
        text = "\n".join(x["text"] for x in panels)
        browser.close()

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
        if x["loading"]:
            probs.append("still loading")
        if x["headers"]:
            report.append(f"        columns: {x['headers']}  rows: {x['rows']}")
        report.append(("  FAIL  " if probs else "  ok    ") + t + ("  <- " + "; ".join(probs) if probs else ""))
        failures += [f"{t}: {p}" for p in probs]
    for e in a.expect:
        if e not in text:
            failures.append(f"expected text missing from row: {e!r}")
    titles = [x["title"] for x in panels]
    for t in a.panel:
        if not any(t in title for title in titles):
            failures.append(f"panel missing from row: {t!r}")
    print(f"row {a.row!r}: {len(panels)} panels" + (f", screenshot {a.shot}" if a.shot else ""))
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
    c.add_argument("--height", type=int, default=2200, help="viewport height; tall so the whole row mounts")
    c.add_argument("--timeout", type=int, default=60)
    c.set_defaults(fn=check)
    a = ap.parse_args()
    if a.cmd in ("serve", "sync") and not a.dashboard:
        a.dashboard = [str(DEFAULT_DASHBOARD)]
    a.fn(a)


if __name__ == "__main__":
    main()
