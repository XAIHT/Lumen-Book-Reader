#Requires -Version 5.1
<#
.SYNOPSIS
    Removes every Windows registration written by register_associations.ps1.

.DESCRIPTION
    The exact mirror image of register_associations.ps1. Where the registrar
    writes conditionally - only the extensions the user ticked - this script
    removes UNCONDITIONALLY: it walks the full type table and deletes anything
    Lumen could ever have written. An uninstall must be complete even when
    LumenInstall.json is missing, truncated, or describes an older selection.

    Two rules keep the clear-down safe for the rest of the machine:

      * Only keys Lumen created are deleted. The shared extension keys
        (HKCU:\Software\Classes\.pdf) survive; we remove our VALUE from their
        OpenWithProgids list, nothing more.

      * The (Default) value of an extension is cleared only when it still
        names one of OUR ProgIDs. If Acrobat or Edge holds .pdf, we leave it.

    Everything is HKCU, so no administrator rights are needed, and a key that
    is already absent counts as success.

.NOTES
    Mirror image: register_associations.ps1. Every key removed here is written
    there. Keep the two in step.
#>

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Best effort: the manifest tells us the executable name, but the defaults are
# correct for every release we have ever shipped, so a missing file is fine.
$exeName = "Lumen.exe"
$configPath = Join-Path $scriptDir "LumenInstall.json"
if (Test-Path $configPath) {
    try {
        $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($config.Executable) { $exeName = $config.Executable }
    } catch {
        Write-Host "[--] LumenInstall.json unreadable - using built-in defaults." -ForegroundColor DarkGray
    }
}

# The SAME table as the registrar. Adding a format means editing both files.
$typeTable = @(
    [pscustomobject]@{ Ext = '.epub'; ProgId = 'Lumen.EpubBook' },
    [pscustomobject]@{ Ext = '.pdf';  ProgId = 'Lumen.PdfDocument' }
)

# ProgIDs from earlier naming schemes. Leaving one behind means a dead entry
# in the user's "Open with" menu forever, so they are swept every time.
$staleProgIds = @('Lumen.Book', 'LumenReader.EpubBook', 'LumenReader.PdfDocument')

$appKey        = "HKCU:\Software\Classes\Applications\$exeName"
$capRoot       = "HKCU:\Software\XAIHT\Lumen Book Reader"
$registeredKey = "HKCU:\Software\RegisteredApplications"
$appPathsKey   = "HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\$exeName"
$regAppName    = "Lumen Book Reader"

$csharpCode = 'using System; using System.Runtime.InteropServices; public class LumenShellNotify { [DllImport("shell32.dll", CharSet = CharSet.Auto, SetLastError = true)] public static extern void SHChangeNotify(int wEventId, int uFlags, IntPtr dwItem1, IntPtr dwItem2); }'

function Remove-KeyIfPresent([string]$path, [string]$label) {
    if (Test-Path $path) {
        Remove-Item -Path $path -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  [OK] Removed $label" -ForegroundColor Green
    } else {
        Write-Host "  [--] $label not present" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "Lumen Book Reader - file association clear-down" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# ── 1) ProgIDs, current and historical ───────────────────────────────────────
foreach ($t in $typeTable) {
    Remove-KeyIfPresent "HKCU:\Software\Classes\$($t.ProgId)" "ProgID $($t.ProgId)"
}
foreach ($stale in $staleProgIds) {
    $staleKey = "HKCU:\Software\Classes\$stale"
    if (Test-Path $staleKey) {
        Remove-Item -Path $staleKey -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  [OK] Removed stale ProgID: $stale" -ForegroundColor Green
    }
}

# ── 2) Per-extension entries - our values only, never the shared key ────────
$allProgIds = @($typeTable | ForEach-Object { $_.ProgId }) + $staleProgIds

foreach ($t in $typeTable) {
    $extKey = "HKCU:\Software\Classes\$($t.Ext)"
    $feKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$($t.Ext)"

    foreach ($owpPath in @("$extKey\OpenWithProgids", "$feKey\OpenWithProgids")) {
        if (-not (Test-Path $owpPath)) { continue }
        # NOTE: not $pid - that is a read-only PowerShell automatic variable
        # (the current process id) and assigning to it aborts the script.
        foreach ($progIdName in $allProgIds) {
            $existing = Get-ItemProperty -Path $owpPath -Name $progIdName -ErrorAction SilentlyContinue
            if ($existing) {
                Remove-ItemProperty -Path $owpPath -Name $progIdName -Force -ErrorAction SilentlyContinue
                Write-Host "  [OK] Removed $progIdName from $owpPath" -ForegroundColor Green
            }
        }
    }

    # Clear the default ONLY when it is still ours.
    if (Test-Path $extKey) {
        $current = (Get-ItemProperty -Path $extKey -Name '(Default)' -ErrorAction SilentlyContinue).'(Default)'
        if ($current -and ($allProgIds -contains $current)) {
            Remove-ItemProperty -Path $extKey -Name '(Default)' -Force -ErrorAction SilentlyContinue
            Write-Host "  [OK] Released the $($t.Ext) default (was $current)" -ForegroundColor Green
        } elseif ($current) {
            Write-Host "  [--] $($t.Ext) default belongs to '$current' - left alone" -ForegroundColor DarkGray
        }
    }

    # Explorer's UserChoice: only ours goes. Another app's stays.
    $ucKey = "$feKey\UserChoice"
    if (Test-Path $ucKey) {
        $ucProg = (Get-ItemProperty -Path $ucKey -Name 'ProgId' -ErrorAction SilentlyContinue).ProgId
        if ($ucProg -and ($allProgIds -contains $ucProg)) {
            Remove-Item -Path $ucKey -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host "  [OK] Cleared Explorer UserChoice for $($t.Ext)" -ForegroundColor Green
        }
    }

    # If the extension key is now completely empty, it is ours to tidy away.
    if (Test-Path $extKey) {
        $hasSub = @(Get-ChildItem -Path $extKey -ErrorAction SilentlyContinue).Count -gt 0
        $props  = Get-ItemProperty -Path $extKey -ErrorAction SilentlyContinue
        $names  = @()
        if ($props) {
            $names = @($props.PSObject.Properties |
                       Where-Object { $_.Name -notlike 'PS*' } |
                       ForEach-Object { $_.Name })
        }
        if (-not $hasSub -and $names.Count -eq 0) {
            Remove-Item -Path $extKey -Force -ErrorAction SilentlyContinue
            Write-Host "  [OK] Removed the now-empty $($t.Ext) key" -ForegroundColor Green
        }
    }
}

# ── 3) Application entry, capabilities, RegisteredApplications, App Paths ───
Remove-KeyIfPresent $appKey      "Applications\$exeName"
Remove-KeyIfPresent $capRoot     "Capabilities ($regAppName)"
Remove-KeyIfPresent $appPathsKey "App Paths\$exeName"

if (Test-Path $registeredKey) {
    $entry = Get-ItemProperty -Path $registeredKey -Name $regAppName -ErrorAction SilentlyContinue
    if ($entry) {
        Remove-ItemProperty -Path $registeredKey -Name $regAppName -Force -ErrorAction SilentlyContinue
        Write-Host "  [OK] Removed RegisteredApplications\$regAppName" -ForegroundColor Green
    } else {
        Write-Host "  [--] RegisteredApplications\$regAppName not present" -ForegroundColor DarkGray
    }
}

# Drop the XAIHT vendor key if Lumen was the only thing under it.
$vendorKey = "HKCU:\Software\XAIHT"
if (Test-Path $vendorKey) {
    if (@(Get-ChildItem -Path $vendorKey -ErrorAction SilentlyContinue).Count -eq 0) {
        Remove-Item -Path $vendorKey -Force -ErrorAction SilentlyContinue
        Write-Host "  [OK] Removed the now-empty HKCU:\Software\XAIHT key" -ForegroundColor Green
    } else {
        Write-Host "  [--] HKCU:\Software\XAIHT still holds other products - kept" -ForegroundColor DarkGray
    }
}

# ── 4) Tell the shell ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "Refreshing the Windows shell..." -ForegroundColor Cyan
try {
    Add-Type -TypeDefinition $csharpCode -ErrorAction Stop
    [LumenShellNotify]::SHChangeNotify(0x08000000, 0x0000, [IntPtr]::Zero, [IntPtr]::Zero)
    Write-Host "  [OK] Notified Windows Shell (SHChangeNotify)" -ForegroundColor Green
} catch {
    Write-Host "  [WARN] Add-Type unavailable - using rundll32 fallback" -ForegroundColor Yellow
    & rundll32.exe user32.dll, UpdatePerUserSystemParameters 2>$null
    Write-Host "  [OK] Notified shell via rundll32" -ForegroundColor Green
}
try {
    & ie4uinit.exe -ClearIconCache 2>$null
    & ie4uinit.exe -show 2>$null
} catch { }

Write-Host ""
Write-Host "Lumen's file associations have been removed." -ForegroundColor Green
exit 0
