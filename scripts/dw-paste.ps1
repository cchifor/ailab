<#
dw-paste — paste the Windows clipboard image into a remote dev-worker agent session.

Saves the clipboard image (or a copied image FILE from Explorer) locally, scp's it to the worker's
/workspace/<user>/pastes/ landing dir (created by the dev_worker role, aged out after 14 days),
preloads the remote tmux paste buffer with the path, and puts the same path on the local clipboard.

In the remote session:
  - Claude Code: press prefix+] in tmux — the bracketed-pasted image path auto-attaches as
    [Image #N]. (Native Ctrl+V cannot work over SSH: Claude Code reads the clipboard via
    xclip/wl-paste, which need a display server.)
  - Codex: paste the path the same way, or run `codex -i <path>`.

Usage:
  powershell -File scripts\dw-paste.ps1                       # defaults to dev-worker-5
  powershell -File scripts\dw-paste.ps1 -SshTarget c4@192.168.0.9
#>
param(
    [string]$SshTarget = 'c4@192.168.0.12',
    [string]$RemoteDir = '/workspace/c4/pastes'
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$local = $null

$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($img) {
    $local = Join-Path $env:TEMP "dw-paste-$stamp.png"
    $img.Save($local, [System.Drawing.Imaging.ImageFormat]::Png)
} else {
    $pick = [System.Windows.Forms.Clipboard]::GetFileDropList() |
        Where-Object { $_ -match '\.(png|jpe?g|gif|webp|bmp)$' } | Select-Object -First 1
    if (-not $pick) {
        Write-Error 'clipboard holds neither an image nor an image file'
    }
    $local = Join-Path $env:TEMP ("dw-paste-$stamp" + [IO.Path]::GetExtension($pick))
    Copy-Item -LiteralPath $pick -Destination $local
}

$remotePath = "$RemoteDir/" + [IO.Path]::GetFileName($local)
scp -q $local "${SshTarget}:$remotePath"
if ($LASTEXITCODE -ne 0) { Write-Error "scp to ${SshTarget}:$RemoteDir failed" }
Remove-Item $local

# Automatic (unnamed) buffer lands on top of the buffer stack, which is what prefix+] pastes.
ssh $SshTarget "tmux set-buffer '$remotePath'"
if ($LASTEXITCODE -ne 0) { Write-Warning 'tmux buffer not set (no tmux server?); use the clipboard path instead' }

Set-Clipboard -Value $remotePath
Write-Output "uploaded -> ${SshTarget}:$remotePath"
Write-Output 'remote tmux: prefix+] pastes the path; it is also on your local clipboard'
