<#
dw-paste for Windows: press Ctrl+Shift+V in a terminal attached to a dev-worker and a clipboard
image (screenshot) or copied files are uploaded to the worker and their paths pasted into the
focused tmux pane, where Claude Code / Codex attach them. Text on the clipboard pastes as usual.

How it knows the worker: the dev-worker tmux sets the terminal title to
"<host> [<user>@<ip>] <session>:<window>" (ansible role dev_worker, tmux.conf.j2). A window whose
title carries that [user@ip] marker is a dev-worker terminal; anything else is left alone.

What a press does:
  - focused window is a terminal whose title has [user@ip], and the clipboard holds an image or
    files: each file is scp'd to /workspace/<user>/pastes/ and its path is pasted, one path per paste
    (Codex only attaches an image when a paste holds exactly one path), each followed by a typed
    space; never Enter. Your clipboard is restored afterwards. If focus moved to another window
    while uploading, nothing is pasted: the worker path(s) are left on the clipboard instead.
  - anything else: Ctrl+Shift+V is passed through untouched (the terminal's own text paste, or
    whatever that key does in the focused app).

Usage (Windows PowerShell 5.1, the one that ships with Windows):
  powershell -File scripts\dw-paste\dw-paste-windows.ps1 -Install     # run at every logon + start now
  powershell -File scripts\dw-paste\dw-paste-windows.ps1 -Uninstall
  powershell -File scripts\dw-paste\dw-paste-windows.ps1 -Once -Target c4@192.168.0.10
                                     # upload the clipboard now, put the remote path(s) on the clipboard
  powershell -File scripts\dw-paste\dw-paste-windows.ps1 -SelfTest -Target c4@192.168.0.10
                                     # upload + report, no keystrokes, clipboard untouched

Needs: OpenSSH client (ssh/scp, built into Windows 10+) with key auth to the worker, and the host
key already accepted (ssh to it once). Log: %LOCALAPPDATA%\dw-paste\dw-paste.log.
Docs: scripts/dw-paste/README.md.
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Run,
    [switch]$Once,
    [switch]$SelfTest,
    [string]$Target = '',
    [string[]]$TerminalProcesses = @('WindowsTerminal', 'OpenConsole', 'conhost', 'wezterm-gui', 'alacritty', 'mintty'),
    [int]$MaxMB = 64
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$AppDir = Join-Path $env:LOCALAPPDATA 'dw-paste'
$LogFile = Join-Path $AppDir 'dw-paste.log'
$TaskName = 'dw-paste'
$MarkerRegex = '\[([a-z_][a-z0-9_.-]*)@(\d{1,3}(?:\.\d{1,3}){3})\]'
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null

function Write-Log([string]$msg) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $msg
    Add-Content -LiteralPath $LogFile -Value $line
    if (-not $Run) { Write-Output $msg }
}

# ---------------------------------------------------------------- install / uninstall
if ($Install) {
    $dest = Join-Path $AppDir 'dw-paste-windows.ps1'
    Copy-Item -LiteralPath $PSCommandPath -Destination $dest -Force
    $arg = "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dest`" -Run"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -like '*dw-paste-windows.ps1*-Run*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "installed: $dest runs at logon (task '$TaskName') and is running now; tray icon 'dw-paste'"
    return
}
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -like '*dw-paste-windows.ps1*-Run*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Output "uninstalled (task '$TaskName' removed, helper stopped); $AppDir left in place"
    return
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

public class DwHotkey : NativeWindow {
    [DllImport("user32.dll")] static extern bool RegisterHotKey(IntPtr hWnd, int id, uint mods, uint vk);
    [DllImport("user32.dll")] static extern bool UnregisterHotKey(IntPtr hWnd, int id);
    const int WM_HOTKEY = 0x0312;
    const uint MOD_CONTROL = 0x2, MOD_SHIFT = 0x4, MOD_NOREPEAT = 0x4000, VK_V = 0x56;
    public event EventHandler Pressed;
    public DwHotkey() { CreateHandle(new CreateParams()); }
    public bool Register() { return RegisterHotKey(Handle, 1, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_V); }
    public void Unregister() { UnregisterHotKey(Handle, 1); }
    protected override void WndProc(ref Message m) {
        if (m.Msg == WM_HOTKEY && Pressed != null) Pressed(this, EventArgs.Empty);
        base.WndProc(ref m);
    }
}

public static class DwWin {
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    const uint KEYUP = 0x2;
    public static IntPtr ForegroundHandle() { return GetForegroundWindow(); }
    public static string ForegroundTitle() {
        var sb = new StringBuilder(1024);
        GetWindowText(GetForegroundWindow(), sb, sb.Capacity);
        return sb.ToString();
    }
    public static uint ForegroundPid() {
        uint pid; GetWindowThreadProcessId(GetForegroundWindow(), out pid); return pid;
    }
    // Ctrl+Shift+V as the terminal sees it. The user may still be holding Ctrl+Shift; pressing them
    // again is harmless, and the synthetic key-ups only reset the logical state.
    public static void CtrlShiftV() {
        keybd_event(0x11, 0, 0, UIntPtr.Zero); keybd_event(0x10, 0, 0, UIntPtr.Zero);
        keybd_event(0x56, 0, 0, UIntPtr.Zero); keybd_event(0x56, 0, KEYUP, UIntPtr.Zero);
        keybd_event(0x10, 0, KEYUP, UIntPtr.Zero); keybd_event(0x11, 0, KEYUP, UIntPtr.Zero);
    }
    public static void Space() {
        keybd_event(0x20, 0, 0, UIntPtr.Zero); keybd_event(0x20, 0, KEYUP, UIntPtr.Zero);
    }
}
'@

# ---------------------------------------------------------------- shared helpers
# Same shape as the web terminal's dw-upload names: [A-Za-z0-9._-], no leading dot or dash.
function Get-SafeName([string]$name) {
    $stem = [IO.Path]::GetFileNameWithoutExtension($name) -replace '[^A-Za-z0-9._-]+', '_'
    $stem = $stem.TrimStart('.', '_', '-')
    if ($stem.Length -gt 64) { $stem = $stem.Substring(0, 64) }
    if (-not $stem) { $stem = 'paste' }
    $ext = ([IO.Path]::GetExtension($name) -replace '[^A-Za-z0-9]', '').ToLower()
    if ($ext.Length -gt 9) { $ext = $ext.Substring(0, 9) }
    if (-not $ext) { $ext = 'bin' }
    return "$stem.$ext"
}

function Invoke-WithClipboardRetry([scriptblock]$block) {
    for ($i = 0; $i -lt 10; $i++) {
        try { return & $block } catch { Start-Sleep -Milliseconds 50 }
    }
    return & $block
}

# What the clipboard holds that is worth uploading: @{ Image = <Image or $null>; Files = <string[]> }
function Get-ClipboardPayload {
    Invoke-WithClipboardRetry {
        $files = @()
        if ([System.Windows.Forms.Clipboard]::ContainsFileDropList()) {
            $files = @([System.Windows.Forms.Clipboard]::GetFileDropList() | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
        }
        $img = $null
        if ([System.Windows.Forms.Clipboard]::ContainsImage()) { $img = [System.Windows.Forms.Clipboard]::GetImage() }
        @{ Image = $img; Files = $files }
    }
}

# Upload what the clipboard holds; returns the remote paths in order. A snipping-tool screenshot
# carries BOTH a bitmap and the file it saved: the file wins (no re-encoding, one upload).
function Send-Payload($payload, [string]$target) {
    if ($target -notmatch '^([a-z_][a-z0-9_.-]*)@(\d{1,3}(?:\.\d{1,3}){3})$') { throw "bad target '$target' (want user@ipv4)" }
    $user = $Matches[1]
    $remoteDir = "/workspace/$user/pastes"
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $staged = @()
    try {
        if ($payload.Files.Count -gt 0) {
            foreach ($f in $payload.Files) {
                $size = (Get-Item -LiteralPath $f).Length
                if ($size -gt $MaxMB * 1MB) { Write-Log "skipping $f ($([math]::Round($size / 1MB)) MB > $MaxMB MB)"; continue }
                $local = Join-Path $env:TEMP ("dw-paste-$stamp-" + [guid]::NewGuid().ToString('N').Substring(0, 4) + '-' + (Get-SafeName (Split-Path $f -Leaf)))
                Copy-Item -LiteralPath $f -Destination $local
                $staged += $local
            }
        } elseif ($payload.Image) {
            $local = Join-Path $env:TEMP ("dw-paste-$stamp-" + [guid]::NewGuid().ToString('N').Substring(0, 4) + '-screenshot.png')
            $payload.Image.Save($local, [System.Drawing.Imaging.ImageFormat]::Png)
            $staged += $local
        }
        if (-not $staged) { throw 'nothing to upload' }
        # Windows PowerShell 5.1 turns ANY stderr line of a native command into a terminating error
        # under ErrorActionPreference=Stop (PSNativeCommandUseErrorActionPreference is pwsh 7.3+), so
        # a benign ssh warning would abort a successful upload. Judge scp by its exit code only.
        $eap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $out = & scp -q -o BatchMode=yes -o ConnectTimeout=8 $staged "${target}:$remoteDir/" 2>&1 | Out-String }
        finally { $ErrorActionPreference = $eap }
        if ($LASTEXITCODE -ne 0) { throw "scp to ${target}:$remoteDir failed: $($out.Trim())" }
        return @($staged | ForEach-Object { "$remoteDir/" + [IO.Path]::GetFileName($_) })
    } finally {
        # Screenshots and documents regularly hold credentials: never leave the copies in %TEMP%.
        $staged | ForEach-Object { Remove-Item -LiteralPath $_ -ErrorAction SilentlyContinue }
    }
}

# ---------------------------------------------------------------- one-shot modes
if ($Once -or $SelfTest) {
    if (-not $Target) { throw '-Target user@ip is required with -Once / -SelfTest' }
    $payload = Get-ClipboardPayload
    if (-not $payload.Image -and $payload.Files.Count -eq 0) { throw 'the clipboard holds neither an image nor files' }
    $paths = Send-Payload $payload $Target
    $paths | ForEach-Object { Write-Output "uploaded -> ${Target}:$_" }
    if ($Once) {
        Set-Clipboard -Value ($paths -join ' ')
        Write-Output 'the remote path(s) are on your clipboard; paste them into the agent one at a time'
    }
    return
}

if (-not $Run) {
    # No mode given: print the header above (usage + what a press does).
    $lines = Get-Content -LiteralPath $PSCommandPath
    $end = [array]::IndexOf($lines, '#>')
    $lines[1..($end - 1)] | Write-Output
    return
}

# ---------------------------------------------------------------- the hotkey helper (-Run)
$mutex = New-Object System.Threading.Mutex($false, 'Local\dw-paste-hotkey')
if (-not $mutex.WaitOne(0)) { exit 0 }  # already running

$hotkey = New-Object DwHotkey
$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = [System.Drawing.SystemIcons]::Application
$tray.Text = 'dw-paste: Ctrl+Shift+V uploads images/files to dev-workers'
$menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add('Open log', $null, { Start-Process notepad.exe $LogFile })
[void]$menu.Items.Add('Quit dw-paste', $null, { [System.Windows.Forms.Application]::Exit() })
$tray.ContextMenuStrip = $menu
$tray.Visible = $true

function Show-Note([string]$text, [System.Windows.Forms.ToolTipIcon]$icon) {
    $tray.ShowBalloonTip(4000, 'dw-paste', $text, $icon)
}

function Send-PassThrough {
    $hotkey.Unregister()
    try { [DwWin]::CtrlShiftV(); Start-Sleep -Milliseconds 80 } finally { [void]$hotkey.Register() }
}

function Paste-Path([string]$path) {
    Invoke-WithClipboardRetry { [System.Windows.Forms.Clipboard]::SetText($path) } | Out-Null
    Start-Sleep -Milliseconds 60
    Send-PassThrough                      # the terminal's own paste: bracketed, into the focused pane
    Start-Sleep -Milliseconds 250         # let the terminal read the clipboard before it changes again
    [DwWin]::Space()                      # typed, not pasted: terminals trim whitespace-only pastes
}

# What gets put back after the paths were pasted: the image, the copied files, any text.
function Save-Clipboard {
    Invoke-WithClipboardRetry {
        $saved = New-Object System.Windows.Forms.DataObject
        $any = $false
        if ([System.Windows.Forms.Clipboard]::ContainsImage()) { $saved.SetImage([System.Windows.Forms.Clipboard]::GetImage()); $any = $true }
        if ([System.Windows.Forms.Clipboard]::ContainsFileDropList()) { $saved.SetFileDropList([System.Windows.Forms.Clipboard]::GetFileDropList()); $any = $true }
        if ([System.Windows.Forms.Clipboard]::ContainsText()) { $saved.SetText([System.Windows.Forms.Clipboard]::GetText()); $any = $true }
        if ($any) { $saved } else { $null }
    }
}

$handler = {
    try {
        $hwnd = [DwWin]::ForegroundHandle()
        $title = [DwWin]::ForegroundTitle()
        $proc = ''
        try { $proc = (Get-Process -Id ([DwWin]::ForegroundPid())).ProcessName } catch { }
        $target = $null
        if (($TerminalProcesses -contains $proc) -and ($title -match $MarkerRegex)) { $target = "$($Matches[1])@$($Matches[2])" }
        $payload = if ($target) { Get-ClipboardPayload } else { $null }
        if (-not $target -or (-not $payload.Image -and $payload.Files.Count -eq 0)) {
            Send-PassThrough
            return
        }
        $saved = Save-Clipboard
        $label = if ($payload.Files.Count -gt 0) { "$($payload.Files.Count) file(s)" } else { 'screenshot' }
        Write-Log "upload $label -> $target"
        $paths = Send-Payload $payload $target
        foreach ($p in $paths) {
            # The upload took a moment: if focus moved (another window, another worker's tab), pasting
            # now would type into the wrong place. Hand the paths over on the clipboard instead.
            $nowTitle = [DwWin]::ForegroundTitle()
            if ([DwWin]::ForegroundHandle() -ne $hwnd -or -not ($nowTitle -match $MarkerRegex) -or "$($Matches[1])@$($Matches[2])" -ne $target) {
                Invoke-WithClipboardRetry { [System.Windows.Forms.Clipboard]::SetText($paths -join ' ') } | Out-Null
                Write-Log "focus changed during upload; paths left on the clipboard: $($paths -join ' ')"
                Show-Note 'Focus changed during the upload: nothing was pasted. The worker path(s) are on your clipboard.' ([System.Windows.Forms.ToolTipIcon]::Warning)
                return
            }
            Paste-Path $p
        }
        Write-Log ("pasted " + ($paths -join ' '))
        Start-Sleep -Milliseconds 200
        if ($saved) { Invoke-WithClipboardRetry { [System.Windows.Forms.Clipboard]::SetDataObject($saved, $true) } | Out-Null }
    } catch {
        Write-Log "error: $($_.Exception.Message)"
        Show-Note ("Upload failed: " + $_.Exception.Message) ([System.Windows.Forms.ToolTipIcon]::Error)
    }
}
$hotkey.add_Pressed($handler)

if (-not $hotkey.Register()) {
    Write-Log 'Ctrl+Shift+V is already registered by another program; exiting'
    Show-Note 'Ctrl+Shift+V is taken by another program; dw-paste is not running.' ([System.Windows.Forms.ToolTipIcon]::Error)
    Start-Sleep -Seconds 5
    exit 1
}
Write-Log "running (pid $PID); terminals: $($TerminalProcesses -join ', ')"
try {
    [System.Windows.Forms.Application]::Run()
} finally {
    $hotkey.Unregister()
    $tray.Visible = $false
    $tray.Dispose()
    $mutex.ReleaseMutex()
    Write-Log 'stopped'
}
