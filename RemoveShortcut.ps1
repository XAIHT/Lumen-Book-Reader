# ═══════════════════════════════════════════════════════════════════
#   Lumen Book Reader - shortcut remover
#   Created by Angela López Mendoza · @angelahack1
# ═══════════════════════════════════════════════════════════════════
#
# The mirror image of CreateShortcut.ps1. It removes every shortcut Lumen has
# ever created, in every location the creator has ever written to - including
# names used by earlier releases - because a leftover .lnk pointing at a
# deleted folder is the most visible way an uninstall can look unfinished.
#
# A shortcut is only deleted when its TargetPath actually points into Lumen's
# install directory (or the file is unreadable, which means it is already
# broken). A same-named shortcut the user made for something else survives.

$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }

Write-Host ""
Write-Host "Lumen Book Reader - shortcut remover" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

# The install directory this uninstall is about. The manifest is the truth;
# the script's own folder is the fallback, because RemoveShortcut.ps1 is
# always run from inside the installation.
$installDir = $scriptDir
$configPath = Join-Path $scriptDir "LumenInstall.json"
if (Test-Path $configPath) {
    try {
        $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($config.InstallDir) { $installDir = $config.InstallDir }
    } catch { }
}
try { $installDir = (Resolve-Path $installDir -ErrorAction Stop).Path } catch { }
Write-Host "Install directory: $installDir" -ForegroundColor White

# Every name Lumen has shipped a shortcut under.
$shortcutNames = @(
    "Lumen Book Reader.lnk",
    "Lumen.lnk",
    "Lumen Reader.lnk"
)

$locations = @(
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("Programs"),
    $installDir,
    $scriptDir
) | Where-Object { $_ } | Select-Object -Unique

$shell = $null
try { $shell = New-Object -ComObject WScript.Shell } catch { }

function Test-IsLumenShortcut([string]$linkPath) {
    # Unreadable .lnk => already broken => safe to remove.
    if (-not $script:shell) { return $true }
    try {
        $sc = $script:shell.CreateShortcut($linkPath)
        $target = $sc.TargetPath
    } catch {
        return $true
    }
    if (-not $target) { return $true }
    if (-not (Test-Path $target)) { return $true }
    try {
        $resolved = (Resolve-Path $target -ErrorAction Stop).Path
    } catch {
        return $true
    }
    return $resolved.StartsWith($script:installDir, [StringComparison]::OrdinalIgnoreCase)
}

$removed = 0
foreach ($loc in $locations) {
    foreach ($name in $shortcutNames) {
        $link = Join-Path $loc $name
        if (-not (Test-Path $link)) { continue }
        if (-not (Test-IsLumenShortcut $link)) {
            Write-Host "[--] Left alone (points elsewhere): $link" -ForegroundColor DarkGray
            continue
        }
        try {
            Remove-Item $link -Force -ErrorAction Stop
            Write-Host "[OK] Removed: $link" -ForegroundColor Green
            $removed++
        } catch {
            Write-Host "[WARN] Could not remove $link - $_" -ForegroundColor Yellow
        }
    }
}

if ($removed -eq 0) {
    Write-Host "[--] No Lumen shortcuts were found." -ForegroundColor DarkGray
}

# ── Tell the shell, so the desktop icon disappears immediately ──────────────
Write-Host ""
Write-Host "Refreshing the Windows shell..." -ForegroundColor Cyan
$csharpCode = 'using System; using System.Runtime.InteropServices; public class LumenShellNotifyRm { [DllImport("shell32.dll", CharSet = CharSet.Auto, SetLastError = true)] public static extern void SHChangeNotify(int wEventId, int uFlags, IntPtr dwItem1, IntPtr dwItem2); }'
try {
    Add-Type -TypeDefinition $csharpCode -ErrorAction Stop
    [LumenShellNotifyRm]::SHChangeNotify(0x08000000, 0x0000, [IntPtr]::Zero, [IntPtr]::Zero)
    Write-Host "  [OK] Notified Windows Shell (SHChangeNotify)" -ForegroundColor Green
} catch {
    Write-Host "  [WARN] Add-Type unavailable - using rundll32 fallback" -ForegroundColor Yellow
    & rundll32.exe user32.dll, UpdatePerUserSystemParameters 2>$null
}

Write-Host ""
Write-Host "$removed shortcut(s) removed." -ForegroundColor Green
exit 0
