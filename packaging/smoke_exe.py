#!/usr/bin/env python3
"""Smoke test for a built SlopMonster.exe. Run: python packaging/smoke_exe.py [dist/SlopMonster.exe]

Starts the exe with --no-window on a spare port and a data folder in a temp folder, so the
real data folder and port are never touched, and then over HTTP: the page and its token,
/api/status, /api/check, a Word file, a PDF and an AES-locked PDF read in the exe's own child
process, a Save as Word round trip, the rewrite instructions read, saved and reset, a second
launch that finds the first copy, the sign-in wrapper, and Turn off. It calls no model, opens
no window and registers no hotkey. First it runs build.ps1 with a stand-in Python, in this
checkout and in a copy outside git, to check the commit time it hands PyInstaller.
Exits non-zero when a check fails.
"""
import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'dist', 'SlopMonster.exe'))
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SAMPLE = ("Are you ready to unlock seamless scheduling? Our game-changing tool empowers clinic "
          "managers to streamline every booking.")
failures = []


def check(name, ok, detail=''):
    print(('ok    ' if ok else 'FAIL  ') + name + ('' if ok else f'  {detail!r}'[:300]), flush=True)
    if not ok:
        failures.append(name)


def spare_port():
    """A port with the four after it free too, as the server may take any of them."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        if port > 65000:
            continue
        try:
            for n in range(5):
                with socket.socket() as s:
                    s.bind(('127.0.0.1', port + n))
            return port
        except OSError:
            continue
    raise SystemExit('no spare port')


def tiny_pdf(text):
    """A one-page PDF that shows `text` in Helvetica."""
    content = f'BT /F1 18 Tf 72 720 Td ({text}) Tj ET'.encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
               b'/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length %d >>\nstream\n' % len(content) + content + b'\nendstream']
    out, offsets = bytearray(b'%PDF-1.4\n'), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    for offset in offsets:
        out += b'%010d 00000 n \n' % offset
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)
    return bytes(out)


def locked_pdf(text):
    """tiny_pdf locked with AES-256 and an empty open password, the way Acrobat saves a PDF
    with editing restrictions. Making one needs pypdf and cryptography here."""
    import io
    import pypdf
    writer = pypdf.PdfWriter(clone_from=io.BytesIO(tiny_pdf(text)))
    writer.encrypt('', 'owner', algorithm='AES-256')
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def first_line(proc, wait):
    """The first line the process prints, or '' after `wait` seconds."""
    got = []
    reader = threading.Thread(target=lambda: got.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(wait)
    return got[0].decode('utf-8', 'replace').strip() if got else ''


def build_epoch():
    """build.ps1 takes its zip times from the last commit, and builds without them outside a
    git checkout, such as a source zip. A stand-in Python prints what PyInstaller would get."""
    tmp = tempfile.mkdtemp(prefix='slop-build-')
    try:
        os.makedirs(os.path.join(tmp, 'tree', 'packaging'))
        script = os.path.join(tmp, 'tree', 'packaging', 'build.ps1')
        shutil.copyfile(os.path.join(ROOT, 'packaging', 'build.ps1'), script)
        fake = os.path.join(tmp, 'python.cmd')
        with open(fake, 'w') as f:
            f.write('@echo off\nif "%1"=="-m" (echo epoch=[%SOURCE_DATE_EPOCH%]& exit /b 3)\nexit /b 0\n')
        env = {k: v for k, v in os.environ.items() if k.upper() != 'SOURCE_DATE_EPOCH'}
        env['GIT_CEILING_DIRECTORIES'] = tmp

        def build(path):
            run = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path,
                                  '-Python', fake], stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=120, env=env, creationflags=NO_WINDOW)
            return (run.stdout + run.stderr).decode('utf-8', 'replace')

        said = build(script)
        check('build.ps1 outside a git checkout goes on without a commit time',
              'epoch=[]' in said and 'PyInstaller failed with exit code 3' in said, said[-300:])
        git = subprocess.run(['git', '-C', ROOT, 'log', '-1', '--format=%ct'], capture_output=True,
                             text=True, creationflags=NO_WINDOW)
        if git.returncode == 0 and git.stdout.strip():
            said = build(os.path.join(ROOT, 'packaging', 'build.ps1'))
            check('build.ps1 in a git checkout takes the time of the last commit',
                  f'epoch=[{git.stdout.strip()}]' in said, said[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    build_epoch()
    if not os.path.isfile(EXE):
        raise SystemExit(f'{EXE} was not found. Build it with packaging\\build.ps1 first.')
    tmp = tempfile.mkdtemp(prefix='slop-exe-')
    data = os.path.join(tmp, 'data')
    port = spare_port()
    real = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'SlopMonster')
    real_before = {n: os.stat(os.path.join(real, n)).st_mtime_ns for n in ('instance.json',)
                   if os.path.exists(os.path.join(real, n))}
    argv = [EXE, '--no-window', '--port', str(port), '--data-dir', data]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        said = first_line(proc, 60)
        check('the exe starts and prints its address', said == f'SlopMonster is running at http://127.0.0.1:{port}/', said)
        if failures:
            return

        def ask(method, path, body=None, headers=None, token=True):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=120)
            sent = dict(headers or {})
            if token:
                sent['X-Slop-Token'] = page_token[0]
            if isinstance(body, (dict, list)):
                body, sent['Content-Type'] = json.dumps(body).encode(), 'application/json'
            conn.request(method, path, body=body, headers=sent)
            reply = conn.getresponse()
            got = (reply.status, dict(reply.getheaders()), reply.read())
            conn.close()
            return got

        page_token = ['']
        status, _, page = ask('GET', '/', token=False)
        found = re.search(rb'name="slop-token" content="([^"]+)"', page)
        check('the page loads with its token', status == 200 and found and b'__SLOP_TOKEN__' not in page, status)
        page_token[0] = found[1].decode() if found else ''
        check('the page shows the version', re.search(rb'SlopMonster GUI \d+\.\d+\.\d+', page) is not None)
        status, _, icon = ask('GET', '/icon.png', token=False)
        check('the icon loads', status == 200 and icon[:8] == b'\x89PNG\r\n\x1a\n', status)

        status, _, body = ask('GET', '/api/status')
        engines = json.loads(body).get('engines', {}) if status == 200 else {}
        check('/api/status answers for both engines', set(engines) == {'claude', 'chatgpt'}, body[:200])
        check('/api/status: no hotkey under --no-window', json.loads(body).get('hotkey') == 'none', body[:200])

        status, _, body = ask('POST', '/api/check', {'text': SAMPLE})
        result = json.loads(body) if status == 200 else {}
        check('/api/check scores text with the bundled checker',
              result.get('words', 0) > 5 and result.get('clean') is False, body[:200])

        with open(os.path.join(ROOT, 'app', 'testdata', 'a_prose.docx'), 'rb') as fh:
            docx = fh.read()
        status, _, body = ask('POST', '/api/extract', docx, {'X-Slop-Name': 'a_prose.docx',
                                                             'Content-Type': 'application/octet-stream'})
        word = json.loads(body) if status == 200 else {}
        check('a Word file is read in the exe child process', status == 200 and len(word.get('text', '')) > 50,
              body[:200])

        status, _, body = ask('POST', '/api/extract', tiny_pdf('The smoke test reads this PDF line.'),
                              {'X-Slop-Name': 'smoke.pdf', 'Content-Type': 'application/pdf'})
        pdf = json.loads(body) if status == 200 else {}
        check('a PDF is read with the bundled pypdf', 'smoke test reads this PDF line' in pdf.get('text', ''),
              body[:200])
        try:
            locked = locked_pdf('The smoke test reads this locked line.')
        except Exception as e:      # pypdf without cryptography cannot write one
            locked, body = None, repr(e).encode()
        if locked:
            status, _, body = ask('POST', '/api/extract', locked,
                                  {'X-Slop-Name': 'locked.pdf', 'Content-Type': 'application/pdf'})
        pdf = json.loads(body) if locked and status == 200 else {}
        check('a PDF locked with AES and no open password is read with the bundled cryptography',
              'smoke test reads this locked line' in pdf.get('text', ''), body[:200])

        if word.get('text'):
            text = json.dumps({'text': word['text'].replace('the ', 'the very ', 1)}).encode()
            status, headers, body = ask('POST', '/api/docx', struct.pack('>I', len(text)) + text + docx,
                                        {'Content-Type': 'application/octet-stream'})
            check('Save as Word writes into the original file', status == 200 and body[:2] == b'PK'
                  and 'X-Slop-Notes' in headers, (status, body[:200]))
        status, headers, body = ask('POST', '/api/docx', {'text': 'A new paragraph.\n\nAnd another.'})
        check('Save as Word writes a new file', status == 200 and body[:2] == b'PK', (status, body[:200]))

        status, _, body = ask('GET', '/api/prompt')
        default = json.loads(body).get('text', '') if status == 200 else ''
        check('the rewrite instructions load from the bundle', 'SLOPMONSTER-NOTES' in default, body[:200])
        status, _, body = ask('POST', '/api/prompt', {'text': default + '\nKeep it short.'})
        check('edited instructions are saved in the test data folder',
              status == 200 and json.loads(body).get('custom') is True
              and os.path.isfile(os.path.join(data, 'cleanse.txt')), body[:200])
        status, _, body = ask('POST', '/api/prompt', {'reset': True})
        check('the instructions reset to the default', status == 200 and json.loads(body).get('custom') is False
              and not os.path.exists(os.path.join(data, 'cleanse.txt')), body[:200])

        check('instance.json is in the test data folder', os.path.isfile(os.path.join(data, 'instance.json')))
        second = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                                creationflags=NO_WINDOW)
        check('a second launch finds the first copy and leaves',
              second.returncode == 0 and f'already running at http://127.0.0.1:{port}/'
              in second.stdout.decode('utf-8', 'replace'), second.stdout[:200])

        # The sign-in wrapper, with the exe's own --version standing in for the CLI's sign-in.
        signin = subprocess.run([EXE, '--sign-in', 'claude', EXE, '--version'], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=60, creationflags=NO_WINDOW,
                                env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))
        check('the sign-in wrapper runs the command and says it is done',
              signin.returncode == 0 and b'Done. Go back to SlopMonster' in signin.stdout, signin.stdout[-200:])

        status, _, _ = ask('POST', '/api/shutdown')
        try:
            code = proc.wait(30)
        except subprocess.TimeoutExpired:
            code = None
        check('Turn off stops the process', status == 200 and code == 0, (status, code))
        check('Turn off removes instance.json', not os.path.exists(os.path.join(data, 'instance.json')))
        real_after = {n: os.stat(os.path.join(real, n)).st_mtime_ns for n in ('instance.json',)
                      if os.path.exists(os.path.join(real, n))}
        check('the real data folder was not touched', real_after == real_before, (real_before, real_after))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'{len(failures)} failed' if failures else 'all passed', flush=True)


if __name__ == '__main__':
    main()
    sys.exit(1 if failures else 0)
