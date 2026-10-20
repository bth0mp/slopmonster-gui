# Builds dist\SlopMonster.exe from this checkout. See packaging\README.md.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python C:\Python312\python.exe
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot

& $Python -c 'import sys, PyInstaller, pypdf, cryptography; sys.exit(sys.version_info < (3, 9))'
if ($LASTEXITCODE) {
    throw "Building needs Python 3.9 or newer with PyInstaller, pypdf and cryptography: $Python -m pip install -r packaging\requirements-build.txt"
}

# The same tree and Python then give the same exe, byte for byte: zip times from the last
# commit, and a fixed hash seed.
if (-not $env:SOURCE_DATE_EPOCH) {
    $commit = git -C $root log -1 --format=%ct 2> $null
    if (-not $LASTEXITCODE -and $commit) { $env:SOURCE_DATE_EPOCH = "$commit".Trim() }
}
$env:PYTHONHASHSEED = '0'

& $Python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build') `
    (Join-Path $PSScriptRoot 'SlopMonster.spec')
if ($LASTEXITCODE) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

$exe = Join-Path $root 'dist\SlopMonster.exe'
$size = (Get-Item $exe).Length / 1MB
Write-Output ('Built {0} ({1:N1} MB)' -f $exe, $size)
