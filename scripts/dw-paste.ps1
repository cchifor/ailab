<#
dw-paste — hand the Windows clipboard (a screenshot, or files copied in Explorer) to a remote
dev-worker agent session over SSH.

Saves a clipboard image as PNG, or takes every file copied in Explorer (any type: images, PDFs,
documents, archives …), scp's each to the worker's pastes directory (created by the dev_worker role,
0700, aged out after 14 days), loads one tmux paste buffer per file, and puts the path(s) on the local
clipboard.

In the remote tmux, with the agent's pane focused:
  - prefix+]  pastes the FIRST file's path. tmux pastes it bracketed, so Claude Code and Codex turn an
              image path into [Image #N]; documents arrive as a path the agent can read.
  - prefix+=  opens tmux's buffer list to paste the others, one per paste (Codex only attaches an image
              when the paste holds exactly one path).
Nothing is pasted automatically: with several clients attached, "the active pane" is ambiguous.

The web terminal (https://dwN.chifor.me) needs none of this: paste, drop or use its paperclip.
docs/runbooks/dev-workers.md § "Pasting images and files into agents".

Usage:
  powershell -File scripts\dw-paste.ps1                       # defaults to dev-worker-4
  powershell -File scripts\dw-paste.ps1 -SshTarget c4@192.168.0.9
#>
param(
    [string]$SshTarget = 'c4@192.168.0.11',
    [string]$RemoteDir = '/workspace/c4/pastes',
    [int]$MaxMB = 64
)
$ErrorActionPreference = 'Stop'
# Pin the pwsh 7.3+ native-command preference so a caller profile can't make ssh/scp exits
# terminating before the intended handling below (a no-op assignment on Windows PowerShell 5.1).
$PSNativeCommandUseErrorActionPreference = $false

# RemoteDir is interpolated into remote shell commands; the stamped basenames are safe by
# construction, so this validation is what keeps every remote path quote- and injection-safe.
if ($RemoteDir -notmatch '^[A-Za-z0-9/._-]+$') {
    Write-Error "RemoteDir may only contain [A-Za-z0-9/._-] (it is used inside remote shell commands)"
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

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

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$staged = @()   # local copies to upload, in order
try {
    $img = [System.Windows.Forms.Clipboard]::GetImage()
    if ($img) {
        $local = Join-Path $env:TEMP "dw-paste-$stamp-$([guid]::NewGuid().ToString('N').Substring(0,4))-screenshot.png"
        $img.Save($local, [System.Drawing.Imaging.ImageFormat]::Png)
        $staged += $local
    } else {
        $drop = @([System.Windows.Forms.Clipboard]::GetFileDropList()) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
        if (-not $drop) {
            Write-Error 'clipboard holds neither an image nor copied files (folders are skipped)'
        }
        foreach ($f in $drop) {
            $size = (Get-Item -LiteralPath $f).Length
            if ($size -gt $MaxMB * 1MB) { Write-Warning "skipping $f ($([math]::Round($size / 1MB)) MB > $MaxMB MB)"; continue }
            $local = Join-Path $env:TEMP ("dw-paste-$stamp-$([guid]::NewGuid().ToString('N').Substring(0,4))-" + (Get-SafeName (Split-Path $f -Leaf)))
            Copy-Item -LiteralPath $f -Destination $local
            $staged += $local
        }
        if (-not $staged) { Write-Error 'nothing left to upload' }
    }

    # One scp for all files: one SSH handshake, and the order is preserved on the remote side.
    scp -q $staged "${SshTarget}:$RemoteDir/"
    if ($LASTEXITCODE -ne 0) { Write-Error "scp to ${SshTarget}:$RemoteDir failed" }
} finally {
    # Screenshots and documents regularly hold credentials and internal UIs — never leave the
    # staging copies in %TEMP%, including on failure.
    $staged | ForEach-Object { Remove-Item -LiteralPath $_ -ErrorAction SilentlyContinue }
}

$remotePaths = @($staged | ForEach-Object { "$RemoteDir/" + [IO.Path]::GetFileName($_) })  # @(): stays an array for one file

# Loaded in reverse, so the newest automatic buffer — the one prefix+] pastes — is the first file.
$cmds = ($remotePaths[($remotePaths.Count - 1)..0] | ForEach-Object { "tmux set-buffer '$_'" }) -join ' && '
ssh $SshTarget $cmds
if ($LASTEXITCODE -ne 0) { Write-Warning 'tmux buffers not set (no tmux server?); use the paths on your clipboard instead' }

Set-Clipboard -Value ($remotePaths -join ' ')
$remotePaths | ForEach-Object { Write-Output "uploaded -> ${SshTarget}:$_" }
if ($remotePaths.Count -gt 1) {
    Write-Output 'remote tmux: prefix+] pastes the first path, prefix+= picks the others (one per paste)'
} else {
    Write-Output 'remote tmux: prefix+] pastes the path; it is also on your local clipboard'
}
