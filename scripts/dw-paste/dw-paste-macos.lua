--[[
dw-paste for macOS (Hammerspoon): press Cmd+Shift+V in a terminal attached to a dev-worker and a
clipboard image (screenshot) or copied files are uploaded to the worker and their paths pasted into
the focused tmux pane, where Claude Code / Codex attach them. Anything else passes Cmd+Shift+V
through unchanged.

How it knows the worker: the dev-worker tmux sets the terminal title to
"<host> [<user>@<ip>] <session>:<window>" (ansible role dev_worker, tmux.conf.j2). Only a window of a
known terminal app whose title carries that [user@ip] marker is treated as a dev-worker terminal.

Install (https://www.hammerspoon.org — `brew install --cask hammerspoon`, then grant it
Accessibility in System Settings > Privacy & Security):
  cp scripts/dw-paste/dw-paste-macos.lua ~/.hammerspoon/dw_paste.lua
  echo 'require("dw_paste").start()' >> ~/.hammerspoon/init.lua
  then Hammerspoon > Reload Config.
Needs ssh/scp with key auth to the worker and its host key accepted (ssh to it once).
Terminal title: Terminal.app shows the program's title only if "Active process name"/"Window title"
settings allow it; iTerm2, kitty, WezTerm, Ghostty and Alacritty show it by default.
Docs: scripts/dw-paste/README.md.
--]]

local M = {}

M.terminals = {
  ["com.apple.Terminal"] = true,
  ["com.googlecode.iterm2"] = true,
  ["net.kovidgoyal.kitty"] = true,
  ["com.github.wez.wezterm"] = true,
  ["org.alacritty"] = true,
  ["com.mitchellh.ghostty"] = true,
}
M.maxBytes = 64 * 1024 * 1024
M.scp = "/usr/bin/scp"

local hotkey

local function log(msg) print("dw-paste: " .. msg) end

-- The [user@ip] marker of the focused terminal window, or nil.
local function target()
  local win = hs.window.focusedWindow()
  if not win then return nil end
  local app = win:application()
  if not app or not M.terminals[app:bundleID() or ""] then return nil end
  local user, ip = (win:title() or ""):match("%[([%l_][%w_.-]*)@(%d+%.%d+%.%d+%.%d+)%]")
  if user then return user, ip end
  return nil
end

-- Same shape as the web terminal's dw-upload names: [A-Za-z0-9._-], no leading dot or dash.
local function safeName(name)
  local stem, ext = name:match("^(.*)%.([^./]+)$")
  if not stem then stem, ext = name, "" end
  stem = stem:gsub("[^%w._-]+", "_"):gsub("^[._-]+", ""):sub(1, 64)
  ext = ext:gsub("[^%w]", ""):lower():sub(1, 9)
  if stem == "" then stem = "paste" end
  if ext == "" then ext = "bin" end
  return stem .. "." .. ext
end

local function passThrough()
  hotkey:disable()
  hs.eventtap.keyStroke({ "cmd", "shift" }, "v", 0)
  hs.timer.doAfter(0.1, function() hotkey:enable() end)
end

-- Local files to upload, staged under a private temp dir with safe, stamped names.
local function stage()
  local dir = (os.getenv("TMPDIR") or "/tmp/"):gsub("/?$", "/") .. "dw-paste-" .. hs.host.uuid()
  hs.fs.mkdir(dir)
  hs.execute(string.format("/bin/chmod 700 %q", dir))
  local stamp = os.date("%Y%m%d-%H%M%S")
  local out = {}
  local function add(src, name)
    local attr = hs.fs.attributes(src)
    if not attr or attr.mode ~= "file" then return end
    if attr.size > M.maxBytes then log("skipping " .. src .. " (over 64 MB)"); return end
    local dest = string.format("%s/dw-paste-%s-%04x-%s", dir, stamp, math.random(0, 0xffff), safeName(name))
    hs.execute(string.format("/bin/cp %q %q", src, dest))
    table.insert(out, dest)
  end
  local urls = hs.pasteboard.readURL(nil, true)
  if type(urls) == "table" then
    for _, u in ipairs(urls) do
      local url = type(u) == "table" and (u.url or u.filePath) or u
      local path = type(url) == "string" and url:match("^file://(.+)$")
      if path then
        path = path:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end)
        add(path, path:match("([^/]+)$") or "file")
      end
    end
  end
  if #out == 0 then
    local img = hs.pasteboard.readImage()
    if img then
      local dest = string.format("%s/dw-paste-%s-%04x-screenshot.png", dir, stamp, math.random(0, 0xffff))
      img:saveToFile(dest, "PNG")
      table.insert(out, dest)
    end
  end
  return out, dir
end

-- Paste each path with the terminal's own paste (bracketed, into the focused pane), one per paste,
-- each followed by a typed space; never Enter. Then put the user's clipboard back.
local function pastePaths(paths, saved)
  local i = 0
  local function nextOne()
    i = i + 1
    if i > #paths then
      hs.timer.doAfter(0.3, function() if saved then hs.pasteboard.writeAllData(saved) end end)
      return
    end
    hs.pasteboard.setContents(paths[i])
    hs.eventtap.keyStroke({ "cmd" }, "v", 0)
    hs.timer.doAfter(0.25, function()
      hs.eventtap.keyStrokes(" ")
      nextOne()
    end)
  end
  nextOne()
end

local function onHotkey()
  local user, ip = target()
  if not user then return passThrough() end
  local files, dir = stage()
  if #files == 0 then
    hs.execute(string.format("/bin/rm -rf %q", dir))
    return passThrough()
  end
  local saved = hs.pasteboard.readAllData()
  local remoteDir = "/workspace/" .. user .. "/pastes"
  local args = { "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8" }
  for _, f in ipairs(files) do table.insert(args, f) end
  table.insert(args, string.format("%s@%s:%s/", user, ip, remoteDir))
  log(string.format("upload %d file(s) -> %s@%s", #files, user, ip))
  hs.task.new(M.scp, function(code, _, stderr)
    hs.execute(string.format("/bin/rm -rf %q", dir)) -- screenshots hold credentials: never keep copies
    if code ~= 0 then
      hs.alert.show("dw-paste: upload failed (" .. (stderr or ""):gsub("%s+$", "") .. ")", 5)
      return
    end
    local remote = {}
    for _, f in ipairs(files) do table.insert(remote, remoteDir .. "/" .. f:match("([^/]+)$")) end
    pastePaths(remote, saved)
  end, args):start()
end

function M.start()
  if hotkey then hotkey:delete() end
  hotkey = hs.hotkey.bind({ "cmd", "shift" }, "v", onHotkey)
  log("Cmd+Shift+V bound")
  return M
end

function M.stop()
  if hotkey then hotkey:delete(); hotkey = nil end
end

return M
