# PyInstaller spec for dist\SlopMonster.exe: one file, no console, the app's icon.
# Build it with packaging\build.ps1, which runs this through python -m PyInstaller.
#
# The exe holds Python, app/server.py and the modules it imports (documents, wordfile,
# version, tools/deslop.py, pypdf, cryptography), and unpacks the files the server opens by
# path into the same layout as the repository: app/index.html, app/icon.png,
# prompts/cleanse.txt, references/*.md and LICENSE. It does not hold Claude Code or the
# Codex CLI.
import os
import re

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                 StringTable, VarFileInfo, VarStruct, VSVersionInfo)

ROOT = os.path.dirname(SPECPATH)
APP = os.path.join(ROOT, 'app')

with open(os.path.join(APP, 'version.py'), encoding='utf-8') as fh:
    VERSION = re.search(r"^VERSION = '(\d+)\.(\d+)\.(\d+)'$", fh.read(), re.M)
if not VERSION:
    raise SystemExit("app/version.py must set VERSION = 'major.minor.patch'")
NUMBERS = tuple(int(n) for n in VERSION.groups()) + (0,)
DOTTED = '.'.join(VERSION.groups())

a = Analysis(
    [os.path.join(APP, 'server.py')],
    pathex=[APP, os.path.join(ROOT, 'tools')],
    datas=[
        (os.path.join(APP, 'index.html'), 'app'),
        (os.path.join(APP, 'icon.png'), 'app'),
        (os.path.join(ROOT, 'prompts', 'cleanse.txt'), 'prompts'),
        (os.path.join(ROOT, 'references', '*.md'), 'references'),
        (os.path.join(ROOT, 'LICENSE'), '.'),
    ],
    # Imported inside functions, so named here as well.
    hiddenimports=['documents', 'wordfile', 'version', 'deslop', 'pypdf'],
    # What pypdf or the standard library would pull in and the app never uses. pypdf
    # reaches for Pillow only to decode images and fontTools only to write fonts, and the
    # app asks it for neither. cryptography stays: pypdf needs it to open a PDF locked with
    # AES, which is what Acrobat's editing restrictions save, even with no open password.
    # The app makes no HTTPS connection, so ssl goes, and with _hashlib that drops OpenSSL's
    # 7 MB: hashlib and hmac fall back to Python's own SHA-256 and MD5.
    excludes=['tkinter', '_tkinter', 'PIL', 'Crypto', 'fontTools', 'brotli',
              'zopfli', 'numpy', 'pytest', 'setuptools', 'pkg_resources', 'lib2to3',
              'pydoc_data', 'sqlite3', 'unittest', 'xmlrpc', 'pdb', 'doctest', 'asyncio',
              'concurrent', 'multiprocessing', 'ssl', '_ssl', '_hashlib', 'ftplib', 'smtplib'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SlopMonster',
    console=False,
    icon=os.path.join(APP, 'slopmonster.ico'),
    upx=False,
    version=VSVersionInfo(
        ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
        kids=[
            StringFileInfo([StringTable('040904B0', [
                StringStruct('ProductName', 'SlopMonster GUI'),
                StringStruct('FileDescription', 'SlopMonster GUI'),
                StringStruct('InternalName', 'SlopMonster'),
                StringStruct('OriginalFilename', 'SlopMonster.exe'),
                StringStruct('FileVersion', DOTTED),
                StringStruct('ProductVersion', DOTTED),
                StringStruct('LegalCopyright', 'MIT License. See LICENSE.'),
            ])]),
            VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
        ],
    ),
)
