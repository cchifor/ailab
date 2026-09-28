# dw-paste — paste screenshots and files into dev-worker agents over SSH

Claude Code and Codex run **on the dev-workers**, inside tmux. A screenshot on your laptop's
clipboard means nothing to them. Over SSH the terminal pastes the laptop's text instead. With a
Snipping Tool capture, that text is a local path like `C:\Users\…\ScreenClip\{GUID}.png`, which
does not exist on the worker. These helpers make **Ctrl+Shift+V** (Cmd+Shift+V on a Mac) do the
right thing in a terminal attached to a dev-worker:

1. read the clipboard: an image (screenshot) or copied files (any type, several at once);
2. `scp` them to the worker's `/workspace/<user>/pastes/` (0700; the worker ages them out after
   14 days);
3. paste each **worker** path with the terminal's own paste — a bracketed paste into the focused
   tmux pane, **one path per paste** (Codex only attaches an image when a paste holds exactly one
   path), each followed by a typed space; **never Enter**;
4. put your clipboard back.

Claude Code and Codex turn a pasted image path into `[Image #N]`; PDFs and documents arrive as a
path the agent can read. With only text on the clipboard, or in any window that is not a dev-worker
terminal, Ctrl+Shift+V pastes (or does) exactly what it did before.

The **web terminal** (`https://dwN.chifor.me`) needs none of this: paste, drop or 📎 there
(`docs/runbooks/dev-workers.md` § "Pasting images and files into agents").

## How a window is recognised

The dev-worker tmux sets the terminal title to

```
<host> [<user>@<ip>] <session>:<window>        e.g.  dev-worker-3 [c4@192.168.0.10] main:claude
```

(`ansible/roles/dev_worker/templates/tmux.conf.j2`, `set-titles`). The `[user@ip]` marker is the
contract: a focused window of a known terminal app whose title carries it is a dev-worker terminal,
and the marker says where to upload. No per-machine configuration, and new workers work as soon as
the role has run on them. Keep the marker's shape if you change the title.

If your terminal shows its own title instead of the program's, allow application titles (Windows
Terminal: profile → *Suppress title changes* off, which is the default).

## Install

All three need `ssh`/`scp` with **key authentication** to the worker (`ssh-copy-id c4@<ip>`) and
its host key already accepted (`ssh c4@<ip>` once). The helpers run `scp` with `BatchMode=yes` and
fail instead of prompting.

### Windows — `dw-paste-windows.ps1`

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dw-paste\dw-paste-windows.ps1 -Install
```

This copies itself to `%LOCALAPPDATA%\dw-paste\`, registers a logon Scheduled Task named `dw-paste`
and starts it now. It runs hidden with a tray icon (right-click → *Open log* / *Quit*) and registers
the global hotkey Ctrl+Shift+V. When the press is not for a dev-worker window, it re-sends
Ctrl+Shift+V to the focused app, so Windows Terminal's text paste and other apps' Ctrl+Shift+V keep
working.

- `-Uninstall` removes the task and stops the helper.
- `-Once -Target c4@192.168.0.10` uploads the clipboard and puts the worker path(s) on your
  clipboard (manual mode).
- `-SelfTest -Target c4@192.168.0.10` uploads and prints the paths; it sends no keystrokes and
  leaves the clipboard alone.
- Log: `%LOCALAPPDATA%\dw-paste\dw-paste.log`. Recognised terminals: Windows Terminal, conhost,
  WezTerm, Alacritty, mintty (`-TerminalProcesses` to change).

### macOS — `dw-paste-macos.lua` (Hammerspoon)

```sh
brew install --cask hammerspoon      # then allow it under Privacy & Security → Accessibility
cp scripts/dw-paste/dw-paste-macos.lua ~/.hammerspoon/dw_paste.lua
echo 'require("dw_paste").start()' >> ~/.hammerspoon/init.lua   # then Hammerspoon → Reload Config
```

The hotkey is Cmd+Shift+V. Recognised terminals: Terminal.app, iTerm2, kitty, WezTerm, Alacritty,
Ghostty (`M.terminals` in the file). Terminal.app shows the program's title only if its profile's
*Window/Tab title* settings include it.

### Linux — `dw-paste-linux.sh`

```sh
sudo apt install xdotool xclip                         # X11
sudo apt install wl-clipboard wtype                    # Wayland
bash scripts/dw-paste/dw-paste-linux.sh --install-gnome   # GNOME: binds Ctrl+Shift+V
```

On other desktops, bind Ctrl+Shift+V to the script in the keyboard-shortcut settings (KDE: System
Settings → Shortcuts → Custom Shortcuts). The desktop then owns Ctrl+Shift+V, so the script also
does the ordinary paste: it copies the clipboard to PRIMARY and presses Shift+Insert, which
terminals and most applications treat as paste.

- **Wayland** does not let a program read another window's title. There the worker comes from
  `DW_PASTE_TARGET=c4@192.168.0.10` or `~/.config/dw-paste/target`, and any focused window counts.
- `--self-test c4@192.168.0.10` uploads the clipboard and prints the paths, with no keystrokes.
- Log: `~/.cache/dw-paste.log`.

## Tested

| | how | result |
|---|---|---|
| Windows | `-SelfTest`/`-Once` against dev-worker-4: a screenshot, and a PDF (spaces/parens in the name) + a PNG copied together; the `-Run` helper registering Ctrl+Shift+V and a second instance exiting | all pass. The keystroke injection into a live Windows Terminal is exercised by the operator's first press, not by an automated test. |
| Linux X11 | `scripts/dw-paste/test-linux.sh`: Ubuntu container with Xvfb, an xterm titled with the marker, a pane recording raw input, a stand-in scp | 7/7: screenshot and file-list upload; real run → `ESC[200~/workspace/c4/pastes/…png ESC[201~` + typed space, no Enter; clipboard restored; text pastes normally; no upload from an unmarked window |
| Linux Wayland | — | not tested (needs a compositor) |
| macOS | `luac5.4 -p` and a load test | syntax and module load only; Hammerspoon's APIs need a Mac |

## Troubleshooting

- **The worker path is pasted, but the agent treats it as text.** Claude Code and Codex attach an
  image only from a *pasted* path, and Codex only when the paste holds exactly one path. Run
  `bash ansible/roles/dev_worker/tests/check-agent-image-paste.sh` on the worker after agent
  upgrades.
- **Nothing happens / the old local path is pasted.** The window title lacks `[user@ip]`: the worker
  has not had the role's tmux change yet (`ansible-playbook dev-workers.yml -l <worker> -t tmux`),
  the terminal suppresses application titles, or you are outside tmux.
- **"Upload failed".** Check the log. Usually key auth, or an unaccepted host key: `ssh c4@<ip>`
  once by hand.
