# Native desktop regression runner. Each invocation owns fresh scratch outside profiles.
param(
    [Parameter(Mandatory=$true)][string]$ShoterLauncher,
    [Parameter(Mandatory=$true)][string]$PhotoPython,
    [switch]$Focused,
    [string[]]$Targets = @('tests/test_catalog_removal.py', 'tests/test_shelf_ui.py',
        'tests/test_library_index.py', 'tests/test_turbo_scan.py', 'tests/test_retrieval_service.py')
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$profileKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList'
$profiles = @($env:USERPROFILE, (Get-ItemProperty $profileKey).ProfilesDirectory)
$profiles += Get-ChildItem $profileKey | ForEach-Object { (Get-ItemProperty $_.PSPath).ProfileImagePath }
foreach ($path in $profiles) {
    if ($path) {
        $prefix = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($path)).TrimEnd('\') + '\'
        if (($repo + '\').StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Run from a checkout outside Windows user profiles; no profile scratch fallback.'
        }
    }
}
Set-Location -LiteralPath $repo
$python = Join-Path $repo '.venv-release\Scripts\python.exe'
$env:LUMEN_SHOTER_LAUNCHER = (Resolve-Path -LiteralPath $ShoterLauncher).Path
$env:LUMEN_PHOTO_PYTHON = (Resolve-Path -LiteralPath $PhotoPython).Path
$run = Join-Path $repo ('.artifacts\catalog-removal\' + [guid]::NewGuid().ToString('N'))
$scratch = New-Item -ItemType Directory -Path (Join-Path $run 'scratch') -Force
$env:TEMP = $env:TMP = $env:TMPDIR = $scratch.FullName
$env:LUMEN_MCP_EVIDENCE = $run
$env:LUMEN_VISIBLE_TESTS = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$env:QT_QPA_PLATFORM = 'windows'
if ($Focused) { $Targets = @('tests/test_catalog_removal.py', 'tests/test_turbo_scan.py') }
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Write-Host "Visible catalog tests. Evidence and scratch: $run"
& $python -m pytest -v -s --tb=short -p tools.validate_mcp_install --basetemp (Join-Path $scratch 'pytest') --junitxml (Join-Path $run 'tests.xml') @Targets 2>&1 |
    Tee-Object -FilePath (Join-Path $run 'validation.log')
$code = $LASTEXITCODE
Set-Content -LiteralPath (Join-Path $run 'exit-code.txt') -Value $code
exit $code
