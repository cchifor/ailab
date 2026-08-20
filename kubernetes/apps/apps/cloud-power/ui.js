/* cloud-power dashboard control — the real UI, served by cloud-power itself at /cloud-power/ui.js.
 *
 * WHY THIS IS NOT IN homepage's custom.js
 *   home.chifor.me sits behind Cloudflare, which caches by file extension. Homepage serves its
 *   config at /api/config/custom.js and sends NO Cache-Control, so Cloudflare applied its own
 *   max-age=14400 and served a 4-hour-stale copy — a UI change was invisible in the browser even
 *   though the origin, the ConfigMap and the pod all had the new bytes (verified: cf-cache-status
 *   HIT, Age 7167). The lab's Cloudflare token is scoped to DNS + Access only, so neither a purge
 *   nor a cache rule can be automated.
 *
 *   This file is served by cloud-power instead, which sets `Cache-Control: no-store` on every
 *   response — Cloudflare honours that and does not cache it. custom.js is now a fixed loader that
 *   never needs to change, so its own caching is harmless.
 *
 *   Bonus: editing this file rolls the cloud-power pod via the ConfigMap content hash, so UI
 *   changes no longer need a Homepage rollout restart either.
 */
(function () {
  var BASE = '/cloud-power';
  var ID = 'cloud-power-card';
  var pending = null, pendingTimer = null;

  function el(tag, css, txt) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (txt != null) e.textContent = txt;
    return e;
  }

  function btnCss(bg) {
    return 'font:inherit;font-weight:700;font-size:.75rem;letter-spacing:.04em;border:0;' +
      'border-radius:.4rem;padding:.4rem 1.3rem;cursor:pointer;color:#fff;background:' + bg;
  }

  function show(msg, text) { msg.style.display = 'block'; msg.textContent = text; }

  function build() {
    var card = el('div', 'background:#1e293b99;border:1px solid #33415588;border-radius:.5rem;' +
      'padding:1rem 1.15rem;font-size:.85rem;color:#e2e8f0;box-sizing:border-box');
    card.id = ID;

    var head = el('div', 'display:flex;align-items:center;gap:.6rem;margin-bottom:.7rem');
    head.appendChild(el('span', 'font-weight:600;letter-spacing:.02em', 'Cloud GPU'));
    var sum = el('span', 'color:#94a3b8;font-size:.78rem', 'checking...');
    head.appendChild(sum);
    card.appendChild(head);

    var dots = el('div', 'display:flex;gap:1.1rem;margin-bottom:.8rem;' +
      'font-family:ui-monospace,monospace;font-size:.78rem;flex-wrap:wrap');
    card.appendChild(dots);

    var row = el('div', 'display:flex;gap:.6rem;align-items:center');
    var bon = el('button', btnCss('#16a34a'), 'ON');
    var boff = el('button', btnCss('#dc2626'), 'OFF');
    row.appendChild(bon); row.appendChild(boff);
    card.appendChild(row);

    var msg = el('div', 'margin-top:.7rem;color:#94a3b8;font-size:.75rem;white-space:pre-wrap;display:none');
    card.appendChild(msg);

    bon.onclick = function () { wake(msg, sum); };
    boff.onclick = function () { off(msg, boff); };

    card._parts = { sum: sum, dots: dots, msg: msg, boff: boff };
    return card;
  }

  function refresh() {
    var card = document.getElementById(ID); if (!card) return;
    var p = card._parts;
    fetch(BASE + '/api/status', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        p.sum.textContent = s.up + '/' + s.total + ' up';
        p.dots.innerHTML = '';
        s.nodes.forEach(function (n) {
          var w = el('span', 'display:flex;align-items:center;gap:.35rem');
          w.appendChild(el('span', 'width:.55rem;height:.55rem;border-radius:50%;background:' +
            (n.up ? '#22c55e' : '#64748b')));
          w.appendChild(el('span', n.up ? 'color:#e2e8f0' : 'color:#64748b', n.name));
          p.dots.appendChild(w);
        });
      })
      .catch(function () { p.sum.textContent = 'unreachable'; });
  }

  function wake(msg, sum) {
    show(msg, 'sending wake packets...');
    fetch(BASE + '/api/wake', { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function () {
        show(msg, 'Magic packets sent. Nodes take about a minute to POST.');
        sum.textContent = 'waking...';
      })
      .catch(function (e) { show(msg, 'wake failed: ' + e); });
  }

  /* Two-step: the first click only REPORTS what would be stopped and arms a single-use,
   * time-boxed token issued by the server. A stray tap cannot power off a cluster mid-inference. */
  function off(msg, boff) {
    if (!pending) {
      show(msg, 'checking what is running...');
      fetch(BASE + '/api/shutdown/preflight', { method: 'POST', credentials: 'same-origin' })
        .then(function (r) { return r.json().then(function (p) { return { ok: r.ok, p: p }; }); })
        .then(function (res) {
          if (!res.ok) { show(msg, 'preflight refused:\n' + JSON.stringify(res.p, null, 1)); return; }
          var p = res.p;
          pending = p.confirm;
          var list = p.guests.length
            ? p.guests.map(function (g) { return '  ' + g.node + '  ' + g.type + ' ' + g.vmid + ' ' + g.name; }).join('\n')
            : '  (no running guests)';
          show(msg, 'WILL STOP:\n' + list + '\n\nClick OFF again within ' + p.expires_in + 's to confirm.');
          boff.textContent = 'CONFIRM';
          clearTimeout(pendingTimer);
          pendingTimer = setTimeout(function () {
            pending = null; boff.textContent = 'OFF'; show(msg, 'confirmation expired');
          }, (p.expires_in || 30) * 1000);
        })
        .catch(function (e) { show(msg, 'preflight failed: ' + e); });
      return;
    }
    clearTimeout(pendingTimer);
    var tok = pending; pending = null; boff.textContent = 'OFF';
    show(msg, 'shutting down...');
    fetch(BASE + '/api/shutdown', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ confirm: tok })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        show(msg, (d.results || []).map(function (x) { return x.node + ': ' + x.result; }).join('\n') ||
          JSON.stringify(d, null, 1));
      })
      .catch(function (e) { show(msg, 'shutdown failed: ' + e); });
  }

  /* Match the dashboard's left edge and width by MEASURING it at runtime rather than hard-coding a
   * container size: Homepage's Tailwind container is responsive and the `fullWidth` setting can
   * change it, so a fixed max-width would drift out of alignment. */
  function align(card) {
    var anchor = document.getElementById('information-widgets');
    var container = anchor && anchor.parentElement;
    var r = container && container.getBoundingClientRect();
    if (r && r.width > 200) {
      card.style.marginLeft = Math.round(r.left) + 'px';
      card.style.marginRight = '0';
      card.style.width = Math.round(r.width) + 'px';
      card.style.maxWidth = 'none';
    } else {
      // Anchor missing (layout changed): stay centred rather than jamming against the edge.
      card.style.margin = '0 auto';
      card.style.maxWidth = '1152px';
      card.style.width = 'calc(100% - 2rem)';
    }
  }

  function mount() {
    if (document.getElementById(ID)) return;
    var card = build();
    card.style.marginTop = '1.5rem';
    card.style.marginBottom = '2rem';
    /* AFTER the Next.js root, so it renders at the BOTTOM of the page in normal flow - and still
     * OUTSIDE React's tree, which is the only way it survives a re-render. An earlier version
     * inserted inside the root and reconciliation deleted it. */
    var root = document.getElementById('__next');
    if (root && root.parentNode) root.parentNode.insertBefore(card, root.nextSibling);
    else document.body.appendChild(card);
    align(card);
    refresh();
  }

  function start() {
    mount();
    console.info('[cloud-power] control mounted');
    setInterval(refresh, 15000);
    window.addEventListener('resize', function () {
      var c = document.getElementById(ID); if (c) align(c);
    });
    /* Re-align after React finishes painting the dashboard: the container's width is not final on
     * first paint, so a single measurement at mount can be wrong. */
    [250, 1000, 3000].forEach(function (d) {
      setTimeout(function () { var c = document.getElementById(ID); if (c) align(c); }, d);
    });
    /* Safety net only. The card lives outside React's tree so nothing should remove it; the id
     * check makes each callback a no-op, so this cannot loop on its own mutations. */
    new MutationObserver(function () { mount(); }).observe(document.body, { childList: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
