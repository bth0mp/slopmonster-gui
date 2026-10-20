# Building SlopMonster.exe

`SlopMonster.exe` is the app in one file: Python, `app/server.py` and what it imports,
the page, the rewrite prompt, the two editing guides, and pypdf and cryptography for PDFs.
It needs nothing else on the PC except Claude Code or the Codex CLI, which it does not
include.

## Build

You need Windows, Python 3.9 or newer, PyInstaller, pypdf and cryptography.
`requirements-build.txt` pins every package the build uses, at the versions the release
workflow installs on Python 3.12:

```powershell
python -m pip install -r packaging\requirements-build.txt
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The exe is written to `dist\SlopMonster.exe`, about 12 MB. PyInstaller's working files go
to `build\`. Both folders are ignored by git. To build with a Python that is not first on
PATH, pass it: `packaging\build.ps1 -Python C:\Python312\python.exe`.

`build.ps1` sets `SOURCE_DATE_EPOCH` to the time of the last commit and `PYTHONHASHSEED`
to 0, so two builds of the same commit with the same Python are identical byte for byte.
An exe built with another Python, such as the 3.12 the release workflow uses, differs.

`SlopMonster.spec` sets what goes in: the files the server reads by path, under the same
folders as in the repository, and the modules it imports. It leaves out Pillow, fontTools
and OpenSSL, which pypdf or the standard library would otherwise pull in and the app never
uses. It keeps cryptography, which pypdf needs to open a PDF locked with AES. Acrobat saves
a PDF that way when it restricts editing, even with no password to open it.

## Version

The version number is `VERSION` in `app/version.py` and nowhere else. The spec writes it
into the exe's Windows version information, under the product name SlopMonster GUI, and
the app shows it at the foot of Settings. Change it there before tagging a release.

## Test the build

```powershell
python packaging\smoke_exe.py dist\SlopMonster.exe
```

This starts the exe with `--no-window`, a spare port and a data folder in a temp folder,
so it never touches `%LOCALAPPDATA%\SlopMonster` or port 47821. It then drives the exe
over HTTP the way the page does: the status and check APIs, opening a Word file and a PDF
(which the exe reads in a child process of its own), Save as Word, the rewrite
instructions, a second launch and Turn off. It also runs the sign-in wrapper. It calls no
model.

The same options let you try the exe by hand without disturbing a copy you use:

```powershell
dist\SlopMonster.exe --no-window --port 48100 --data-dir $env:TEMP\slop-test | Write-Output
```

`--port` is the first port to try (0 takes any free one), and `--data-dir` holds the log,
`instance.json`, the browser profile and saved instructions. The variable
`SLOPMONSTER_DATA_DIR` does the same as `--data-dir`. With `--no-window` the exe prints its
address, opens no window and does not register Ctrl+Alt+U. Open the address it prints in
a browser to use the page. Turn off in the page stops it.

The exe is a windowed program, so PowerShell and cmd start it and go straight back to the
prompt. It still prints to the console it was started from, but the line can land after the
next prompt. `| Write-Output` makes PowerShell wait for it, and works for `--version` and
`--help` too. The address is also in `instance.json` in the data folder, and in the line
`serving http://127.0.0.1:...` of `server.log` there.

## How the exe differs from a source run

- It finds its page, prompt and guides in the folder PyInstaller unpacks it to.
- To read a Word or PDF file it starts itself again as `SlopMonster.exe --documents`,
  where a source run starts `documents.py`.
- The sign-in window is `SlopMonster.exe --sign-in`, which opens a console and runs the
  CLI's own sign-in in it, where a source run uses `python.exe`.
- It runs as two `SlopMonster.exe` processes: PyInstaller's launcher, which unpacks it to a
  `_MEI` folder in `%TEMP%`, and the app. The launcher removes that folder when the app
  ends. If the launcher is ended first, from Task Manager or by a power cut, the folder
  stays, and the next start of SlopMonster removes it once it is ten minutes old and no
  running copy uses it.

## Releases

`.github/workflows/release.yml` runs on a tag such as `v1.0.0`. Its build job has
read-only access to the repository. It runs the app tests, builds the exe, runs
`smoke_exe.py` on it and keeps `SlopMonster.exe` and `SHA256SUMS.txt` as a workflow
artifact. Then a second job, which runs only `gh`, attaches those two files to the GitHub
Release for the tag. It creates the release when there is none, so create the release with
its notes first and then push the tag. The tag has to match `app/version.py`. Started by
hand from the Actions tab, the workflow builds the same two files and keeps them as an
artifact only.

On a fork, GitHub keeps workflows off until the owner turns them on in the Actions tab.
Do that before pushing the first tag, or the tag builds nothing. If a tag went out first,
start the workflow by hand on that tag: it then attaches the files as a tag push would.

Upload only what the workflow builds. `SHA256SUMS.txt` holds the hash of the exe from the
same run, and a later run on the tag replaces both files together.

The exe is not code-signed, so Windows SmartScreen warns about it on first launch. See
"Install the app" in `app/README.md` for what users see and do.
