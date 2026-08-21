# ═══════════════════════════════════════════════════════════════════
#   Lumen Book Reader - shortcut creator
#   Created by Angela López Mendoza · @angelahack1
# ═══════════════════════════════════════════════════════════════════
#
# Reads LumenInstall.json (written by install.py) and creates the shortcuts
# the user ticked in the installation dialog: Desktop, Start menu, and one
# beside the executable itself.
#
# WORKING DIRECTORY IS LOAD-BEARING, not cosmetic. Lumen builds its shelf from
# the current directory and writes reading marks into `lumen-reading-marks.json`
# there. A shortcut whose working directory is the install folder therefore
# opens an empty shelf and drops the reader's notes among the program files.
# So every shortcut starts in the LIBRARY folder the user chose at install
# time, and falls back to the install folder only if that path has gone.

param(
    [switch]$DesktopOnly,
    [switch]$StartMenuOnly
)

$scriptDir  = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$configPath = Join-Path $scriptDir "LumenInstall.json"

Write-Host ""
Write-Host "Lumen Book Reader - shortcut creator" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $configPath)) {
    Write-Host "Error: LumenInstall.json not found at: $configPath" -ForegroundColor Red
    exit 1
}

try {
    $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Host "Error: failed to parse LumenInstall.json: $_" -ForegroundColor Red
    exit 1
}

if (-not $config.InstallDir) {
    Write-Host 'Error: "InstallDir" key not found in LumenInstall.json' -ForegroundColor Red
    exit 1
}

$installDir = $config.InstallDir
if (-not (Test-Path $installDir)) {
    Write-Host "Error: installation directory not found: $installDir" -ForegroundColor Red
    exit 1
}
$installDir = (Resolve-Path $installDir).Path

$exeName = if ($config.Executable) { $config.Executable } else { "Lumen.exe" }
$icoName = if ($config.IconFile)   { $config.IconFile   } else { "lumen.ico" }
$exePath = Join-Path $installDir $exeName
$iconPath = Join-Path $installDir $icoName

if (-not (Test-Path $exePath)) {
    Write-Host "Error: $exeName not found at: $exePath" -ForegroundColor Red
    exit 1
}
$exePath = (Resolve-Path $exePath).Path
Write-Host "[OK] $exeName found: $exePath" -ForegroundColor Green

if (Test-Path $iconPath) {
    $iconPath = (Resolve-Path $iconPath).Path
    Write-Host "[OK] Icon found: $iconPath" -ForegroundColor Green
} else {
    Write-Host "[!] $icoName not found - falling back to the .exe's embedded icon." -ForegroundColor Yellow
    $iconPath = $exePath
}

# ── Working directory: the library, if it still exists ──────────────────────
$workingDir = $installDir
if ($config.LibraryDir -and (Test-Path $config.LibraryDir)) {
    $workingDir = (Resolve-Path $config.LibraryDir).Path
    Write-Host "[OK] Shortcuts will start in the library: $workingDir" -ForegroundColor Green
} else {
    if ($config.LibraryDir) {
        Write-Host "[!] Library folder '$($config.LibraryDir)' is gone - starting in the install folder." -ForegroundColor Yellow
    } else {
        Write-Host "[--] No library folder recorded - starting in the install folder." -ForegroundColor DarkGray
    }
}

# ── Which shortcuts were requested? ─────────────────────────────────────────
$wantDesktop   = $true
$wantStartMenu = $true
$wantLocal     = $true
if ($config.Shortcuts) {
    if ($null -ne $config.Shortcuts.Desktop)    { $wantDesktop   = [bool]$config.Shortcuts.Desktop }
    if ($null -ne $config.Shortcuts.StartMenu)  { $wantStartMenu = [bool]$config.Shortcuts.StartMenu }
    if ($null -ne $config.Shortcuts.InstallDir) { $wantLocal     = [bool]$config.Shortcuts.InstallDir }
}
# Explicit switches override the manifest (used when re-running by hand).
if ($DesktopOnly)   { $wantDesktop = $true;  $wantStartMenu = $false; $wantLocal = $false }
if ($StartMenuOnly) { $wantDesktop = $false; $wantStartMenu = $true;  $wantLocal = $false }

$description = "Lumen Book Reader - a focused desktop reading room for EPUB and PDF"
$created = 0

function New-LumenShortcut {
    param([string]$LinkPath, [string]$Label)
    try {
        $parent = Split-Path -Parent $LinkPath
        if (-not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($LinkPath)
        $sc.TargetPath       = $script:exePath
        $sc.Arguments        = ""
        $sc.WorkingDirectory = $script:workingDir
        $sc.Description      = $script:description
        $sc.IconLocation     = "$script:iconPath,0"
        $sc.WindowStyle      = 1
        $sc.Save()
        Write-Host "[OK] Created $Label" -ForegroundColor Green
        Write-Host "     $LinkPath" -ForegroundColor DarkGray
        $script:created++
        return $true
    } catch {
        Write-Host "[WARN] Could not create $Label - $_" -ForegroundColor Yellow
        return $false
    }
}

Write-Host ""

if ($wantDesktop) {
    # GetFolderPath honours a redirected Desktop (OneDrive, roaming profiles);
    # $env:USERPROFILE\Desktop does not, and would silently write the shortcut
    # into a folder the user never sees.
    $desktop = [Environment]::GetFolderPath("Desktop")
    New-LumenShortcut (Join-Path $desktop "Lumen Book Reader.lnk") "desktop shortcut" | Out-Null
} else {
    Write-Host "[--] Desktop shortcut not requested" -ForegroundColor DarkGray
}

if ($wantStartMenu) {
    $startMenu = [Environment]::GetFolderPath("Programs")
    New-LumenShortcut (Join-Path $startMenu "Lumen Book Reader.lnk") "Start menu shortcut" | Out-Null
} else {
    Write-Host "[--] Start menu shortcut not requested" -ForegroundColor DarkGray
}

if ($wantLocal) {
    New-LumenShortcut (Join-Path $installDir "Lumen Book Reader.lnk") "install-folder shortcut" | Out-Null
} else {
    Write-Host "[--] Install-folder shortcut not requested" -ForegroundColor DarkGray
}

Write-Host ""
if ($created -gt 0) {
    Write-Host "$created shortcut(s) created." -ForegroundColor Green
    exit 0
}

# Zero shortcuts is only an error when some were actually asked for.
if ($wantDesktop -or $wantStartMenu -or $wantLocal) {
    Write-Host "No shortcuts could be created." -ForegroundColor Red
    exit 1
}
Write-Host "No shortcuts were requested." -ForegroundColor Cyan
exit 0
