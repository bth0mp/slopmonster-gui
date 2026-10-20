#!/usr/bin/env python3
"""Tests for the page. Run: python app/test_page.py

The real page from the real server, in a headless Edge or Chrome that test_page.mjs drives
over its debugging port. The model, the sign-in checks and the clipboard are stand-ins, so
no model is called, no window appears and no hotkey is registered. Needs Node 22 or newer
and Edge or Chrome; without them it says so and skips.

test_page.mjs steers the stand-ins through files in a temp folder: control.json says what
the next status and model call give, each model call is added to calls.jsonl, and a
hotkey.txt that appears is handed to the page as a Ctrl+Alt+U press would hand it.
"""
import contextlib
import http.client
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server

try:
    sys.stdout.reconfigure(errors='replace')
except (AttributeError, ValueError):
    pass


def skip(why):
    print(f'skipped: {why}')
    sys.exit(0)


node = shutil.which('node')
if not node:
    skip('Node.js is not installed.')
version = subprocess.run([node, '--version'], capture_output=True, text=True).stdout.strip()
if int((re.match(r'v(\d+)', version) or [0, 0])[1]) < 22:
    skip(f'Node 22 or newer is needed for its WebSocket, and this is {version or "unknown"}.')
browser = next((b for b in server.BROWSERS if os.path.isfile(b)), None)
if not browser:
    skip('neither Edge nor Chrome was found.')

# ── a sandbox: no real data folder, model, sign-in or clipboard ──────────────
tmp = tempfile.mkdtemp(prefix='slop-page-')
server.DATA_DIR = os.path.join(tmp, 'data')
CONTROL, CALLS, HOTKEY = (os.path.join(tmp, n) for n in ('control.json', 'calls.jsonl', 'hotkey.txt'))
MARKER = '<<<SLOPMONSTER-NOTES>>>'
# What the stand-in model changes. The first makes the page test's sample pass the checker.
FIXES = [("Are you ready to unlock seamless scheduling? Our game-changing tool empowers clinic managers — "
          "whether you're a solo practice or a hospital group — to streamline every room, every clinician, "
          "and every shift. It's not just a calendar, it's a journey.",
          'Our scheduling tool gives clinic managers one calendar for every room and every clinician.'),
         ("It's not just a tool, it's a game-changing journey.", 'It is a tool.'),
         ('4,000 clinics', 'thousands of clinics'),
         ('began shortly afterwards', 'began soon after'),
         # Text that passes, made worse: the rewrite adds a list of three.
         ('The tool reads one file.', 'The tool reads files, folders, and links.')]


def control():
    with contextlib.suppress(OSError, ValueError):
        with open(CONTROL, encoding='utf-8') as fh:
            return json.load(fh)
    return {}


_calls_lock = threading.Lock()


def fake(engine, system_prompt, message, cancel):
    """The model: waits `delay` seconds, fails with `fail` when set, else applies FIXES.
    With `fail_from`, only the calls after that many in calls.jsonl fail."""
    c = control()
    with _calls_lock, open(CALLS, 'a+', encoding='utf-8') as fh:
        fh.write(json.dumps({'engine': engine, 'custom': 'CUSTOM-PROMPT-MARK' in system_prompt,
                             'thorough': 'THOROUGH EDIT' in system_prompt}) + '\n')
        fh.seek(0)
        made = len(fh.readlines())
    for _ in range(int(float(c.get('delay', 1)) * 10)):
        if cancel.wait(0.1):
            raise server.EngineError('cancelled', server.STOPPED)
    if c.get('fail') and made > int(c.get('fail_from', 0)):
        raise server.EngineError(c['fail'], 'The stand-in model failed on purpose.')
    text = message.split('TEXT TO EDIT:\n', 1)[-1]
    for old, new in FIXES:
        text = text.replace(old, new)
    return text + '\n' + MARKER + '\n- Cut the opening question.'


def claude_status():
    how = control().get('claude')                     # ready | login | missing
    if how == 'login':
        return {'ok': False, 'code': 'not_logged_in', 'message': server.NOT_LOGGED_IN}
    if how == 'missing':
        return {'ok': False, 'code': 'missing', 'message': server.MISSING}
    return {'ok': True, 'code': 'ready', 'message': 'Ready.'}


def chatgpt_status():
    how = control().get('chatgpt', 'chatgpt')         # chatgpt | api_key | unknown | login | missing
    if how == 'login':
        return {'installed': True, 'signed_in': False, 'how': ''}
    if how == 'missing':
        return {'installed': False, 'signed_in': False, 'how': ''}
    return {'installed': True, 'signed_in': True, 'how': '' if how == 'unknown' else how}


server.run_model = lambda system_prompt, message, model, cancel: fake('claude', system_prompt, message, cancel)
server.run_codex = lambda system_prompt, message, model, effort, cancel, seen=None: \
    fake('chatgpt', system_prompt, message, cancel)
server.engine_status = claude_status
server.chatgpt_status = chatgpt_status
server.start_sign_in = lambda *a, **k: True
server.clipboard_text = lambda: control().get('clipboard')
server.set_clipboard = lambda text: True
server.launch_window = lambda url: False


def hotkey_watch():
    while True:
        time.sleep(0.2)
        with contextlib.suppress(OSError):
            with open(HOTKEY, encoding='utf-8') as fh:
                text = fh.read()
            os.remove(HOTKEY)
            server.queue_event({'event': 'hotkey', 'text': text})


def free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def word_text(path):
    """The text of a Word file's body, run by run."""
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    return ''.join(re.findall(r'<w:t(?: [^>]*)?>([^<]*)</w:t>', xml))


out = io.StringIO()
with contextlib.redirect_stdout(out):          # with no window, serve() prints its URL
    serving = threading.Thread(target=server.serve, kwargs={'port': 0, 'open_window': False}, daemon=True)
    serving.start()
    deadline = time.time() + 10
    while time.time() < deadline and not out.getvalue().endswith('\n'):
        time.sleep(0.01)
url = re.search(r'http://127\.0\.0\.1:\d+/', out.getvalue()).group(0)
threading.Thread(target=hotkey_watch, daemon=True).start()

failed = True
proc = job = None
try:
    debug = free_port()
    proc = subprocess.Popen([browser, '--headless=new', f'--remote-debugging-port={debug}',
                             '--user-data-dir=' + os.path.join(tmp, 'profile'), '--no-first-run',
                             '--no-default-browser-check', '--disable-sync', '--disable-features=msImplicitSignin',
                             '--window-size=1180,820', '--hide-scrollbars', 'about:blank'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    job = server.contain(proc)                 # Edge and every process it starts end with this one
    run = subprocess.run([node, os.path.join(HERE, 'test_page.mjs'), url, str(debug), tmp, HERE] + sys.argv[1:],
                         capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900)
    report = run.stdout + run.stderr
    failed = run.returncode != 0
    saved = os.path.join(tmp, 'saved.docx')
    if os.path.exists(saved):
        body = word_text(saved)
        if 'began soon after' not in body or 'Harbour Renewal Programme' not in body:
            failed = True
            report += f'\n  · Save as Word: the saved file does not hold the rewrite: {body[:200]!r}'
    elif not failed and (not sys.argv[1:] or 'saveWord' in sys.argv[1:]):
        failed = True
        report += '\n  · Save as Word: nothing was saved'
    print(report.rstrip())
finally:
    if proc:
        server.kill_tree(proc, job)
        server.release(job)
    with contextlib.suppress(Exception):
        conn = http.client.HTTPConnection('127.0.0.1', int(url.rsplit(':', 1)[1].strip('/')), timeout=5)
        conn.request('POST', '/api/shutdown', body=b'{}', headers={'X-Slop-Token': server.TOKEN})
        conn.getresponse().read()
    serving.join(10)
    logging.shutdown()                         # serve() leaves server.log open
    for _ in range(20):                        # the profile is free once Edge has gone
        shutil.rmtree(tmp, ignore_errors=True)
        if not os.path.exists(tmp):
            break
        time.sleep(0.25)

print('SOME TESTS FAILED' if failed else 'all tests passed')
sys.exit(1 if failed else 0)
