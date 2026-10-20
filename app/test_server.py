#!/usr/bin/env python3
"""Tests for the app server. Run: python app/test_server.py

The real server on an ephemeral port, real HTTP over 127.0.0.1, and a fake where the model
goes. Nothing here runs the claude or codex CLI or opens a browser. Where the real run_model,
run_codex and the window watcher are tested, the "CLI" is a short script in a temp folder and the "browser"
is the lock file a real one would hold.
"""
import contextlib
import errno
import hashlib
import hmac
import http.client
import http.server
import io
import json
import logging
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

# A failure detail can quote text the console codepage cannot print. Replace, do not raise.
try:
    sys.stdout.reconfigure(errors='replace')
except (AttributeError, ValueError):
    pass

fails = []


def check(name, cond, detail=''):
    if not cond:
        fails.append(f'{name}: {detail}')


def section(fn):
    """Run one group of checks now. A crash inside it is one more failure, not the end of the run.

    Most groups talk to one long-lived server. If an early failure takes that server down,
    the rest should each say so in a line rather than bury the cause under a traceback.
    """
    try:
        fn()
    except Exception as e:
        line = traceback.extract_tb(e.__traceback__)[1].lineno
        fails.append(f'{fn.__name__}: crashed at line {line} with {e!r}')


# ── a sandbox: no real data folder, no real page, no real CLI ───────────────
tmp = tempfile.mkdtemp()
server.DATA_DIR = os.path.join(tmp, 'data')
server.APP_DIR = os.path.join(tmp, 'app')
os.makedirs(server.APP_DIR)
with open(os.path.join(server.APP_DIR, 'index.html'), 'w', encoding='utf-8') as fh:
    fh.write('<meta name="slop-token" content="__SLOP_TOKEN__"><script>t="__SLOP_TOKEN__"</script>')
with open(os.path.join(server.APP_DIR, 'icon.png'), 'wb') as fh:
    fh.write(b'\x89PNG stand-in')

real_run_model, real_popen = server.run_model, subprocess.Popen
real_launch_window = server.launch_window


def no_cli(*args, **kwargs):
    return None


# The safety net. Even a test that forgets to fake run_model cannot find the real `claude`,
# nor the real `claude` or `codex` where their installers put them when not on PATH.
real_which = shutil.which
real_claude_paths = getattr(server, 'CLAUDE_PATHS', [])
shutil.which = no_cli
server.CODEX_PATHS = server.CLAUDE_PATHS = []

SLOPPY = "It's not just a tool, it's a game-changing journey."
CLEAN = 'It is a tool that checks copy.'
# Spelled out, not read from server.MARKER: it is what prompts/cleanse.txt tells the model
# to emit, so a server that split on anything else would be splitting on nothing.
MARKER = '<<<SLOPMONSTER-NOTES>>>'
NOTES = f'\n{MARKER}\n'
calls = []


def start(window=None, port=0):
    """serve() in a thread. Returns (port, thread) once the server is listening.

    With `window`, serve() runs the way the shortcut runs it. The fake launch_window
    starts no browser: 'opens' leaves the lock file a real one would hold, and 'never'
    reports a launch whose window does not appear.
    """
    urls = []

    def launch(url):
        if window == 'opens':
            open_window()
        urls.append(url)
        return True

    server.launch_window = launch
    out = io.StringIO()
    thread = threading.Thread(target=server.serve, daemon=True,
                              kwargs={'port': port, 'open_window': window is not None})
    with contextlib.redirect_stdout(out):     # with no window, serve() prints its URL
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not urls and not out.getvalue().endswith('\n'):
            time.sleep(0.01)
    return int(re.search(r':(\d+)/', (urls or [out.getvalue()])[0]).group(1)), thread


def instance_file(folder=None):
    """What instance.json in the data folder holds, or None when there is none."""
    try:
        with open(os.path.join(folder or server.DATA_DIR, 'instance.json'), encoding='utf-8') as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def request(method, path, body=None, token=server.TOKEN, headers=(), port=None):
    """One request. Returns (status, headers with lower-case names, body bytes)."""
    conn = http.client.HTTPConnection('127.0.0.1', port or PORT, timeout=30)
    head = dict(headers)
    if token:
        head['X-Slop-Token'] = token
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
    conn.request(method, path, body=body, headers=head)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data


def refused(method, path, body=None, **kwargs):
    """(status, error code) for a request that should be turned away with a JSON error."""
    status, _, data = request(method, path, body, **kwargs)
    try:
        return status, json.loads(data)['error']['code']
    except (ValueError, KeyError, TypeError):
        return status, f'not a JSON error: {data[:80]!r}'


def raw_status(head):
    """Send these exact header lines and no body. Returns the status code that comes back."""
    with socket.create_connection(('127.0.0.1', PORT), timeout=10) as s:
        s.sendall(head.encode() + b'\r\n\r\n')
        return int(s.makefile('rb').readline().split()[1])


def run(text, model='haiku', **more):
    """POST /api/unslop. Returns the events, or (status, error code) when it is refused."""
    status, head, data = request('POST', '/api/unslop', {'text': text, 'model': model, **more})
    if head.get('content-type') != 'application/x-ndjson':
        return status, json.loads(data)['error']['code']
    return [json.loads(line) for line in data.splitlines()]


def kinds(events):
    return [e['event'] for e in events if e['event'] != 'tick']


def model_says(*replies):
    """Replace run_model with a fake that gives these replies, in order, one per call."""
    queue = list(replies)

    def fake(system_prompt, user_message, model, cancel):
        calls.append({'system': system_prompt, 'message': user_message, 'model': model})
        reply = queue.pop(0)          # an unexpected extra call raises, and the run errors
        if isinstance(reply, Exception):
            raise reply
        return reply

    calls.clear()
    server.run_model = fake


entered = threading.Event()
held = {}


def blocks(system_prompt, user_message, model, cancel):
    """A model call that does not return until it is cancelled."""
    held['cancel'] = cancel
    entered.set()
    cancel.wait(30)
    raise server.EngineError('cancelled', 'stopped')


def run_in_background(into):
    """run(SLOPPY) on a thread, its events left in `into`. Returns the thread."""
    def target():
        with contextlib.suppress(Exception):      # a dead server fails a check, quietly
            into['events'] = run(SLOPPY)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


LOCK = os.path.join(server.DATA_DIR, 'browser-profile', 'lockfile')


def open_window():
    """Stand in for the browser: its lock file is there for as long as the window is open."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    open(LOCK, 'w').close()


def close_window():
    with contextlib.suppress(FileNotFoundError):
        os.remove(LOCK)


PORT, main = start()
model_says()


# ── ping, and the headers every response carries ────────────────────────────
@section
def ping_and_headers():
    status, head, data = request('GET', '/api/ping', token=None)
    check('ping answers without a token, and names no account or folder',
          status == 200 and json.loads(data) == {'app': 'slopmonster'}, f'{status} {data!r}')
    # The copy notes its port and a key in the data folder, which other accounts cannot read.
    # The ping proves the copy holds that key, for a nonce the caller chose, and on this port.
    held = instance_file()
    check('instance.json: the port and a 32-byte key, once the copy listens',
          held and held.get('port') == PORT and re.fullmatch(r'[0-9a-f]{64}', held.get('key', '')), held)
    nonce = os.urandom(16).hex()
    status, _, data = request('GET', '/api/ping?n=' + nonce, token=None)
    want = hmac.new(bytes.fromhex(held['key']), f'{nonce}:{PORT}'.encode(), hashlib.sha256).hexdigest()
    check('ping: a nonce gets the proof, and nothing else about the key',
          json.loads(data) == {'app': 'slopmonster', 'proof': want}, data)
    for bad in ('abc', 'zz' * 16, '0' * 65, nonce + '&n=1'):
        status, _, data = request('GET', '/api/ping?n=' + bad, token=None)
        check(f'ping: no proof for the nonce {bad[:12]!r}', json.loads(data) == {'app': 'slopmonster'}, data)
    check('ping: the copy that wrote instance.json is this account\'s', server.already_running(PORT))
    check('responses are not cached', head.get('cache-control') == 'no-store', head)
    check('responses are not sniffed', head.get('x-content-type-options') == 'nosniff', head)
    check('responses send no referrer', head.get('referrer-policy') == 'no-referrer', head)
    check('no CORS headers', not any(k.startswith('access-control') for k in head), head)


# ── the trust boundary ──────────────────────────────────────────────────────
@section
def trust_boundary():
    # /api/shutdown is in this list on purpose. If a request without the token could stop
    # the server, every group below this one would fail to connect.
    for name, token in (('missing', None), ('wrong', 'x' * 43), ('non-ASCII', 'töken')):
        for method, path in (('GET', '/api/status'), ('POST', '/api/check'),
                             ('POST', '/api/unslop'), ('GET', '/api/events'),
                             ('POST', '/api/hotkey'), ('POST', '/api/shutdown')):
            body = {'text': SLOPPY} if method == 'POST' else None
            got = refused(method, path, body, token=token)
            check(f'{name} token is refused on {path}', got == (403, 'forbidden'), got)

    # DNS rebinding: the attacker's hostname resolves here, and the Host header gives it away.
    for path in ('/', '/api/ping', '/api/status'):
        got = refused('GET', path, headers={'Host': f'evil.example:{PORT}'})
        check(f'foreign Host is refused on {path}', got == (403, 'forbidden'), got)
    got = refused('GET', '/api/ping', headers={'Host': '127.0.0.1'})
    check('Host without the port is refused', got == (403, 'forbidden'), got)
    status, _, _ = request('GET', '/api/ping', headers={'Host': f'localhost:{PORT}'})
    check('localhost is an accepted Host', status == 200, status)

    for origin in ('http://evil.example', 'null', f'https://127.0.0.1:{PORT}', 'http://127.0.0.1'):
        got = refused('POST', '/api/check', {'text': SLOPPY}, headers={'Origin': origin})
        check(f'Origin {origin} is refused', got == (403, 'forbidden'), got)
    for origin in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'):
        status, _, _ = request('POST', '/api/check', {'text': SLOPPY}, headers={'Origin': origin})
        check(f'own Origin {origin} is accepted', status == 200, status)

    # A link or a script on another site, or on another port here, can open the page in the
    # owner's own browser, where it would turn the hotkey back on. The browser says who sent it.
    for site in ('cross-site', 'same-site'):
        for path in ('/', '/api/ping'):
            got = refused('GET', path, token=None, headers={'Sec-Fetch-Site': site})
            check(f'Sec-Fetch-Site {site} is refused on {path}', got == (403, 'forbidden'), got)
    for site in ('none', 'same-origin'):
        status, _, _ = request('GET', '/', token=None, headers={'Sec-Fetch-Site': site})
        check(f'Sec-Fetch-Site {site} is accepted', status == 200, status)


# ── the page ─────────────────────────────────────────────────────────────────
@section
def page():
    status, head, data = request('GET', '/', token=None)
    check('page is served', status == 200 and head.get('content-type', '').startswith('text/html'),
          f'{status} {head}')
    check('token is written into the page',
          data.count(server.TOKEN.encode()) == 2 and b'__SLOP_TOKEN__' not in data, data)
    csp = head.get('content-security-policy', '')
    for rule in ("default-src 'self'", "img-src 'self' data:", "connect-src 'self'", "frame-ancestors 'none'"):
        check(f'page CSP has {rule}', rule in csp, csp)
    check('page CSP names no other origin', 'http' not in csp and '*' not in csp, csp)
    status, head, data = request('GET', '/icon.png', token=None)
    check('icon is served', (status, head.get('content-type'), data) ==
          (200, 'image/png', b'\x89PNG stand-in'), f'{status} {head}')
    check('unknown path is a JSON 404', refused('GET', '/nope') == (404, 'not_found'))
    check('unsupported method is a JSON error',
          refused('PUT', '/api/check') == (501, 'bad_request'), refused('PUT', '/api/check'))


# ── /api/check ───────────────────────────────────────────────────────────────
@section
def check_shape():
    status, head, data = request('POST', '/api/check', {'text': SLOPPY})
    r = json.loads(data)
    cats = r['categories']
    check('check answers JSON',
          status == 200 and head.get('content-type') == 'application/json', head)
    check('check has the four fields', set(r) == {'words', 'score', 'clean', 'categories'}, set(r))
    check('all six categories, in order', [c['key'] for c in cats] == [
        'vocab', 'phrases', 'punctuation', 'rhythm', 'proof', 'placeholders'], cats)
    check('category titles', [c['title'] for c in cats] == [
        'Vocabulary', 'Sentence patterns', 'Punctuation', 'Three-item lists',
        'Possible unsupported proof', 'Placeholders to fill in'], [c['title'] for c in cats])
    check('proof, lists of three and placeholders are advisory',
          [c['advisory'] for c in cats] == [False] * 3 + [True] * 3, cats)
    check('sloppy text: words, score, clean',
          (r['words'], r['score'], r['clean']) == (9, 3, False), r)
    check('vocabulary hits carry a count', cats[0]['hits'] == [
        {'label': 'game-changing', 'detail': '×1'},
        {'label': 'journey', 'detail': '×1'}], cats[0])
    check('construction hits carry a count, and what they matched', cats[1]['hits'] == [
        {'label': "the 'not just X, it's Y' construction", 'detail': '×1',
         'match': '"not just a tool, it"'}], cats[1])
    check('passing categories have no hits', [c['hits'] for c in cats[2:]] == [[], [], [], []], cats)

    r = server.check('It is faster, smarter, and better.')
    check('rhythm hits carry the snippet', r['categories'][3]['hits'] == [
        {'label': 'rule-of-three list', 'detail': 'faster, smarter, and better'}], r)
    r = server.check('Our team — trained, certified and local — is ready to help.')
    check('punctuation hits carry the snippet',
          r['categories'][2]['hits'][0]['detail'].startswith('Our team'), r['categories'][2])

    # Proof alone costs a point and still counts as clean: only the author can verify it.
    r = server.check('Trusted by 25 businesses.')
    check('proof is a plain label', r['categories'][4]['hits'] == [
        {'label': '25 businesses', 'detail': ''}], r['categories'][4])
    check('proof does not block clean', (r['score'], r['clean']) == (4, True), r)

    # A chat reply around a draft: its lines are read as pasted, not only as prose.
    r = server.check("Here's a draft you can adapt:\n\n---\n\nHi Sam,\n\nThe room is booked for "
                     "Monday.\n\nBest,\n[Your Name]\n\n---\n\nA couple of notes: I kept it short.")
    check('a chat reply around a draft is flagged', r['clean'] is False and r['categories'][1]['hits'], r)
    check('its placeholder is listed, once, as a plain label', r['categories'][5]['hits'] == [
        {'label': '[Your Name]', 'detail': ''}], r['categories'][5])
    # A placeholder alone costs nothing and still counts as clean: only the author can fill it in.
    r = server.check('Hi Sam,\n\nThe room is booked for Monday.\n\nBest,\n[Your Name]')
    check('a placeholder alone does not block clean or cost a point',
          (r['score'], r['clean'], server.placeholders(r)) == (5, True, ['[Your Name]']), r)

    for nothing in ('', '   \n', '```\ncode only\n```'):
        status, _, data = request('POST', '/api/check', {'text': nothing})
        r = json.loads(data)
        check('empty text has no score', status == 200 and (r['words'], r['score']) == (0, None), r)
        check('empty text still lists six categories',
              len(r['categories']) == 6 and not any(c['hits'] for c in r['categories']), r)


@section
def score_is_upstreams():
    # The score is whatever upstream says it is. Ask the upstream CLI and compare.
    for text in (SLOPPY, 'Trusted by 25 businesses.', 'Six nails per shingle, every shingle.',
                 'We leverage — truly — a seamless, robust, and holistic journey; it is '
                 'not just fast, it is 10,000 users strong.'):
        cli = subprocess.run([sys.executable, os.path.join(server.REPO, 'tools', 'deslop.py'),
                              '--markdown', '--text', text],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
        want = re.search(r'score (\d)/5', cli.stdout)
        check('score matches the upstream CLI', want and
              server.check(text)['score'] == int(want[1]), f'{text!r}: {cli.stdout[-40:]!r}')


@section
def bad_bodies():
    for name, body in (('not JSON', b'not json'), ('a JSON list', b'[1, 2]'),
                       ('text that is not a string', b'{"text": 5}'), ('no text', b'{}'),
                       ('100,000 open brackets', b'[' * 100_000),
                       ('bad UTF-8', b'{"text": "\xff"}')):
        got = refused('POST', '/api/check', body)
        check(f'{name} is a bad request', got == (400, 'bad_request'), got)
    got = raw_status(f'POST /api/check HTTP/1.1\r\nHost: 127.0.0.1:{PORT}')
    check('a body with no Content-Length is refused', got == 411, got)
    # No body follows these headers. A server that tried to read that many bytes would hang.
    got = raw_status(f'POST /api/check HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n'
                     'Content-Length: 1300000')
    check('an oversized body is refused before it is read', got == 413, got)
    got = raw_status(f'POST /api/extract HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n'
                     'Content-Length: 31000000')
    check('an oversized file is refused before it is read', got == 413, got)
    # The big allowance is for the page alone. Without the token even 2 MB is refused unread.
    got = raw_status(f'POST /api/extract HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n'
                     'Content-Length: 2000000')
    check('a file from a stranger gets no big allowance', got == 413, got)


# ── /api/unslop: refusals come before any stream, and before any model call ─
@section
def unslop_refusals():
    model_says()
    check('empty text is refused', run('  \n ') == (400, 'empty'), run('  \n '))
    check('prose-free text is refused', run('```\ncode\n```') == (400, 'empty'))
    # Text that is all code blocks is not "no text": the user is told why nothing runs (QA FR-4).
    said = json.loads(request('POST', '/api/unslop', {'text': '```python\ndef add(a, b):\n    return a + b\n```',
                                                      'model': 'haiku'})[2]).get('error', {})
    check('code blocks alone: the refusal says they are left as they are',
          said.get('code') == 'empty' and said.get('message') == server.NO_PROSE, said)
    said = json.loads(request('POST', '/api/unslop', {'text': ' \n ', 'model': 'haiku'})[2]).get('error', {})
    check('blank text: the refusal does not talk about code',
          said.get('code') == 'empty' and said.get('message') != server.NO_PROSE, said)
    check('unknown model is refused', run(SLOPPY, model='gpt-4') == (400, 'bad_model'))
    check('non-string model is refused', run(SLOPPY, model=['haiku']) == (400, 'bad_model'))
    check('200,001 characters is too long', run('a' * 200_001) == (413, 'too_long'))
    check('non-string context is refused', run(SLOPPY, context={'a': 1}) == (400, 'bad_request'))
    check('a refused request never reaches the model', not calls, calls)
    longest = 'word ' * 40_000
    model_says()
    check('200,000 characters is accepted', kinds(run(longest))[-1:] == ['done'])


# ── the happy path: one round ───────────────────────────────────────────────
@section
def one_round():
    with open(os.path.join(server.REPO, 'prompts', 'cleanse.txt'), encoding='utf-8') as fh:
        prompt = fh.read()
    check('the prompt still asks for the marker this app splits on', MARKER in prompt)
    # A draft shown in a '>' blockquote came back with '> ' on every line (review E2E3-2).
    check('PASS 0 asks for a quoted draft back without its quote marks',
          'If the reply shows the draft inside a blockquote or quotation marks, return the draft without them.'
          in ' '.join(prompt.split()), prompt[:400])

    model_says(CLEAN + '\r\n' + MARKER + '  \r\n- Removed the contrast.\n\n'
               '* Dropped two vague words.\n')
    status, head, data = request('POST', '/api/unslop', {
        'text': SLOPPY, 'model': 'haiku', 'context': '  A README\nfor developers '})
    ev = [json.loads(line) for line in data.splitlines()]
    check('stream is NDJSON',
          status == 200 and head.get('content-type') == 'application/x-ndjson', head)
    check('stream has no length and closes',
          'content-length' not in head and head.get('connection') == 'close', head)
    check('one round: events', kinds(ev) == ['start', 'round', 'checked', 'done'], kinds(ev))
    begin, done = ev[0], ev[-1]
    check('start carries the draft result',
          begin['rounds'] == 3 and begin['before']['score'] == 3, begin)
    check('round is announced', ev[1] == {'event': 'round', 'round': 1, 'of': 3}, ev[1])
    check('rewrite is checked', ev[2]['round'] == 1 and ev[2]['check']['clean'] is True, ev[2])
    check('done has the copy only', done['text'] == CLEAN, done['text'])
    check('done has the notes, bullets stripped',
          done['notes'] == ['Removed the contrast.', 'Dropped two vague words.'], done['notes'])
    check('done has before and after', done['before'] == begin['before'] and
          done['after'] == ev[2]['check'] and done['after']['score'] == 5, done)
    check('done has rounds, model and ms', (done['rounds'], done['model']) == (1, 'haiku') and
          isinstance(done['ms'], int), done)

    system, message = calls[0]['system'], calls[0]['message']
    check('system prompt is cleanse.txt minus its last line', system and prompt.startswith(system)
          and prompt[len(system):].strip() == 'TEXT TO EDIT:', system[-80:])
    check('model choice reaches the engine', calls[0]['model'] == 'haiku', calls[0]['model'])
    check('message opens with the context on one line',
          message.startswith('Context: A README for developers\n'), message[:60])
    check('message names what the checker found',
          'game-changing' in message and "not just X, it's Y" in message, message)
    check('message lets a necessary match stand, from the first round',
          'Fix each one as part of your edit.' + server.UNLESS in message, message)
    check('message carries the two hard rules',
          'Never invent a number' in message and 'code formatting' in message, message)
    check('message lets a number go with the chat framing around the draft',
          'never delete one from the draft' in message and 'deleted chat framing' in message, message)
    check('message quotes what each sentence pattern matched',
          "Y' construction (×1, matched: \"not just a tool, it\")" in message, message)
    check('message ends with the draft',
          message.endswith('TEXT TO EDIT:\n' + SLOPPY), message[-80:])


# ── a run nobody pressed a button for skips text that has nothing to fix ────
@section
def automatic_runs():
    model_says()
    ev = run(CLEAN, auto=True)
    done = ev[-1]
    check('auto: text that passes is not sent to the model',
          kinds(ev) == ['start', 'done'] and not calls, (kinds(ev), calls))
    check('auto: it comes back as it was, in no rounds',
          (done['text'], done['rounds'], done['after']) == (CLEAN, 0, done['before']), done)
    model_says(CLEAN + NOTES)
    check('auto: flagged text still runs',
          kinds(run(SLOPPY, auto=True))[-1] == 'done' and len(calls) == 1, len(calls))
    # A draft with a [Your Name] in it passes, but it is most likely a chat reply's template.
    model_says('Hi Sam, the room is booked.\n\n[Your Name]' + NOTES)
    ev = run('Hi Sam, the room is booked.\n\n[Your Name]', auto=True)
    check('auto: text that passes with a placeholder to fill in still runs',
          ev[-1]['rounds'] == 1 and len(calls) == 1, (kinds(ev), calls))
    model_says(CLEAN + NOTES)
    check('by hand: text that passes is still edited',
          run(CLEAN)[-1]['rounds'] == 1 and len(calls) == 1, len(calls))
    model_says(CLEAN + NOTES)
    check('auto has to be exactly true', run(CLEAN, auto='yes')[-1]['rounds'] == 1, len(calls))
    # The "Rewrite text that already passes" switch sends always: an automatic run then runs.
    model_says(CLEAN + NOTES)
    ev = run(CLEAN, auto=True, always=True)
    check('auto with always: text that passes is sent to the model',
          kinds(ev)[-1] == 'done' and ev[-1]['rounds'] == 1 and len(calls) == 1, (kinds(ev), calls))
    model_says()
    ev = run(CLEAN, auto=True, always='yes')
    check('always has to be exactly true', kinds(ev) == ['start', 'done'] and not calls,
          (kinds(ev), calls))


# ── proof is advisory: it is named to the model and never costs a round ─────
@section
def proof_is_advisory():
    model_says('We use feedback from 25 businesses.' + NOTES + '- Swapped one word.')
    ev = run('We leverage feedback from 25 businesses.')
    done = ev[-1]
    check('proof left in place ends the run',
          kinds(ev) == ['start', 'round', 'checked', 'done'], kinds(ev))
    check('proof left in place is clean at 4/5',
          (done['after']['clean'], done['after']['score'], done['rounds']) == (True, 4, 1), done)
    check('the flagged number is named to the model', '25 businesses' in calls[0]['message']
          and 'exactly as written' in calls[0]['message'], calls[0]['message'])
    model_says('We leverage feedback from 25 businesses and 900 users.' + NOTES,
               'We use feedback from 25 businesses.' + NOTES)
    run('We leverage feedback from 25 businesses.')
    asked = calls[1]['message'].split('TEXT TO EDIT:')[0]
    check('a figure the model made up is not named as one to keep',
          '25 businesses' in asked and '900 users' not in asked, asked)


# ── a second round when the first rewrite is still sloppy ───────────────────
@section
def two_rounds():
    model_says('We leverage a tool.' + NOTES + '- First pass.',
               'We use a tool.' + NOTES + '- Second pass.')
    ev = run(SLOPPY)
    done = ev[-1]
    check('two rounds: events',
          kinds(ev) == ['start', 'round', 'checked', 'round', 'checked', 'done'], kinds(ev))
    check('two rounds: first still flagged, second clean',
          (ev[2]['check']['clean'], ev[4]['check']['clean']) == (False, True), ev)
    check('two rounds: result', (done['text'], done['rounds']) == ('We use a tool.', 2), done)
    check('two rounds: notes from both',
          done['notes'] == ['First pass.', 'Second pass.'], done['notes'])
    # A note the model repeats in a later round is shown once.
    model_says('We leverage a tool.' + NOTES + '- Cut the opener.',
               'We use a tool.' + NOTES + '- cut  the opener\n- Kept the facts.')
    again_done = run(SLOPPY)[-1]
    check('two rounds: a note said again is listed once',
          again_done.get('notes') == ['Cut the opener.', 'Kept the facts.'], again_done.get('notes'))
    check('notes: case, spacing and the full stop do not make a note new',
          server.fresh_notes(['Cut it.'], ['cut it', 'CUT  IT!', 'Kept it.', 'kept it']) == ['Kept it.'])
    again = calls[1]['message']
    check('round two sends the current copy, not the draft',
          again.endswith('TEXT TO EDIT:\nWe leverage a tool.'), again[-60:])
    check('round two sends what is still flagged',
          'leverage' in again.split('TEXT TO EDIT:')[0], again)
    check('round two drops what was fixed',
          'journey' not in again and 'not just' not in again, again)


# ── never clean: the best candidate comes back, and a later round wins a tie ─
@section
def best_candidate():
    two_flags = "It's not just a tool, it's a journey."
    model_says(two_flags + NOTES + '- one', 'It is a seamless tool.' + NOTES + '- two',
               'It is a robust tool.' + NOTES + '- three')
    done = run(SLOPPY)[-1]
    check('three rounds, then stop', done['rounds'] == 3 and len(calls) == 3, done)
    check('a tie goes to the later round', done['text'] == 'It is a robust tool.', done['text'])
    check('a still-flagged result says so', done['after']['clean'] is False, done['after'])
    check('a run that used every round did not stop early', 'stopped' not in done, done.get('stopped'))
    model_says('It is a seamless tool.' + NOTES + '- one', two_flags + NOTES + '- two',
               two_flags + NOTES + '- three')
    done = run(SLOPPY)[-1]
    check('a worse later round does not replace a better one',
          (done['text'], done['notes']) == ('It is a seamless tool.', ['one']), done)
    after = {c['key']: [h['label'] for h in c['hits']] for c in done['after']['categories']}
    check('and the check returned is that round\'s',
          after['vocab'] == ['seamless'] and not after['phrases'], after)
    check('a round that fixes nothing ends the run',
          done['rounds'] == 2 and len(calls) == 2 and done.get('stopped') == 'no_progress',
          (done['rounds'], len(calls), done.get('stopped')))


# ── a round that fixes nothing ends the run; one that fixes something does not ─
@section
def no_progress():
    check('flags: every repeat counts, and proof does not', server.flag_count(server.check(SLOPPY)) == 3
          and server.flag_count(server.check('We leverage and leverage it.')) == 2
          and server.flag_count(server.check('We leverage feedback from 25 businesses.')) == 1,
          server.check('We leverage and leverage it.'))
    model_says('We leverage a seamless tool.' + NOTES, 'We leverage a tool.' + NOTES, 'We use a tool.' + NOTES)
    done = run(SLOPPY)[-1]
    check('fewer flags each round: every round runs',
          (done['rounds'], len(calls), done['after']['clean']) == (3, 3, True) and 'stopped' not in done, done)
    model_says('It is a seamless tool.' + NOTES + '- one', 'It is a robust tool.' + NOTES + '- two')
    done = run(SLOPPY)[-1]
    check('as many flags as the round before: stop, and the later round wins the tie',
          (done['rounds'], len(calls), done['text'], done.get('stopped'))
          == (2, 2, 'It is a robust tool.', 'no_progress'), done)
    model_says('It is a seamless tool.' + NOTES + '- one', 'It is a seamless tool.' + NOTES + '- kept')
    done = run(SLOPPY)[-1]
    check('a round sent back unchanged ends the run, which says Claude kept the rest',
          (done['rounds'], len(calls), done['text'], done.get('stopped'))
          == (2, 2, 'It is a seamless tool.', 'kept'), done)
    # Unchanged, but an earlier round with fewer kinds of flag is the result: not what was kept.
    model_says('It is a seamless, robust tool that we leverage.' + NOTES,
               "It's not just a tool, it's a seamless one." + NOTES, "It's not just a tool, it's a seamless one." + NOTES)
    done = run(SLOPPY)[-1]
    check('a round sent back unchanged that is not the result says nothing was kept',
          (done['rounds'], done['text']) == (3, 'It is a seamless, robust tool that we leverage.')
          and 'stopped' not in done, done)
    # A reply in the wrong shape is not a round that fixed anything or nothing. The merged
    # reply here is clean: counted, it would make the next round look stuck.
    draft = "# Plan\n\nIt's not just a tool, it's a game-changing journey.\n\n- First item\n\n- Second item"
    merged = '# Plan\n\nIt is a tool.\n\n- First item\n- Second item'
    model_says(merged + NOTES, draft.replace(SLOPPY, 'It is a seamless tool.') + NOTES,
               draft.replace(SLOPPY, 'It is a robust tool.') + NOTES)
    done = run(draft, structure=True)[-1]
    check('keep: a reply sent back for its shape never counts as a round to beat',
          (done['rounds'], len(calls), done['text']) == (3, 3, draft.replace(SLOPPY, 'It is a robust tool.'))
          and 'stopped' not in done, (done['rounds'], len(calls), done.get('stopped')))


# ── a rewrite of text that passes never comes back worse than the text ──────
@section
def never_worse():
    # Round one adds a word; round two keeps it, citing the exception (DAILY-3).
    worse = 'It is a robust tool that checks copy.'
    model_says(worse + NOTES + '- one', worse + NOTES + '- a product term')
    done = run(CLEAN)[-1]
    check('a draft that passes beats every round that added a pattern',
          (done['text'], done['after']['clean'], done['rounds'], done.get('stopped'))
          == (CLEAN, True, 2, 'added'), done)
    check('and the notes say why the text is as it was',
          len(done['notes']) == 1 and 'as it was' in done['notes'][0], done['notes'])
    check('round two is told the edit added it, without the exception',
          'Your last edit added' in calls[1]['message'] and server.UNLESS not in calls[1]['message']
          and 'still flags' not in calls[1]['message'], calls[1]['message'])
    # A pattern the draft had keeps the exception; one the edit added does not get it.
    draft = 'We use feedback from clients. It is a seamless tool.'
    model_says('We leverage feedback from clients. It is a seamless tool.' + NOTES,
               'We use feedback from clients. It is a tool.' + NOTES)
    run(draft)
    second = calls[1]['message']
    check('round two: what the draft had is still flagged, with the exception',
          'still flags the following' in second and server.UNLESS in second
          and 'Your last edit added' in second
          and second.index('seamless') < second.index('Your last edit added'), second)
    check('round two: and what the edit added is listed after it',
          'leverage' in second[second.index('Your last edit added'):].split('TEXT TO EDIT:')[0], second)
    # A draft that was flagged is not put back: a round that is better is the result.
    model_says('It is a seamless tool.' + NOTES, 'It is a seamless tool.' + NOTES)
    done = run(SLOPPY)[-1]
    check('a flagged draft is never the result', done['text'] == 'It is a seamless tool.', done)


# ── a list of three costs a point, is asked about once, and never costs a round ─
@section
def lists_of_three_are_advisory():
    r = server.check('It is faster, smarter, and better.')
    rhythm = r['categories'][3]
    check('a list of three alone is clean at 4/5', (r['clean'], r['score']) == (True, 4), r)
    check('and is advisory, with its snippet', rhythm['advisory'] is True and rhythm['hits'] == [
        {'label': 'rule-of-three list', 'detail': 'faster, smarter, and better'}], rhythm)
    check('it is not counted as a flag', server.flag_count(r) == 0, r)
    # Round one names it, with the exception for a real list; a rewrite that keeps it passes.
    model_says('We use feedback, notes, and calls.' + NOTES)
    ev = run('We leverage feedback, notes, and calls.')
    done = ev[-1]
    check('a list of three left in place ends the run in one round',
          (done['rounds'], len(calls), done['after']['clean']) == (1, 1, True), done)
    check('round one names the list of three, with the exception',
          'Three-item lists' in calls[0]['message'] and server.UNLESS in calls[0]['message'],
          calls[0]['message'])
    # Later rounds do not ask about it again.
    model_says('We leverage a tool for tone, length, and style.' + NOTES,
               'We use a tool for tone, length, and style.' + NOTES)
    done = run('We leverage a seamless tool for tone, length, and style.')[-1]
    asked = calls[1]['message'].split('TEXT TO EDIT:')[0]
    check('round two does not ask about a list of three',
          'Three-item lists' not in asked and 'tone, length' not in asked and 'leverage' in asked, asked)
    check('and the run ends clean', (done['rounds'], done['after']['clean']) == (2, True), done)
    # A list of three the edit added is not listed as added.
    model_says('We leverage a tool for notes, calls, and emails.' + NOTES,
               'We use a tool for notes, calls, and emails.' + NOTES)
    run('We leverage a seamless tool.')
    asked = calls[1]['message'].split('TEXT TO EDIT:')[0]
    check('a list of three the edit added is not named in round two',
          'Your last edit added' not in asked and 'notes, calls' not in asked, asked)
    # An automatic run on text whose only hit is a list of three is skipped, as text that passes is.
    listed = 'The app checks tone, length, and style for you.'
    model_says()
    ev = run(listed, auto=True)
    check('auto: a paste whose only hit is a list of three is not sent',
          kinds(ev) == ['start', 'done'] and not calls and ev[-1]['rounds'] == 0, (kinds(ev), calls))
    check('its flag count is 0', server.flag_count(server.check(listed)) == 0, server.check(listed))
    model_says(listed + NOTES)
    check('by hand: it is still sent', run(listed)[-1]['rounds'] == 1 and len(calls) == 1, len(calls))


# ── a chat answer's bold run-in labels are folded in round one, as Copy writes plain text ─
@section
def bold_labels():
    three = '- **Cost:** It is cheap.\n- **Speed:** It is quick.\n- **Support:** It is local.'
    check('labels: three labelled bullets give three labels',
          server.bold_labels(three) == ['Cost:', 'Speed:', 'Support:'], server.bold_labels(three))
    check('labels: numbered items and paragraphs count too', len(server.bold_labels(
        '1. **Cost:** It is cheap.\n2. **Speed:** It is quick.\n\n**Why it fits:**\n\nIt is local.')) == 3)
    check('labels: none in a text with a heading', server.bold_labels('## Plan\n\n' + three) == [])
    check('labels: two are not enough', server.bold_labels(three.rsplit('\n', 1)[0]) == [])
    check('labels: none inside a fenced block', server.bold_labels('```\n' + three + '\n```') == [])
    check('labels: a bold Subject line is not one',
          server.bold_labels('**Subject:** Hi\n\n' + three.rsplit('\n', 1)[0]) == [])
    check('labels: none in a Word file', server.bold_labels(three, document=True) == [])
    started = time.monotonic()
    server.bold_labels('*' * 200_000)
    server.bold_labels('**' + 'a' * 200_000)
    check('labels: a long run of asterisks returns at once', time.monotonic() - started < 1,
          time.monotonic() - started)

    # Round one asks for them to be folded; round two does not ask again.
    draft = 'We leverage a seamless tool.\n\n' + three
    model_says('We leverage a tool.\n\n' + three + NOTES, 'We use a tool.\n\n' + three + NOTES)
    done = run(draft)[-1]
    first = calls[0]['message'].split('TEXT TO EDIT:')[0]
    check('labels: round one asks for them to be folded, naming them',
          'bold label' in first and '"**Cost:**"' in first, first)
    check('labels: the ask comes before the rules',
          first.index('bold label') < first.index('Never invent a number'), first)
    # A title, a section name or a price on a line of its own stays a plain line, not a sentence:
    # "**Details**" made into "The mug has the following features:" reads worse than before.
    check('labels: a label on a line of its own stays a short line, not a sentence',
          'A label on a line of its own, such as a title, a section name or a price, stays a short line' in first
          and 'never make it into a sentence' in first and 'into a plain sentence' not in first, first)
    asked = calls[1]['message'].split('TEXT TO EDIT:')[0]
    check('labels: round two does not ask again', 'bold label' not in asked and 'Cost' not in asked, asked)
    check('labels: labels left in the result are counted', done.get('labels_left') == 3, done.get('labels_left'))
    model_says('We use a tool.\n\n- It is cheap.\n- It is quick.\n- It is local.' + NOTES)
    done = run(draft)[-1]
    check('labels: none counted when the rewrite folded them', 'labels_left' not in done, done.get('labels_left'))

    # A Word file keeps its bold.
    blocks = 'We leverage a tool.\n\n- **Cost:** It is cheap.\n\n- **Speed:** It is quick.\n\n- **Support:** It is local.'
    model_says(blocks.replace('leverage', 'use') + NOTES)
    done = run(blocks, structure=True)[-1]
    check('labels: a Word file is not asked', len(calls) == 1 and 'bold label' not in calls[0]['message']
          and 'labels_left' not in done, calls)

    # A long text with a heading: the part with the labels has none of its own, and is still not asked.
    plain = ('The tool reads a file and reports what it finds. ' * 20).strip()
    slop = "It's not just a tool, it's a game-changing journey."
    long = '\n\n'.join(['# Guide'] + [plain] * 6 + [slop, three] + [plain] * 7 + [slop])
    check('labels: the long text is in parts, its labels not in the first',
          len(server.split_parts(long)) > 1 and 'Cost' not in server.split_parts(long)[0])
    calls.clear()

    def fixes(system_prompt, user_message, model, cancel):
        calls.append({'message': user_message})
        return user_message.split('TEXT TO EDIT:\n', 1)[1].replace(slop, 'It is a tool.') + NOTES

    server.run_model = fixes
    done = run(long)[-1]
    check('labels: a long text with a heading is never asked',
          done['event'] == 'done' and calls and not any('bold label' in c['message'] for c in calls),
          (done.get('event'), len(calls)))
    # With no heading, the part that has the labels is sent and asked, though it passes.
    calls.clear()
    long = '\n\n'.join([plain] * 6 + [slop] + [plain] * 7 + ['Here is the plan.', three])
    done = run(long)[-1]
    asked = [c for c in calls if 'bold label' in c['message']]
    check('labels: in a long text with no heading, the part with labels is sent and asked',
          done['event'] == 'done' and len(calls) == 2 and len(asked) == 1 and 'Cost' in asked[0]['message'],
          (done.get('event'), len(calls), len(asked)))

    # A text that passes but has labels is still sent by an automatic run.
    passing = 'Here is the plan.\n\n' + three
    check('labels: the passing sample passes', server.check(passing)['clean'], server.check(passing))
    model_says('Here is the plan.\n\n- It is cheap.\n- It is quick.\n- It is local.' + NOTES)
    ev = run(passing, auto=True)
    check('auto: a passing text with three labels still runs',
          ev[-1]['rounds'] == 1 and len(calls) == 1 and 'bold label' in calls[0]['message'], (kinds(ev), calls))


# ── no marker: fail open, as tools/cleanse.sh does ──────────────────────────
@section
def marker():
    model_says('It is a tool.\n\n- Removed the contrast.')
    done = run(SLOPPY)[-1]
    check('no marker: the whole reply is copy',
          done['text'] == 'It is a tool.\n\n- Removed the contrast.', done['text'])
    check('no marker: the user is told to check the tail',
          len(done['notes']) == 1 and 'end of the text' in done['notes'][0], done['notes'])
    # A draft that contains the marker must not lose everything beneath it.
    model_says('Before.' + NOTES + 'After.' + NOTES + '- note')
    done = run(SLOPPY)[-1]
    check('only the last marker divides copy from notes',
          (done['text'], done['notes']) == ('Before.' + NOTES + 'After.', ['note']), done)


# ── replies the checker cannot see into ─────────────────────────────────────
@section
def hidden_rewrites():
    # A rewrite wrapped whole in a code fence is taken out of it and scored.
    model_says('```markdown\n' + CLEAN + '\n```' + NOTES + '- note')
    done = run(SLOPPY)[-1]
    check('a fenced rewrite is unwrapped',
          done.get('text') == CLEAN and done['after']['score'] == 5, done)
    model_says('```\n' + SLOPPY + '\n```' + NOTES)
    done = run(SLOPPY)[-1]
    check('slop inside a fence is still flagged',
          done.get('text') == SLOPPY and done['after']['clean'] is False, done)
    # The same reply is also the draft handed back untouched, which ends the run.
    check('text returned unchanged is not sent again, and the result says it was kept',
          done.get('rounds') == 1 and len(calls) == 1 and done.get('stopped') == 'kept',
          (done.get('rounds'), len(calls), done.get('stopped')))
    # A line of preamble and the rewrite in a fence: nearly nothing to score, so not a pass.
    draft = SLOPPY + ' The tool reads a file and reports what it finds.' * 4
    model_says('Here is the edit:\n```\n' + draft + '\n```' + NOTES)
    err = run(draft)[-1]
    check('a reply with almost no prose is an error, not a pass',
          err.get('error', {}).get('code') == 'empty_output', err)
    # A chat answer loses its reply on purpose (PASS 0), so the floor is the draft's own words.
    # Three short options under an offer: the first is far under a quarter of the paste.
    options = ('**Option 1**\n\nFresh bread on Saturdays.\n\n'
               '**Option 2**\n\nEvery Saturday morning the ovens are on early, and the sourdough, '
               'the rye and the seeded loaves are out of them by eight.\n\n'
               '**Option 3**\n\nCome by on Saturday for bread still warm from the oven, rolls for '
               'the week ahead and a coffee while you wait for the last batch.\n\n'
               'If you tell me the name of the bakery, I can tailor these further.')
    model_says('Fresh bread on Saturdays.' + NOTES + '- Kept Option 1.')
    done = run(options, auto=True)[-1]
    check('several versions: the first alone is a reply, not an empty one',
          done.get('event') == 'done' and done.get('text') == 'Fresh bread on Saturdays.', done)
    # A one-line message with a long list of tips under it.
    tips = ('Hey team, the coffee machine on floor 3 works again.\n\n---\n\n**A few tips:**\n\n'
            '- Run a cycle of plain water through it first thing, so that the first cup of the day '
            'does not taste of yesterday.\n- Empty the drip tray before it fills, because it '
            'overflows onto the counter and soaks the paper towels.\n- Keep the beans in the sealed '
            'tin, not the open hopper, or they go stale within a couple of days.\n- If it beeps '
            'twice, the descaling light is on, and the kit is in the cupboard under the sink.')
    model_says('Hey team, the coffee machine on floor 3 works again.' + NOTES + '- Cut the tips.')
    done = run(tips, auto=True)[-1]
    check('a short draft with long tips: the draft alone is a reply',
          done.get('event') == 'done' and done.get('text').startswith('Hey team'), done)
    model_says(NOTES + '- Kept nothing.')
    err = run(options, auto=True)[-1]
    check('several versions: a reply with no prose is still empty_output',
          err.get('error', {}).get('code') == 'empty_output', err)
    # Menus the rules cannot see whole: a numbered list, bold tone names over blockquotes, a
    # plain copy with no ** and no ---. The first version alone is still the reply.
    numbered = ('Here are a few options:\n\n'
                '1. The shop is open again from Monday, with new shelves and the same coffee. Come by '
                'and say hello to the team.\n\n'
                '2. Good news: the shop opens its doors again on Monday. New shelves, new stock and the '
                'coffee you missed are all waiting for you.\n\n'
                '3. We are back! From Monday the shop is open, the shelves are full and the kettle is '
                'on. Drop in and see what changed.\n\n'
                '4. No more closed sign. The shop opens on Monday with fresh stock and the usual warm '
                'welcome. See you there.')
    first = ('The shop is open again from Monday, with new shelves and the same coffee. Come by and '
             'say hello to the team.')
    bold = ('Here are a few options in different tones:\n\n**Warm & simple**\n> Thank you for nine '
            'years of looking after our street. Happy holidays!\n\n**A little more personal**\n> Nine '
            'years! You have carried more cards and parcels to our door than we can count, through '
            'rain, snow and heat, and always with a wave for the kids. Thank you for all of it.\n\n'
            '**Short & sweet**\n> Thanks for nine wonderful years. Happy holidays!\n\n**Lighthearted**'
            '\n> For nine years you have braved the weather, the dog and our parcels, and never once '
            'complained. Enjoy a treat on us!\n\nAdd her name at the top and sign off with your '
            "family's name. A line about a small memory would make it feel more personal.")
    plain = ('Sure! Here are a few options, from simple to playful:\n\nOption 1: Simple and clear\n\n'
             'Yard sale Saturday, 8 to 2. Furniture, books and toys.\n\n'
             'Option 2: Friendly and detailed\n\nClearing out the garage! Come by on Saturday from 8 '
             'to 2 for furniture, kitchen things, books, toys and a few surprises. Cash only, early '
             'birds welcome.\n\nOption 3: Playful\n\nOne family\'s clutter, your treasure. Saturday, '
             '8 to 2, rain or shine.\n\nWant me to tweak the tone for a flyer or a post?')
    tips = ('Here is a quick message you can send:\n\nHey team, the coffee machine on floor 3 works '
            'again.\n\nA few tips for keeping it that way:\nRun a cycle of plain water through it '
            'first thing, so that the first cup of the day does not taste of yesterday.\nEmpty the '
            'drip tray before it fills, because it overflows onto the counter and soaks the paper '
            'towels.\nKeep the beans in the sealed tin, not the open hopper, or they go stale within '
            'a couple of days.\nIf it beeps twice, the descaling light is on, and the kit is in the '
            'cupboard under the sink.')
    for name, paste, reply in (
            ('a numbered list of options', numbered, first),
            ('bold tone names over blockquotes', bold,
             'Thank you for nine years of looking after our street. Happy holidays!'),
            ("a plain copy's option labels", plain, 'Yard sale Saturday, 8 to 2. Furniture, books and toys.'),
            ('a plain copy of a message with tips', tips,
             'Hey team, the coffee machine on floor 3 works again.')):
        model_says(reply + NOTES + '- Kept the first version.')
        done = run(paste, auto=True)[-1]
        check(f'{name}: the first version alone is a reply',
              done.get('event') == 'done' and done.get('text') == reply, done)
    # Only a cut of the draft passes: the same menu handed back inside a fence is still empty.
    model_says('Here is the edit:\n```\n' + numbered + '\n```' + NOTES)
    err = run(numbered, auto=True)[-1]
    check('a chat answer handed back in a fence is still empty_output',
          err.get('error', {}).get('code') == 'empty_output', err)
    # And a text with no chat reply around it still needs a quarter of its words back.
    model_says(first + NOTES)
    err = run(numbered.replace('Here are a few options:\n\n', '') + '\n\n' + SLOPPY)[-1]
    check('without a chat reply, a first paragraph alone is still empty_output',
          err.get('error', {}).get('code') == 'empty_output', err)
    # Menus with no labels under "Here are a few options:": a first version of four to six
    # words is the whole draft PASS 0 keeps (review R3L-2).
    for name, menu, reply in (
            ('numbered', "1. Quick update on Friday's launch\n2. Friday launch: what you need to know\n"
                         '3. Heads up: the launch moves to 3pm\n4. A quick note about Friday', "Quick update on Friday's launch"),
            ('table', "| Tone | Caption |\n|---|---|\n| Short | Sun's out, buns out. |\n| Warm | Nothing says "
                      'Saturday like warm bread from the oven and a slow coffee on the step. |\n| Funny | Our '
                      'sourdough has more layers than my excuses for skipping the gym this week. |', "Sun's out, buns out."),
            ('quoted', "> Running ten minutes late, so sorry!\n\n> Hey, I'm running about ten minutes behind, I'm "
                       'really sorry, save me a seat and order me a coffee.', 'Running ten minutes late, so sorry!'),
            ('plain', "Running ten minutes late, so sorry!\n\nHey, I'm running about ten minutes behind, I'm "
                      'really sorry, save me a seat and order me a coffee.', 'Running ten minutes late, so sorry!')):
        model_says(reply + NOTES + '- Kept the first version.')
        done = run('Here are a few options:\n\n' + menu + '\n\nWant it a bit punchier?', auto=True)[-1]
        check(f'a {name} menu with no labels: a short first version is a reply',
              done.get('event') == 'done' and done.get('text') == reply, done)
    # One draft is no menu: a reply that stops partway through it is still empty, though the
    # draft has a chat reply around it and the reply is the draft's own words (review R3L-1).
    letter = ("Here's a draft you can adapt:\n\nDear Ms. Alvarez,\n\n"
              'Thank you for taking the time to meet with me on Tuesday to talk about the role on your team.'
              ' I enjoyed hearing about the work your group is doing on the new reading program.\n\n'
              'Our conversation about the pilot schools stayed with me. The way your team gathers feedback'
              ' from teachers before changing a lesson is something I would be glad to help with, and I have'
              ' done similar work in my current job.\n\n'
              'I also appreciated your honesty about the hard parts of the role: the travel, the tight grant'
              ' deadlines and the reporting. None of that worries me, and I would welcome the chance to take'
              ' it on with your team.\n\n'
              'I have attached the writing sample you asked for. Please let me know if anything else would'
              ' help you as you make your decision, and thank you again for a very warm and open'
              ' conversation.\n\nWith best wishes,\nJordan Lee\n\nWant me to make it more formal?')
    for name, reply in (
            ('its first sentence', 'Dear Ms. Alvarez,\n\nThank you for taking the time to meet with me on '
                                   'Tuesday to talk about the role on your team.'),
            ('eight words of it', 'Thank you for taking the time to meet'),
            ('its first paragraph', letter.split('\n\n')[2])):
        model_says(reply + NOTES + '- Kept the letter.')
        err = run(letter, auto=True)[-1]
        check(f'a single draft cut to {name} is still empty_output',
              err.get('error', {}).get('code') == 'empty_output', err)
    # One paragraph of "Yes No N/A" is never split in a Word file. An exact diff of it against
    # a reply of a quarter of it took 38 s, and Stop waited on it (review R3L-5).
    column = ' '.join(['Yes', 'No', 'Yes', 'Yes', 'No', 'N/A'] * 9000)
    began = time.time()
    server.cut_of("Here's a draft you can adapt:\n\n" + column + '\n\nWant me to make it warmer?',
                  ' '.join(column.split()[:len(column.split()) // 4]))
    check('cut_of on a long block of few words is quick', time.time() - began < 2,
          f'{time.time() - began:.1f}s')
    # The walk still finds a lightly edited message, and still refuses words out of order.
    check('cut_of: an edited message over tips is a cut',
          server.cut_of(tips, 'Hi team, the coffee machine on floor 3 is working again.'))
    check('cut_of: the same words shuffled are not',
          not server.cut_of(tips, 'Again works 3 floor on machine coffee the team hey, now.'))
    # Stop pressed while a reply is checked ends the run there, before the cut is looked for.
    real_check, real_cut, seen = server.check, server.cut_of, {}

    def stopping_model(system_prompt, user_message, model, cancel):
        seen['cancel'] = cancel
        return 'Hey team, the coffee machine on floor 3 works again.' + NOTES

    def stopping_check(text):
        if 'cancel' in seen:
            seen['cancel'].set()
        return real_check(text)

    def counted_cut(draft, reply):
        seen['cut'] = True
        return real_cut(draft, reply)

    server.run_model, server.check, server.cut_of = stopping_model, stopping_check, counted_cut
    try:
        ev = run(tips, auto=True)
    finally:
        server.check, server.cut_of = real_check, real_cut
    check('Stop while a reply is checked: the run is cancelled before cut_of',
          ev[-1].get('error', {}).get('code') == 'cancelled' and 'cut' not in seen, (ev[-1], seen))


# ── numbers, links and placeholders are compared, not only asked for ────────
@section
def carried_over():
    draft = 'We leverage feedback from 25 businesses. Write to help@example.com or see https://example.com/pricing.'
    model_says('We use feedback. Write to help@example.com.' + NOTES + '- Cut a clause.')
    done = run(draft)[-1]
    check('carried: a dropped link and number are listed',
          done['warnings']['lost'] == ['https://example.com/pricing', '25'], done['warnings'])
    check('carried: nothing is invented into the notes', done['notes'] == ['Cut a clause.'], done['notes'])
    model_says('We use feedback from 25 businesses and 10,000 users, about [needs number] a year. '
               'Write to help@example.com or see https://example.com/pricing.' + NOTES)
    warnings = run(draft)[-1]['warnings']
    check('carried: an invented number and a placeholder are listed',
          warnings == {'lost': [], 'new': ['[needs number]', '10,000'], 'checked': 3}, warnings)
    model_says('We use feedback from 25 Leeds businesses. See https://example.com/pricing, '
               'or write to help@example.com.' + NOTES)
    warnings = run(draft)[-1]['warnings']
    check('carried: everything kept raises nothing', warnings == {'lost': [], 'new': [], 'checked': 3},
          warnings)

    same = server.carried('It cost 4,000 in 2024 [see note](https://a.example/x).\n1. First\n2. Second',
                          'In 2024 it cost 4000, as [the note](https://a.example/x) says.\n\nFirst, then second.')
    check('carried: 4,000 is 4000, link text may change, list numbers are not facts',
          (same['lost'], same['new']) == ([], []), same)
    check('carried: a text handed back unchanged has no warnings',
          run(CLEAN + ' Call 555 0100.', auto=True)[-1]['warnings'] == {'lost': [], 'new': [], 'checked': 2})

    tidy = server.carried('Start at https://acme.io/start\u2014no card needed\u2014today. It ends by\n2026. After that it is off.',
                          'Start at https://acme.io/start. No card is needed. It ends by 2026, then it is off.')
    check('carried: tidying the dash after a link, or reflowing a wrapped year, raises nothing',
          (tidy['lost'], tidy['new']) == ([], []), tidy)
    check('carried: a year that opens a wrapped line is still a fact',
          server.carried('It ends by\n2026. Then it is off.', 'It ends soon.')['lost'] == ['2026'])
    long_gap = '[needs number: how many customers, counted when, and from which source exactly?]'
    check('carried: a long placeholder is seen',
          server.carried('Used by many.', 'Used by ' + long_gap + '.')['new'] == [long_gap])
    check('carried: an entity dash is not the number 8212',
          server.carried('Fast &#8212; and $9 a month.', 'Fast, and $9 a month.')['lost'] == [])

    # A chat answer's option labels and notes go when the rewrite hands back the draft alone.
    chat = ('**Option 1**\n\nI start on May 4.\n\n**Option 2**\n\nMay 4 is my first day.\n\n---\n\n'
            'A few notes: I guessed 2026.')
    kept, dropped = server.carried(chat, 'I start on May 4.'), server.carried(chat, 'I start in May.')
    check('carried: what was only in the chat reply around the draft is listed apart',
          kept == {'lost': [], 'new': [], 'checked': 5, 'framing': ['1', '4 (1 of 2)', '2', '2026']}, kept)
    check('carried: a number lost from the draft itself is still lost', dropped['lost'] == ['4'], dropped)
    # Only framing that left explains a loss: a reply the model kept still holds its number.
    stays = server.carried(chat, chat.replace('I start on May 4', 'I start in May'))
    check('carried: a chat reply left in place does not hide a number lost from the draft',
          stays['lost'] == ['4 (1 of 2)'] and 'framing' not in stays, stays)
    paste = ("Here's a draft you can adapt:\n\nHi Sam,\n\nThe room seats 12 people.\n\nBest,\n"
             '[Your Name]\n\n---\n\nA few notes: it seats 12, so check the list.')
    kept_notes = server.carried(paste, paste.replace('12 people', 'a dozen people'))
    check('carried: kept notes sharing a number do not hide it',
          kept_notes['lost'] == ['12 (1 of 2)'] and 'framing' not in kept_notes, kept_notes)
    # The same when the rewrite drops or rewords the line that made the rest a chat answer:
    # the notes it kept word for word still hold their number (review ADV-1).
    dropped_opener = server.carried(paste, paste.replace("Here's a draft you can adapt:\n\n", '')
                                    .replace('12 people', 'a dozen people'))
    check('carried: kept notes hide nothing when the opener is gone',
          dropped_opener['lost'] == ['12 (1 of 2)'] and 'framing' not in dropped_opener, dropped_opener)
    menu = ('Option 1 (formal):\n\nWe need 40 chairs for the dinner on Friday.\n\n'
            'Option 2 (casual):\n\nCan we grab 40 chairs for Friday?')
    relabelled = server.carried(menu, menu.replace('Option 1 (formal):', 'Formal note:')
                                .replace('We need 40 chairs', 'We need chairs'))
    check('carried: a kept second version hides nothing when the first label is reworded',
          relabelled['lost'] == ['40 (1 of 2)'] and relabelled.get('framing') == ['1'], relabelled)
    # A line both versions share is the draft's while the rewrite has no more copies of it.
    twice = '**Option 1**\n\nThe sale runs 8 to 2.\n\n**Option 2**\n\nThe sale runs 8 to 2.\nBring cash.'
    shared = server.carried(twice, 'The sale runs 8 to 2.')
    check('carried: a line the versions share is not a kept reply', shared['lost'] == [], shared)
    # A plain copy has no ** around its labels, and its second version left all the same (E2E-2).
    plain = ('Option 1: Short and sweet\n\nBake sale on the 3rd, 10 to 2.\n\n'
             'Option 2: Warm\n\nJoin us on the 3rd from 10 to 2 for cakes, and bring 10 friends.\n\n'
             'Want me to tweak the tone?')
    first = server.carried(plain, 'Bake sale on the 3rd, 10 to 2.')
    check('carried: a plain-copied second version is framing, not lost', first['lost'] == [], first)
    # Bold tone names copied as plain text are lines of tones alone (review E2E3-1).
    toned = ('Keep it short and warm. Here are a few options you can use as-is or mix:\n\n'
             'Simple and sincere\n"Congratulations on your retirement, [Name]! 22 years is an incredible run."\n\n'
             'A bit warmer\n"[Name], thank you for 22 years of help and good humour. Enjoy every minute!"\n\n'
             'Light and fun\n"[Name], 22 years done, and now you get to sleep in. Happy retirement!"\n\n'
             'Sign off with "Best wishes," plus your name.')
    first = server.carried(toned, '"Congratulations on your retirement, [Name]! 22 years is an incredible run."')
    check('carried: versions under plain lines of tones are framing, not lost', first['lost'] == [], first)
    # A [placeholder] is compared like the rest, but it is no number or link to count.
    signed = server.carried('Room 4 is booked.\n\nBest,\n[Your Name]', 'Room 4 is free.\n\nBest,')
    check('carried: a placeholder is compared but not counted as a number or link',
          signed == {'lost': ['[Your Name]'], 'new': [], 'checked': 1}, signed)
    # A cliché in an install guide is no chat answer: its second option is the guide's (CHK-1).
    guide = ('# Install\n\nIt gives you the best of both worlds.\n\n### Option A (recommended)\n\n'
             'Use pip with Python 3.11.\n\n### Option B\n\nBuild it with make: 4 GB of RAM, about 15 minutes.'
             '\n\n---\n\nIf you get stuck, open an issue.')
    cut = server.carried(guide, guide.split('### Option B')[0])
    check('carried: a guide that drops its second option has lost it', cut['lost'] == ['4', '15']
          and 'framing' not in cut, cut)

    began = time.time()
    for hostile in ('a' * 50_000, '[' * 50_000, '&#' * 25_000, '1,' * 25_000, 'x@' * 25_000, '\n' * 50_000 + '1. a',
                    'https://' + 'a' * 49_000, '7' * 50_000):
        server.facts(hostile)
    check('carried: long unbroken runs are scanned quickly', time.time() - began < 3,
          f'{time.time() - began:.1f}s')


# ── a rewrite that adds a caveat the draft did not have is named, never blocked ─
@section
def added_caveats():
    got = server.hedged('In most markets, React roles outnumber Vue roles by a wide margin.',
                        'React roles generally outnumber Vue roles, though the balance varies by market.')
    check('caveats: an added though, generally and varies are named',
          len(got) == 1 and all(w in got[0][1] for w in ('generally', 'though', 'varies'))
          and got[0][1].startswith('added: ') and got[0][0].startswith('React roles generally'), got)
    got = server.hedged('It may rain on Friday.', 'It may rain on Friday, so bring a coat.')
    check('caveats: the same qualifier on both sides is not added', got == [], got)
    got = server.hedged('The launch is set for early spring this year.', 'The launch is set for May 3 this year.')
    check('caveats: May 3 is a date, not a hedge', got == [], got)
    got = server.hedged('The tool reads one file and stops.', 'The tool reads one file and stops. Weather might change quickly tomorrow.')
    check('caveats: a new sentence that shares no words is not paired', got == [], got)
    check('caveats: an unchanged text has no hedged key',
          'hedged' not in server.carried(CLEAN + ' It runs every night.', CLEAN + ' It runs every night.'))
    many = ' '.join(f'Team {n} ships the build on Monday.' for n in range(10))
    got = server.hedged(many, many.replace('ships the build on Monday.', 'generally ships the build on Monday.'))
    check('caveats: at most 6 sentences are listed', len(got) == 6, len(got))

    board = {'text': None, 'writes': []}

    def put(text):
        board['writes'].append(text)
        return True

    real = server.clipboard_text, server.set_clipboard
    draft = 'Our robust backups run every night at two.'
    server.clipboard_text, server.set_clipboard = (lambda: draft), put
    try:
        model_says('Our backups generally run every night at two, though timing varies.' + NOTES)
        done = run(draft, copy=True)[-1]
        hedges = done['warnings'].get('hedged')
        check('caveats: a run that adds one lists it in the warnings',
              hedges and len(hedges) == 1 and 'generally' in hedges[0][1], done['warnings'])
        check('caveats: and it still passes and is copied, as the copy rules decide',
              done['after']['clean'] is True and done.get('copied') is True and len(board['writes']) == 1,
              (done['after']['clean'], done.get('copied'), board))
    finally:
        server.clipboard_text, server.set_clipboard = real

    status, _, data = request('POST', '/api/check', {'text': 'Our backups generally run every night at two.',
                                                    'draft': 'Our backups run every night at two.'})
    got = json.loads(data).get('warnings', {})
    check('caveats: /api/check with a draft returns them', status == 200 and len(got.get('hedged', [])) == 1, got)

    big = ('The tool reads a file and reports what it finds. ' * 4000)[:200_000]
    began = time.time()
    server.hedged(big, big.replace('reports', 'generally reports'))
    server.carried(big, big.replace('reports', 'generally reports'))
    check('caveats: a 200,000-character text is checked quickly', time.time() - began < 2,
          f'{time.time() - began:.1f}s')
    began = time.time()
    unbroken = 'word ' * 40_000
    server.hedged(unbroken, unbroken + 'may')
    check('caveats: one 200,000-character sentence is checked quickly', time.time() - began < 2,
          f'{time.time() - began:.1f}s')


# ── a long text is rewritten a part at a time ────────────────────────────────
@section
def long_texts():
    plain = 'The tool reads a file and reports what it finds. ' * 20        # about 1,000 characters, clean
    slop = "It's not just a tool, it's a game-changing journey. " * 10
    fixed = slop.replace("It's not just a tool, it's a game-changing journey.", 'It is a tool.')
    blocks = [plain.strip()] * 6 + [slop.strip()] + [plain.strip()] * 7 + ['# A heading', slop.strip()] + [plain.strip()] * 3
    text = '\n\n'.join(blocks)
    parts = server.split_parts(text)
    check('parts: a long text is cut up', len(parts) >= 3, len(parts))
    check('parts: put together again they are the text', '\n\n'.join(parts) == text)
    check('parts: none is over the size', all(len(p) <= server.PART_CHARS * 1.2 for p in parts),
          [len(p) for p in parts])
    check('parts: a heading stays with what follows it',
          not any(p.split('\n\n')[-1].startswith('#') for p in parts[:-1]))
    # The heading above lands mid-part; this one is the last block before a cut.
    at_cut = server.split_parts('\n\n'.join([plain.strip()] * 6 + ['# At the cut'] + [plain.strip()] * 5))
    check('parts: a heading where the cut falls moves to the next part',
          len(at_cut) == 2 and at_cut[1].startswith('# At the cut') and '# At the cut' not in at_cut[0],
          [p[-40:] for p in at_cut])
    check('parts: a short text is one part', server.split_parts(SLOPPY) == [SLOPPY])
    check('parts: one enormous paragraph is still cut',
          max(len(p) for p in server.split_parts('word ' * 5000)) <= server.PART_CHARS)
    # A blank line inside a fenced code block divides nothing. Cut there, each half would be
    # scored with its code as prose, and prose between two blocks could vanish from the check.
    fenced = '\n\n'.join([plain.strip()] * 5 + ['```\nfirst = leverage(1)\n\nsecond = leverage(2)\n```'] + [plain.strip()] * 6)
    check('parts: a fenced block is never cut in two',
          all(p.count('```') % 2 == 0 for p in server.split_parts(fenced)) and len(server.split_parts(fenced)) > 1,
          [p.count('```') for p in server.split_parts(fenced)])
    # The block above fits inside one part; this one runs over where the cut would fall.
    code = '```python\n' + '\n\n'.join(f'def f{i}(x):\n    return x + {i}' for i in range(80)) + '\n```'
    over = server.split_parts('\n\n'.join([plain.strip()] * 5 + [code] + [plain.strip()] * 4))
    check('parts: a fenced block over the cut is kept whole',
          len(over) > 1 and all(p.count('```') % 2 == 0 for p in over), [p.count('```') for p in over])
    # Text with single line ends is one long block. It is cut to make parts and has to come
    # back exactly as it was when nothing in it is rewritten.
    loose = ('The tool reads a file and reports what it finds.\n' * 300).strip()
    model_says()
    done = run(loose)[-1]
    check('parts: text cut inside a block is put back as it was',
          done.get('text') == loose and done['parts']['of'] > 1 and not calls, (done.get('parts'), len(done.get('text', ''))))
    joins = []
    cut = server.split_parts(loose, joins=joins)
    check('parts: each cut falls on white space, which is kept for the join',
          len(joins) == len(cut) - 1 and cut[0] + ''.join(j + p for j, p in zip(joins, cut[1:])) == loose, joins[:3])
    check('parts: blank space alone makes no part',
          all(p.strip() for p in server.split_parts('\n\n \n\n'.join([plain] * 12))))

    def editor(fail_on=(), code='engine_failed'):
        seen = []

        def fake(system_prompt, message, model, cancel):
            seen.append(message)
            if len(seen) in fail_on:
                raise server.EngineError(code, 'The model did not answer.')
            draft = message.split('TEXT TO EDIT:\n', 1)[1]
            return draft.replace("It's not just a tool, it's a game-changing journey.", 'It is a tool.') + NOTES + '- Cut the contrast.'

        server.run_model = fake
        return seen

    seen = editor()
    ev = run(text, context='A report')
    done = ev[-1]
    flagged = [i for i, p in enumerate(parts, 1) if 'game-changing' in p]
    check('parts: every part is announced', [e['part'] for e in ev if e['event'] == 'part'] == list(range(1, len(parts) + 1)), kinds(ev))
    check('parts: only the flagged ones reach the model', len(seen) == len(flagged) == 2, (len(seen), flagged))
    check('parts: the result is the whole text, rewritten where it was flagged',
          done.get('text') == text.replace(slop.strip(), fixed.strip()) and done['after']['clean'] is True, done.get('after'))
    check('parts: the counts say what happened',
          done.get('parts') == {'of': len(parts), 'rewritten': 2, 'unchanged': len(parts) - 2, 'failed': 0, 'stopped': 0},
          done.get('parts'))
    check('parts: notes say which part', done['notes'] == [f'Part {i}: Cut the contrast.' for i in flagged], done['notes'])
    check('parts: the model is told it has one part of several',
          f'part {flagged[0]} of {len(parts)}' in seen[0] and seen[0].startswith('Context: A report This is part'), seen[0][:120])
    check('parts: each call carries only its part', all(len(m) < server.PART_CHARS * 1.5 for m in seen), [len(m) for m in seen])

    seen = editor(fail_on=(2,))
    done = run(text)[-1]
    check('parts: a part that fails is kept as it was, and said so',
          done.get('parts', {}).get('failed') == 1 and slop.strip() in done['text'] and fixed.strip() in done['text']
          and any('could not be rewritten' in n for n in done['notes']), (done.get('parts'), done.get('notes')))
    many = '\n\n'.join(([plain.strip()] * 5 + [slop.strip()]) * 6)        # six parts, each flagged
    seen = editor(fail_on=(2, 3, 4))
    done = run(many)[-1]
    check('parts: after three failures the parts left are not tried',
          len(seen) == 4 and done.get('parts', {}).get('failed') == 3 and done['parts']['of'] == 6
          and any('Three parts failed' in n for n in done['notes']), (len(seen), done.get('parts')))
    editor(fail_on=(1,))
    check('parts: when the first call fails the run fails',
          run(text)[-1].get('error', {}).get('code') == 'engine_failed')
    # A usage limit or a lapsed sign-in part-way through keeps the parts already rewritten,
    # and spends no more calls on the parts after it. So does a lost network or a call that ran
    # out of time: each CLI retries for minutes first, so each call more would cost as long again.
    longer = '\n\n'.join([text] + [plain.strip()] * 6 + [slop.strip()])
    for code in ('usage_limit', 'not_logged_in', 'offline', 'timeout'):
        seen = editor(fail_on=(2,), code=code)
        done = run(longer)[-1]
        check(f'parts: {code} part-way through keeps the parts already rewritten',
              done.get('event') == 'done' and len(seen) == 2
              and done['parts']['rewritten'] == 1 and done['parts']['failed'] == 1
              and fixed.strip() in done['text'] and 'The parts after this one were not tried.' in done['notes'],
              (done.get('event'), done.get('error'), done.get('parts'), len(seen), done.get('notes', [])[:2]))
        # The page has to know why the rest was left, to show the sign-in (QA FR-2).
        check(f'parts: {code} part-way through is named in the counts',
              done.get('parts', {}).get('gone') == code, done.get('parts'))
    editor(fail_on=(1,), code='usage_limit')
    check('parts: a usage limit on the first call fails the run',
          run(longer)[-1].get('error', {}).get('code') == 'usage_limit')
    model_says()
    done = run('\n\n'.join([plain.strip()] * 14))[-1]
    check('parts: a long text with nothing flagged makes no model call',
          done.get('rounds') == 0 and done['parts']['rewritten'] == 0 and not calls, (done.get('parts'), calls))

    def swapper(system_prompt, message, model, cancel):
        """One flagged word for another: each part's second round fixes nothing."""
        draft = message.split('TEXT TO EDIT:\n', 1)[1]
        swap = ('seamless', 'robust') if 'already been edited' in message else (
            "It's not just a tool, it's a game-changing journey.", 'It is a seamless tool.')
        return draft.replace(*swap) + NOTES

    server.run_model = swapper
    done = run(text)[-1]
    check('parts: the counts say how many parts stopped early',
          done.get('parts', {}).get('stopped') == 2 and done['rounds'] == 2 and 'stopped' not in done, done.get('parts'))
    # A part sent back unchanged was kept on purpose: it did not stop for fixing nothing.
    server.run_model = lambda system_prompt, message, model, cancel: message.split('TEXT TO EDIT:\n', 1)[1] + NOTES
    done = run(text)[-1]
    check('parts: a part sent back unchanged is not counted as stopped early',
          done.get('parts', {}).get('stopped') == 0 and done['rounds'] == 1, done.get('parts'))


# ── text from a Word file keeps its blocks, so it can be written back ───────
@section
def keeping_the_blocks():
    draft = "# Plan\n\nIt's not just a tool, it's a game-changing journey.\n\n- First item\n\n- Second item"
    merged = '# Plan\n\nIt is a tool.\n\n- First item\n- Second item'          # two items in one block
    kept = '# Plan\n\nIt is a tool.\n\n- First item\n\n- Second item'
    model_says(merged + NOTES + '- merged', kept + NOTES + '- kept')
    ev = run(draft, structure=True)
    done = ev[-1]
    check('keep: a reply that merges blocks is not taken',
          done.get('text') == kept and done['rounds'] == 2 and done['notes'] == ['kept'], done)
    change = done.get('changes') or {}
    check('keep: the changes pair the blocks, one for each of the draft\'s',
          change.get('paired') is True and len(change.get('blocks', [])) == len(server.blocks_of(draft)) == 4
          and change.get('changed') == 1, change)
    check('keep: the rejected round is not scored',
          kinds(ev) == ['start', 'round', 'round', 'checked', 'done'], kinds(ev))
    check('keep: the model is told the shape is fixed', 'same number of blocks' in calls[0]['message'], calls[0]['message'])
    check('keep: and that chat framing stays word for word', 'word for word' in calls[0]['message'],
          calls[0]['message'])
    # A table of contents is no longer in the text at all (documents.py leaves it out).
    check('keep: and is told nothing about a table of contents', 'table of contents' not in server.KEEP.lower(),
          server.KEEP)
    check('keep: and told the count when it gets it wrong',
          'had 3 blocks' in calls[1]['message'] and 'exactly 4' in calls[1]['message']
          and calls[1]['message'].endswith('TEXT TO EDIT:\n' + draft), calls[1]['message'][-200:])
    model_says(merged + NOTES, merged + NOTES, merged + NOTES)
    done = run(draft, structure=True)[-1]
    check('keep: when no round keeps the shape the text comes back as it was',
          done.get('text') == draft and done['rounds'] == 3 and 'left as it was' in done['notes'][0], done)
    check('keep: and so it has no changes to show', 'changes' not in done, done.get('changes'))
    model_says(merged + NOTES)
    check('without the switch a merged reply is taken as before', run(draft)[-1].get('text') == merged)
    # A Word file keeps its chat reply (PASS 0 deletes nothing there), so the reply's 40
    # cannot stand in for the 40 the draft lost.
    word = ("Here's a draft you can adapt:\n\nWe need 40 chairs for the dinner.\n\n---\n\n"
            'A few notes: I assumed 40 guests.')
    model_says(word.replace('40 chairs', 'chairs') + NOTES + '- Cut a number.')
    done = run(word, structure=True)[-1]
    check('keep: a number lost from the draft is lost, though the kept reply has it',
          done['warnings']['lost'] == ['40 (1 of 2)'] and 'framing' not in done['warnings'], done.get('warnings'))
    # The check flags the opener, so the model rewords it and keeps the notes as they were.
    model_says(word.replace("Here's a draft you can adapt:", 'The draft for the dinner:')
               .replace('40 chairs', 'chairs') + NOTES + '- Reworded the first line.')
    done = run(word, structure=True)[-1]
    check('keep: rewording the opener does not hide the number the draft lost',
          done['warnings']['lost'] == ['40 (1 of 2)'] and 'framing' not in done['warnings'], done.get('warnings'))
    # Two versions share a line. The rewrite edits version 1's copy and leaves version 2's:
    # the copy that left is the draft's, so its 7 is lost (review R3L-4).
    shared = "Dinner is at 7 on Friday at Luigi's."
    word = ("Here's a draft you can adapt:\n\nVersion 1: Short and sweet\n\nHi all,\n\n" + shared +
            "\n\nSee you there!\n\nVersion 2: Warm and friendly\n\nHi everyone, I hope your week is going "
            "well.\n\n" + shared + "\n\nCan't wait to see you all!")
    model_says(word.replace('Hi all,\n\n' + shared, "Hi all,\n\nDinner is on Friday at Luigi's.") + NOTES
               + '- Left the framing in place.')
    done = run(word, structure=True)[-1]
    check('keep: a line in both versions, edited in the draft, is lost from the draft',
          done['warnings']['lost'] == ['7 (1 of 2)'] and 'framing' not in done['warnings'], done.get('warnings'))
    # A notes label is part of its line. Kept word for word, its 14 explains no loss.
    word = ("Here's a draft you can adapt:\n\nHi team,\n\nThe offsite is for 14 people on May 3.\n\n"
            'Best,\nSam\n\nOne thing to check for the 14 seats: the room may hold fewer.')
    model_says(word.replace('for 14 people on May 3', 'for everyone on May 3') + NOTES + '- Cut a number.')
    done = run(word, structure=True)[-1]
    check('keep: a notes label kept with its line explains no loss',
          done['warnings']['lost'] == ['14 (1 of 2)'] and 'framing' not in done['warnings'], done.get('warnings'))
    # The count is not the whole shape. A list item opened another way, or a row with bars
    # of its own, would be typed into the document as text.
    model_says(kept.replace('- First', '* First') + NOTES, kept + NOTES + '- kept')
    done = run(draft, structure=True)[-1]
    check('keep: a changed marker is not taken either',
          done.get('text') == kept and done['rounds'] == 2 and 'how a block opens' in calls[1]['message'], done)
    # No marker line means the notes cannot be told from the text, and they would be saved as a paragraph.
    model_says(kept, kept + NOTES + '- kept')
    done = run(draft, structure=True)[-1]
    check('keep: a reply without the notes marker is asked for again',
          done.get('text') == kept and done['rounds'] == 2 and 'did not have the line' in calls[1]['message'], done)
    check('keep: the shape of a text is its markers and bars',
          server.shape_of('# A\n\nPlain\n\n- b\n\n12. c\n\nx | y | z')
          == [('#', 0), ('', 0), ('-', 0), ('1', 0), ('', 2)], server.shape_of('# A\n\nPlain\n\n- b\n\n12. c\n\nx | y | z'))
    huge = 'word ' * 4000
    check('keep: a block is never cut to make parts',
          server.split_parts(huge.strip() + '\n\nSecond.', whole=True)[0] == huge.strip())


# ── what the rewrite changed, for the Changes view and Keep original ────────
def rebuilt(segs):
    """(old, new) put together again from diff segments."""
    return (''.join(text for op, text in segs if op != '+'), ''.join(text for op, text in segs if op != '-'))


def compose(change, kept=()):
    """What the page shows with the blocks numbered in `kept` put back as they were."""
    texts = [block['old'] if i in kept else block['new'] for i, block in enumerate(change['blocks'])]
    return change['lead'] + ''.join(t + s for t, s in zip(texts, change['seps'] + [''])) + change['trail']


@section
def changes_view():
    for text in ('a\n\nb', '\n\na\n \nb\n\n\n\nc\n', 'only one', '  - x\n\n  - y', '', SLOPPY):
        lead, blocks, seps, trail = server.pieces(text)
        check(f'pieces: {text!r} is put together again exactly',
              lead + ''.join(b + s for b, s in zip(blocks, seps + [''])) + trail == text, (lead, blocks, seps, trail))
        check(f'pieces: {text!r} has the blocks blocks_of finds',
              blocks == server.blocks_of(text) and len(seps) == max(0, len(blocks) - 1), blocks)
    check('pieces: a text with no block is all lead', server.pieces(' \n\n \n') == (' \n\n \n', [], [], ''))

    pairs = ((SLOPPY, CLEAN), ('The cat sat on the mat.', 'The dog sat on the mat.'), ('', 'New words.'),
             ('Old words.', ''), ('one two\nthree', 'one  two three'), ('  It opens with spaces.', 'It does not'),
             ('A, b; c! d? e.', 'A b c d e'), ('Café — 你好, naïve.', 'Cafe, 你好吗, naive.'),
             ('word ' * 2000, 'term ' * 2000), ("It's a seamless, robust journey.", "It's a long, robust trip."))
    for old, new in pairs:
        segs = server.diff_words(old, new)
        ops = ''.join(op for op, _ in segs)
        check(f'diff: {old[:30]!r} and {new[:30]!r} are rebuilt exactly from the segments',
              rebuilt(segs) == (old, new) and all(text for _, text in segs), segs[:6])
        check(f'diff: {old[:30]!r}: neighbours never share an op, and a deletion comes before its insertion',
              set(ops) <= set('=-+') and '--' not in ops and '++' not in ops and '==' not in ops
              and '+-' not in ops, ops)
    # And a few hundred made up from the awkward parts: blank lines, tabs, marks, other scripts.
    pick, atoms = random.Random(11), ['a', 'the', 'tool', "'s", ',', '.', ' ', '  ', '\n', '\n\n', '\n \n', '\t',
                                      '- ', '|', 'é', '你好', '—', '12']
    broken = []
    for _ in range(400):
        old = ''.join(pick.choice(atoms) + pick.choice(['', ' ', '\n']) for _ in range(pick.randint(0, 25)))
        new = ''.join(pick.choice(atoms) if pick.random() < .15 else c for c in old) + pick.choice(['', ' ', '.'])
        segs, change = server.diff_words(old, new), server.changes(old, new)
        if rebuilt(segs) != (old, new) or change and (compose(change) != new or not 0 <= change['kept'] <= 100):
            broken.append((old, new))
    check('diff: made-up pairs are rebuilt exactly, and their changes put the rewrite together again',
          not broken, broken[:2])
    segs = server.diff_words('The cat sat on the mat.', 'The dog sat on the mat.')
    check('diff: one word changed is one deletion and one insertion',
          segs == [['=', 'The '], ['-', 'cat '], ['+', 'dog '], ['=', 'sat on the mat.']], segs)
    ops = [op for op, _ in server.diff_words(SLOPPY, CLEAN)]
    check('diff: a rewritten sentence is one deletion and one insertion, not a scatter',
          ops.count('-') <= 1 and ops.count('+') <= 1, server.diff_words(SLOPPY, CLEAN))
    segs = server.diff_words('Tell me\nwhat it does.', 'Tell me what  it does.')
    check('diff: white space alone changing keeps every word',
          rebuilt(segs) == ('Tell me\nwhat it does.', 'Tell me what  it does.')
          and all(not text.strip() for op, text in segs if op != '='), segs)
    budget = {'left': 10}
    server.diff_words('a b c', 'a b d', budget)
    left = budget['left']
    server.diff_words('a b c', 'a b d', budget)
    check('diff: an exact diff spends the run\'s budget, and one that does not fit spends none',
          (left, budget['left']) == (4, 4), (left, budget['left']))

    check('changes: equal texts have none', server.changes(SLOPPY, SLOPPY) is None)
    check('changes: nor do texts that differ only between blocks',
          server.changes('One.\n\nTwo.', 'One.\n\n\nTwo.\n') is None)
    draft = '# Plan\n\nIt is a seamless journey.\n\nThis one stays.\n\n- A list item that leverages synergy'
    out = '\n\n# Plan\n \nIt is a trip.\n\nThis one stays.\n\n\n- A list item that works\n\n'
    change = server.changes(draft, out)
    check('changes: the same number of blocks are paired',
          change['paired'] is True and len(change['blocks']) == 4, change)
    check('changes: a block the rewrite left alone has no segments',
          ['segs' in block for block in change['blocks']] == [False, True, False, True] and change['changed'] == 2,
          change['blocks'])
    check('changes: each changed block\'s segments rebuild it',
          all(rebuilt(b['segs']) == (b['old'], b['new']) for b in change['blocks'] if 'segs' in b))
    check('changes: with nothing kept, the rewrite is rebuilt exactly', compose(change) == out, compose(change))
    lead, _, seps, trail = server.pieces(out)
    want = lead + ''.join(b + s for b, s in zip(server.blocks_of(draft), seps + [''])) + trail
    check('changes: with every block kept, the draft\'s blocks come back between the rewrite\'s blank lines',
          compose(change, kept=(1, 3)) == want, compose(change, kept=(1, 3)))
    check('changes: one block kept changes that block only',
          compose(change, kept=(1,)) == out.replace('It is a trip.', 'It is a seamless journey.'), compose(change, kept=(1,)))
    # Of the 15 words, "Plan" and "This one stays" are in blocks left alone; "It is a" and
    # "A list item that" are the words kept in the two that changed.
    check('changes: kept counts the draft\'s words that stayed', change['kept'] == round(100 * 11 / 15), change['kept'])

    change = server.changes('One paragraph.\n\nAnd a second one.', 'One paragraph, and a second one.')
    check('changes: different block counts are one unpaired block',
          change['paired'] is False and len(change['blocks']) == 1 and (change['lead'], change['seps'], change['trail'])
          == ('', [], '') and rebuilt(change['blocks'][0]['segs']) == ('One paragraph.\n\nAnd a second one.',
                                                                        'One paragraph, and a second one.'), change)
    # Same count, but the rewrite split B and dropped the conclusion: pairing by position would
    # let Keep original put the conclusion where the parking paragraph is.
    draft = ('The lease on the old office ends in March.\n\n'
             'We looked at four buildings. The one on Mill Street is cheaper and closer to the station, '
             'and it has room for the team to grow over that time.\n\n'
             'Parking is limited to twelve spaces, so most of us will come by train.\n\n'
             'In conclusion, this move is a game-changing step on our journey.')
    out = ('The lease on the old office ends in March.\n\n'
           'We looked at four buildings. The one on Mill Street is cheaper and closer to the station.\n\n'
           'The building has room for the team to grow over that time.\n\n'
           'Parking is limited to twelve spaces, so most of us will come by train.')
    change = server.changes(draft, out)
    check('changes: blocks that were split and dropped are not paired by position',
          change['paired'] is False and compose(change) == out, change)
    # A heading whose words the edited paragraph under it repeats is still that heading.
    for heading in ('## Site induction', '**Site induction**'):
        draft = (f'{heading}\n\nEvery contractor completes the site induction before starting work. '
                 'It is crucial to note that the induction covers fire exits and first aid.\n\nAsk the site manager.')
        out = draft.replace('It is crucial to note that the induction', 'The induction')
        change = server.changes(draft, out)
        check(f'changes: {heading} echoed in the edited paragraph under it is still paired',
              change['paired'] is True and len(change['blocks']) == 3 and change['changed'] == 1, change)

    # A pasted chat reply whose rewrite dropped the lead-in and the closing: the draft's own
    # paragraphs still pair, and the reply around them shows as taken out, with no way back.
    body = ['Hi Sam,',
            'Thanks for flagging the delay on the March invoice. We looked into it this morning and the '
            'payment went out on the 14th.',
            'We try to say yes to every reasonable request, so if you need a copy of the remittance advice '
            'we can send it today.',
            'The notice period for changes to the contract is 30 days, and we will hold to that for the renewal.',
            'Best,\nAlex']
    head, tail = "Here's a draft you can adapt:\n\n---\n\n", ('\n\n---\n\nA few notes:\n- I kept it short.\n'
                                                            '- Adjust the notice period if it differs.\n\n'
                                                            'Want me to make it more formal?')
    draft = head + '\n\n'.join(body) + tail
    edited = list(body)
    edited[2] = 'If you need a copy of the remittance advice, we can send it today.'
    edited[3] = "Changes to the contract need 30 days' notice, and that applies to the renewal."
    out = '\n\n'.join(edited)
    change = server.changes(draft, out)
    gone = [block for block in change['blocks'] if block.get('gone')]
    check('changes: a chat reply that lost its framing pairs the paragraphs it kept',
          change['paired'] is True and change['changed'] == 2 and compose(change) == out
          and [block['old'] for block in gone] == [head, tail]
          and all(block['new'] == '' and block['segs'] == [['-', block['old']]] for block in gone)
          and compose(change, kept=(3,)) == '\n\n'.join(body[:3] + edited[3:]), change)
    change = server.changes(head + '\n\n'.join(body), out)
    check('changes: a chat reply that lost only its lead-in pairs too',
          change['paired'] is True and change['changed'] == 2 and len(change['blocks']) == 6
          and compose(change) == out, change)
    change = server.changes(draft, '\n\n'.join(edited[1:-1]))
    check('changes: a greeting and sign-off dropped with the framing are not shown as the chat reply',
          change['paired'] is False and compose(change) == '\n\n'.join(edited[1:-1]), change)
    change = server.changes('\n\n'.join(body), '\n\n'.join(edited[:2] + edited[3:]))
    check('changes: a text that is not a chat reply and lost a paragraph is not paired',
          change['paired'] is False, change)
    change = server.changes(draft, '\n\n'.join(body))
    check('changes: a chat reply that lost only its framing is shown whole',
          change['paired'] is False and compose(change) == '\n\n'.join(body), change)
    change = server.changes('Tell me\nwhat it does.\n\nNext.', 'Tell me what  it does.\n\nNext.')
    check('changes: kept is 100 when only white space changed in a block',
          change['kept'] == 100 and change['changed'] == 1, change)

    words = ('the tool reads each file and reports what it finds in plain words for the people who wrote it '
             'while leaving every number link and address exactly where it was before').split()
    pick = random.Random(7)
    paragraphs = []
    while sum(len(p) + 2 for p in paragraphs) < 199_000:
        paragraphs.append(' '.join(pick.choice(words) for _ in range(pick.randint(40, 160))).capitalize() + '.')
    draft = '\n\n'.join(paragraphs)[:200_000]
    edited = '\n\n'.join(' '.join('rewritten' if i % 6 == 0 else w for i, w in enumerate(p.split(' ')))
                         for p in draft.split('\n\n'))
    began = time.time()
    change = server.changes(draft, edited)
    took = time.time() - began
    check('changes: 200,000 characters with every paragraph changed take under 3 seconds',
          took < 3 and change['paired'] and change['changed'] == len(server.blocks_of(draft)),
          f'{took:.1f}s, {change and change["changed"]} of {len(server.blocks_of(draft))}')
    check('changes: and the long diffs are still good ones', 75 <= change['kept'] <= 90, change['kept'])
    check('changes: unpaired texts of more than 60,000 tokens have none',
          server.changes('word ' * 30_001, 'term ' * 30_000 + '\n\nAnd one more.') is None)

    model_says(CLEAN + NOTES)
    done = run(SLOPPY)[-1]
    change = done.get('changes') or {}
    check('done: a rewrite carries its changes, paired', change.get('paired') is True and change.get('changed') == 1, change)
    check('done: whose segments rebuild the draft and the rewrite',
          [rebuilt(b['segs']) for b in change.get('blocks', [])] == [(SLOPPY, CLEAN)], change)
    model_says()
    done = run(CLEAN, auto=True)[-1]
    check('done: text handed back unchanged has no changes', done.get('rounds') == 0 and 'changes' not in done, done)

    # Paragraphs put back as they were are checked again, and compared with the draft.
    text = 'We use feedback from 25 businesses and 10,000 users. See https://example.com/a.'
    draft = 'We leverage feedback from 25 businesses. See https://example.com/b.'
    status, _, data = request('POST', '/api/check', {'text': text, 'draft': draft})
    reply = json.loads(data)
    check('check with a draft: the check, and what carried over from the draft',
          status == 200 and reply.get('warnings') == server.carried(draft, text)
          and {k: v for k, v in reply.items() if k not in ('warnings', 'labels_left')} == server.check(text)
          and reply.get('labels_left') == 0, reply)
    check('check with a draft: which here is a lost link, a new one and a new number',
          reply.get('warnings') == {'lost': ['https://example.com/b'], 'new': ['https://example.com/a', '10,000'],
                                    'checked': 2}, reply.get('warnings'))
    labelled = '- **Cost:** It is cheap.\n- **Speed:** It is quick.\n- **Support:** It is local.'
    reply = json.loads(request('POST', '/api/check', {'text': labelled, 'draft': labelled})[2])
    check('check with a draft: bold labels put back with a paragraph are counted', reply.get('labels_left') == 3,
          reply.get('labels_left'))
    reply = json.loads(request('POST', '/api/check', {'text': text})[2])
    check('check without a draft: still exactly four fields', set(reply) == {'words', 'score', 'clean', 'categories'},
          set(reply))
    for draft in (5, None, ['a']):
        got = refused('POST', '/api/check', {'text': text, 'draft': draft})
        check(f'check: a draft of {draft!r} is a bad request', got == (400, 'bad_request'), got)
    got = refused('POST', '/api/check', {'text': text, 'draft': 'a' * 200_001})
    check('check: a draft over 200,000 characters is too long', got == (413, 'too_long'), got)


# ── copy when done: only a result that is safe to use unread ────────────────
@section
def copy_when_done():
    board = {'text': None, 'writes': []}

    def put(text):
        board['writes'].append(text)
        board['text'] = text
        return True

    def result_of(draft, on_clipboard, *replies, **more):
        board.update(text=on_clipboard, writes=[])
        model_says(*replies)
        return run(draft, **more)[-1]

    real = server.clipboard_text, server.set_clipboard
    server.clipboard_text, server.set_clipboard = (lambda: board['text']), put
    try:
        done = result_of(SLOPPY, SLOPPY, CLEAN + NOTES, copy=True)
        check('copy: a result that passes goes to the clipboard',
              done.get('copied') is True and board['writes'] == [CLEAN], (done.get('copied'), board))
        done = result_of(SLOPPY, SLOPPY.replace(' ', '\r\n', 1) + '\r\n', CLEAN + NOTES, copy=True)
        check('copy: a clipboard that differs by more than line endings is left alone',
              done.get('copied') is False and not board['writes'], board)
        done = result_of('One.\nTwo is a seamless journey.', 'One.\r\nTwo is a seamless journey.\r\n',
                         'One.\nTwo is a trip.' + NOTES, copy=True)
        check('copy: CRLF on the clipboard is the same text', done.get('copied') is True, done.get('copied'))
        # A textarea makes LF of a lone CR too, and of each line break in \r\r\n.
        for held in ('One.\rTwo is a seamless journey.', 'One.\r\r\nTwo is a seamless journey.'):
            done = result_of(re.sub(r'\r\n?', '\n', held), held, 'One.\nTwo is a trip.' + NOTES, copy=True)
            check(f'copy: {held!r} on the clipboard is the same text', done.get('copied') is True,
                  done.get('copied'))
        done = result_of(SLOPPY, SLOPPY, CLEAN + NOTES)
        check('copy: only when asked', done.get('copied') is False and not board['writes'], board)
        # Thorough rewrites most sentences and can change how firm a promise is, which no
        # check sees: the result has to be read before it is used.
        done = result_of(SLOPPY, SLOPPY, CLEAN + NOTES, copy=True, strength='thorough')
        check('copy: never a Thorough rewrite',
              done['after']['clean'] is True and done.get('copied') is False and not board['writes'], board)
        done = result_of(SLOPPY, SLOPPY, CLEAN + NOTES, copy='yes')
        check('copy: asked means exactly true', done.get('copied') is False and not board['writes'], board)
        done = result_of(SLOPPY, 'something else copied meanwhile', CLEAN + NOTES, copy=True)
        check('copy: never over something else the user copied',
              done.get('copied') is False and not board['writes'], board)
        done = result_of(SLOPPY, None, CLEAN + NOTES, copy=True)
        check('copy: not when the clipboard holds no text', done.get('copied') is False, done.get('copied'))
        flagged = 'It is a seamless tool.' + NOTES
        done = result_of(SLOPPY, SLOPPY, flagged, flagged, copy=True)
        check('copy: not while something is still flagged',
              done['after']['clean'] is False and done.get('copied') is False and not board['writes'], board)
        draft = 'We leverage feedback from 25 businesses.'
        done = result_of(draft, draft, 'We use feedback.' + NOTES, copy=True)
        check('copy: not when a number went missing',
              done['after']['clean'] is True and done.get('copied') is False and not board['writes'], board)
        draft = "Here's a draft:\n\nThe room is booked.\n\n---\n\nA few notes: it seats 12."
        done = result_of(draft, draft, 'The room is booked.' + NOTES, copy=True)
        check('copy: a number that left with the chat reply around the draft does not stop it',
              done['warnings'].get('framing') == ['12'] and done.get('copied') is True, done['warnings'])
        draft = "Here's a draft:\n\nThe room seats 12 people.\n\n---\n\nA few notes: it seats 12."
        dropped = draft.replace("Here's a draft:\n\n", '').replace('12 people', 'a dozen people')
        done = result_of(draft, draft, dropped + NOTES, copy=True)
        check('copy: not when the notes kept their number and the draft lost it',
              done['warnings']['lost'] == ['12 (1 of 2)'] and done.get('copied') is False
              and not board['writes'], done['warnings'])
        # The rule over the draft is not a fenced note because the letter opens with "I'd"
        # (review R3L-3): the dates it lost are lost, and the result is not copied.
        draft = ("Here's a draft:\n\n---\n\nI'd like to let you know that I am resigning from my role as "
                 'Support Lead.\n\nMy last day will be March 28, which gives the team 4 weeks to plan the '
                 'handover.\n\nThank you for the last 6 years.\n\nBest,\nRiley\n\n---\n\n'
                 'Want me to make it more formal?')
        dropped = ("I'd like to let you know that I am resigning from my role as Support Lead.\n\nMy last day "
                   'will be at the end of March, which gives the team time to plan the handover.\n\n'
                   'Thank you for the last few years.\n\nBest,\nRiley')
        done = result_of(draft, draft, dropped + NOTES, copy=True, auto=True)
        check('copy: not when a draft fenced by rules lost its dates',
              done['warnings']['lost'] == ['28', '4', '6'] and done.get('copied') is False
              and not board['writes'], done['warnings'])
        done = result_of(CLEAN, CLEAN, copy=True, auto=True)
        check('copy: text handed back unchanged is not copied',
              done['rounds'] == 0 and done.get('copied') is False and not board['writes'], board)
        done = result_of(CLEAN, CLEAN, CLEAN + NOTES, copy=True)
        check('copy: nor a rewrite that changed nothing, which is on the clipboard already',
              done['rounds'] == 1 and done.get('copied') is False and not board['writes'], board)
        done = result_of(SLOPPY, SLOPPY, CLEAN + '\n\nChanges I made:\n- Removed the contrast.', copy=True)
        check('copy: a reply with no notes marker is shown but not copied',
              done['after']['clean'] is True and done.get('copied') is False and not board['writes'], board)
        done = result_of(SLOPPY, SLOPPY, 'It is a seamless tool.\n\n- my notes', 'It is a tool.\n\n- my notes' + NOTES,
                         copy=True)
        check('copy: nor when only an earlier round lacked the marker',
              done['rounds'] == 2 and done.get('copied') is False and not board['writes'], (done['rounds'], board))

        def broken(text):
            raise OSError('clipboard went away')

        server.set_clipboard = broken
        done = result_of(SLOPPY, SLOPPY, CLEAN + NOTES, copy=True)
        check('copy: a clipboard error costs the copy, not the result',
              done.get('event') == 'done' and done.get('copied') is False, done)
        server.set_clipboard = lambda text: False
        done = result_of(SLOPPY, SLOPPY, CLEAN + NOTES, copy=True)
        check('copy: a write that did not take is not reported as copied', done.get('copied') is False)
    finally:
        server.clipboard_text, server.set_clipboard = real


# ── upstream's patterns must not be able to hold the server ─────────────────
@section
def long_runs():
    began = time.time()
    status, _, data = request('POST', '/api/check',
                              {'text': 'Order ' + '7' * 3000 + ' went to 12 customers.'})
    took = time.time() - began
    check('a 3,000-digit number is checked quickly', status == 200 and took < 5,
          f'{status} in {took:.1f}s')
    check('and the real figure beside it is still found', b'12 customers' in data, data[:300])
    # Commas are not \w, so a spreadsheet column pasted as 1,1,1,... gets past the rule above.
    # The checker's PROOF no longer starts inside a number either, so this stays quick with
    # either guard alone: the timing checks the two together, and the line after it the
    # checker's copy, where checkable() breaks the run up.
    began = time.time()
    server.check('Order ' + '1,' * 6000 + '1 went to 12 customers.')
    check('a 12,000-character comma-grouped number is checked quickly', time.time() - began < 5,
          f'{time.time() - began:.1f}s')
    check('the checker is shown a long comma-grouped run broken up',
          max(map(len, server.checkable('1,' * 6000).split())) <= 40)
    began = time.time()
    for hostile in ('[' * 200_000, '[32m colour [0m ' * 12_000, '](' * 100_000):
        server.check(hostile)
    # About 2 s here, and over 5 s on a busy PC. A pattern that backtracked would take minutes.
    check('200,000 characters of unclosed brackets are checked quickly', time.time() - began < 10,
          f'{time.time() - began:.1f}s')
    began = time.time()
    for hostile in ('Option 1' + ' ' * 2000 + 'x', 'Draft' + '\t' * 2000 + ':' + ' ' * 2000 + 'x',
                    "Here's a draft:" + ' ' * 150_000 + 'x',
                    'Best,\n[Your name]\n' + '```a```\n' * 30 + '-- \n555 0100'):
        server.check(hostile)
    check('a long run of blanks after a chat label is checked quickly', time.time() - began < 3,
          f'{time.time() - began:.1f}s')
    # Twelve thousand short lines, a few hundred of them different: a diff of kept framing
    # by line costs the square of that, so a text this long counts lines instead.
    menu = [f'Item {n % 300}: {n % 7} left' for n in range(12_000)]
    draft = "Here's a draft you can adapt:\n\n" + '\n'.join(menu) + '\n\nWant me to make it shorter?'
    began = time.time()
    server.carried(draft, '\n'.join(line + ' today' if n % 7 == 0 else line for n, line in enumerate(menu)))
    check('the framing kept in a rewrite of twelve thousand lines is found quickly', time.time() - began < 3,
          f'{time.time() - began:.1f}s')
    linked ='See [the long guide to how the parts\nfit together](https://example.com/robust-seamless-design) first.'
    check('and a real link, even one that wraps, is still read as a link',
          server.check(linked)['score'] == 5, server.check(linked))
    check('/api/check has the length limit too',
          refused('POST', '/api/check', {'text': 'a' * 200_001}) == (413, 'too_long'))


# ── engine failures reach the page as an error event ────────────────────────
@section
def engine_failures():
    model_says(server.EngineError('not_logged_in', 'log in first'))
    ev = run(SLOPPY)
    check('not logged in: events', kinds(ev) == ['start', 'round', 'error'], kinds(ev))
    check('not logged in: code and message',
          ev[-1]['error'] == {'code': 'not_logged_in', 'message': 'log in first'}, ev[-1])
    for name, reply in (('an empty reply', ''), ('a reply that is only notes', NOTES + '- no')):
        model_says(reply)
        ev = run(SLOPPY)
        check(f'{name} is empty_output',
              ev[-1].get('error', {}).get('code') == 'empty_output', ev[-1])
    model_says(RuntimeError('boom'))
    ev = run(SLOPPY)
    check('a crash in the engine is engine_failed',
          ev[-1].get('error', {}).get('code') == 'engine_failed', ev[-1])
    # A later round failing must not throw away the rewrite an earlier round produced.
    for name, failure in (('empty', ''), ('timed out', server.EngineError('timeout', 'slow'))):
        model_says('We leverage a tool.' + NOTES + '- one', failure)
        done = run(SLOPPY)[-1]
        check(f'round two {name}: round one is returned',
              done['event'] == 'done' and done['text'] == 'We leverage a tool.', done)
        check(f'round two {name}: a note says so', 'Round 2' in done['notes'][-1], done['notes'])
        check(f'round two {name}: it is not counted as a round', done['rounds'] == 1, done['rounds'])


# ── one log line per run, with counts and scores and none of the text ───────
@section
def run_log():
    lines = []

    class Kept(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    kept = Kept()
    server.log.setLevel(logging.INFO)
    server.log.addHandler(kept)
    try:
        model_says(CLEAN + NOTES)
        run(SLOPPY)
        done = [line for line in lines if line.startswith('run done:')]
        check('log: a run is one line, with the model and the rounds',
              len(done) == 1 and 'model=haiku' in done[0] and 'rounds=1' in done[0], lines)
        check('log: and its scores', done and 'score=3->5' in done[0] and 'stopped=-' in done[0], done)
        check('log: and how much to change, and whose instructions',
              done and 'strength=light' in done[0] and 'instructions=default' in done[0], done)
        model_says()
        run(CLEAN, auto=True)
        check('log: a run with nothing to fix says it was skipped',
              any(line.startswith('run skipped:') for line in lines), lines)
        model_says(server.EngineError('not_logged_in', 'log in first'))
        run(SLOPPY)
        check('log: a failed run says how',
              any(line.startswith('run failed: code=not_logged_in') for line in lines), lines)
        model_says('It is a seamless tool.' + NOTES + '- the journey is gone', 'It is a robust tool.' + NOTES)
        run(SLOPPY)
        check('log: a run that stopped early says so',
              any(line.startswith('run done:') and 'stopped=no_progress' in line for line in lines), lines)
        check('log: no line holds the text, the notes or what was flagged',
              not any(word in line for line in lines for word in ('game-changing', 'journey', 'tool', 'seamless')),
              [line for line in lines if 'tool' in line or 'journey' in line])
    finally:
        server.log.removeHandler(kept)
        server.log.setLevel(logging.NOTSET)


# ── heartbeat while the model works ─────────────────────────────────────────
@section
def heartbeat():
    def slow(system_prompt, user_message, model, cancel):
        time.sleep(1.5)
        return CLEAN + NOTES

    server.run_model = slow
    events = [e['event'] for e in run(SLOPPY)]
    check('a tick arrives while the model runs',
          'tick' in events[events.index('round'):events.index('checked')], events)


# ── cancelling ──────────────────────────────────────────────────────────────
@section
def abort_cancels():
    # The page cancels by aborting its fetch. The server finds out when a tick cannot be
    # written, and must then stop the model rather than let it finish for nobody.
    entered.clear()
    server.run_model = blocks
    conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
    conn.request('POST', '/api/unslop', json.dumps({'text': SLOPPY, 'model': 'haiku'}).encode(),
                 {'X-Slop-Token': server.TOKEN})
    resp = conn.getresponse()
    check('abort: the stream had started', json.loads(resp.readline())['event'] == 'start')
    check('abort: the model was running', entered.wait(5))
    resp.close()
    conn.close()
    check('aborting the request cancels the run', held['cancel'].wait(10),
          'the model was left running')


@section
def new_run_takes_over():
    entered.clear()
    server.run_model = blocks
    first = {}
    runner = run_in_background(first)
    check('takeover: the first run reached the model', entered.wait(5))
    first_cancel = held['cancel']
    model_says(CLEAN + NOTES + '- done')
    ev = run(SLOPPY)
    runner.join(10)
    check('takeover: the new run finishes', ev[-1]['event'] == 'done', ev[-1])
    check('takeover: the old run was cancelled', first_cancel.is_set())
    check('takeover: the old stream ends with an error',
          first.get('events', [{}])[-1].get('error', {}).get('code') == 'cancelled', first)


# ── the real run_model and engine_status, against a fake CLI ────────────────
# Popen is redirected to a script that answers the way the real CLI was observed to
# (2.1.197), so argument passing, UTF-8 on stdin, the JSON reply, cancel and timeout all
# run for real. What this cannot show is that the real CLI behaves as the script does.
FAKE_CLI = '''
import json, os, sys, time
mode, args = os.environ['SLOP_FAKE'], sys.argv[1:]
if args == ['auth', 'status']:
    print(json.dumps({'loggedIn': mode != 'logged_out', 'authMethod': 'none'}, indent=2))
    sys.exit(1 if mode == 'logged_out' else 0)
if args == ['--version']:
    print(os.environ.get('SLOP_FAKE_VERSION', '2.1.999') + ' (Claude Code)')
    sys.exit(0)
# 2.1.292 turns down a command line on stderr, with nothing on stdout, before any model call.
REFUSALS = {'usage': "error: unknown option '--safe-mode'\\n(Did you mean --sage-mode?) SECRET-WORDS",
            'usage_value': "error: option '--output-format <format>' argument 'json' is invalid. "
                           "Allowed choices are text, stream-json.",
            'usage_other': 'error: the pasted SECRET-WORDS were refused'}
if mode in REFUSALS:
    sys.stderr.write(REFUSALS[mode] + '\\n')
    sys.exit(1)
stdin = sys.stdin.buffer.read().decode('utf-8')
code, model = 0, args[args.index('--model') + 1]
if mode == 'hang':
    time.sleep(60)
if mode == 'garbage':
    out = 'Segmentation fault'
elif mode == 'no_model' or mode == 'no_pin' and model == 'claude-haiku-5-5':
    # How 2.1.292 answered a model id that does not exist, after the API said 404.
    sys.stderr.write('[claude-code:unrecognized_model] {"model":"%s"}\\n' % model)
    out, code = json.dumps({'type': 'result', 'is_error': True, 'api_error_status': 404,
                            'result': "There's an issue with the selected model (%s). It may not "
                                      "exist or you may not have access to it." % model}), 1
elif mode == 'logged_out':
    out, code = json.dumps({'type': 'result', 'is_error': True,
                            'result': 'Not logged in - Please run /login'}), 1
elif mode in ('limit', 'limit_old', 'overloaded', 'offline', 'refused', 'ssl'):
    # How 2.1.292 says the plan is spent, how older copies said it, and an error that is no limit.
    # Then how it gives up after its retries with no network, and a certificate error, which is
    # no network problem.
    said = {'limit': "You've hit your session limit \\u00b7 resets 3pm (Europe/Berlin)",
            'limit_old': 'Claude AI usage limit reached|1760900000',
            'overloaded': 'API Error: 529 Overloaded',
            'offline': "API Error: Can't reach the API server - check your internet or DNS (ENOTFOUND)",
            'refused': 'API Error: Connection refused - a firewall or proxy may be blocking it (ECONNREFUSED)',
            'ssl': 'API Error: Unable to connect to API: SSL certificate has expired'}[mode]
    out, code = json.dumps({'type': 'result', 'is_error': True, 'result': said}), 1
else:
    system = None
    if '--system-prompt-file' in args:
        with open(args[args.index('--system-prompt-file') + 1], encoding='utf-8', newline='') as fh:
            system = fh.read()
    echo = json.dumps({'argv': args, 'stdin': stdin, 'cwd': os.getcwd(), 'system': system,
                       'attach': os.environ.get('CLAUDE_CODE_DISABLE_ATTACHMENTS')})
    out = json.dumps({'type': 'result', 'is_error': False, 'result': echo}, ensure_ascii=False)
sys.stdout.buffer.write(out.encode('utf-8'))
sys.exit(code)
'''


def engine_code(mode, cancel=None):
    """The EngineError code the real run_model raises with the fake CLI in this mode."""
    return engine_error(mode, cancel)[0]


def engine_error(mode, cancel=None):
    """(code, message) of the EngineError the real run_model raises with the fake CLI in this mode."""
    os.environ['SLOP_FAKE'] = mode
    try:
        real_run_model('system', 'message', 'haiku', cancel or threading.Event())
    except server.EngineError as e:
        return e.code, e.message
    return 'no error', ''


def status_now(mode, fresh=True):
    os.environ['SLOP_FAKE'] = mode
    if fresh:
        server._status['until'] = 0
    return json.loads(request('GET', '/api/status')[2])


@section
def real_engine_with_a_fake_cli():
    fake_cli = os.path.join(tmp, 'fake_claude.py')
    with open(fake_cli, 'w', encoding='utf-8') as fh:
        fh.write(FAKE_CLI)
    shutil.which = lambda *a, **k: 'claude'
    spawned = []

    def popen(argv, **kw):
        spawned.append(real_popen([sys.executable, fake_cli] + argv[1:], **kw))
        return spawned[-1]

    subprocess.Popen = popen
    try:
        os.environ['SLOP_FAKE'] = 'ok'
        system = 'Line one — “quoted”\nline two with "straight" quotes and %PATH%'
        message = 'héllo — it’s 你好\nsecond line'
        got = json.loads(real_run_model(system, message, 'opus', threading.Event()))
        args = got['argv']

        def after(flag):
            return args[args.index(flag) + 1] if flag in args else None

        check('CLI: the message goes in on stdin as UTF-8', got['stdin'] == message,
              ascii(got['stdin']))
        check('CLI: the system prompt arrives intact, in a file', got['system'] == system,
              ascii(got['system']))
        check('CLI: the system prompt file is removed after the call',
              after('--system-prompt-file') is not None and
              not os.path.exists(after('--system-prompt-file')), after('--system-prompt-file'))
        # On the command line, more than 32,767 characters stop Windows starting the CLI at all.
        long = 'Keep the "quotes". ' * 4000
        try:
            arrived = json.loads(real_run_model(long, 'm', 'opus', threading.Event()))['system']
        except Exception as e:                          # WinError 206 when it went on the command line
            arrived = repr(e)
        check('CLI: instructions as long as the app saves still arrive', arrived == long, arrived[:200])
        check('CLI: print mode, cut off from user config', args[0] == '-p' and
              '--safe-mode' in args and '--no-session-persistence' in args, args)
        check('CLI: no tools, JSON out, chosen model', (after('--tools'), after('--output-format'),
              after('--model')) == ('', 'json', 'opus'), args)
        fast = json.loads(real_run_model('s', 'm', 'haiku', threading.Event()))['argv']
        check('CLI: Fast is asked for by a full model name, not the slow alias',
              fast[fast.index('--model') + 1] == 'claude-haiku-5-5', fast)
        check('CLI: runs in the data folder',
              os.path.realpath(got['cwd']) == os.path.realpath(server.DATA_DIR), got['cwd'])
        # Without it the CLI reads any file the pasted text names as @path into the request.
        check('CLI: @file attachments are switched off', got['attach'] == '1', got['attach'])

        for mode, want in (('logged_out', 'not_logged_in'), ('limit', 'usage_limit'),
                           ('limit_old', 'usage_limit'), ('overloaded', 'engine_failed'),
                           ('garbage', 'engine_failed'), ('offline', 'offline'), ('refused', 'offline'),
                           ('ssl', 'engine_failed')):
            got = engine_code(mode)
            check(f'CLI: {mode} is {want}', got == want, got)
        # A spent plan is told in the app's words, with the time the CLI gives (QA FR-1).
        said = engine_error('limit')[1]
        check('CLI: a usage limit says when it resets, in the app\'s words',
              said.startswith(server.CLAUDE_LIMIT) and said.endswith('resets 3pm (Europe/Berlin).'), said)
        said = engine_error('limit_old')[1]
        check('CLI: the older wording gives the app\'s words, not the raw reply',
              said == server.CLAUDE_LIMIT, said)
        # The log holds sizes only, so it is no place to send the user for unreadable output (QA FR-7).
        said = engine_error('garbage')[1]
        check('CLI: unreadable output says how to see what the CLI says',
              'server.log' not in said and '"claude -p hi"' in said, said)
        # Where the log does hold a traceback, the message says where the log is.
        check('an unexpected error names the log\'s folder',
              server.UNEXPECTED.endswith('in %LOCALAPPDATA%\\SlopMonster\\server.log.'), server.UNEXPECTED)
        # What the CLI printed can be the user's text in some form. It stays out of the log.
        with open(os.path.join(server.DATA_DIR, 'server.log'), encoding='utf-8') as fh:
            logged = fh.read()
        check('CLI: unreadable output is logged by size, not content',
              'unreadable output' in logged and 'Segmentation fault' not in logged, logged[-300:])
        # No network: the CLI retries for about three minutes, then says so in its own words.
        said = engine_error('offline')[1]
        check('CLI: no network says to check the connection', said == server.OFFLINE.format('Claude'), said)

        # A data folder that cannot be written: say so, not "see the log", which cannot be written either.
        real_dir, blocked = server.DATA_DIR, os.path.join(tmp, 'not-a-folder')
        open(blocked, 'w').close()
        server.DATA_DIR = blocked
        try:
            ran = len(spawned)
            got = engine_error('ok')
        finally:
            server.DATA_DIR = real_dir
        check('CLI: a data folder that cannot be written is said so, and no CLI starts',
              got[0] == 'data_folder' and 'data folder' in got[1] and len(spawned) == ran, got)

        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        began = time.time()
        got = engine_code('hang', cancel)
        check('CLI: cancel kills the call promptly', got == 'cancelled' and
              time.time() - began < 5, f'{got} after {time.time() - began:.1f}s')
        check('CLI: cancel leaves no process behind', spawned[-1].poll() is not None)
        server.TIMEOUT = 1
        began = time.time()
        got = engine_code('hang')
        check('CLI: a call past the time limit is a timeout', got == 'timeout' and
              time.time() - began < 6, f'{got} after {time.time() - began:.1f}s')
        check('CLI: a timeout leaves no process behind', spawned[-1].poll() is not None)
        server.TIMEOUT = 180

        # The same failure through the whole stack: page request, real run_model, fake CLI.
        os.environ['SLOP_FAKE'] = 'logged_out'
        server.run_model = real_run_model
        err = run(SLOPPY)[-1].get('error', {})
        check('not logged in, end to end',
              err.get('code') == 'not_logged_in' and 'auth login' in err.get('message', ''), err)
        model_says()

        s = status_now('ok')
        check('status: ready', s['engines']['claude'] == {'ok': True, 'code': 'ready', 'message': 'Ready.'}, s)
        # Only what the page reads: a field it ignores can drift from the page unnoticed.
        check('status: the engines and the hotkey, nothing more', set(s) == {'engines', 'hotkey'}, s)
        s = status_now('logged_out')['engines']['claude']
        check('status: not logged in', (s['ok'], s['code']) == (False, 'not_logged_in'), s)
        check('status: the message says what to do', 'auth login' in s['message'], s['message'])
        s = status_now('ok', fresh=False)['engines']['claude']
        check('status: the answer is cached', s['code'] == 'not_logged_in', s)
        # An npm install's batch shim would lose the prompt and the flags, so it is refused.
        shutil.which = lambda *a, **k: 'C:/npm/claude.cmd'
        s = status_now('ok')['engines']['claude']
        check('npm shim: status says so', s['code'] == 'missing' and 'npm' in s['message'], s)
        check('npm shim: run_model refuses it', engine_code('ok') == 'missing')
        shutil.which = no_cli
        s = status_now('ok')['engines']['claude']
        check('status: missing', (s['ok'], s['code']) == (False, 'missing'), s)
        check('no CLI: run_model says missing', engine_code('ok') == 'missing')
    finally:
        subprocess.Popen, shutil.which, server.TIMEOUT = real_popen, no_cli, 180
        model_says()


# ── a Claude Code update that refuses the command line, or the pinned Fast name ──
@section
def engine_changes():
    fake_cli = os.path.join(tmp, 'fake_claude.py')
    with open(fake_cli, 'w', encoding='utf-8') as fh:
        fh.write(FAKE_CLI)
    shutil.which = lambda *a, **k: 'claude'
    spawned = []

    def popen(argv, **kw):
        spawned.append(argv[1:])
        return real_popen([sys.executable, fake_cli] + argv[1:], **kw)

    def refusal(mode):
        os.environ['SLOP_FAKE'] = mode
        try:
            real_run_model('system', 'message', 'sonnet', threading.Event())
        except server.EngineError as e:
            return e
        return None

    subprocess.Popen = popen
    try:
        e = refusal('usage')
        check('refused flag: engine_changed', e and e.code == 'engine_changed', e and e.code)
        check('refused flag: the message names the flag and the version', e and e.message == (
            "Claude Code refused the command: unknown option '--safe-mode'. This version of Claude "
            "Code (2.1.999) needs a newer SlopMonster."), e and e.message)
        e = refusal('usage_value')
        check('refused value: engine_changed, naming the flag', e and e.code == 'engine_changed' and
              "'--output-format'" in e.message and 'json' not in e.message, e and e.message)
        e = refusal('usage_other')
        check('any other stderr: engine_failed, as before', e and e.code == 'engine_failed' and
              'SECRET' not in e.message, e and e.message)
        spawned.clear()
        refusal('usage')
        check('the version is asked once', spawned.count(['--version']) == 0, spawned)
        # A copy older than the one the command line was measured on: update Claude Code.
        server._version.clear()
        os.environ['SLOP_FAKE_VERSION'] = '2.1.19'
        try:
            e = refusal('usage')
        finally:
            del os.environ['SLOP_FAKE_VERSION']
            server._version.clear()
        check('refused flag on an older Claude Code: cli_old, saying to update it',
              e and e.code == 'cli_old' and e.message == (
                  "Claude Code refused the command: unknown option '--safe-mode'. Claude Code "
                  "2.1.19 is older than this app needs. Update Claude Code, then try again."),
              e and (e.code, e.message))
        os.environ['SLOP_FAKE'] = 'usage'
        server.run_model = real_run_model
        err = run(SLOPPY)[-1].get('error', {})
        check('refused flag, end to end', err.get('code') == 'engine_changed' and
              '--safe-mode' in err.get('message', ''), err)
        model_says()
        with open(os.path.join(server.DATA_DIR, 'server.log'), encoding='utf-8') as fh:
            logged = fh.read()
        check('refused flag: the log names the flag and keeps the rest of stderr out',
              "unknown option '--safe-mode'" in logged and 'SECRET' not in logged and
              'sage-mode' not in logged, logged[-400:])

        # The pinned Fast name refused: once with it, once with the alias, then the alias only.
        os.environ['SLOP_FAKE'] = 'no_pin'
        spawned.clear()
        got = json.loads(real_run_model('s', 'm', 'haiku', threading.Event()))['argv']
        models = [a[a.index('--model') + 1] for a in spawned if '--model' in a]
        check('pinned name refused: tried again with the alias',
              models == ['claude-haiku-5-5', 'haiku'] and got[got.index('--model') + 1] == 'haiku',
              models)
        spawned.clear()
        real_run_model('s', 'm', 'haiku', threading.Event())
        models = [a[a.index('--model') + 1] for a in spawned if '--model' in a]
        check('pinned name refused: later calls go straight to the alias', models == ['haiku'], models)
        got = json.loads(real_run_model('s', 'm', 'opus', threading.Event()))['argv']
        check('pinned name refused: other models are left alone',
              got[got.index('--model') + 1] == 'opus', got)
        # The alias refused too is reported as it is, with no third try.
        os.environ['SLOP_FAKE'] = 'no_model'
        spawned.clear()
        e = refusal('no_model')
        check('a refused alias or model is engine_failed, as before', e and e.code == 'engine_failed'
              and 'selected model (sonnet)' in e.message and len(spawned) == 1, (e and e.message, spawned))
        spawned.clear()
        try:
            real_run_model('s', 'm', 'haiku', threading.Event())
            e = None
        except server.EngineError as caught:
            e = caught
        check('the alias refused after the fallback: reported, one call',
              e and e.code == 'engine_failed' and len(spawned) == 1, (e and e.message, spawned))
        server._cli_model.update(server.CLI_MODEL)
        spawned.clear()
        e = refusal('no_model')
        check('a refused model that is not pinned: one call', len(spawned) == 1, spawned)

        # The note on the result: on every Fast run once the alias is in use, and only then.
        model_says(CLEAN + NOTES)
        done = run(SLOPPY)[-1]
        check('no note while the pinned name works',
              server.ALIAS_NOTE not in done.get('notes', []), done)

        def falls_back(system_prompt, user_message, model, cancel):
            server._cli_model[model] = model
            return CLEAN + NOTES + '- one'

        server.run_model = falls_back
        done = run(SLOPPY)[-1]
        check('fallback: the result says so, first', done.get('notes', [None])[0] == server.ALIAS_NOTE
              and len(done['notes']) == 2, done)
        model_says(CLEAN + NOTES)
        done = run(SLOPPY)[-1]
        check('fallback: a later Fast run says so too', server.ALIAS_NOTE in done.get('notes', []), done)
        model_says(CLEAN + NOTES)
        done = run(SLOPPY, model='sonnet')[-1]
        check('fallback: a Balanced run does not', server.ALIAS_NOTE not in done.get('notes', []), done)
    finally:
        subprocess.Popen, shutil.which = real_popen, no_cli
        server._cli_model.update(server.CLI_MODEL)
        model_says()


# ── the ChatGPT engine: the real run_codex against a fake Codex ─────────────
# Popen is redirected to a script that answers the way codex 0.163.0 was observed to: the
# reply in the -o file, and on stderr a header, the whole prompt, then any "ERROR: " lines.
FAKE_CODEX = r'''
import json, os, subprocess, sys, time
mode, args = os.environ['SLOP_CODEX'], sys.argv[1:]
if args == ['login', 'status']:
    said = {'logged_out': 'Not logged in', 'api_key': 'Logged in using an API key - sk-proj-***SECRET'}
    sys.stderr.write(said.get(mode, 'Logged in using ChatGPT') + '\n')
    sys.exit(1 if mode == 'logged_out' else 0)
if args == ['--version']:
    sys.stdout.write('codex-cli %s\n' % os.environ.get('SLOP_CODEX_VERSION', '0.163.0'))
    sys.exit(0)
stdin = sys.stdin.buffer.read().decode('utf-8')
refused = {'refused_flag': "error: unexpected argument '--ignore-rules' found\n\n  tip: to pass '--ignore-rules' "
                           "as a value, use '-- --ignore-rules'\n\nUsage: codex exec [OPTIONS] [PROMPT]\n",
           'refused_value': "error: invalid value 'read-only' for '--sandbox <SANDBOX_MODE>'\n"
                            "  [possible values: workspace-write, danger-full-access]\n",
           'refused_key': 'Error loading config.toml: unknown variant `disabled`, expected one of `cached`, `live`\n'}
if mode in refused:
    sys.stderr.write(refused[mode])
    sys.exit(2 if mode != 'refused_key' else 1)
sys.stderr.write('OpenAI Codex v0.163.0\n--------\nworkdir: %s\nmodel: gpt-test-1\nprovider: openai\n'
                 'session id: 0000\n--------\nuser\n%s\n' % (os.getcwd(), stdin))
if mode == 'ignoring':
    sys.stderr.write('warning: Codex is ignoring 2 unrecognized configuration settings. Check for typos or '
                     'deprecated settings.\n  session-flags: `features.shell_tool` is ignored.\n'
                     '  session-flags: `features.SECRET_marker` is ignored.\n')
out = args[args.index('-o') + 1]
errors = {'signed_out': 'ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication',
          'limit': "ERROR: You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro)",
          'broken': 'ERROR: something went wrong with SECRET-WORDS',
          'no_model': 'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":'
                      '"The \'gpt-x\' model is not supported when using Codex with a ChatGPT account."}}'}
if mode in errors:
    sys.stderr.write(errors[mode] + '\n')
    sys.exit(1)
if mode == 'hang':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    with open(os.environ['SLOP_CODEX_PIDS'], 'w') as fh:
        fh.write('%d %d' % (os.getpid(), child.pid))
    time.sleep(60)
if mode == 'empty':
    sys.exit(0)
if mode == 'clean':
    reply = 'It is a tool that checks copy.\n<<<SLOPMONSTER-NOTES>>>\n- done'
else:
    ins = next(a.split('=', 1)[1] for a in args if a.startswith('model_instructions_file='))
    reply = json.dumps({'argv': args, 'stdin': stdin, 'cwd': os.getcwd(), 'instructions': open(
        json.loads(ins), encoding='utf-8').read(), 'keys': sorted(k for k in os.environ if 'API_KEY' in k)})
with open(out, 'w', encoding='utf-8') as fh:
    fh.write(reply)
'''


def alive(pid):
    """Whether a process is still running, by its id."""
    import ctypes
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


@section
def chatgpt_engine():
    fake = os.path.join(tmp, 'fake_codex.py')
    with open(fake, 'w', encoding='utf-8') as fh:
        fh.write(FAKE_CODEX)
    exe = os.path.join(tmp, 'bin', 'codex.exe')
    spawned = []

    def popen(argv, **kw):
        if argv and argv[0] == exe:
            spawned.append(argv[1:])
            argv = [sys.executable, fake] + argv[1:]
        return real_popen(argv, **kw)

    def codex(mode, message='message', model='', effort='', cancel=None, seen=None):
        """(reply, None) or (None, EngineError) from the real run_codex with the fake in this mode."""
        os.environ['SLOP_CODEX'] = mode
        server._codex_status['until'] = 0
        try:
            return server.run_codex('system', message, model, effort, cancel or threading.Event(), seen), None
        except server.EngineError as e:
            return None, e

    def leftovers():
        return [n for n in os.listdir(server.DATA_DIR) if n.startswith('codex-')]

    lines = []

    class Kept(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    kept = Kept()
    server.log.setLevel(logging.INFO)
    server.log.addHandler(kept)
    shutil.which = lambda name, *a, **k: exe if name == 'codex.exe' else None
    subprocess.Popen = popen
    os.environ.update(OPENAI_API_KEY='sk-test', CODEX_API_KEY='ck-test')
    try:
        system = 'Line one — “quoted”\nline two with "straight" quotes and %PATH%'
        message = 'héllo — it’s 你好\nsecond line\nmodel: spoofed'
        os.environ['SLOP_CODEX'] = 'ok'
        seen = {}
        got = json.loads(server.run_codex(system, message, 'gpt-6.1-sol', 'high', threading.Event(), seen))
        args = got['argv']
        check('codex: the native exe, never a .cmd', spawned and spawned[-1] == args, spawned)
        check('codex: every locked-down flag is passed',
              args[:len(server.CODEX_FLAGS)] == server.CODEX_FLAGS, args)
        check('codex: no shell, web search, apps, plugins, hooks or helper agents', all(
            flag in args for flag in ('features.shell_tool=false', 'features.unified_exec=false',
                                      'web_search="disabled"', 'features.hooks=false', 'features.apps=false',
                                      'features.plugins=false', 'agents.enabled=false', 'read-only',
                                      '--ignore-user-config', '--ignore-rules', '--ephemeral')), args)
        check('codex: no AGENTS.md from a git repository above the work folder',
              'project_doc_max_bytes=0' in args, args)
        check('codex: the chosen model and effort',
              args[args.index('-m') + 1] == 'gpt-6.1-sol' and 'model_reasoning_effort="high"' in args, args)
        check('codex: the reply from the -o file, the prompt on stdin', args[-3] == '-o' and args[-1] == '-'
              and got['stdin'] == message, ascii(got['stdin']))
        check('codex: the system prompt arrives intact in its file', got['instructions'] == system,
              ascii(got['instructions']))
        check('codex: runs in an empty folder of its own under the data folder, removed afterwards',
              os.path.dirname(os.path.realpath(got['cwd'])) == os.path.realpath(server.DATA_DIR)
              and not os.path.exists(got['cwd']) and not leftovers(), (got['cwd'], leftovers()))
        check('codex: no API key, so the ChatGPT plan is used', got['keys'] == [], got['keys'])
        check('codex: the model that answered is read from the header, not the prompt',
              seen == {'model': 'gpt-test-1'}, seen)
        got, _ = codex('ok')
        args = json.loads(got)['argv']
        check('codex: the plan\'s default model and effort ask for neither',
              '-m' not in args and not any('reasoning_effort' in a for a in args), args)

        for mode, want in (('signed_out', 'not_logged_in'), ('limit', 'usage_limit'),
                           ('broken', 'engine_failed'), ('empty', 'empty_output')):
            _, e = codex(mode, message='the pasted SECRET-TEXT')
            check(f'codex: {mode} is {want}', e and e.code == want, e and e.code)
            check(f'codex: {mode}: nothing of stderr in the message',
                  e and 'SECRET' not in e.message and 'Upgrade' not in e.message, e and e.message)
            check(f'codex: {mode}: the folder is removed', not leftovers(), leftovers())
        # A Codex update that drops or renames part of CODEX_FLAGS, or a Codex too old for them.
        got, e = codex('ignoring', message='the pasted SECRET-TEXT')
        check('codex: a setting Codex ignored is engine_changed, and the reply is not used',
              got is None and e and e.code == 'engine_changed' and '(0.163.0)' in e.message, (got, e and e.message))
        check('codex: the ignored key the app passes is logged, nothing else of stderr',
              any('features.shell_tool' in line and 'SECRET' not in line for line in lines), lines[-3:])
        for mode, said in (('refused_flag', "'--ignore-rules'"), ('refused_value', "'--sandbox'"),
                           ('refused_key', 'the setting `disabled`')):
            _, e = codex(mode)
            check(f'codex: {mode} is engine_changed, naming what was refused', e and e.code == 'engine_changed'
                  and said in e.message and 'newer SlopMonster' in e.message, e and (e.code, e.message))
        _, e = codex('broken', message="error: unexpected argument '--x' found")
        check('codex: the same words in the pasted text are not a refused command',
              e and e.code == 'engine_failed', e and e.code)
        os.environ['SLOP_CODEX_VERSION'] = '0.150.2'
        server._version.clear()
        try:
            _, e = codex('refused_flag')
        finally:
            del os.environ['SLOP_CODEX_VERSION']
            server._version.clear()
        check('codex: an older Codex that refuses a flag needs an update itself',
              e and e.code == 'cli_old' and 'Codex 0.150.2 is older' in e.message, e and (e.code, e.message))
        # Codex echoes the prompt on stderr: an ERROR line in the pasted text is not Codex's.
        _, e = codex('broken', message='Incident notes\nERROR: 401 Unauthorized from the billing API\nusage limit')
        check('codex: an ERROR line in the text itself is not read as Codex\'s',
              e and e.code == 'engine_failed', e and e.code)
        _, e = codex('no_model', model='gpt-x')
        check('codex: a model the plan does not offer is bad_model, named from the request, not stderr',
              e and e.code == 'bad_model' and '"gpt-x"' in e.message and 'account' not in e.message,
              e and (e.code, e.message))
        check('codex: the sign-in message says what to do', 'codex login' in server.CODEX_NOT_LOGGED_IN)
        check('codex: no log line holds the prompt or stderr',
              not any(w in line for line in lines for w in ('SECRET', 'Upgrade', '401', 'pasted')),
              [line for line in lines if 'codex' in line])
        # Signed out as `codex login status` says: refused at once, without the 16 s of retries.
        spawned.clear()
        _, e = codex('logged_out')
        check('codex: signed out per login status is not_logged_in, with no model call',
              e and e.code == 'not_logged_in' and spawned == [['login', 'status']], (e and e.code, spawned))

        cancel = threading.Event()
        pids = os.path.join(tmp, 'codex-pids')
        os.environ['SLOP_CODEX_PIDS'] = pids
        threading.Timer(2, cancel.set).start()
        began = time.time()
        _, e = codex('hang', cancel=cancel)
        with open(pids) as fh:
            tree = [int(n) for n in fh.read().split()]
        check('codex: cancel stops the call promptly', e and e.code == 'cancelled' and time.time() - began < 6,
              f'{e and e.code} after {time.time() - began:.1f}s')
        check('codex: cancel ends Codex and what it started', len(tree) == 2 and not any(map(alive, tree)), tree)
        check('codex: cancel removes the folder', not leftovers(), leftovers())
        os.remove(pids)
        server.TIMEOUT = 1
        began = time.time()
        _, e = codex('hang')
        server.TIMEOUT = 180
        deadline = time.time() + 5
        while not os.path.exists(pids) and time.time() < deadline:
            time.sleep(0.1)
        with open(pids) as fh:
            tree = [int(n) for n in fh.read().split()]
        check('codex: a call past the time limit is a timeout', e and e.code == 'timeout'
              and time.time() - began < 8, f'{e and e.code} after {time.time() - began:.1f}s')
        check('codex: a timeout ends Codex and what it started', not any(map(alive, tree)), tree)
        check('codex: a timeout removes the folder', not leftovers(), leftovers())

        # The whole stack: page request, dispatch, the real run_codex, the fake Codex.
        os.environ['SLOP_CODEX'] = 'clean'
        server._codex_status['until'] = 0
        model_says()
        done = run(SLOPPY, engine='chatgpt', model='', effort='low')[-1]
        check('chatgpt end to end: done names the engine and the model that answered',
              done.get('event') == 'done' and (done.get('engine'), done.get('model')) == ('chatgpt', 'gpt-test-1')
              and done.get('text') == CLEAN, done)
        check('chatgpt end to end: the log names them too',
              any(line.startswith('run done: engine=chatgpt model=gpt-test-1 ') for line in lines), lines[-3:])
        os.environ['SLOP_CODEX'] = 'signed_out'
        server._codex_status['until'] = 0
        err = run(SLOPPY, engine='chatgpt')[-1].get('error', {})
        check('chatgpt end to end: signed out', err.get('code') == 'not_logged_in'
              and 'Sign in to ChatGPT' in err.get('message', ''), err)
        check('chatgpt end to end: a failed run\'s log line names the engine',
              any(line.startswith('run failed: code=not_logged_in engine=chatgpt') for line in lines), lines[-3:])

        # Status: both engines, and nothing about the account.
        for mode, want in (('ok', {'installed': True, 'signed_in': True, 'how': 'chatgpt'}),
                           ('api_key', {'installed': True, 'signed_in': True, 'how': 'api_key'}),
                           ('logged_out', {'installed': True, 'signed_in': False, 'how': ''})):
            os.environ['SLOP_CODEX'] = mode
            server._codex_status['until'] = server._status['until'] = 0
            status, _, data = request('GET', '/api/status')
            s = json.loads(data)
            check(f'status: ChatGPT {mode}', s['engines']['chatgpt'] == want and b'SECRET' not in data
                  and b'sk-' not in data, s.get('engines'))
        os.environ['SLOP_CODEX'] = 'ok'
        s = json.loads(request('GET', '/api/status')[2])['engines']['chatgpt']
        check('status: ChatGPT\'s answer is cached while it says signed out', not s['signed_in'], s)
        # Signed in is trusted for minutes, so a run after a pause does not wait on `codex login status`.
        server._codex_status['until'] = 0
        s = json.loads(request('GET', '/api/status')[2])['engines']['chatgpt']
        check('status: ChatGPT signed in is trusted for minutes',
              s['signed_in'] and server._codex_status['until'] - time.monotonic() > 200,
              server._codex_status['until'] - time.monotonic())
        shutil.which = no_cli
        server._codex_status['until'] = 0
        s = json.loads(request('GET', '/api/status')[2])['engines']['chatgpt']
        check('status: ChatGPT not installed', s == {'installed': False, 'signed_in': False, 'how': ''}, s)
        _, e = codex('ok')
        check('codex: with no codex.exe it says so', e and e.code == 'missing' and 'codex' in e.message, e)
        server.CODEX_PATHS = [exe]
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        with open(exe, 'w') as fh:
            fh.write('stand-in')
        check('codex: found where npm puts it when it is not on PATH', server.find_codex() == exe)
        server.CODEX_PATHS = []
    finally:
        subprocess.Popen, shutil.which, server.TIMEOUT, server.CODEX_PATHS = real_popen, no_cli, 180, []
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'SLOP_CODEX_PIDS'):
            os.environ.pop(key, None)
        server._codex_status['until'] = 0
        server.log.removeHandler(kept)
        server.log.setLevel(logging.NOTSET)
        model_says()


# ── which engine a run goes to, and the values a run may ask for ────────────
@section
def engine_choice():
    asked = []

    def codex(system_prompt, user_message, model, effort, cancel, seen=None):
        asked.append(('chatgpt', model, effort))
        seen['model'] = 'gpt-seen'
        return CLEAN + NOTES

    real_codex = server.run_codex
    server.run_codex = codex
    try:
        model_says(CLEAN + NOTES)
        done = run(SLOPPY, model='opus')[-1]
        check('dispatch: no engine is Claude', len(calls) == 1 and calls[0]['model'] == 'opus' and not asked
              and (done.get('engine'), done.get('model')) == ('claude', 'opus'), (calls, asked, done))
        model_says()
        done = run(SLOPPY, engine='chatgpt', model='gpt-6.1-sol', effort='xhigh')[-1]
        check('dispatch: chatgpt goes to Codex, with the model and effort asked for',
              asked == [('chatgpt', 'gpt-6.1-sol', 'xhigh')] and not calls, (asked, calls))
        check('dispatch: the done event names the model that answered',
              (done.get('engine'), done.get('model')) == ('chatgpt', 'gpt-seen'), done)
        model_says(CLEAN + NOTES)
        done = run(SLOPPY, engine='claude', model='claude-opus-5-5')[-1]
        check('a full Claude model name is accepted', done.get('event') == 'done'
              and calls[0]['model'] == 'claude-opus-5-5', done)
        model_says()
        for name, more, want in (
                ('an unknown engine', {'engine': 'gemini'}, 'bad_engine'),
                ('an engine that is not a string', {'engine': ['chatgpt']}, 'bad_engine'),
                ('a ChatGPT model with a space', {'engine': 'chatgpt', 'model': 'gpt 5'}, 'bad_model'),
                ('a ChatGPT model shaped like a flag', {'engine': 'chatgpt', 'model': '--help'}, 'bad_model'),
                ('a ChatGPT model that is too long', {'engine': 'chatgpt', 'model': 'g' * 65}, 'bad_model'),
                ('a model that is not a string', {'engine': 'chatgpt', 'model': 5}, 'bad_model'),
                ('a Claude engine with a ChatGPT model', {'engine': 'claude', 'model': 'gpt-6.1-sol'}, 'bad_model'),
                ('a Claude model with a quote', {'model': 'claude-x"y'}, 'bad_model'),
                ('an unknown effort', {'engine': 'chatgpt', 'effort': 'max'}, 'bad_effort'),
                ('an effort that is not a string', {'engine': 'chatgpt', 'effort': 3}, 'bad_effort'),
                ('an unknown strength', {'strength': 'heavy'}, 'bad_strength'),
                ('a strength that is not a string', {'strength': ['light']}, 'bad_strength')):
            more.setdefault('model', 'sonnet' if more.get('engine', 'claude') == 'claude' else '')
            got = run(SLOPPY, **more)
            check(f'refused: {name}', got == (400, want), got)
        check('nothing reached a model', not calls and len(asked) == 1, (calls, asked))
        for strength in ('light', 'thorough'):
            model_says(CLEAN + NOTES)
            done = run(SLOPPY, strength=strength)[-1]
            check(f'strength {strength} is accepted', done.get('event') == 'done' and len(calls) == 1, done)
    finally:
        server.run_codex = real_codex
        model_says()


# ── the rewrite instructions: the user's copy, the default, and the reply contract ──
@section
def rewrite_instructions():
    repo_prompt = os.path.join(server.REPO, 'prompts', 'cleanse.txt')
    with open(repo_prompt, 'rb') as fh:
        untouched = fh.read()
    copy = os.path.join(server.DATA_DIR, 'cleanse.txt')
    base = os.path.join(server.DATA_DIR, 'cleanse.base.txt')
    default = server.default_instructions()

    def get():
        status, _, data = request('GET', '/api/prompt')
        return status, json.loads(data)

    def save(body):
        status, _, data = request('POST', '/api/prompt', body)
        return status, json.loads(data)

    def system_of_a_run():
        model_says(CLEAN + NOTES)
        run(SLOPPY)
        return calls[0]['system'] if calls else ''

    try:
        status, got = get()
        check('prompt: the default to start with', status == 200
              and got == {'text': default, 'custom': False, 'stale': False} and not os.path.exists(copy), got)
        check('prompt: the default holds the marker line and not the cleanse.sh ending',
              MARKER in default.split('\n') and not default.endswith('TEXT TO EDIT:'), default[-80:])
        check('prompt: a run uses the default as it is', system_of_a_run() == default)

        mine = 'Edit the text for a busy reader.\r\nKeep it short.'
        status, got = save({'text': '  ' + mine + '\n\n'})
        check('prompt: a copy is saved', status == 200
              and got == {'text': mine.replace('\r\n', '\n'), 'custom': True, 'stale': False}
              and os.path.isfile(copy) and os.path.isfile(base), got)
        status, got = get()
        check('prompt: and read back', got == {'text': 'Edit the text for a busy reader.\nKeep it short.',
                                               'custom': True, 'stale': False}, got)
        system = system_of_a_run()
        check('prompt: a run uses the copy, with the reply contract after it',
              system.startswith('Edit the text for a busy reader.\nKeep it short.\n\n' + server.CONTRACT[:6])
              and system.endswith(server.CONTRACT) and MARKER in system.split('\n'), system)
        check('prompt: and the reply is still split at the marker',
              server.split_reply('Copy.\n' + MARKER + '\n- a note') == ('Copy.', ['a note'], True))

        marked = f'Edit the text.\n\nReturn the text, then\n{MARKER}\nthen your notes.\n\nTEXT TO EDIT:'
        save({'text': marked})
        check('prompt: a copy with the marker line gets no second contract',
              system_of_a_run() == marked.removesuffix('\n\nTEXT TO EDIT:'))
        save({'text': marked.replace(f'\n{MARKER}\n', f'\n    {MARKER}\n')})
        check('prompt: nor is an indented one, which split_reply would not find',
              system_of_a_run().endswith(server.CONTRACT))
        save({'text': f'Edit the text, then write {MARKER} and your notes.'})
        check('prompt: a marker inside a sentence is not the line split_reply needs',
              system_of_a_run().endswith(server.CONTRACT))

        for name, body, want in (
                ('empty instructions', {'text': ''}, (400, 'empty_prompt')),
                ('white space only', {'text': ' \n\t '}, (400, 'empty_prompt')),
                ('instructions over 60,000 characters', {'text': 'x' * 60_001}, (413, 'too_long')),
                ('text that is not a string', {'text': ['x']}, (400, 'bad_request')),
                ('reset that is not true', {'reset': 'yes'}, (400, 'bad_request')),
                ('a body that is not an object', ['x'], (400, 'bad_request'))):
            check(f'prompt: refused: {name}', refused('POST', '/api/prompt', body) == want,
                  refused('POST', '/api/prompt', body))
        check('prompt: a refusal leaves the copy as it was', get()[1]['text'].endswith('and your notes.'))
        status, got = save({'text': 'y' * 60_000})
        check('prompt: 60,000 characters is allowed', status == 200 and got['custom'] is True, status)
        for path, method in (('/api/prompt', 'GET'), ('/api/prompt', 'POST')):
            check(f'prompt: {method} needs the token',
                  # A GET with a body is refused before the body is read, and Windows may
                  # then reset the connection under the reply.
                  refused(method, path, {'reset': True} if method == 'POST' else None,
                          token=None) == (403, 'forbidden'))

        status, got = save({'reset': True})
        check('prompt: reset deletes the copy', status == 200
              and got == {'text': default, 'custom': False, 'stale': False}
              and not os.path.exists(copy) and not os.path.exists(base), got)
        check('prompt: and runs use the default again', system_of_a_run() == default)
        check('prompt: reset with no copy is fine',
              save({'reset': True}) == (200, {'text': default, 'custom': False, 'stale': False}))
        save({'text': 'Edit it.'})
        status, got = save({'text': default + '\n'})
        check('prompt: saving the default word for word is a reset',
              got == {'text': default, 'custom': False, 'stale': False} and not os.path.exists(copy)
              and not os.path.exists(base), got)

        os.makedirs(server.DATA_DIR, exist_ok=True)
        with open(copy, 'w', encoding='utf-8') as fh:
            fh.write('\n \n')
        check('prompt: an empty copy on disk counts as none',
              get()[1] == {'text': default, 'custom': False, 'stale': False})

        # An update changes prompts/cleanse.txt after the user saved a copy of it (DRIFT-1).
        status, got = save({'text': default + '\n- One rule of mine.'})
        check('prompt: an edit of the default is not stale', status == 200 and got['stale'] is False
              and os.path.isfile(base), got.get('stale'))
        real_default = server.default_instructions
        server.default_instructions = lambda: real_default() + '\n- A rule the update added.'
        try:
            check('prompt: after the default changes, the copy is stale', get()[1]['stale'] is True)
            status, got = save({'text': got['text'] + '\n- And one more of mine.'})
            check('prompt: and stays stale when the user edits it again', got['stale'] is True, got.get('stale'))
            status, got = save({'reset': True})
            check('prompt: reset clears it, and the record of the old default', got['stale'] is False
                  and not os.path.exists(base) and 'A rule the update added.' in got['text'], got.get('stale'))
        finally:
            server.default_instructions = real_default
        # A copy saved before the base was kept was made from the prompts/cleanse.txt of that
        # time, whose hash is UNTRACKED_BASE. The default has changed since (DAILY-5).
        with open(copy, 'w', encoding='utf-8') as fh:
            fh.write('Edit it.')
        check("prompt: a copy with no base is stale under today's default",
              get()[1] == {'text': 'Edit it.', 'custom': True, 'stale': True}, get()[1])
        real_base = server.UNTRACKED_BASE
        server.UNTRACKED_BASE = hashlib.sha256(real_default().encode('utf-8')).hexdigest()[:16]
        try:
            check('prompt: a copy with no base, made from this default, is not stale',
                  get()[1] == {'text': 'Edit it.', 'custom': True, 'stale': False}, get()[1])
            server.default_instructions = lambda: real_default() + '\n- A rule the update added.'
            check('prompt: and is stale once the default changes', get()[1]['stale'] is True)
        finally:
            server.default_instructions = real_default
            server.UNTRACKED_BASE = real_base
        with open(repo_prompt, 'rb') as fh:
            check('prompt: prompts/cleanse.txt is never written', fh.read() == untouched)
    finally:
        for path in (copy, base):
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
        model_says()


# ── Thorough: THOROUGH and the guides between the instructions and the contract ──
@section
def thorough_runs():
    copy = os.path.join(server.DATA_DIR, 'cleanse.txt')
    default = server.default_instructions()
    lines = []

    class Kept(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    kept = Kept()
    server.log.addHandler(kept)
    try:
        model_says(CLEAN + NOTES)
        run(SLOPPY)
        check('thorough: a Light run is the instructions alone', calls[0]['system'] == default, calls[0]['system'][-80:])
        model_says(CLEAN + NOTES)
        run(SLOPPY, strength='thorough')
        system = calls[0]['system']
        at = system.find(server.THOROUGH)
        check('thorough: the instructions in use come first, THOROUGH right after them',
              system.startswith(default + '\n\n' + server.THOROUGH), system[len(default) - 40:len(default) + 60])
        check('thorough: then the two guides, and the reply contract last',
              at < system.find('# Editing principles') < system.find('# Signs of AI writing')
              < system.find('## 5 · Invented proof') < system.rfind(server.CONTRACT)
              and system.endswith('\n\n' + server.CONTRACT), system[-120:])
        guides = system[at:]
        check('thorough: the guides leave out links, tools and the rules between sections',
              '](' not in guides and '`tools/' not in guides and '\n---\n' not in guides
              and 'Steve Krug' not in guides, guides)
        check('thorough: and a heading left with nothing under it', 'Review the complete draft' not in guides, guides[-300:])

        os.makedirs(server.DATA_DIR, exist_ok=True)
        with open(copy, 'w', encoding='utf-8') as fh:
            fh.write('Edit it for a busy reader.')
        model_says(CLEAN + NOTES)
        run(SLOPPY, strength='thorough')
        system = calls[0]['system']
        check('thorough: an edited copy gets THOROUGH too, and one contract after it',
              system.startswith('Edit it for a busy reader.\n\n' + server.THOROUGH)
              and system.endswith(server.CONTRACT) and system.count(MARKER) == 1, system[:80])
        os.remove(copy)

        real_guides = server.GUIDES
        server.GUIDES = ('missing.md',)
        try:
            check('thorough: a guide that cannot be read is left out',
                  server.system_prompt('thorough')[0] == default + '\n\n' + server.THOROUGH + '\n\n' + server.CONTRACT)
        finally:
            server.GUIDES = real_guides
        check('thorough: and the log says so', any('references/missing.md' in line for line in lines), lines)

        model_says(CLEAN + NOTES)
        ev = run(CLEAN, auto=True, strength='thorough')
        check('thorough: text that passes is rewritten, even on a paste',
              ev[-1].get('rounds') == 1 and len(calls) == 1 and 'THOROUGH EDIT' in calls[0]['system'], kinds(ev))
        check('thorough: and the model is told the check found nothing',
              'found nothing to flag' in calls[0]['message'], calls[0]['message'][:120])

        para = ('The valley had rain on Tuesday and sun on Wednesday, and the river stayed '
                'below its banks the whole week. ') * 6
        long = '\n\n'.join([para.strip()] * 20)
        parts = server.split_parts(long)
        model_says()
        done = run(long, strength='light')[-1]
        check('thorough: (a long text with nothing flagged makes no call when Light)',
              len(parts) > 1 and done.get('rounds') == 0 and not calls, (len(parts), done.get('parts')))
        model_says(*[p + NOTES for p in parts])
        done = run(long, strength='thorough')[-1]
        check('thorough: every part of a long text is rewritten, flagged or not',
              len(calls) == len(parts) and done.get('parts', {}).get('rewritten') == len(parts), done.get('parts'))

        draft = '# Plan\n\nIt is a tool that checks copy.\n\n- First item\n\n- Second item'
        merged = '# Plan\n\nIt is a tool that checks copy.\n\n- First item\n- Second item'
        model_says(merged + NOTES, draft + NOTES)
        done = run(draft, structure=True, strength='thorough')[-1]
        check('thorough: a Word file still gets the fixed-shape rule, and THOROUGH defers to it',
              server.KEEP in calls[0]['message'] and 'that rule comes first' in calls[0]['system']
              and 'THOROUGH EDIT' in calls[0]['system'], calls[0]['message'][-300:])
        check('thorough: and a reply with merged blocks is still turned down',
              done.get('text') == draft and done.get('rounds') == 2 and 'had 3 blocks' in calls[1]['message'], done)
    finally:
        server.log.removeHandler(kept)
        with contextlib.suppress(FileNotFoundError):
            os.remove(copy)
        model_says()


# ── the ChatGPT sign-in window ──────────────────────────────────────────────
@section
def chatgpt_sign_in():
    opened = []

    class Window:
        def poll(self):
            return None                   # still open

    def popen(argv, **kw):
        opened.append((argv, kw))
        return Window()

    exe = os.path.join(tmp, 'bin', 'codex.exe')
    shutil.which = lambda name, *a, **k: exe if name == 'codex.exe' else 'claude'
    subprocess.Popen = popen
    os.environ['OPENAI_API_KEY'] = 'sk-test'
    try:
        status, _, data = request('POST', '/api/login', {'engine': 'chatgpt'})
        check('chatgpt sign-in: answers ok', status == 200 and json.loads(data) == {'ok': True, 'opened': True}, data)
        argv, kw = opened[0] if opened else ([], {})
        python = os.path.join(os.path.dirname(sys.executable), 'python.exe')
        check('chatgpt sign-in: runs `codex login` and nothing else',
              argv == [python, '-I', '-c', server.CODEX_SIGN_IN, exe, 'login'], argv)
        check('chatgpt sign-in: the window says ChatGPT', 'Sign in to ChatGPT' in server.CODEX_SIGN_IN
              and 'Claude' not in server.CODEX_SIGN_IN, server.CODEX_SIGN_IN)
        check('chatgpt sign-in: in a console of its own, with no API key',
              kw.get('creationflags') == subprocess.CREATE_NEW_CONSOLE and 'OPENAI_API_KEY' not in kw.get('env', {}),
              sorted(kw))
        status, _, data = request('POST', '/api/login', {})
        check('chatgpt sign-in: Claude\'s has a window of its own',
              json.loads(data) == {'ok': True, 'opened': True} and opened[-1][0][-2:] == ['auth', 'login'], data)
        again = json.loads(request('POST', '/api/login', {'engine': 'chatgpt'})[2])
        check('chatgpt sign-in: one window at a time', len(opened) == 2 and again['opened'] is False, again)
        check('sign-in: an unknown engine is refused',
              refused('POST', '/api/login', {'engine': 'gemini'}) == (400, 'bad_request'))
        server._sign_in['chatgpt'] = None
        shutil.which = no_cli
        check('chatgpt sign-in: with no codex it says so',
              refused('POST', '/api/login', {'engine': 'chatgpt'}) == (409, 'missing'))
    finally:
        subprocess.Popen, shutil.which = real_popen, no_cli
        server._sign_in.update(claude=None, chatgpt=None)
        os.environ.pop('OPENAI_API_KEY', None)


# ── files in, and a Word file out ───────────────────────────────────────────
@section
def document_routes():
    import documents
    text = '# Title\n\nIt is a tool.\n\n- one\n\n- two'
    docx = documents.write_docx(text)
    status, _, data = request('POST', '/api/extract', docx, headers={'X-Slop-Name': 'My%20report.docx'})
    got = json.loads(data)
    check('extract: a Word file comes back as its text',
          status == 200 and (got['kind'], got['text']) == ('docx', text), got)
    check('extract: and says what it read', bool(got['summary']) and isinstance(got['notes'], list), got)
    check('extract: needs the token',
          refused('POST', '/api/extract', docx, token='') == (403, 'forbidden'))
    got = refused('POST', '/api/extract', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 old word', headers={'X-Slop-Name': 'old.doc'})
    check('extract: a file it cannot read is refused with the reason', got == (422, 'unsupported'), got)
    got = refused('POST', '/api/extract', b'PK\x03\x04 not a zip at all', headers={'X-Slop-Name': 'x.docx'})
    check('extract: a broken file is refused, and the server carries on',
          got == (422, 'unreadable') and server.already_running(PORT), got)
    # The name only picks a parser: a path in it, or something shaped like an option, changes nothing.
    status, _, data = request('POST', '/api/extract', 'Plain words here.'.encode(),
                              headers={'X-Slop-Name': '..%5C..%5C--help%20notes.txt'})
    check('extract: a text file, whatever its name claims to be',
          status == 200 and json.loads(data)['text'] == 'Plain words here.', data[:200])
    keep, server.MAX_CHARS = server.MAX_CHARS, 50
    try:
        got = refused('POST', '/api/extract', ('word ' * 40).encode(), headers={'X-Slop-Name': 'big.txt'})
        check('extract: a file with too much text is refused', got == (413, 'too_long'), got)
    finally:
        server.MAX_CHARS = keep

    status, head, data = request('POST', '/api/docx', {'text': text},
                                 headers={'Content-Type': 'application/json'})
    check('docx: text comes back as a Word file',
          status == 200 and head.get('content-type') == server.DOCX and data[:4] == b'PK\x03\x04',
          (status, head.get('content-type')))
    check('docx: which reads back as the same text', documents.extract('x.docx', data)['text'] == text)
    # "cell | cell" paragraphs are a table only in text the page read from a Word file.
    rows = 'Region | Income\n\nNorth | 1,240\n\nSouth | 980'
    for said, want in (({}, False), ({'rows': True}, True), ({'rows': 'yes'}, False)):
        data = request('POST', '/api/docx', dict(said, text=rows), headers={'Content-Type': 'application/json'})[2]
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            table = '<w:tbl>' in z.read('word/document.xml').decode('utf-8')
        check(f'docx: rows {said} make a table: {want}', table == want, said)
    check('docx: needs a text', refused('POST', '/api/docx', {'nope': 1},
                                        headers={'Content-Type': 'application/json'}) == (400, 'bad_request'))
    check('docx: needs the token', refused('POST', '/api/docx', {'text': text}, token='',
                                           headers={'Content-Type': 'application/json'}) == (403, 'forbidden'))

    # Saving in place: the Word file the text came from goes back with the edited text, and
    # what returns is that file with only the words changed.
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'testdata', 'a_prose.docx'), 'rb') as fh:
        original = fh.read()
    shown = documents.extract('a_prose.docx', original)['text']
    edited = shown.replace('budget', 'spending plan', 1)

    def in_place(new_text, file=original):
        meta = json.dumps({'text': new_text}).encode()
        return request('POST', '/api/docx', struct.pack('>I', len(meta)) + meta + file,
                       headers={'Content-Type': 'application/octet-stream'})

    check('in place: the fixture has the word this test edits', 'budget' in shown and edited != shown)
    status, head, data = in_place(edited)
    check('in place: a Word file comes back', status == 200 and head.get('content-type') == server.DOCX
          and data[:4] == b'PK\x03\x04', (status, head.get('content-type')))
    check('in place: it reads as the edited text', documents.extract('x.docx', data)['text'] == edited)
    check('in place: notes come with it', json.loads(urllib.parse.unquote(head.get('x-slop-notes', ''))) == [],
          head.get('x-slop-notes'))
    before, after = zipfile.ZipFile(io.BytesIO(original)), zipfile.ZipFile(io.BytesIO(data))
    check('in place: every part but the body is the file\'s own, byte for byte',
          before.namelist() == after.namelist() and all(
              before.read(n) == after.read(n) for n in before.namelist() if n != 'word/document.xml'))
    check('in place: text that was not edited gives back the very same file', in_place(shown)[2] == original)
    # One Enter too many in the text box must not get a "# " typed into a heading.
    check('in place: nor does a stray blank line change it',
          in_place('\n' + shown.replace('\n\n', '\n\n\n', 3) + '\n\n')[2] == original)
    got = refused('POST', '/api/docx', struct.pack('>I', 13) + b'{"text": "x"}' + b'not a Word file',
                  headers={'Content-Type': 'application/octet-stream'})
    check('in place: a file that is not a Word file is refused, and the server carries on',
          got[0] == 422 and server.already_running(PORT), got)
    got = refused('POST', '/api/docx', b'\x00\x00', headers={'Content-Type': 'application/octet-stream'})
    check('in place: a body cut short is refused', got[0] == 422, got)


# ── the sign-in window ──────────────────────────────────────────────────────
@section
def sign_in_window():
    opened = []

    class Window:
        def poll(self):
            return None                   # still open

    def popen(argv, **kw):
        opened.append((argv, kw))
        return Window()

    shutil.which, subprocess.Popen = (lambda *a, **k: 'claude'), popen
    try:
        status, _, data = request('POST', '/api/login', {})
        check('sign-in: answers ok and says a window opened',
              status == 200 and json.loads(data) == {'ok': True, 'opened': True}, data)
        argv, kw = opened[0] if opened else ([], {})
        python = os.path.join(os.path.dirname(sys.executable), 'python.exe')
        check('sign-in: runs the CLI\'s own sign-in and nothing else',
              argv == [python, '-I', '-c', server.SIGN_IN, 'claude', 'auth', 'login'], argv)
        check('sign-in: in a console window of its own',
              kw.get('creationflags') == subprocess.CREATE_NEW_CONSOLE and 'shell' not in kw, kw)
        again = json.loads(request('POST', '/api/login', {})[2])
        check('sign-in: one window at a time, and the page is told',
              len(opened) == 1 and again == {'ok': True, 'opened': False}, (len(opened), again))
        check('sign-in: needs the token',
              refused('POST', '/api/login', {}, token='') == (403, 'forbidden'))
        check('sign-in: a foreign site cannot start it', refused(
            'POST', '/api/login', {}, headers={'Origin': 'https://evil.example'}) == (403, 'forbidden'))
        server._sign_in['claude'] = None
        shutil.which = no_cli
        check('sign-in: with no CLI it says so',
              refused('POST', '/api/login', {}) == (409, 'missing'))
        check('sign-in: and opens nothing', len(opened) == 1, len(opened))
    finally:
        subprocess.Popen, shutil.which = real_popen, no_cli
        server._sign_in.update(claude=None, chatgpt=None)

    # The window's own script, run hidden against a stand-in that fails once, then succeeds.
    standin = os.path.join(tmp, 'standin_cli.py')
    with open(standin, 'w', encoding='utf-8') as fh:
        fh.write("import os, sys\nprint('args', sys.argv[2:], flush=True)\n"
                 "if os.path.exists(sys.argv[1]): sys.exit(0)\n"
                 "open(sys.argv[1], 'w').close()\nsys.exit(1)\n")
    window = real_popen([sys.executable, '-I', '-c', server.SIGN_IN, sys.executable, standin,
                         os.path.join(tmp, 'tried-once'), 'auth', 'login'],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    out, _ = window.communicate('\n', timeout=60)
    check('sign-in window: hands the command to the CLI as given',
          out.count("args ['auth', 'login']") == 2, out)
    check('sign-in window: a sign-in that did not finish is retried in the window',
          'did not finish' in out and out.rstrip().endswith('closes by itself.'), out)
    check('sign-in window: closes by itself once signed in', window.returncode == 0,
          window.returncode)


# ── a CLI installed while the app is open ──────────────────────────────────
@section
def installed_while_open():
    # The installer's folder is on the PATH Windows now hands out, not on this process's.
    folder = os.path.join(tmp, 'installed-later')
    os.makedirs(folder)
    real_saved = server.saved_path
    shutil.which = lambda name, mode=os.F_OK | os.X_OK, path=None: (
        None if path is None else real_which(name, mode, path))
    server.saved_path = lambda: folder
    try:
        for name, find in (('claude.exe', server.find_cli), ('codex.exe', server.find_codex)):
            try:
                find()
                before = 'found'
            except server.EngineError as e:
                before = e.code
            open(os.path.join(folder, name), 'wb').close()
            try:
                after = find()
            except server.EngineError as e:
                after = e.code
            check(f'{name} installed while the app is open: Check again finds it',
                  before == 'missing' and os.path.normcase(str(after)) == os.path.normcase(os.path.join(folder, name)),
                  (before, after))
    finally:
        shutil.which, server.saved_path = no_cli, real_saved
    saved = server.saved_path()
    check('saved_path reads the PATH Windows keeps', isinstance(saved, str), saved)


# ── claude.exe where its installers put it, off every PATH ─────────────────
@section
def claude_install_folders():
    # Claude Code's Windows installer puts claude.exe in %USERPROFILE%\.local\bin and leaves
    # PATH alone; WinGet links it from its Links folder. Stand-ins here, never the real ones.
    folder = os.path.join(tmp, 'home', '.local', 'bin')
    os.makedirs(folder)
    exe = os.path.join(folder, 'claude.exe')
    server.CLAUDE_PATHS = [os.path.join(tmp, 'winget-links', 'claude.exe'), exe]
    try:
        try:
            server.find_cli()
            before = 'found'
        except server.EngineError as e:
            before = e.code
        open(exe, 'wb').close()
        try:
            after = server.find_cli()
        except server.EngineError as e:
            after = e.code
        check("claude.exe in the installer's folder is found with nothing on PATH",
              before == 'missing' and os.path.normcase(str(after)) == os.path.normcase(exe), (before, after))
        # An npm claude.cmd on PATH must not hide a native copy that is off PATH.
        shutil.which = lambda name, *a, **k: 'C:/npm/claude.cmd' if name == 'claude' else None
        try:
            got = server.find_cli()
        except server.EngineError as e:
            got = e.code
        check('the native claude.exe wins over an npm claude.cmd on PATH',
              os.path.normcase(str(got)) == os.path.normcase(exe), got)
    finally:
        shutil.which, server.CLAUDE_PATHS = no_cli, []
    check('CLAUDE_PATHS holds the two folders Claude Code installs to',
          [p.lower().split(os.sep)[-3:] for p in real_claude_paths]
          == [['.local', 'bin', 'claude.exe'], ['winget', 'links', 'claude.exe']], real_claude_paths)
    # The page's ChatGPT card says CODEX_MISSING itself: /api/status says only "not installed".
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'), encoding='utf-8') as fh:
        page = fh.read()
    check("the page's ChatGPT card has the server's words for a missing Codex",
          "const CODEX_MISSING = '%s';" % server.CODEX_MISSING.replace("'", "\\'") in page)


# ── what a one-file exe ended from Task Manager leaves in TEMP ─────────────
@section
def unpacked_leftovers():
    base = os.path.join(tmp, 'temp')
    old = time.time() - 3600

    def unpacked(name, ours=True, age=old):
        folder = os.path.join(base, name)
        os.makedirs(os.path.join(folder, 'app'))
        os.makedirs(os.path.join(folder, 'prompts'))
        for part in (('python313.dll',), ('base_library.zip',), ('app', 'index.html')) + (
                (('prompts', 'cleanse.txt'),) if ours else ()):
            open(os.path.join(folder, *part), 'wb').close()
        os.utime(folder, (age, age))
        return folder

    own = unpacked('_MEI100')
    stale = unpacked('_MEI200')
    young = unpacked('_MEI300', age=time.time())
    running = unpacked('_MEI400')
    other_app = unpacked('_MEI500', ours=False)
    not_mei = unpacked('keepme')
    with open(os.path.join(running, 'python313.dll'), 'rb'):   # Windows will not delete it now
        os.utime(running, (old, old))
        swept = server.sweep_unpacked(own)
    check('a left-behind unpack folder is removed', not os.path.exists(stale) and swept == [stale], swept)
    check('its own, a running copy\'s, a young one and other apps\' are kept',
          all(os.path.isfile(os.path.join(f, 'app', 'index.html'))
              for f in (own, young, running, other_app, not_mei))
          and os.path.isfile(os.path.join(running, 'python313.dll')),
          sorted(os.listdir(base)))
    check('a missing TEMP is no error', server.sweep_unpacked(os.path.join(tmp, 'nowhere', '_MEI1')) == [])


@section
def run_leftovers():
    # A copy ended mid-run (Task Manager, a forced restart) never reaches the finally blocks
    # that remove a run's Codex work folder and prompt file. The next start does.
    old = time.time() - server.TIMEOUT - 120
    os.makedirs(server.DATA_DIR, exist_ok=True)

    def made(name, folder=False, age=old):
        path = os.path.join(server.DATA_DIR, name)
        if folder:
            os.makedirs(path)
            with open(os.path.join(path, 'instructions.txt'), 'w') as fh:
                fh.write('system')
        else:
            with open(path, 'w') as fh:
                fh.write('system')
        os.utime(path, (age, age))
        return name

    stale = [made('codex-ab12cd34', folder=True), made('prompt-ef56gh78.txt')]
    kept = [made('codex-young001', folder=True, age=time.time()), made('prompt-young001.txt', age=time.time()),
            made('prompt-notes.txt'), made('codex-notes', folder=True)]
    noted = os.path.join(server.DATA_DIR, 'instance.json')
    with open(noted, 'rb') as fh:             # the long-lived server's, which this copy replaces
        held = fh.read()
    port, thread = start()
    present = os.listdir(server.DATA_DIR)
    request('POST', '/api/shutdown', port=port)
    thread.join(10)
    with open(noted, 'wb') as fh:
        fh.write(held)
    check('a killed run\'s work folder and prompt file are removed at the next start',
          not set(stale) & set(present), present)
    check('a run that may still be going, and names the app did not make, are kept',
          set(kept) <= set(present), present)
    for name in kept + stale:
        path = os.path.join(server.DATA_DIR, name)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else (
            os.path.exists(path) and os.remove(path))


@section
def unwritable_data_folder():
    # instance.json is optional: a copy that cannot write it still starts.
    gone = os.path.join(tmp, 'gone', 'instance.json')
    try:
        server.write_instance(gone, 47999, b'k' * 32)
        raised = None
    except Exception as e:                    # noqa: BLE001
        raised = e
    check('instance.json that cannot be written is no reason to stop', raised is None, repr(raised))
    check('... and leaves nothing behind', not os.path.exists(os.path.dirname(gone)))


# ── SlopMonster.exe: its own child processes, and the options for testing ──
@section
def the_exe():
    opened = []

    class Window:
        def poll(self):
            return None

    def popen(argv, **kw):
        opened.append((argv, kw))
        return Window()

    shutil.which, subprocess.Popen, server.FROZEN = (lambda *a, **k: 'claude'), popen, True
    os.environ['OPENAI_API_KEY'] = 'sk-test'
    try:
        request('POST', '/api/login', {})
        request('POST', '/api/login', {'engine': 'chatgpt'})
        (claude, kw), (codex, codex_kw) = opened if len(opened) == 2 else [([], {})] * 2
        check('exe sign-in: the exe runs the wrapper itself, with no python.exe',
              claude == [sys.executable, '--sign-in', 'claude', 'claude', 'auth', 'login']
              and codex == [sys.executable, '--sign-in', 'chatgpt', 'claude', 'login'], (claude, codex))
        check('exe sign-in: unpacked afresh, so the window outlives Turn off',
              kw.get('env', {}).get('PYINSTALLER_RESET_ENVIRONMENT') == '1'
              and codex_kw.get('env', {}).get('PYINSTALLER_RESET_ENVIRONMENT') == '1', sorted(kw))
        check('exe sign-in: ChatGPT\'s still has no API key', 'OPENAI_API_KEY' not in codex_kw.get('env', {}))
    finally:
        subprocess.Popen, shutil.which, server.FROZEN = real_popen, no_cli, False
        server._sign_in.update(claude=None, chatgpt=None)
        del os.environ['OPENAI_API_KEY']

    # What DOCUMENTS_CMD runs in the exe, run here from source: server.py --documents.
    source = os.path.dirname(os.path.abspath(server.__file__))      # server.APP_DIR is a stand-in here
    script = os.path.join(source, 'server.py')
    done = subprocess.run([sys.executable, script, '--documents', 'extract', 'note.txt'], input=b'Hello there.',
                          capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    check('--documents: does what documents.py does', json.loads(done.stdout or b'{}').get('text') == 'Hello there.',
          done.stdout[:200])
    check('source runs still use documents.py', server.DOCUMENTS_CMD == [sys.executable, server.DOCUMENTS])

    where = os.path.join(tmp, 'by-variable')
    done = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import server; '
                           'print(server.DATA_DIR)', source], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, SLOPMONSTER_DATA_DIR=where), creationflags=subprocess.CREATE_NO_WINDOW)
    check('SLOPMONSTER_DATA_DIR moves the data folder', done.stdout.strip() == where, done.stdout)

    served, mine = [], server.DATA_DIR
    real_serve, server.serve = server.serve, lambda **kw: served.append(dict(kw, data=server.DATA_DIR))
    try:
        server.main(['server.py', '--no-window', '--port', '50123', '--data-dir', os.path.join(tmp, 'given')])
        server.main(['server.py'])
        check('--port and --data-dir reach serve, and --no-window takes no hotkey', served == [
            {'port': 50123, 'open_window': False, 'hotkey': None, 'data': os.path.join(tmp, 'given')},
            {'port': server.PORT, 'open_window': True, 'hotkey': server.HOTKEY, 'data': os.path.join(tmp, 'given')}],
            served)
        codes = []
        for bad in (['--port', '70000'], ['--port', 'x'], ['--window']):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    server.main(['server.py'] + bad)
            except SystemExit as e:
                codes.append(e.code)
        check('a bad option is refused before anything starts', codes == [2, 2, 2] and len(served) == 2, codes)
    finally:
        server.serve, server.DATA_DIR = real_serve, mine


# ── events for the page: what Ctrl+Alt+U hands it ──────────────────────────
def events(port=None):
    status, _, data = request('GET', '/api/events', port=port)
    return status, json.loads(data).get('events')


@section
def events_for_the_page():
    status, _, data = request('GET', '/api/status')
    check('status: a launch without the hotkey says so', json.loads(data).get('hotkey') == 'none',
          data)
    status, _, data = request('POST', '/api/hotkey', {'on': True})
    check('hotkey: a launch without one registers nothing',
          (status, json.loads(data)) == (200, {'hotkey': 'none'}) and not server._hotkey['thread'],
          f'{status} {data!r}')
    for body in ({'on': 'yes'}, {'on': 1}, {}, b'not json'):
        got = refused('POST', '/api/hotkey', body)
        check(f'hotkey: {body!r} is refused', got == (400, 'bad_request'), got)

    wait = server.EVENT_WAIT
    server.EVENT_WAIT = 0.5
    try:
        began = time.time()
        check('events: nothing happened is an empty list, after the wait',
              events() == (200, []) and time.time() - began >= 0.4, time.time() - began)
        server.queue_event({'event': 'hotkey', 'text': 'one'})
        server.queue_event({'event': 'hotkey', 'text': 'two'})
        check('events: a newer press replaces one not picked up',
              events() == (200, [{'event': 'hotkey', 'text': 'two'}]))
        check('events: each is handed out once', events() == (200, []))
        with server._events_cond:
            server._events[:] = [(time.monotonic() - server.EVENT_LIFE - 1, {'event': 'hotkey'})]
        check('events: one the page never picked up is dropped', events() == (200, []))

        server.EVENT_WAIT = 10
        got = {}

        def wait_for_one():
            got['events'] = events()

        waiter = threading.Thread(target=wait_for_one, daemon=True)
        began = time.time()
        waiter.start()
        time.sleep(0.3)
        server.queue_event({'event': 'hotkey', 'empty': True})
        waiter.join(5)
        check('events: a waiting request answers as soon as one arrives',
              got.get('events') == (200, [{'event': 'hotkey', 'empty': True}])
              and time.time() - began < 3, (got, time.time() - began))

        # A reload leaves the old page's request waiting. The event goes to the new page.
        for attempt in range(3):
            with socket.create_connection(('127.0.0.1', PORT), timeout=10) as left:
                left.sendall(f'GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n'
                             f'X-Slop-Token: {server.TOKEN}\r\n\r\n'.encode())
                time.sleep(0.2)
            got.clear()
            waiter = threading.Thread(target=wait_for_one, daemon=True)
            waiter.start()
            time.sleep(0.2)
            server.queue_event({'event': 'hotkey', 'text': f'try {attempt}'})
            waiter.join(5)
            check('events: a page that left does not take what the page there now is waiting for',
                  got.get('events') == (200, [{'event': 'hotkey', 'text': f'try {attempt}'}]), got)
    finally:
        server.EVENT_WAIT = wait

    lines = []

    class Kept(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    kept = Kept()
    server.log.setLevel(logging.INFO)
    server.log.addHandler(kept)
    real = server.clipboard_text
    server.EVENT_WAIT = 0.5
    try:
        for held, want in (('Copied words from a chat', {'event': 'hotkey', 'text': 'Copied words from a chat'}),
                           (None, {'event': 'hotkey', 'empty': True}),
                           (' \r\n ', {'event': 'hotkey', 'empty': True})):
            server.clipboard_text = lambda: held
            server.hotkey_pressed()
            check(f'hotkey: the clipboard holding {held!r} is handed over', events() == (200, [want]))

        def broken():
            raise OSError('clipboard is busy')

        server.clipboard_text = broken
        server.hotkey_pressed()
        check('hotkey: a clipboard that cannot be read is an empty one',
              events() == (200, [{'event': 'hotkey', 'empty': True}]))

        def busy():
            raise server.ClipboardBusy('another program has the clipboard open')

        server.clipboard_text = busy
        server.hotkey_pressed()
        check('hotkey: a clipboard another program has open is said to be busy, not empty',
              events() == (200, [{'event': 'hotkey', 'busy': True}]))
        if hasattr(server.ctypes, 'windll'):
            opener = server._clipboard
            server._clipboard = lambda: (None, None)            # what ten failed opens give
            try:
                real()
                check('clipboard: one that cannot be opened raises ClipboardBusy', False)
            except server.ClipboardBusy:
                pass
            finally:
                server._clipboard = opener
        check('hotkey: the log has the length and never the text',
              'hotkey pressed: chars=24' in lines and not any('Copied' in line for line in lines), lines)
    finally:
        server.clipboard_text = real
        server.EVENT_WAIT = wait
        server.log.removeHandler(kept)
        server.log.setLevel(logging.NOTSET)


# ── two windows: a press goes to the one the user was in last ───────────────
@section
def front_window():
    status, _, data = request('POST', '/api/front', {'page': 'a'})
    check('front: a page says its window is in front', (status, json.loads(data)) == (200, {}), data)
    for body in ({'page': 1}, {}, b'not json', ['a']):
        got = refused('POST', '/api/front', body)
        check(f'front: {body!r} is refused', got == (400, 'bad_request'), got)
    wait = server.EVENT_WAIT
    server.EVENT_WAIT = 10
    got = {}

    def poll(page):
        status, _, data = request('GET', '/api/events?page=' + page)
        got[page] = (json.loads(data).get('events'), time.monotonic())

    def waiting(page):
        thread = threading.Thread(target=poll, args=(page,), daemon=True)
        thread.start()
        time.sleep(0.2)
        return thread

    try:
        # b waits first, so without the routing it would be first to wake.
        b, a = waiting('b'), waiting('a')
        queued = time.monotonic()
        server.queue_event({'event': 'hotkey', 'text': 'front'})
        a.join(5)
        check('front: the window last in front takes the press at once',
              got.get('a', (None, 99))[0] == [{'event': 'hotkey', 'text': 'front'}]
              and got['a'][1] - queued < server.EVENT_HANDOFF * 0.8, (got, queued))
        check('front: the other window is still waiting', b.is_alive(), got)
        # The window in front was closed: the other takes the next press, after the hand-off.
        queued = time.monotonic()
        server.queue_event({'event': 'hotkey', 'text': 'late'})
        b.join(5)
        check('front: with the front window gone, another takes the press after a second',
              got.get('b', (None, 0))[0] == [{'event': 'hotkey', 'text': 'late'}]
              and server.EVENT_HANDOFF * 0.9 <= got['b'][1] - queued < server.EVENT_HANDOFF + 1.5,
              (got, queued))
        # The user switches to b.
        got.clear()
        request('POST', '/api/front', {'page': 'b'})
        a, b = waiting('a'), waiting('b')
        queued = time.monotonic()
        server.queue_event({'event': 'hotkey', 'text': 'switched'})
        b.join(5)
        check('front: after a switch the press goes to the new front window',
              got.get('b', (None, 99))[0] == [{'event': 'hotkey', 'text': 'switched'}]
              and got['b'][1] - queued < server.EVENT_HANDOFF * 0.8 and a.is_alive(), (got, queued))
        server.queue_event({'event': 'hotkey', 'text': 'end'})     # lets a's request go
        a.join(5)
    finally:
        server.EVENT_WAIT = wait
        request('POST', '/api/front', {'page': ''})


# ── the log keeps runs, not the page's routine polling ──────────────────────
@section
def quiet_log():
    lines = []

    class Kept(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    kept = Kept()
    server.log.setLevel(logging.INFO)
    server.log.addHandler(kept)
    wait = server.EVENT_WAIT
    server.EVENT_WAIT = 0.2
    try:
        request('GET', '/api/events?page=x')
        request('GET', '/api/status')
        request('POST', '/api/check', {'text': CLEAN})
        request('POST', '/api/front', {'page': ''})
        check('log: routine answers to the page add no line', not lines, lines)
        request('GET', '/api/events', token='wrong')
        request('POST', '/api/check', b'not json')
        request('GET', '/api/ping')
        check('log: refusals and other requests are still logged',
              any('GET /api/events' in line and '403' in line for line in lines)
              and any('POST /api/check' in line and '400' in line for line in lines)
              and any('GET /api/ping' in line for line in lines), lines)
    finally:
        server.EVENT_WAIT = wait
        server.log.removeHandler(kept)
        server.log.setLevel(logging.NOTSET)


# ── the window opens at the size it had last time ───────────────────────────
@section
def window_size():
    profile = os.path.join(tmp, 'profile-size')
    prefs = os.path.join(profile, 'Default', 'Preferences')
    os.makedirs(os.path.dirname(prefs), exist_ok=True)

    def flags(place, raw=None):
        with open(prefs, 'w', encoding='utf-8') as fh:
            fh.write(raw if raw is not None else json.dumps(
                {'browser': {'app_window_placement': {'127': {'0': {'0': {'1_/': place}}}}}}))
        return server.window_flags(profile)

    check('window: maximised last time opens maximised',
          flags({'left': 0, 'top': 0, 'right': 1920, 'bottom': 1040, 'maximized': True}) == ['--start-maximized'])
    check('window: a size set last time comes back',
          flags({'left': 300, 'top': 120, 'right': 1700, 'bottom': 1020, 'maximized': False})
          == ['--window-size=1400,900'])
    default = ['--window-size=1180,820']
    check('window: a size too small is not used', flags({'left': 0, 'top': 0, 'right': 100, 'bottom': 50}) == default)
    check('window: a placement without numbers is not used', flags({'left': 'x'}) == default)
    check('window: a corrupt Preferences file is not used', flags(None, '{not json') == default)
    check('window: a placement under another name is not used',
          flags(None, json.dumps({'browser': {'app_window_placement': {'127.0.0.1_/': {'maximized': True}}}})) == default)
    os.remove(prefs)
    check('window: a new profile opens at 1180x820', server.window_flags(profile) == default)


# ── shutdown ────────────────────────────────────────────────────────────────
@section
def shutdown():
    entered.clear()
    server.run_model = blocks
    run_in_background({})
    check('shutdown: a run was in flight', entered.wait(5))
    check('shutdown: instance.json is there while the copy runs', (instance_file() or {}).get('port') == PORT)
    status, _, data = request('POST', '/api/shutdown')
    check('shutdown answers ok', status == 200 and json.loads(data) == {'ok': True},
          f'{status} {data!r}')
    main.join(10)
    check('shutdown makes serve() return', not main.is_alive())
    check('shutdown removes instance.json', instance_file() is None, instance_file())
    check('shutdown cancels the run in flight', held['cancel'].is_set())
    with socket.socket() as s:
        try:
            s.bind(('127.0.0.1', PORT))
            released = True
        except OSError:
            released = False
    check('shutdown releases the port', released)


# ── the window ──────────────────────────────────────────────────────────────
@section
def closing_the_window():
    # Closing the window is the other way to turn the app off.
    port, thread = start(window='opens')
    time.sleep(1.2)                       # longer than one look at the lock file
    check('server stays up while the window is open',
          thread.is_alive() and server.already_running(port))
    close_window()
    thread.join(10)
    check('closing the window stops the server', not thread.is_alive())


@section
def saved_sessions():
    # The browser keeps its windows in Sessions, with what was typed in them. Each launch
    # clears it, unless a browser still holds the profile.
    sessions = os.path.join(server.DATA_DIR, 'browser-profile', 'Default', 'Sessions')
    started = []
    browsers, popen = server.BROWSERS, subprocess.Popen
    server.BROWSERS, subprocess.Popen = (sys.executable,), lambda argv, **kw: started.append(argv)
    try:
        for held in (True, False):
            os.makedirs(sessions, exist_ok=True)
            with open(os.path.join(sessions, 'Tabs_1'), 'w') as fh:
                fh.write('a pasted draft')
            if held:
                open_window()
            real_launch_window('http://127.0.0.1:1/')
            close_window()
            check(f'sessions: {"kept while a browser holds the profile" if held else "cleared at launch"}',
                  os.path.exists(sessions) == held and len(started) == (1 if held else 2), started)
    finally:
        server.BROWSERS, subprocess.Popen = browsers, popen


@section
def launch_window_flags():
    # The flags that keep the window out of the user's own browser profile and accounts.
    exe = os.path.join(tmp, 'browser.exe')
    open(exe, 'w').close()
    missing = os.path.join(tmp, 'no-such-browser.exe')
    started, tabs, url = [], [], 'http://127.0.0.1:1/'
    browsers, popen, tab = server.BROWSERS, subprocess.Popen, server.webbrowser.open
    server.BROWSERS = (missing, exe)
    subprocess.Popen, server.webbrowser.open = (lambda argv, **kw: started.append(argv)), tabs.append
    try:
        check('launch_window: the first browser there, in a window it can follow',
              real_launch_window(url) is True and started and started[0][0] == exe, started)
        argv = started[0] if started else []
        profile = os.path.join(server.DATA_DIR, 'browser-profile')
        for flag in (f'--app={url}', '--user-data-dir=' + profile, '--disable-sync',
                     '--disable-component-update'):
            check(f'launch_window: the window is started with {flag}', flag in argv, argv)
        # A second --disable-features would replace the first, and Edge would then sign the
        # profile in to the user's Microsoft account.
        features = [arg.split('=', 1)[1].split(',') for arg in argv if arg.startswith('--disable-features=')]
        check('launch_window: one --disable-features, with msImplicitSignin and OptimizationHints',
              len(features) == 1 and {'msImplicitSignin', 'OptimizationHints'} <= set(features[0]), argv)
        check('launch_window: the lock file watched is that profile\'s',
              os.path.join(profile, 'lockfile') == LOCK)
        server.BROWSERS = (missing,)
        check('launch_window: without Chrome or Edge, a tab',
              real_launch_window(url) is False and tabs == [url] and len(started) == 1, (tabs, started))
    finally:
        server.BROWSERS, subprocess.Popen, server.webbrowser.open = browsers, popen, tab
    if os.environ.get('LOCALAPPDATA'):
        mine = os.path.join(os.environ['LOCALAPPDATA'], 'Google', 'Chrome', 'Application', 'chrome.exe')
        check('browsers: a Chrome installed for this account alone is looked for', mine in server.BROWSERS,
              server.BROWSERS)


@section
def window_that_never_appears_and_second_launch():
    # No lock file ever shows up (the browser failed to start, or is one that keeps no
    # lock). That is not a closed window, and the server must stay up.
    port, thread = start(window='never')
    time.sleep(1.2)
    check('a window that never appears does not stop the server',
          thread.is_alive() and server.already_running(port))

    # Single instance: a second launch opens a window on the first and leaves.
    opened = []
    server.launch_window = opened.append
    second = threading.Thread(target=server.serve, kwargs={'port': port}, daemon=True)
    second.start()
    second.join(10)
    check('a second launch returns', not second.is_alive())
    check('a second launch opens a window on the first',
          opened == [f'http://127.0.0.1:{port}/'], opened)
    check('the first instance is still serving', server.already_running(port))
    request('POST', '/api/shutdown', port=port)
    thread.join(10)
    check('the first instance shuts down', not thread.is_alive())


@section
def late_window():
    port, thread = start(window='never')
    time.sleep(1.2)
    open_window()                         # slow browser, or the window of a second launch
    time.sleep(1.2)
    check('a late window keeps the server up', thread.is_alive())
    close_window()
    thread.join(10)
    check('closing a late window stops the server', not thread.is_alive())


@section
def shutdown_with_the_window_open():
    # The page closes its own window after the server has answered, so shutdown must not
    # wait for the lock file to go.
    try:
        port, thread = start(window='opens')
        began = time.time()
        request('POST', '/api/shutdown', port=port)
        thread.join(15)
        check('shutdown with the window open returns', not thread.is_alive())
        check('shutdown does not wait for the window', time.time() - began < 8,
              f'{time.time() - began:.1f}s')
    finally:
        close_window()


def free_ports(count=5):
    """The first of `count` ports in a row that nothing holds."""
    for _ in range(50):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            first = probe.getsockname()[1]
        held = []
        try:
            for port in range(first, first + count):
                held.append(socket.socket())
                held[-1].bind(('127.0.0.1', port))
            return first
        except OSError:
            pass
        finally:
            for s in held:
                s.close()
    raise OSError('no run of free ports')


def second_launch(port):
    """serve(port) the way the shortcut runs it. Returns the URLs it opened a window on."""
    opened = []
    server.launch_window = opened.append
    thread = threading.Thread(target=server.serve, kwargs={'port': port}, daemon=True)
    thread.start()
    thread.join(15)
    return opened if not thread.is_alive() else ['still running']


@section
def port_taken_by_something_else():
    # Something else on the port: the one after it, not any free one. The page keeps its
    # settings per port, and a second launch has to find the first there.
    base = free_ports()
    with socket.socket() as squatter:
        squatter.bind(('127.0.0.1', base))
        squatter.listen(1)
        port, thread = start(port=base)
        check('a taken port falls back to the next one',
              port == base + 1 and server.already_running(port), (base, port))
        got = second_launch(base)
        check('a second launch finds the first on the port after the taken one',
              got == [f'http://127.0.0.1:{port}/'], got)
    # Whatever had the port has let it go. The copy on the next port is still the one.
    got = second_launch(base)
    check('and still finds it once the taken port is free', got == [f'http://127.0.0.1:{port}/'], got)
    request('POST', '/api/shutdown', port=port)
    thread.join(10)
    check('the fallback instance shuts down', not thread.is_alive())


def fake(port, status, body):
    """Something on `port` that answers every GET with `status` and `body`, which may be a
    function of the request path. Returns the server, to shut down."""
    class Fake(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = body(self.path) if callable(body) else body
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', port), Fake, bind_and_activate=False)
    httpd.allow_reuse_address = False
    httpd.server_bind()
    httpd.server_activate()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@section
def port_taken_by_another_account():
    # Every account signed in on the PC shares 127.0.0.1, and any local program can answer a
    # ping. A launch opens a window, where the user pastes text, only on a copy that proves it
    # holds the key in this account's instance.json. Whatever cannot is something else.
    old_owner = hashlib.sha256(os.path.normcase(os.path.abspath(server.DATA_DIR)).encode()).hexdigest()[:16]
    ping = lambda body: json.dumps(body).encode()   # noqa: E731
    impostors = {
        'an older build, or anything that only says it is SlopMonster': ping({'app': 'slopmonster'}),
        'one that names the owner hash anyone can work out': ping({'app': 'slopmonster', 'owner': old_owner}),
        'one with a made-up proof': lambda path: ping({'app': 'slopmonster', 'proof': os.urandom(32).hex()}),
    }
    for name, body in impostors.items():
        base = free_ports()
        other = fake(base, 200, body)
        try:
            check(f'not this account\'s copy: {name}', not server.already_running(base))
            port, thread = start(port=base)
            check(f'a launch beside {name} starts its own',
                  port == base + 1 and thread.is_alive() and server.already_running(port), (base, port))
            if name.startswith('an older'):
                got = second_launch(base)
                check('and a second launch finds that one', got == [f'http://127.0.0.1:{port}/'], got)
            # A program that passes the nonce on to the real copy gets a proof made for the
            # real copy's port, not its own.
            relay = fake(port + 1, 200, lambda path: request('GET', path, token=None, port=port)[2])
            try:
                check('not this account\'s copy: one that asks the real copy for the proof',
                      not server.already_running(port + 1) and server.already_running(port))
            finally:
                relay.shutdown()
                relay.server_close()
            request('POST', '/api/shutdown', port=port)
            thread.join(10)
            check(f'beside {name}: shutdown removes instance.json', instance_file() is None, instance_file())
        finally:
            other.shutdown()
            other.server_close()

    # Another account's real copy, with a data folder and key of its own.
    mine, theirs = server.DATA_DIR, os.path.join(tmp, 'other-account')
    server.DATA_DIR = theirs
    try:
        port, thread = start()
    finally:
        server.DATA_DIR = mine
    try:
        check('the other account\'s copy notes itself in its own folder',
              (instance_file(theirs) or {}).get('port') == port and instance_file() is None)
        check('another account\'s real copy is not this one', not server.already_running(port))
        # Even with this account's own file pointing at it, the key is not the one it holds.
        with open(os.path.join(mine, 'instance.json'), 'w', encoding='utf-8') as fh:
            json.dump({'port': port, 'key': os.urandom(32).hex()}, fh)
        check('a stale instance.json naming the port is not enough', not server.already_running(port))
        os.remove(os.path.join(mine, 'instance.json'))
    finally:
        request('POST', '/api/shutdown', port=port)
        thread.join(10)
    check('the other copy removes its own instance.json', instance_file(theirs) is None)

    # Two copies of this account at once: the second one's file stays when the first stops.
    first_port, first = start()
    second_port, second = start()
    request('POST', '/api/shutdown', port=first_port)
    first.join(10)
    check('a copy that stops leaves a newer copy\'s instance.json alone',
          (instance_file() or {}).get('port') == second_port and server.already_running(second_port),
          instance_file())
    request('POST', '/api/shutdown', port=second_port)
    second.join(10)
    check('and the newer copy removes it', instance_file() is None)


def raw_holder(port, mode):
    """Something on `port` that answers a ping badly: 'deep' with nested brackets, 'trickle'
    with a body a byte at a time, 'headers' with headers a byte at a time. Returns the
    listening socket, to close."""
    ls = socket.socket()
    ls.bind(('127.0.0.1', port))
    ls.listen(5)

    def feed(conn):
        with contextlib.suppress(OSError), conn:
            conn.recv(4096)
            if mode == 'deep':
                body = b'[' * 200_000
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(body) + body)
                time.sleep(1)
                return
            conn.sendall(b'HTTP/1.1 200 OK\r\n' + (b'Content-Length: 100000\r\n\r\n' if mode == 'trickle' else b'X-Slow: '))
            for _ in range(40):
                conn.sendall(b'a')
                time.sleep(0.5)

    def loop():
        while True:
            try:
                conn, _ = ls.accept()
            except OSError:
                return
            threading.Thread(target=feed, args=(conn,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return ls


@section
def hostile_holders():
    # Whatever holds the port can answer anything. A launch beside it neither crashes nor hangs:
    # it starts a copy of its own on the next port.
    for mode in ('deep', 'trickle', 'headers'):
        base = free_ports()
        holder = raw_holder(base, mode)
        try:
            began = time.time()
            try:
                answer = server.running_copy(base)
            except Exception as e:            # noqa: BLE001
                answer = e
            took = time.time() - began
            check(f'a holder that answers {mode}: something else, within the wait',
                  answer is False and took < 3, (answer, round(took, 2)))
            port, thread = start(port=base)
            check(f'a launch beside a holder that answers {mode} starts its own copy',
                  port == base + 1 and thread.is_alive() and server.already_running(port), (base, port))
            request('POST', '/api/shutdown', port=port)
            thread.join(10)
        except Exception as e:                # noqa: BLE001
            check(f'a launch beside a holder that answers {mode} starts its own copy', False, repr(e)[:100])
        finally:
            holder.close()


@section
def nested_instance_file():
    # instance.json in the user's own folder, but nested too deep to read.
    noted = os.path.join(server.DATA_DIR, 'instance.json')
    with open(noted, 'w', encoding='utf-8') as fh:
        fh.write('[' * 200_000 + ']' * 200_000)
    try:
        held = server.read_instance()
    except Exception as e:                    # noqa: BLE001
        held = e
    check('a deeply nested instance.json reads as none', held is None, repr(held)[:100])
    try:
        port, thread = start(port=free_ports())
        check('and a launch still starts a copy', thread.is_alive() and server.already_running(port))
        request('POST', '/api/shutdown', port=port)
        thread.join(10)
    finally:
        with contextlib.suppress(OSError):
            os.remove(noted)


@section
def two_launches_at_once():
    # A shortcut that fires twice: the second launch pings the first before the first has
    # written instance.json. The first's answer waits for serve_forever, which starts after
    # the file is written, so the second reads the file once it has the answer, and joins.
    base = free_ports()
    real_write, written = server.write_instance, threading.Event()

    def slow_write(path, port, key):
        written.set()                         # the first copy has its port
        time.sleep(0.5)
        real_write(path, port, key)

    server.write_instance = slow_write
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            first = threading.Thread(target=server.serve, kwargs={'port': base, 'open_window': False}, daemon=True)
            first.start()
            written.wait(10)
            server.write_instance = real_write
            second = threading.Thread(target=server.serve, kwargs={'port': base, 'open_window': False}, daemon=True)
            second.start()
            second.join(10)
        check('two launches at once: the second joins the first',
              not second.is_alive() and first.is_alive() and 'already running' in out.getvalue(), out.getvalue())
    finally:
        server.write_instance = real_write
        for port in (base, base + 1):
            with contextlib.suppress(OSError):
                request('POST', '/api/shutdown', port=port)
        first.join(10)
        second.join(10)
    check('two launches at once: both have stopped', not first.is_alive() and not second.is_alive())


@section
def every_port_taken():
    # Windows can keep a block of ports back for Hyper-V or WSL, all five of these among them.
    # The copy then takes any free port. instance.json names it, so a second launch finds it.
    base = free_ports()
    real_bind, kept = server.bind, []

    def reserved(port):
        if base <= port <= base + server.FALLBACKS:
            raise PermissionError(errno.EACCES, 'reserved')
        return real_bind(port)

    server.bind = reserved
    try:
        for p in range(base, base + server.FALLBACKS + 1):   # so that bind(0) cannot get one
            kept.append(socket.socket())
            kept[-1].bind(('127.0.0.1', p))
        port, thread = start(port=base)
        check('every port taken: the copy takes another one',
              not base <= port <= base + server.FALLBACKS and server.already_running(port), (base, port))
        got = second_launch(base)
        check('every port taken: a second launch finds the copy on it',
              got == [f'http://127.0.0.1:{port}/'], got)
        request('POST', '/api/shutdown', port=port)
        thread.join(10)
    finally:
        server.bind = real_bind
        for s in kept:
            s.close()


@section
def port_taken_by_a_web_server_or_an_older_copy():
    # Only a copy that is stopping is worth a wait. A web server that answers is no copy of
    # this app, and a launch does not wait a second for its port on every start.
    waited, real_bind_soon = [], server.bind_soon
    server.bind_soon = lambda port: waited.append(port) or real_bind_soon(port)
    base = free_ports()
    web = fake(base, 404, b'<h1>Not Found</h1>')
    try:
        port, thread = start(port=base)
        check('a web server on the port is passed over without a wait for it',
              port == base + 1 and waited == [] and thread.is_alive(), (base, port, waited))
        request('POST', '/api/shutdown', port=port)
        thread.join(10)
    finally:
        server.bind_soon = real_bind_soon
        web.shutdown()
        web.server_close()
    # Something on a fallback port that never answers costs a moment, not the full wait.
    base = free_ports()
    with socket.socket() as mute:
        mute.bind(('127.0.0.1', base + 1))
        mute.listen(1)
        began = time.time()
        port, thread = start(port=base)
        took = time.time() - began
        check('a listener that keeps quiet on a later port barely delays a launch',
              port == base and took < 1.5, (base, port, round(took, 2)))
        request('POST', '/api/shutdown', port=port)
        thread.join(10)


@section
def relaunch_while_the_last_copy_stops():
    # Closing the window and starting again at once: the last copy is still winding down a
    # run. The new one waits for the port rather than taking another, where the page's
    # settings are not.
    base = free_ports()
    entered.clear()

    def slow_to_stop(system_prompt, user_message, model, cancel):
        entered.set()
        cancel.wait(30)
        time.sleep(1.5)                       # a model process that takes its time to die
        raise server.EngineError('cancelled', 'stopped')

    server.run_model = slow_to_stop
    first_port, first = start(port=base)
    running = threading.Thread(target=lambda: request(
        'POST', '/api/unslop', {'text': SLOPPY, 'model': 'haiku'}, port=first_port), daemon=True)
    running.start()
    check('relaunch: a run was in flight', entered.wait(5))
    request('POST', '/api/shutdown', port=first_port)
    port, thread = start(port=base)
    check('relaunch: the new copy gets the same port', port == base and thread.is_alive(), (base, port))
    request('POST', '/api/shutdown', port=port)
    for t in (thread, first, running):
        t.join(10)
    check('relaunch: both copies stop', not thread.is_alive() and not first.is_alive())
    model_says()


@section
def launch_after_a_crash():
    # A copy ended from Task Manager leaves instance.json naming a port nobody holds. A ping
    # there takes Windows two seconds to refuse; the next launch should not wait for it.
    base = free_ports()
    noted = os.path.join(server.DATA_DIR, 'instance.json')
    os.makedirs(server.DATA_DIR, exist_ok=True)
    with open(noted, 'w', encoding='utf-8') as fh:
        json.dump({'port': base, 'key': os.urandom(32).hex()}, fh)
    began = time.time()
    port, thread = start(port=base)
    took = time.time() - began
    request('POST', '/api/shutdown', port=port)
    thread.join(10)
    check('after a crash: the next launch takes the same port without waiting',
          port == base and took < 1, (base, port, round(took, 2)))


@section
def tab_without_hotkey():
    # Without Chrome or Edge the page opens as a tab. Closing a tab tells the server nothing,
    # so a tab gets no Ctrl+Alt+U switch that would hold the keys after it is gone.
    keys = (server.MOD_CONTROL | server.MOD_ALT | server.MOD_SHIFT | server.MOD_NOREPEAT, 0x87)
    for windowed, want in ((False, 'none'), (True, 'off')):
        urls = []

        def launch(url):
            urls.append(url)
            return windowed

        server.launch_window = launch
        thread = threading.Thread(target=server.serve, daemon=True,
                                  kwargs={'port': 0, 'hotkey': keys})
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not urls:
            time.sleep(0.01)
        port = int(re.search(r':(\d+)/', urls[0]).group(1))
        status, _, data = request('GET', '/api/status', port=port)
        check(f'hotkey: {"an app window" if windowed else "a tab"} has the switch {want}',
              json.loads(data).get('hotkey') == want, data)
        request('POST', '/api/shutdown', port=port)
        thread.join(10)


# ── the hotkey, registered for real on a combination nobody types ───────────
@section
def hotkey_for_real():
    import ctypes
    if not hasattr(ctypes, 'windll'):
        return
    user32 = server._user32()[0]
    import inspect
    check('only the launch from the shortcut has the hotkey',
          inspect.signature(server.serve).parameters['hotkey'].default is None)
    # Another test run or dev server may hold the keys: take the first pair nobody holds.
    for key in (0x87, 0x85):                         # F24, F22
        keys = (server.MOD_CONTROL | server.MOD_ALT | server.MOD_SHIFT | server.MOD_NOREPEAT, key)
        if user32.RegisterHotKey(None, 7, *keys):
            user32.UnregisterHotKey(None, 7)
            break
    else:
        print('Ctrl+Alt+Shift+F24 and F22 are held by other programs: the hotkey checks were skipped')
        return

    real = server.clipboard_text
    server.clipboard_text = lambda: 'What was copied'
    out = io.StringIO()
    thread = threading.Thread(target=server.serve, daemon=True,
                              kwargs={'port': 0, 'open_window': False, 'hotkey': keys})
    with contextlib.redirect_stdout(out):
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and not out.getvalue().endswith('\n'):
            time.sleep(0.01)
    port = int(re.search(r':(\d+)/', out.getvalue()).group(1))
    try:
        status, _, data = request('GET', '/api/status', port=port)
        check('hotkey: nothing is registered until the page asks',
              json.loads(data).get('hotkey') == 'off' and not server._hotkey['thread'], data)

        # Something else holds the keys: the page is told, and can try again later.
        if user32.RegisterHotKey(None, 7, *keys):
            try:
                status, _, data = request('POST', '/api/hotkey', {'on': True}, port=port)
                check('hotkey: keys another program holds are reported as taken',
                      json.loads(data) == {'hotkey': 'taken'}, data)
                status, _, data = request('GET', '/api/status', port=port)
                check('status: says the hotkey is taken', json.loads(data).get('hotkey') == 'taken', data)
            finally:
                user32.UnregisterHotKey(None, 7)

        status, _, data = request('POST', '/api/hotkey', {'on': True}, port=port)
        check('hotkey: switched on', json.loads(data) == {'hotkey': 'on'}, data)
        taken = not user32.RegisterHotKey(None, 7, *keys)
        if not taken:
            user32.UnregisterHotKey(None, 7)
        check('hotkey: while on, no other program can have the keys', taken)
        user32.PostThreadMessageW(server._hotkey['tid'], server.WM_HOTKEY, 1, 0)
        check('hotkey: a press hands the page what was copied',
              events(port) == (200, [{'event': 'hotkey', 'text': 'What was copied'}]))

        status, _, data = request('POST', '/api/hotkey', {'on': False}, port=port)
        check('hotkey: switched off', json.loads(data) == {'hotkey': 'off'}, data)
        freed = user32.RegisterHotKey(None, 7, *keys)
        if freed:
            user32.UnregisterHotKey(None, 7)
        check('hotkey: off lets the keys go', freed)
        request('POST', '/api/hotkey', {'on': True}, port=port)

        got = {}

        def wait_for_one():
            got['events'] = events(port)

        waiter = threading.Thread(target=wait_for_one, daemon=True)
        waiter.start()
        time.sleep(0.3)
        began = time.time()
        request('POST', '/api/shutdown', port=port)
        waiter.join(10)
        thread.join(10)
        check('shutdown: a waiting request for events answers at once',
              got.get('events') == (200, []) and time.time() - began < 3, (got, time.time() - began))
        check('shutdown: the server stops', not thread.is_alive())
        freed = user32.RegisterHotKey(None, 7, *keys)
        if freed:
            user32.UnregisterHotKey(None, 7)
        check('shutdown: the keys are let go', freed)
    finally:
        server.clipboard_text = real
        if thread.is_alive():
            request('POST', '/api/shutdown', port=port)
            thread.join(10)


logging.shutdown()                    # lets go of server.log so the temp folder can go
shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f'{len(fails)} FAILED\n')
    for f in fails:
        print(f'  · {f}')
    sys.exit(1)
print('all tests passed')
