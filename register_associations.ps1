#Requires -Version 5.1
<#
.SYNOPSIS
    Registers Lumen Book Reader with Windows: ProgIDs, per-extension file
    associations, the "Open with" list, and Default Programs capabilities.

.DESCRIPTION
    Reads LumenInstall.json (written by install.py next to Lumen.exe) and
    registers ONLY the extensions the user ticked in the installation dialog.

    Everything is written under HKCU, so no administrator rights are needed and
    nothing is done to other users of the machine.

    Five registry surfaces are written, and each one buys a distinct behaviour.
    Skipping any of them is what makes a hand-rolled association feel broken:

      1. ProgID           HKCU:\Software\Classes\<ProgID>
                          The document type itself: friendly name, icon, and
                          the shell verbs (open / read) that launch Lumen.

      2. Extension link   HKCU:\Software\Classes\<ext>\OpenWithProgids
                          Additive. Puts Lumen in the "Open with" menu for the
                          extension WITHOUT stealing the current default. The
                          (Default) value - which does claim the type - is only
                          written when SetAsDefault is true.

      3. Application      HKCU:\Software\Classes\Applications\Lumen.exe
                          SupportedTypes + FriendlyAppName. This is what makes
                          Lumen appear in "Open with ▸ Choose another app" for
                          .epub and .pdf even when it owns neither.

      4. Capabilities     HKCU:\Software\XAIHT\Lumen Book Reader\Capabilities
                          + HKCU:\Software\RegisteredApplications
                          The Default Programs contract. This is what lists
                          Lumen in Settings ▸ Apps ▸ Default apps, so the user
                          can hand it the file types through Windows' own UI -
                          the only route Windows actually blesses.

      5. Explorer cache   ...\Explorer\FileExts\<ext>\OpenWithProgids
                          Explorer keeps its own copy; without this the menu can
                          take a reboot to notice.

    Run PHASE 1 (unregister) before PHASE 2 (register) every time, so a repeat
    install can never leave half of a previous version's keys behind.

.NOTES
    Mirror image: unregister_associations.ps1 removes exactly these keys.
    Every key written here appears there. Keep the two in step.
#>

$ErrorActionPreference = 'Stop'

# ── Read the install manifest ────────────────────────────────────────────────
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$configPath = Join-Path $scriptDir "LumenInstall.json"

if (-not (Test-Path $configPath)) {
    Write-Error "LumenInstall.json not found at: $configPath"
    exit 1
}

try {
    $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Error ("Failed to parse LumenInstall.json: " + $_.Exception.Message)
    exit 1
}

if (-not $config.InstallDir) {
    Write-Error '"InstallDir" key not found in LumenInstall.json'
    exit 1
}

$installDir = $config.InstallDir
if (-not (Test-Path $installDir)) {
    Write-Error "Installation directory not found: $installDir"
    exit 1
}
$installDir = (Resolve-Path $installDir).Path

$exeName = if ($config.Executable) { $config.Executable } else { "Lumen.exe" }
$icoName = if ($config.IconFile)   { $config.IconFile   } else { "lumen.ico" }
$exePath = Join-Path $installDir $exeName
$icoPath = Join-Path $installDir $icoName

if (-not (Test-Path $exePath)) {
    Write-Error "$exeName not found at: $exePath"
    exit 1
}
$exePath = (Resolve-Path $exePath).Path
$hasIcon = Test-Path $icoPath
if ($hasIcon) { $icoPath = (Resolve-Path $icoPath).Path }
else { Write-Warning "$icoName not found at $icoPath - falling back to the .exe icon." }

$iconRef = if ($hasIcon) { "`"$icoPath`",0" } else { "`"$exePath`",0" }

# ── The type table: one row per file type Lumen can own ──────────────────────
# Adding a format later is a one-line change here plus the same row in
# unregister_associations.ps1 and in install.py's ASSOCIATIONS tuple.
$typeTable = @(
    [pscustomobject]@{
        Ext          = '.epub'
        ProgId       = 'Lumen.EpubBook'
        FriendlyType = 'EPUB Book'
        PerceivedType= 'document'
        ContentType  = 'application/epub+zip'
    },
    [pscustomobject]@{
        Ext          = '.pdf'
        ProgId       = 'Lumen.PdfDocument'
        FriendlyType = 'PDF Document'
        PerceivedType= 'document'
        ContentType  = 'application/pdf'
    }
)

# Which extensions did the user tick? Normalised to lower case with a dot.
$selected = @()
if ($config.Associations) {
    foreach ($raw in $config.Associations) {
        $e = ([string]$raw).Trim().ToLowerInvariant()
        if ($e -and -not $e.StartsWith('.')) { $e = ".$e" }
        if ($e) { $selected += $e }
    }
}
$setDefault = [bool]$config.SetAsDefault

$appKey        = "HKCU:\Software\Classes\Applications\$exeName"
$capRoot       = "HKCU:\Software\XAIHT\Lumen Book Reader"
$capKey        = "$capRoot\Capabilities"
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

function New-SharedKey([string]$path) {
    <#
        Create a registry key ONLY if it is absent.

        `New-Item -Force` on an EXISTING key does not "create if missing" - it
        DELETES the key and makes a new empty one, taking its default value and
        every subkey with it. On a key Lumen owns that is harmless. On a key
        Lumen SHARES it is destructive, and it was: registering .pdf with
        `New-Item -Path HKCU:\Software\Classes\.pdf -Force` silently erased that
        key's existing (Default) - another application's ProgID - and emptied
        its OpenWithProgids list of every other app.

        Every shared key below therefore goes through this function.
        Lumen-owned keys (its ProgIDs, Applications\Lumen.exe, its Capabilities)
        still use -Force deliberately, so a reinstall starts them clean.
    #>
    if (-not (Test-Path $path)) {
        New-Item -Path $path -Force | Out-Null
    }
}

Write-Host ""
Write-Host "Lumen Book Reader - file association registrar" -ForegroundColor Cyan
Write-Host "===============================================" -ForegroundColor Cyan
Write-Host "  InstallDir : $installDir"    -ForegroundColor Cyan
Write-Host "  Executable : $exePath"       -ForegroundColor Cyan
Write-Host "  Icon       : $iconRef"       -ForegroundColor Cyan
if ($selected.Count -gt 0) {
    Write-Host ("  Selected   : " + ($selected -join ', ')) -ForegroundColor Cyan
} else {
    Write-Host "  Selected   : (none - only the Open-with entry will be written)" -ForegroundColor Yellow
}
Write-Host "  Default    : $setDefault"    -ForegroundColor Cyan
Write-Host ""

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1 - UNREGISTER. Always run first so a reinstall cannot inherit stale
#           keys pointing at a directory that no longer exists.
# ═════════════════════════════════════════════════════════════════════════════
Write-Host "PHASE 1  Clearing any previous Lumen registration..." -ForegroundColor Yellow

foreach ($t in $typeTable) {
    $progIdKey = "HKCU:\Software\Classes\$($t.ProgId)"
    Remove-KeyIfPresent $progIdKey "ProgID $($t.ProgId)"

    # Our OpenWithProgids entry under the extension - remove the VALUE only,
    # never the extension key itself: that key is shared with every other
    # application on the machine, and deleting it would break their menus too.
    $owp = "HKCU:\Software\Classes\$($t.Ext)\OpenWithProgids"
    if (Test-Path $owp) {
        Remove-ItemProperty -Path $owp -Name $t.ProgId -Force -ErrorAction SilentlyContinue
    }
    $feOwp = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$($t.Ext)\OpenWithProgids"
    if (Test-Path $feOwp) {
        Remove-ItemProperty -Path $feOwp -Name $t.ProgId -Force -ErrorAction SilentlyContinue
    }

    # If the extension's default still points at one of OUR ProgIDs, drop it.
    # Another app's default is left completely alone.
    $extKey = "HKCU:\Software\Classes\$($t.Ext)"
    if (Test-Path $extKey) {
        $current = (Get-ItemProperty -Path $extKey -Name '(Default)' -ErrorAction SilentlyContinue).'(Default)'
        if ($current -eq $t.ProgId) {
            Remove-ItemProperty -Path $extKey -Name '(Default)' -Force -ErrorAction SilentlyContinue
            Write-Host "  [OK] Cleared stale default for $($t.Ext)" -ForegroundColor Green
        }
    }
}

Remove-KeyIfPresent $appKey "Applications\$exeName"
# ONLY the Capabilities subkey. $capRoot is the discovery key install.py just
# wrote (InstallLocation, Version, LibraryDir, RegisteredExtensions...), and
# clearing the parent here erased all of it moments after it was created -
# leaving a working installation that no updater or companion tool could find.
Remove-KeyIfPresent $capKey "Capabilities ($regAppName)"
if (Test-Path $registeredKey) {
    Remove-ItemProperty -Path $registeredKey -Name $regAppName -Force -ErrorAction SilentlyContinue
}
Write-Host "  Clear-down complete." -ForegroundColor Yellow
Write-Host ""

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 - REGISTER
# ═════════════════════════════════════════════════════════════════════════════
Write-Host "PHASE 2  Registering Lumen with Windows..." -ForegroundColor Cyan

try {
    # ── 2.0  App Paths: makes Win+R "lumen" and ShellExecute("Lumen.exe") work
    New-Item -Path $appPathsKey -Force | Out-Null
    Set-ItemProperty -Path $appPathsKey -Name '(Default)' -Value $exePath
    Set-ItemProperty -Path $appPathsKey -Name 'Path'      -Value $installDir
    Write-Host "  [OK] App Paths -> $exePath" -ForegroundColor Green

    # ── 2.1  Applications\Lumen.exe: the always-available "Open with" entry
    New-Item -Path $appKey -Force | Out-Null
    Set-ItemProperty -Path $appKey -Name 'FriendlyAppName' -Value $regAppName
    New-Item -Path "$appKey\DefaultIcon" -Force | Out-Null
    Set-ItemProperty -Path "$appKey\DefaultIcon" -Name '(Default)' -Value $iconRef
    New-Item -Path "$appKey\shell\open\command" -Force | Out-Null
    Set-ItemProperty -Path "$appKey\shell\open\command" -Name '(Default)' -Value "`"$exePath`" `"%1`""
    New-Item -Path "$appKey\SupportedTypes" -Force | Out-Null
    foreach ($t in $typeTable) {
        Set-ItemProperty -Path "$appKey\SupportedTypes" -Name $t.Ext -Value ''
    }
    Write-Host "  [OK] Applications\$exeName (Open with, all supported types)" -ForegroundColor Green

    # ── 2.2  Per-type ProgIDs, but ONLY for the extensions the user selected
    $registeredTypes = @()
    foreach ($t in $typeTable) {
        if ($selected -notcontains $t.Ext) {
            Write-Host "  [--] $($t.Ext) not selected - skipping" -ForegroundColor DarkGray
            continue
        }

        $progIdKey = "HKCU:\Software\Classes\$($t.ProgId)"
        New-Item -Path $progIdKey -Force | Out-Null
        Set-ItemProperty -Path $progIdKey -Name '(Default)'       -Value "$($t.FriendlyType) (Lumen)"
        Set-ItemProperty -Path $progIdKey -Name 'FriendlyTypeName' -Value "$($t.FriendlyType) (Lumen)"
        Set-ItemProperty -Path $progIdKey -Name 'PerceivedType'    -Value $t.PerceivedType
        Set-ItemProperty -Path $progIdKey -Name 'AlwaysShowExt'    -Value ''
        # EditFlags 0x00010000 = FTA_OpenIsSafe: no "are you sure" prompt when
        # opening a downloaded book that Windows has marked with a zone stream.
        New-ItemProperty -Path $progIdKey -Name 'EditFlags' -PropertyType DWord -Value 0x00010000 -Force | Out-Null

        New-Item -Path "$progIdKey\DefaultIcon" -Force | Out-Null
        Set-ItemProperty -Path "$progIdKey\DefaultIcon" -Name '(Default)' -Value $iconRef

        # Canonical "open" verb.
        New-Item -Path "$progIdKey\shell" -Force | Out-Null
        Set-ItemProperty -Path "$progIdKey\shell" -Name '(Default)' -Value 'open'
        New-Item -Path "$progIdKey\shell\open" -Force | Out-Null
        Set-ItemProperty -Path "$progIdKey\shell\open" -Name '(Default)' -Value '&Open with Lumen'
        Set-ItemProperty -Path "$progIdKey\shell\open" -Name 'Icon'      -Value $iconRef
        New-Item -Path "$progIdKey\shell\open\command" -Force | Out-Null
        Set-ItemProperty -Path "$progIdKey\shell\open\command" -Name '(Default)' -Value "`"$exePath`" `"%1`""

        # A second verb so the right-click menu reads like the product does.
        New-Item -Path "$progIdKey\shell\read" -Force | Out-Null
        Set-ItemProperty -Path "$progIdKey\shell\read" -Name '(Default)' -Value 'Read in &Lumen'
        Set-ItemProperty -Path "$progIdKey\shell\read" -Name 'Icon'      -Value $iconRef
        New-Item -Path "$progIdKey\shell\read\command" -Force | Out-Null
        Set-ItemProperty -Path "$progIdKey\shell\read\command" -Name '(Default)' -Value "`"$exePath`" `"%1`""

        Write-Host "  [OK] ProgID $($t.ProgId) -> $($t.FriendlyType)" -ForegroundColor Green

        # ── Extension: additive OpenWithProgids (never destructive) ─────────
        # These four keys belong to every application on the machine, not to
        # Lumen. They are created only when absent, and only OUR value is added.
        $extKey = "HKCU:\Software\Classes\$($t.Ext)"
        New-SharedKey $extKey
        if ($t.ContentType) {
            # Do not overwrite a Content Type another application already set.
            $existingType = (Get-ItemProperty -Path $extKey -Name 'Content Type' `
                             -ErrorAction SilentlyContinue).'Content Type'
            if (-not $existingType) {
                Set-ItemProperty -Path $extKey -Name 'Content Type' -Value $t.ContentType
            }
        }
        $owp = "$extKey\OpenWithProgids"
        New-SharedKey $owp
        New-ItemProperty -Path $owp -Name $t.ProgId -PropertyType None -Value ([byte[]]@()) -Force | Out-Null

        $feKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$($t.Ext)"
        $feOwp  = "$feKey\OpenWithProgids"
        New-SharedKey $feKey
        New-SharedKey $feOwp
        New-ItemProperty -Path $feOwp -Name $t.ProgId -PropertyType None -Value ([byte[]]@()) -Force | Out-Null

        $keptDefault = (Get-ItemProperty -Path $extKey -Name '(Default)' `
                        -ErrorAction SilentlyContinue).'(Default)'
        if ($keptDefault) {
            Write-Host "  [OK] $($t.Ext) -> Open with Lumen (default '$keptDefault' left alone)" -ForegroundColor Green
        } else {
            Write-Host "  [OK] $($t.Ext) -> Open with Lumen (no default was set)" -ForegroundColor Green
        }

        # ── Claim the default, but only when the user asked for it ──────────
        if ($setDefault) {
            Set-ItemProperty -Path $extKey -Name '(Default)' -Value $t.ProgId
            # Explorer's UserChoice outranks everything above. It is
            # hash-protected, so it cannot be forged - but the owning user may
            # delete their own copy, which returns the choice to the ProgID we
            # just wrote. If Windows re-prompts on the next double-click, that
            # is Windows asserting the user's right to choose, and the correct
            # answer is the Default apps page, not a louder hack.
            $ucKey = "$feKey\UserChoice"
            if (Test-Path $ucKey) {
                Remove-Item -Path $ucKey -Force -Recurse -ErrorAction SilentlyContinue
                Write-Host "  [OK] Cleared Explorer UserChoice for $($t.Ext)" -ForegroundColor Green
            }
            Write-Host "  [OK] $($t.Ext) default -> $($t.ProgId)" -ForegroundColor Green
        }

        $registeredTypes += $t
    }

    # ── 2.3  Default Programs capabilities (Settings ▸ Default apps) ────────
    New-Item -Path $capKey -Force | Out-Null
    Set-ItemProperty -Path $capKey -Name 'ApplicationName'        -Value $regAppName
    Set-ItemProperty -Path $capKey -Name 'ApplicationDescription' -Value 'A focused desktop reading room for EPUB and PDF - faithful pages, deep definitions, durable notes, and RSVP speed reading.'
    Set-ItemProperty -Path $capKey -Name 'ApplicationIcon'        -Value $iconRef
    $faKey = "$capKey\FileAssociations"
    New-Item -Path $faKey -Force | Out-Null
    foreach ($t in $registeredTypes) {
        Set-ItemProperty -Path $faKey -Name $t.Ext -Value $t.ProgId
    }
    # RegisteredApplications is a MACHINE-WIDE-STYLE index shared by every
    # application that offers itself to Settings > Default apps. Recreating it
    # with -Force deletes everyone else's entry - it silently unregistered
    # Telegram Desktop the first time this ran.
    New-SharedKey $registeredKey
    Set-ItemProperty -Path $registeredKey -Name $regAppName -Value "Software\XAIHT\Lumen Book Reader\Capabilities"
    Write-Host "  [OK] Default Programs capabilities registered" -ForegroundColor Green

    # ── 2.4  Tell the shell, then refresh the icon cache ────────────────────
    try {
        Add-Type -TypeDefinition $csharpCode -ErrorAction Stop
        [LumenShellNotify]::SHChangeNotify(0x08000000, 0x0000, [IntPtr]::Zero, [IntPtr]::Zero)
        Write-Host "  [OK] Notified Windows Shell (SHChangeNotify)" -ForegroundColor Green
    } catch {
        # Constrained Language Mode blocks Add-Type; the rundll32 route still works.
        Write-Host "  [WARN] Add-Type unavailable - using rundll32 fallback" -ForegroundColor Yellow
        & rundll32.exe user32.dll, UpdatePerUserSystemParameters 2>$null
        Write-Host "  [OK] Notified shell via rundll32" -ForegroundColor Green
    }

    try {
        & ie4uinit.exe -ClearIconCache 2>$null
        & ie4uinit.exe -show 2>$null
    } catch {
        Write-Host "  [INFO] Icon cache not refreshed; sign out and back in to see new icons." -ForegroundColor Yellow
    }

    Write-Host ""
    if ($registeredTypes.Count -gt 0) {
        $names = ($registeredTypes | ForEach-Object { $_.Ext }) -join ', '
        Write-Host "SUCCESS: $names now open in Lumen Book Reader." -ForegroundColor Green
        if (-not $setDefault) {
            Write-Host "Lumen was added to 'Open with' without taking the default." -ForegroundColor Cyan
            Write-Host "Make it the default any time in Settings > Apps > Default apps." -ForegroundColor Cyan
        }
    } else {
        Write-Host "No file types were selected. Lumen is still reachable through" -ForegroundColor Cyan
        Write-Host "right-click > Open with > Lumen Book Reader." -ForegroundColor Cyan
    }
    exit 0
} catch {
    Write-Error ("Failed to register file associations: " + $_.Exception.Message)
    exit 1
}
