# Creates the SlopMonster shortcuts (Desktop + Start Menu). Re-run after moving this folder.
#   powershell -ExecutionPolicy Bypass -File app\make-shortcut.ps1
# With -Startup, the same shortcut also goes in the Startup folder, so SlopMonster starts when
# you sign in to Windows and Ctrl+Alt+U works after a restart.
param([switch]$Startup)
$ErrorActionPreference = 'Stop'

# pythonw, not python: no console window sits behind the app.
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    # python.org's installer leaves Python off PATH unless asked, but its py launcher finds it.
    try { $pythonw = & py -3 -c "import os,sys;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" } catch { }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    throw 'Python was not found. Install Python 3.9 or newer from python.org, ticking "Add python.exe to PATH", then run this again.'
}
# python.exe beside it, because pythonw does not reliably hand back an exit code.
& (Join-Path (Split-Path $pythonw) 'python.exe') -c 'import sys; sys.exit(sys.version_info < (3, 9))'
if ($LASTEXITCODE) { throw "SlopMonster needs Python 3.9 or newer, and $pythonw is older. Install a newer one from python.org." }

$shellApp = New-Object -ComObject Shell.Application
$folders = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
if ($Startup) { $folders += [Environment]::GetFolderPath('Startup') }
foreach ($folder in $folders) {
    # Not WScript.Shell: it passes paths through the ANSI code page, so a letter outside it, in
    # this folder's path or the Desktop's, arrives as '?'. Write an empty shell link (the 76-byte
    # header, shown normal, then the 4-byte end) and let Shell.Application fill it in, in Unicode.
    $file = Join-Path $folder 'SlopMonster.lnk'
    $bytes = New-Object byte[] 80
    $bytes[0] = 0x4C
    (New-Object Guid '00021401-0000-0000-C000-000000000046').ToByteArray().CopyTo($bytes, 4)
    $bytes[60] = 1
    [IO.File]::WriteAllBytes($file, $bytes)
    $link = $shellApp.NameSpace($folder).ParseName('SlopMonster.lnk').GetLink
    $link.Description = 'Paste text, get it unslopped'
    $link.Path = $pythonw
    $link.Arguments = '"' + (Join-Path $PSScriptRoot 'server.py') + '"'
    $link.WorkingDirectory = $PSScriptRoot
    $link.SetIconLocation((Join-Path $PSScriptRoot 'slopmonster.ico'), 0)
    $link.Save()
    Write-Output "Created $file"
}
