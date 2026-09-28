// dw_paste.js: paste, drop or pick files in the dev-worker web terminal and hand them to the agent.
//
// Loaded by the terminal page Caddy serves (web_gate.yml appends a <script> tag to ttyd's own
// index). Each file is PUT to /_dw/upload (files/dw_upload.py), which stores it under
// /workspace/<user>/pastes/ and answers its path; the path is then pasted into the terminal with
// xterm's term.paste(), i.e. as a bracketed paste into whichever tmux pane has focus. Claude Code and
// Codex both turn a bracketed-pasted image path into [Image #N]; Codex only does so when the paste
// holds ONE path, so every path is its own paste and the separating space is a paste of its own.
// Nothing ever sends Enter.
//
// Ctrl+V: xterm turns it into ^V and cancels the browser's paste, so clipboard images never reach
// the page. Outside macOS (where Cmd+V is the browser paste) Ctrl+V is handed back to the browser;
// the cost is ^V (literal-next) in the web terminal only. Text pastes still go through xterm.
//
// docs/runbooks/dev-workers.md § "Pasting images and files into agents".
(function () {
  "use strict";

  var MAX_BYTES = 64 * 1024 * 1024; // must match dw_upload.py MAX_BYTES and Caddy's request_body
  var AUTO_PASTE_MS = 3000; // a slower upload may finish after focus moved: ask before pasting
  var IS_MAC = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || "");
  var hookedTerm = null;
  var queue = Promise.resolve();

  // ---- UI: a toast stack and a paperclip button ------------------------------------------------
  var style = document.createElement("style");
  style.textContent =
    "#dw-toasts{position:fixed;right:12px;bottom:12px;z-index:2147483647;display:flex;flex-direction:column;gap:6px;max-width:min(380px,calc(100vw - 24px));font:13px/1.4 system-ui,sans-serif}" +
    ".dw-toast{background:#1f2937;color:#f9fafb;border:1px solid #4b5563;border-radius:6px;padding:8px 10px;box-shadow:0 2px 8px rgba(0,0,0,.4);word-break:break-all}" +
    ".dw-toast.err{border-color:#ef4444}.dw-toast.ok{border-color:#22c55e}" +
    ".dw-toast button{margin:6px 6px 0 0;background:#374151;color:#f9fafb;border:1px solid #6b7280;border-radius:4px;padding:3px 8px;cursor:pointer;font:inherit}" +
    "#dw-clip{position:fixed;top:8px;right:12px;z-index:2147483646;width:34px;height:34px;border-radius:17px;border:1px solid #6b7280;background:rgba(31,41,55,.85);color:#f9fafb;font-size:18px;cursor:pointer;opacity:.55}" +
    "#dw-clip:hover,#dw-clip:focus{opacity:1}";
  document.head.appendChild(style);

  var stack = document.createElement("div");
  stack.id = "dw-toasts";
  document.body.appendChild(stack);

  function toast(text, kind, ms) {
    var el = document.createElement("div");
    el.className = "dw-toast" + (kind ? " " + kind : "");
    var msg = document.createElement("div");
    msg.textContent = text;
    el.appendChild(msg);
    stack.appendChild(el);
    el.set = function (t, k, after) {
      msg.textContent = t;
      el.className = "dw-toast" + (k ? " " + k : "");
      if (after) setTimeout(function () { el.remove(); }, after);
    };
    el.button = function (label, fn) {
      var b = document.createElement("button");
      b.textContent = label;
      b.addEventListener("click", function () { fn(); el.remove(); });
      el.appendChild(b);
      return b;
    };
    if (ms) setTimeout(function () { el.remove(); }, ms);
    return el;
  }

  var picker = document.createElement("input");
  picker.type = "file";
  picker.multiple = true;
  picker.style.display = "none";
  picker.addEventListener("change", function () {
    enqueue(Array.prototype.slice.call(picker.files || []));
    picker.value = "";
  });
  document.body.appendChild(picker);

  var clip = document.createElement("button");
  clip.id = "dw-clip";
  clip.type = "button";
  clip.textContent = "📎";
  clip.title = "Upload files to the agent (Ctrl+V and drag-and-drop work too)";
  clip.setAttribute("aria-label", clip.title);
  clip.addEventListener("click", function () { picker.click(); });
  document.body.appendChild(clip);

  // ---- terminal hook ------------------------------------------------------------------------------
  function hook() {
    var term = window.term;
    if (!term || term === hookedTerm) return;
    hookedTerm = term;
    if (!IS_MAC && typeof term.attachCustomKeyEventHandler === "function") {
      term.attachCustomKeyEventHandler(function (ev) {
        var plainCtrlV = ev.type === "keydown" && ev.ctrlKey && !ev.shiftKey && !ev.altKey && !ev.metaKey &&
          (ev.key === "v" || ev.key === "V");
        return !plainCtrlV; // false = xterm ignores it and does NOT cancel it: the browser pastes
      });
    }
  }
  hook();
  setInterval(hook, 500); // window.term appears after the socket opens and may be replaced

  function pastePath(path) {
    var term = window.term;
    if (!term) return false;
    term.paste(path);
    term.paste(" ");
    term.focus();
    return true;
  }

  // ---- collecting files ---------------------------------------------------------------------------
  function filesFrom(dt) {
    var out = [];
    if (!dt) return out;
    var i;
    if (dt.files && dt.files.length) {
      for (i = 0; i < dt.files.length; i++) out.push(dt.files[i]);
    } else if (dt.items) {
      for (i = 0; i < dt.items.length; i++) {
        if (dt.items[i].kind === "file") {
          var f = dt.items[i].getAsFile();
          if (f) out.push(f);
        }
      }
    }
    return out;
  }

  function carriesFiles(dt) {
    return !!dt && Array.prototype.indexOf.call(dt.types || [], "Files") !== -1;
  }

  // Capture phase on window: runs before xterm's own handlers, which only understand text.
  window.addEventListener("paste", function (ev) {
    var files = filesFrom(ev.clipboardData);
    if (!files.length) return; // plain text: xterm pastes it as usual
    ev.preventDefault();
    ev.stopImmediatePropagation();
    enqueue(files);
  }, true);
  window.addEventListener("dragover", function (ev) {
    if (!carriesFiles(ev.dataTransfer)) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    ev.dataTransfer.dropEffect = "copy";
  }, true);
  window.addEventListener("drop", function (ev) {
    var files = filesFrom(ev.dataTransfer);
    if (!files.length) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    enqueue(files);
  }, true);

  // ---- uploading ----------------------------------------------------------------------------------
  function enqueue(files) {
    files.forEach(function (f) {
      queue = queue.then(function () { return upload(f); }, function () { return upload(f); });
    });
  }

  function human(n) {
    return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";
  }

  function upload(file) {
    var label = (file.name || "clipboard image") + " (" + human(file.size) + ")";
    if (!file.size) { toast(label + ": empty file, not uploaded", "err", 6000); return Promise.resolve(); }
    if (file.size > MAX_BYTES) { toast(label + ": larger than 64 MB, not uploaded", "err", 8000); return Promise.resolve(); }
    var note = toast("Uploading " + label + "…");
    var started = Date.now();
    return fetch("/_dw/upload?name=" + encodeURIComponent(file.name || ""), {
      method: "PUT",
      body: file,
      headers: { "X-DW-Upload": "1", "Content-Type": file.type || "application/octet-stream" },
      credentials: "same-origin",
      redirect: "manual", // an expired Access session answers with a redirect to the login page
      cache: "no-store"
    }).then(function (resp) {
      if (resp.type === "opaqueredirect" || resp.status === 0) {
        note.set(label + ": your login has expired. Reload the page, then paste again.", "err");
        note.button("Reload", function () { location.reload(); });
        return;
      }
      if (resp.status === 401) {
        note.set(label + ": not logged in. Reload the page, then paste again.", "err");
        return;
      }
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok || !data.path) {
          note.set(label + ": upload failed (" + resp.status + (data.error ? ", " + data.error : "") + ")", "err", 12000);
          return;
        }
        if (Date.now() - started <= AUTO_PASTE_MS && document.hasFocus() && pastePath(data.path)) {
          note.set("Pasted " + data.path, "ok", 5000);
        } else {
          note.set("Uploaded " + data.path, "ok");
          note.button("Paste path", function () { pastePath(data.path); });
          note.button("Dismiss", function () {});
        }
      });
    }).catch(function (err) {
      note.set(label + ": upload failed (" + (err && err.message ? err.message : "network error") + ")", "err", 12000);
    });
  }
})();
