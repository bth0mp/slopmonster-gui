#!/usr/bin/env python3
"""The SlopMonster app server: paste text into a local page, get it back unslopped.

    pythonw app/server.py              # what the desktop shortcut runs: opens the app window
    python  app/server.py --no-window  # development: no window or hotkey, prints the URL
    SlopMonster.exe                    # the same app, built by packaging/build.ps1

For testing, --port N starts at port N instead of PORT (0: any free port), and --data-dir
DIR, or the SLOPMONSTER_DATA_DIR variable, keeps the log, instance.json, the browser
profile and saved instructions in DIR instead of the real data folder.

Pasted text is checked with tools/deslop.py and rewritten by the local `claude` CLI, or by
OpenAI's `codex` CLI on the user's ChatGPT plan, under prompts/cleanse.txt or the user's
edited copy of it, round after round, until the checker passes, a round fixes nothing more
or MAX_ROUNDS is spent.
Standard library only. tools/deslop.py and prompts/cleanse.txt are upstream's files plus
this fork's chat-reply rules, which this server calls, and its rule against added hedges;
merge upstream changes into them by hand.

It listens on 127.0.0.1 and still trusts nothing, because any web page the user has open
can aim a request at localhost, and a request here can spend their model quota or switch
the app off. Handler.route holds the checks. They are aimed at web pages. Another
program on this PC, or another signed-in Windows account, can fetch the page, read its
token and use the API; guarding against those is out of scope.
"""
import bisect
import collections
import contextlib
import ctypes
import difflib
import errno
import hashlib
import hmac
import http.client
import io
import itertools
import json
import logging
import logging.handlers
import os
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.stderr is None:
    # pythonw: a traceback has nowhere to go, and a shortcut that silently does nothing
    # is the one failure the user cannot diagnose. Show it.
    sys.excepthook = lambda kind, error, trace: ctypes.windll.user32.MessageBoxW(
        None, f'SlopMonster stopped because of an error.\n\n{error!r}', 'SlopMonster', 0x10)

# SlopMonster.exe, which PyInstaller builds from this file (packaging/SlopMonster.spec). It
# unpacks the page, the prompt and the guides to sys._MEIPASS, laid out as in the repository.
FROZEN = bool(getattr(sys, 'frozen', False))
if FROZEN:
    REPO = sys._MEIPASS
    APP_DIR = os.path.join(REPO, 'app')
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    REPO = os.path.dirname(APP_DIR)
sys.path.insert(0, os.path.join(REPO, 'tools'))
from version import VERSION  # noqa: E402
# Upstream's checker plus this fork's chat-reply rules (chat_framing, chat_reply,
# framing_spans, markdown_raw, raw_rules, audit's raw=). Merge upstream into it by hand,
# keeping both sides, then run tools/test_deslop.py and app/test_server.py.
import deslop  # noqa: E402

DATA_DIR = os.path.abspath(os.environ.get('SLOPMONSTER_DATA_DIR') or os.path.join(
    os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'SlopMonster'))
TOKEN = secrets.token_urlsafe(32)      # new every launch; the page finds it in index.html
PORT = 47821
FALLBACKS = 4                          # PORT+1 to PORT+4, in turn, when something else has PORT
MAX_ROUNDS = 3
MAX_CHARS = 200_000                    # of text; anything long is rewritten in parts
PART_CHARS = 6_000                     # about a thousand words to a part, one model call each
MAX_BODY = 1_000_000                   # bytes of a JSON request
MAX_FILE = 30_000_000                  # bytes of a Word or PDF file sent to /api/extract
DOCUMENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documents.py')
# How documents.py runs in a process of its own: SlopMonster.exe runs itself to do it.
DOCUMENTS_CMD = [sys.executable, '--documents'] if FROZEN else [sys.executable, DOCUMENTS]
DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
TIMEOUT = 180                          # seconds per model call, the bound tools/cleanse.sh uses
MARKER = '<<<SLOPMONSTER-NOTES>>>'
# Claude's three choices, by the names the CLI takes. The page lists them on its own, in
# index.html's claude-model buttons, CLAUDE_MODELS and prefs: change them together.
MODELS = ('sonnet', 'haiku', 'opus')
# What a choice is called on the CLI's command line when the CLI's own alias will not do.
# Measured on CLI 2.1.292, one call each on the same short text: "sonnet" and "opus" pick
# the current models and answer in 5 to 8 seconds, but "haiku" still picks an older model
# that took 57 (45 to 90 on 2.1.197). The name below took 6 to 8.
CLI_MODEL = {'haiku': 'claude-haiku-5-5'}
ALIAS_NOTE = ("The Fast model's pinned name was refused, so Claude Code's own 'haiku' alias was "
              "used. It can be slower.")
# Which AI rewrites: Claude through Claude Code, or ChatGPT through OpenAI's Codex CLI on the
# user's ChatGPT plan. A draft Claude wrote is best rewritten by the other one (SKILL.md Step 3).
ENGINES = ('claude', 'chatgpt')
CLAUDE_ID = re.compile(r'claude-[a-z0-9]+(?:[.-][a-z0-9]+){0,12}')   # a full Claude model name
CODEX_ID = re.compile(r'[a-z0-9]+(?:[.-][a-z0-9]+){0,12}')          # such as gpt-6.1-sol
EFFORTS = ('low', 'medium', 'high', 'xhigh')                         # ChatGPT's reasoning effort
# How much a rewrite may change, the page's "How much to change". Light is the instructions
# alone. Thorough adds THOROUGH and the two guides in references/, as system_prompt() puts them.
STRENGTHS = ('light', 'thorough')
MAX_PROMPT = 60_000                    # characters of rewrite instructions the user may save
# What a Thorough run adds after the instructions, with the guides after it. The pattern
# check names words and a few shapes; what reads as machine-written is mostly in the rest.
THOROUGH = '''THOROUGH EDIT
The author chose a thorough rewrite. It goes further than the passes above, and the text
gets it even when the pattern check found nothing: fixing what the check flags is only the
start. Go through the text sentence by sentence, outside code, commands and quotations,
and ask of each one whether a careful person writing to this reader would have put it that
way. Where the answer is no, write the sentence again in your own words. In text a chat
assistant wrote, that is most sentences. Look in particular for:
- The same sentence shape used again and again, within a paragraph or across them.
- An even rhythm, where sentence after sentence has the same length and build.
- Openings that announce, praise or restate the question instead of answering it.
- Paragraphs that end on a summary, a moral or a neat restatement of what they just said.
- Stock transitions, hedges, intensifiers and enthusiasm that carry no information.

Keep every fact, number, date, name, quotation, placeholder, link and address, and the
Markdown structure: headings, lists, tables, code, bold and italic. Keep the author's
position, how sure they are, and their register. Do not add a fact, example, opinion or
closing of your own, and do not drop a point to make the text shorter. When you are told
to return the same number of blocks, that rule comes first: rework the sentences inside
each block and keep every block where it is.

The two guides below are SlopMonster's own: its editing principles, and a catalogue of
patterns that mark AI writing. Use them for this edit.'''
GUIDES = ('principles.md', 'signs-of-ai-writing.md')
# What the model has no use for in the guides: a paragraph that points at a file, a command or
# the guide's sources, and the rules between sections.
GUIDE_SKIP = re.compile(r'\]\(|`tools/|Steve\s+Krug|run\s+the\s+checker\s+again|\A---\Z')
# What split_reply() needs from the model, added to edited instructions that no longer ask
# for the marker line, so that a reply can still be told apart from its notes. A Thorough run
# always ends with it, so that the last thing the model reads is how to reply.
CONTRACT = f'''OUTPUT
Return the rewritten copy in full, and nothing before it. Then a line containing
exactly
{MARKER}
and nothing else, then at most five bullets describing the substantive edits or
unresolved concerns. Emit the marker exactly once, alone on its line, with no bullet,
quotes or backticks around it, even when you changed nothing.'''
# How the CLI turns down its command line before calling any model: the first line of stderr,
# with no text of the user's in it. Measured on 2.1.292 with a made-up flag and a bad value.
REFUSED = [(re.compile(r"error: unknown option '(--?[A-Za-z0-9][\w-]{0,40})'"), "unknown option '{}'"),
           (re.compile(r"error: option '(--?[A-Za-z0-9][\w-]{0,40})(?: <[\w.-]{1,20}>)?' argument "
                       r"'[^'\n]{0,40}' is invalid\.[^\n]{0,200}"),
            "it does not accept the value given to '{}'")]
# The CLI the command line was last measured on. An older one that turns it down is out of
# date itself; a newer one may have dropped something the app passes.
TESTED_CLI = (2, 1, 292)
TITLES = {'vocab': 'Vocabulary', 'phrases': 'Sentence patterns', 'punctuation': 'Punctuation',
          'rhythm': 'Three-item lists', 'proof': 'Possible unsupported proof',
          'placeholders': 'Placeholders to fill in'}
ADVISORY = ('proof', 'placeholders')    # listed for the author, never a reason to rewrite
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)   # Windows: no console flashing up
CLIENT_GONE = (ConnectionError, TimeoutError)            # what writing to a closed socket raises

NOT_LOGGED_IN = ('The Claude Code command-line tool is not signed in. Press "Sign in to Claude", or '
                 'run "claude auth login" in a terminal.')
# The page looks again when its window comes back to the front, so "come back here" is enough
# for an install it can find: on a PATH, or in the installers' own folders.
MISSING = ('The "claude" command was not found. Install Claude Code, then come back here and sign '
           'in. If it is still not found, close SlopMonster and open it again.')
NPM_ONLY = ('Only the npm copy of Claude Code (claude.cmd) was found, and this app cannot '
            'run that one. Install Claude Code with its Windows installer, then come back '
            'here and sign in.')
CODEX_NOT_LOGGED_IN = ('ChatGPT is not signed in. Press "Sign in to ChatGPT", or run "codex login" in '
                       'a terminal.')
CODEX_MISSING = ('The "codex" command was not found. Install OpenAI\'s Codex command-line tool, then '
                 'come back here and sign in with ChatGPT. If it is still not found, close '
                 'SlopMonster and open it again.')    # index.html has the same words
CODEX_LIMIT = ('Your ChatGPT plan has reached its usage limit for now. Try again later, or switch '
               'to Claude.')
CLAUDE_LIMIT = ('Your Claude plan has reached its usage limit for now. Try again later, or switch '
                'to ChatGPT.')
TOO_LONG = f'That is over the {MAX_CHARS:,} character limit. Split the text and run each part.'
NO_PROSE = 'SlopMonster leaves code blocks as they are, and this text has nothing outside them to rewrite.'
STOPPED = 'This run was stopped.'
LOG_AT = r'%LOCALAPPDATA%\SlopMonster\server.log'      # as the README names it, for Explorer's address bar
UNEXPECTED = f'SlopMonster hit an unexpected error. The details are in {LOG_AT}.'

log = logging.getLogger('slopmonster')

# ── the checker ──────────────────────────────────────────────────────────────

_report_lock = threading.Lock()


def checkable(text):
    """The copy of `text` the checker is shown: what would stall its patterns broken up."""
    # Upstream's patterns backtrack without limit on a long unbroken run (a 3,000-digit
    # number takes over a minute), and Python holds the GIL for the whole match, so one
    # pasted table of numbers would freeze every thread, Turn off included. The checker is
    # shown such runs broken up. Only its copy: the text itself is never changed.
    # The same for a "[" that is never closed: upstream's link pattern runs from each one to
    # the end of the text. A bracket with no partner near it cannot be a link, so it goes.
    text = re.sub(r'\[(?![^\]]{0,300}\])', ' ', text)
    text = re.sub(r'\]\((?![^)\n]{0,2000}\))', '] ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[\d,]{40}(?=[\d,])', r'\g<0> ', text)
    return re.sub(r'\w{200}(?=\w)', r'\g<0> ', text)


def check(text):
    """Run the upstream checker over pasted text and shape the result for the page."""
    text = checkable(text)
    prose = deslop.markdown_prose(text)
    words = len(prose.split())
    # The text as pasted, lines and all, is what shows a chat reply around a draft: an opening
    # "Sure! Here's...", a "---" with notes under it, a bold **Subject:**, a [Your Name].
    hits = deslop.audit(prose, raw=text)
    # report() is the one place upstream turns hits into a score, and it prints as it goes.
    # Capturing the output keeps this score from drifting away from the command-line one.
    # redirect_stdout swaps sys.stdout for the whole process, hence the lock.
    with _report_lock, contextlib.redirect_stdout(io.StringIO()):
        score = deslop.report(hits)
    categories = []
    for key, title in TITLES.items():
        if key in ADVISORY:
            found = [{'label': claim, 'detail': ''} for claim in hits[key]]
        elif key == 'phrases':
            found = [{'label': label, 'detail': f'×{count}', 'match': match}
                     for (label, count), match in zip(hits[key], first_matches(prose, text))]
        elif key == 'vocab':
            found = [{'label': label, 'detail': f'×{count}'} for label, count in hits[key]]
        else:
            found = [{'label': label, 'detail': snippet} for label, snippet in hits[key]]
        categories.append({'key': key, 'title': title, 'advisory': key in ADVISORY, 'hits': found})
    # Proof is advisory: the regex sees a number beside a noun, and only the author knows
    # whether it is true. It costs a point, as upstream scores it, but never blocks "clean".
    # Placeholders cost nothing: the rewrite keeps them, so only the author can fill them in.
    return {'words': words, 'score': score if words else None,
            'clean': not any(c['hits'] for c in categories if not c['advisory']),
            'categories': categories}


def first_matches(prose, raw):
    """What each sentence-pattern hit matched, the first three, in the order audit() lists
    the hits: the raw rules, then PHRASES. The model is shown the lines themselves, so that
    it fixes those and does not go looking for the other examples in a rule's label."""
    view, low = deslop.markdown_raw(raw), prose.lower()
    found = [itertools.islice(re.finditer(pat, view, re.M), 3)
             for pat, _ in deslop.raw_rules(view, low)]
    found += [itertools.islice(re.finditer(pat, low), 3) for pat, _ in deslop.PHRASES]
    found = [['"' + ' '.join(m.group(0).split())[:100] + '"' for m in ms] for ms in found]
    return ['; '.join(ms) for ms in found if ms]


def placeholders(result):
    """The slots a check found for the author to fill in, such as [Your Name]."""
    return [h['label'] for c in result['categories'] if c['key'] == 'placeholders' for h in c['hits']]


def flag_count(result):
    """How many things a check flagged that a round is asked to fix.

    A word or pattern used three times is three. Proof is advisory and not counted.
    """
    count = 0
    for c in result['categories']:
        if not c['advisory']:
            for h in c['hits']:
                times = re.fullmatch(r'×(\d+)', h['detail'])
                count += int(times[1]) if times else 1
    return count


# ── the rewrite engine ───────────────────────────────────────────────────────

class EngineError(Exception):
    """The rewrite could not be done, with a code and a message the page can show."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


_status = {'until': 0, 'engine': None}
_cli_model = dict(CLI_MODEL)          # this session's names, after any the CLI refused
_version = {}                         # (path, modified time) of the CLI: the version it gave
_codex_status = {'until': 0, 'engine': None}


def saved_path():
    """PATH as Windows now gives it to a program started afresh: the machine's, then the user's.
    An installer run while SlopMonster is open adds its folder there, and the PATH this process
    started with never shows it, so Check again would not find the new CLI until a restart."""
    try:
        import winreg
    except ImportError:                       # not Windows
        return ''
    folders = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
                      (winreg.HKEY_CURRENT_USER, 'Environment')):
        try:
            with winreg.OpenKey(root, key) as k:
                folders.append(os.path.expandvars(str(winreg.QueryValueEx(k, 'Path')[0])))
        except OSError:
            pass
    return os.pathsep.join(folders)


def which(name):
    """shutil.which on this process's PATH, then on the one saved_path() reads."""
    return shutil.which(name) or shutil.which(name, path=saved_path())


# Where claude.exe is when it is on no PATH: Claude Code's Windows installer puts it in
# .local\bin and leaves PATH alone, and WinGet links it from its Links folder (the setup docs).
CLAUDE_PATHS = [os.path.join(base, *rest, 'claude.exe') for base, *rest in (
    (os.environ.get('USERPROFILE'), '.local', 'bin'),
    (os.environ.get('LOCALAPPDATA'), 'Microsoft', 'WinGet', 'Links')) if base]


def find_cli():
    """Path of the claude CLI to run, or an EngineError saying why there is none."""
    # By its .exe name first: an npm install adds a claude.cmd that can sit earlier on PATH.
    # The installers' own folders come before that claude.cmd for the same reason.
    exe = (which('claude.exe') or next((p for p in CLAUDE_PATHS if os.path.isfile(p)), None)
           or which('claude'))
    if not exe:
        raise EngineError('missing', MISSING)
    if exe.lower().endswith(('.cmd', '.bat')):
        # Windows runs a batch file through cmd.exe, which ends the command line at the
        # first newline of the system prompt and loses every flag after it.
        raise EngineError('missing', NPM_ONLY)
    return exe


def engine_status():
    """Whether the claude CLI is installed and logged in.

    "Ready" is cached for 20 seconds. Anything else for 2, so that signing in shows up the
    next time the page asks rather than 20 seconds later.
    """
    if time.monotonic() < _status['until']:
        return _status['engine']
    try:
        exe = find_cli()
    except EngineError as e:
        engine = {'ok': False, 'code': e.code, 'message': e.message}
    else:
        try:
            out = subprocess.run([exe, 'auth', 'status'], stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=15, cwd=DATA_DIR,
                                 creationflags=NO_WINDOW).stdout
            logged_in = json.loads(out).get('loggedIn') is not False
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            logged_in = True          # cannot tell from here; a rewrite will report it
        engine = ({'ok': True, 'code': 'ready', 'message': 'Ready.'} if logged_in else
                  {'ok': False, 'code': 'not_logged_in', 'message': NOT_LOGGED_IN})
    _status.update(until=time.monotonic() + (20 if engine['ok'] else 2), engine=engine)
    return engine


# What the sign-in window runs: the CLI's own sign-in, with a line before and after so the
# window explains itself and stays open long enough to read how it went.
SIGN_IN = '''import ctypes, subprocess, sys, time
ctypes.windll.kernel32.SetConsoleTitleW("Sign in to Claude - SlopMonster")
print("Signing in to Claude Code so that SlopMonster can use it.")
print("Your browser will open. Approve the sign-in there, then come back here.")
print()
while subprocess.call(sys.argv[1:]) != 0:
    print()
    input("The sign-in did not finish. Press Enter to try again, or close this window. ")
print()
print("Done. Go back to SlopMonster. This window closes by itself.")
time.sleep(3)
'''
CODEX_SIGN_IN = SIGN_IN.replace('Sign in to Claude', 'Sign in to ChatGPT').replace(
    'Signing in to Claude Code', "Signing in to ChatGPT in OpenAI's Codex command-line tool")
_sign_in = {'claude': None, 'chatgpt': None}


def start_sign_in(engine='claude'):
    """Open a console window running `claude auth login`, or `codex login` for ChatGPT. The
    user finishes it in their browser.

    The app never sees the account or a password: this starts the CLI's own sign-in and
    nothing more. One window per engine at a time: False means the one from an earlier press
    is still open, so that the page does not claim to have opened another.
    """
    if engine == 'chatgpt':
        argv, script, env = [find_codex(), 'login'], CODEX_SIGN_IN, codex_env()
    else:
        argv, script, env = [find_cli(), 'auth', 'login'], SIGN_IN, None
    if _sign_in[engine] and _sign_in[engine].poll() is None:
        return False
    if FROZEN:
        # SlopMonster.exe runs the wrapper itself, in a console it opens (sign_in_window).
        # PYINSTALLER_RESET_ENVIRONMENT: unpacked afresh, so the window outlives Turn off.
        argv = [sys.executable, '--sign-in', engine] + argv
        env = dict(env or os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    # python.exe, not the pythonw this app runs under: the wrapper needs a console.
    python = os.path.join(os.path.dirname(sys.executable), 'python.exe')
    if not FROZEN and os.path.isfile(python):
        argv = [python, '-I', '-c', script] + argv
    _sign_in[engine] = subprocess.Popen(
        argv, cwd=DATA_DIR, env=env, creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
    _status['until'] = _codex_status['until'] = 0     # the next status request asks afresh
    return True


def cli_version(exe):
    """The CLI's version number, such as "2.1.292", or Codex's "0.163.0" from "codex-cli
    0.163.0", or '' when it does not say.

    Asked once for each copy of the file: an update replaces it, and the number goes stale.
    """
    try:
        key = (exe, os.path.getmtime(exe))
    except OSError:
        key = (exe, None)
    if key not in _version:
        try:
            out = subprocess.run([exe, '--version'], stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=15, cwd=DATA_DIR, creationflags=NO_WINDOW).stdout
            found = re.match(rb'\s*(?:codex-cli )?(\d{1,4}(?:\.\d{1,6}){1,3})\b', out or b'')
            _version[key] = found[1].decode('ascii') if found else ''
        except (OSError, subprocess.SubprocessError):
            _version[key] = ''
    return _version[key]


def refused_command(complaint):
    """What the CLI said it refused on our command line, or None when stderr is not that."""
    first = complaint.strip().split('\n', 1)[0].strip()
    for pattern, said in REFUSED:
        found = len(first) <= 300 and pattern.fullmatch(first)
        if found:
            return said.format(found[1])
    return None


@contextlib.contextmanager
def prompt_file(text):
    """The path of a file in DATA_DIR that holds `text`, removed afterwards.

    The system prompt goes to the CLI in a file: on the command line, edited instructions of
    more than about 32,000 characters pass Windows' limit, and the CLI never starts.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    handle, path = tempfile.mkstemp(prefix='prompt-', suffix='.txt', dir=DATA_DIR)
    try:
        with open(handle, 'w', encoding='utf-8', newline='') as fh:
            fh.write(text)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


# How the CLI says the plan is spent, in the reply of a result marked is_error: 2.1.292's
# "You've hit your session limit · resets 3pm (Europe/Berlin)" (or weekly, Opus, monthly spend
# limit, or the team's shared budget), "You're out of usage credits", and older copies'
# "Claude AI usage limit reached|<time>".
CLAUDE_SPENT = re.compile(r'usage limit|(?:hit|reached) your .{0,40}?(?:limit|budget)|out of usage credits',
                          re.I)


def run_model(system_prompt, user_message, model, cancel):
    """One call to the local Claude Code CLI. Returns its reply or raises EngineError.

    One of the two places this app reaches a model (run_codex is ChatGPT's), and the one
    function most tests replace.
    --safe-mode keeps the user's hooks, plugins, skills, MCP servers and CLAUDE.md out of
    the rewrite, and --tools "" leaves the model no tools. The CLI itself still expands
    @path in a prompt by reading that file into the request, whatever the tool list says,
    and pasted text is somebody else's words: CLAUDE_CODE_DISABLE_ATTACHMENTS turns that
    off. `cancel` is a threading.Event: once it is set the CLI is killed within a fraction
    of a second.

    When the CLI turns down a flag, as a Claude Code update or a copy older than TESTED_CLI
    can, the error names the flag and the version, and which of the two needs updating. When
    it does not know the name Fast is pinned to, the call is made again with its own "haiku"
    alias, and so is every later one.
    """
    exe = find_cli()
    while True:                           # a second time only after the pinned name was refused
        name = _cli_model.get(model, model)
        # ponytail: temp files, not pipes: no pipe deadlocks, but the text sits in %TEMP% for the call.
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, \
                tempfile.TemporaryFile() as stderr, prompt_file(system_prompt) as system:
            stdin.write(user_message.encode('utf-8', 'replace'))
            stdin.seek(0)
            proc = subprocess.Popen(
                [exe, '-p', '--safe-mode', '--tools', '', '--system-prompt-file', system,
                 '--no-session-persistence', '--output-format', 'json',
                 '--model', name],
                stdin=stdin, stdout=stdout, stderr=stderr, cwd=DATA_DIR, creationflags=NO_WINDOW,
                env=dict(os.environ, CLAUDE_CODE_DISABLE_ATTACHMENTS='1'))
            deadline = time.monotonic() + TIMEOUT
            while proc.poll() is None:
                if cancel.wait(0.2) or time.monotonic() > deadline:
                    # ponytail: kills the CLI, not its process tree. Fine while it has no tools.
                    proc.kill()
                    proc.wait()
                    if cancel.is_set():
                        raise EngineError('cancelled', STOPPED)
                    raise EngineError('timeout', f'The model did not answer within {TIMEOUT} seconds. '
                                                 'Try again, or use a shorter text or the Fast model.')
            stdout.seek(0)
            stderr.seek(0)
            raw = stdout.read().decode('utf-8', 'replace')
            complaint = stderr.read().decode('utf-8', 'replace')
        try:
            out = json.loads(raw)
            reply, failed = out['result'], bool(out.get('is_error')) or proc.returncode != 0
            if not isinstance(reply, str):
                raise TypeError('result is not a string')
        except (ValueError, KeyError, TypeError, AttributeError):
            # stdout may be the rewrite in some other shape, and the log never holds the user's
            # text, so only its size goes in.
            log.error('claude exited %s with unreadable output (%d characters, and %d on stderr)',
                      proc.returncode, len(raw), len(complaint))
            # Only the flag is taken from stderr, and only from a line in the CLI's own words.
            refused = refused_command(complaint)
            if refused:
                version = cli_version(exe)
                log.error('claude refused the command line: %s (version %s)', refused, version or '?')
                if version and tuple(int(n) for n in version.split('.')) < TESTED_CLI:
                    raise EngineError('cli_old', f'Claude Code refused the command: {refused}. Claude '
                                      f'Code {version} is older than this app needs. Update Claude '
                                      'Code, then try again.')
                raise EngineError('engine_changed', f'Claude Code refused the command: {refused}. This '
                                  f'version of Claude Code{f" ({version})" if version else ""} needs a '
                                  'newer SlopMonster.')
            # The log holds only the sizes, so it is no place to send the user.
            raise EngineError('engine_failed', 'Claude Code returned something this app could not '
                                               'read. Try again, or run "claude -p hi" in a terminal '
                                               'to see what it says.')
        if failed:
            if 'not logged in' in reply.lower() or '/login' in reply:
                raise EngineError('not_logged_in', NOT_LOGGED_IN)
            if name != model and (out.get('api_error_status') == 404
                                  or f'selected model ({name})' in reply):
                log.warning('the CLI refused the model name %s, so %s is used from now on',
                            name, model)
                _cli_model[model] = model
                continue
            if CLAUDE_SPENT.search(reply):
                # The CLI's own words are not shown, so the |<time> of the old wording goes too.
                when = re.search(r'resets ([^·\n]{1,60})', reply)
                raise EngineError('usage_limit', CLAUDE_LIMIT + (
                    f' Claude Code says the limit resets {when[1].strip()}.' if when else ''))
            raise EngineError('engine_failed', f'Claude Code reported an error: {reply[:300]}')
        return reply


# ── the ChatGPT engine ───────────────────────────────────────────────────────

# Where Codex puts its own codex.exe when it is not on PATH: inside the npm package, for each
# processor, and the folder the standalone installer uses (the enterprise deployment docs).
CODEX_PATHS = [os.path.join(os.environ['APPDATA'], 'npm', 'node_modules', '@openai', 'codex', 'node_modules',
                            '@openai', f'codex-win32-{arch}', 'vendor', f'{cpu}-pc-windows-msvc', 'bin', 'codex.exe')
               for arch, cpu in (('x64', 'x86_64'), ('arm64', 'aarch64')) if os.environ.get('APPDATA')]
if os.environ.get('LOCALAPPDATA'):
    CODEX_PATHS.append(os.path.join(os.environ['LOCALAPPDATA'], 'Programs', 'OpenAI', 'Codex', 'bin', 'codex.exe'))
# One turn with no tools that reach out: no shell, code runner, web search, apps, plugins,
# hooks, skills or helper agents, a read-only sandbox, and nothing kept. The user's
# config.toml and rules are left out, and so is any AGENTS.md in or above the folder it runs in
# (project_doc_max_bytes=0: a .git folder above DATA_DIR would make Codex send them all, with
# the folder's path). So are Codex's own notes on skills, permissions, modes and that folder:
# the model sees the system prompt and the text. An AGENTS.md in CODEX_HOME still goes, since
# no flag turns it off (0.163.0).
# A run with these flags and --json, told to run a command, read a file and fetch a page,
# showed no tool item and no file contents (codex 0.163.0).
CODEX_FLAGS = ['exec', '--ephemeral', '--skip-git-repo-check', '--ignore-user-config', '--ignore-rules',
               '--sandbox', 'read-only', '-c', 'features.shell_tool=false', '-c', 'features.unified_exec=false',
               '-c', 'features.code_mode=false', '-c', 'features.code_mode_host=false',
               '-c', 'web_search="disabled"', '-c', 'features.hooks=false', '-c', 'history.persistence="none"',
               '-c', 'features.remote_plugin=false', '-c', 'features.apps=false', '-c', 'features.plugins=false',
               '-c', 'features.multi_agent=false', '-c', 'agents.enabled=false', '-c', 'features.goals=false',
               '-c', 'features.image_generation=false', '-c', 'features.browser_use=false',
               '-c', 'features.computer_use=false', '-c', 'features.sleep_tool=false',
               '-c', 'skills.include_instructions=false', '-c', 'include_permissions_instructions=false',
               '-c', 'include_collaboration_mode_instructions=false', '-c', 'include_environment_context=false',
               '-c', 'project_doc_max_bytes=0']
# How Codex says the sign-in is gone or the plan is spent, in an "ERROR: " line on stderr.
# Measured on 0.163.0 with an empty CODEX_HOME; the rest are the CLI's own strings.
CODEX_SIGNED_OUT = re.compile(r'401 Unauthorized|sign in again')
CODEX_SPENT = re.compile(r'usage limit|out of credits|spend cap', re.I)
CODEX_NO_MODEL = re.compile(r'model is not supported')   # measured with a made-up model name
# How a Codex that no longer knows part of CODEX_FLAGS says so (0.163.0, with a made-up key, flag
# and value). An unknown -c key is only warned about, after the echoed prompt, and the run goes
# on with that setting at its default: whatever it switched off is on again. The others stop
# Codex before it reads the text, so stderr holds none of it.
CODEX_IGNORING = re.compile(r'warning: Codex is ignoring \d{1,3} unrecognized configuration settings?\..{0,200}')
CODEX_IGNORED_KEY = re.compile(r'\s*session-flags: `([\w.]{1,64})` is ignored\.')
CODEX_REFUSED = [(re.compile(r"error: unexpected argument '(--?[A-Za-z0-9][\w-]{0,40})' found"),
                  "unknown option '{}'"),
                 (re.compile(r"error: invalid value '[^'\n]{0,40}' for '(--?[A-Za-z0-9][\w-]{0,40})[^'\n]{0,40}'"),
                  "it does not accept the value given to '{}'"),
                 (re.compile(r'Error loading config\.toml: unknown variant (`[^`\n]{0,40}`).{0,200}'),
                  'it no longer accepts the setting {}')]
TESTED_CODEX = (0, 163, 0)      # the Codex CODEX_FLAGS was last measured on


def find_codex():
    """Path of the native codex.exe to run, or an EngineError saying why there is none."""
    # Never codex.cmd or codex.ps1: a shell would end the command line at the first newline.
    exe = which('codex.exe') or next((p for p in CODEX_PATHS if os.path.isfile(p)), None)
    if not exe:
        raise EngineError('missing', CODEX_MISSING)
    return exe


def codex_env():
    """The environment Codex runs in: without an API key, so that it uses the ChatGPT plan."""
    return {k: v for k, v in os.environ.items()
            if not (k.upper().startswith('OPENAI_') or k.upper() == 'CODEX_API_KEY')}


def chatgpt_status():
    """{'installed', 'signed_in', 'how'} for the Codex CLI, cached as engine_status() is.

    `how` is 'chatgpt' or 'api_key', from `codex login status`. Nothing about the account.
    """
    if time.monotonic() < _codex_status['until']:
        return _codex_status['engine']
    try:
        exe = find_codex()
    except EngineError:
        engine = {'installed': False, 'signed_in': False, 'how': ''}
    else:
        try:
            done = subprocess.run([exe, 'login', 'status'], stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=15, cwd=DATA_DIR, creationflags=NO_WINDOW, env=codex_env())
            said = (done.stderr + done.stdout).decode('utf-8', 'replace')
        except (OSError, subprocess.SubprocessError):
            said = ''                     # cannot tell from here; a rewrite will report it
        how = re.search(r'(?m)^Logged in using (ChatGPT|an API key)\b', said)
        engine = {'installed': True, 'signed_in': not re.search(r'(?m)^Not logged in\s*$', said),
                  'how': {'ChatGPT': 'chatgpt', 'an API key': 'api_key'}[how[1]] if how else ''}
    _codex_status.update(until=time.monotonic() + (20 if engine['signed_in'] else 2), engine=engine)
    return engine


class _Limits(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION, of which only LimitFlags is set."""
    _fields_ = [('user_time', ctypes.c_int64 * 2), ('LimitFlags', ctypes.c_uint32),
                ('working_set', ctypes.c_size_t * 2), ('processes', ctypes.c_uint32),
                ('affinity', ctypes.c_size_t), ('classes', ctypes.c_uint32 * 2),
                ('io', ctypes.c_uint64 * 6), ('memory', ctypes.c_size_t * 4)]


def contain(proc):
    """A Windows job holding `proc`, so that it and every process it starts end together:
    when the job is ended, or its handle closed. None when there is no job to be had.

    taskkill /T is no substitute: it asks WMI, which on some PCs answers "No such interface
    supported" and kills nothing. ponytail: a process started before the job is assigned, a
    moment after Popen, escapes it; Codex starts nothing that early.
    """
    try:
        k = ctypes.windll.kernel32
    except AttributeError:                    # not Windows
        return None
    k.CreateJobObjectW.restype = ctypes.c_void_p
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    job = k.CreateJobObjectW(None, None)
    limits = _Limits(LimitFlags=0x2000)       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if job and k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) \
            and k.AssignProcessToJobObject(job, int(proc._handle)):
        return job
    if job:
        k.CloseHandle(job)
    return None


def release(job):
    """Close a job from contain(), which ends whatever is still running in it."""
    if job:
        ctypes.windll.kernel32.CloseHandle(job)


def kill_tree(proc, job):
    """End a process and every process it started, and wait for it."""
    if job:
        ctypes.windll.kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        ctypes.windll.kernel32.TerminateJobObject(job, 1)
    with contextlib.suppress(OSError):
        proc.kill()
    proc.wait()


def codex_argv(exe, instructions, out, model, effort):
    """The command line of one Codex call: the system prompt in a file, the reply to `out`."""
    argv = [exe] + CODEX_FLAGS + ['-c', 'model_instructions_file=' + json.dumps(instructions)]
    if model:
        argv += ['-m', model]
    if effort:
        argv += ['-c', f'model_reasoning_effort="{effort}"']
    return argv + ['-o', out, '-']


def run_codex(system_prompt, user_message, model, effort, cancel, seen=None):
    """One call to OpenAI's Codex CLI on the user's ChatGPT plan. Returns its reply or raises
    EngineError. `model` and `effort` are '' for the plan's defaults, and `seen`, when given,
    gets the name of the model that answered.

    The call is made from an empty folder of its own, removed afterwards, with the system
    prompt in a file there and the text on stdin. Codex echoes the whole prompt on stderr, so
    nothing of stderr reaches a message or the log: only the model's name, read from the
    header by one strict pattern, and whether an error line says the sign-in is gone or the
    plan is spent. Cancel and the time limit end Codex and anything it started.
    """
    exe = find_codex()
    if chatgpt_status()['signed_in'] is False:
        raise EngineError('not_logged_in', CODEX_NOT_LOGGED_IN)   # signed out, Codex retries for 16 s
    os.makedirs(DATA_DIR, exist_ok=True)
    work = tempfile.mkdtemp(prefix='codex-', dir=DATA_DIR)
    try:
        instructions, out = os.path.join(work, 'instructions.txt'), os.path.join(work, 'reply.txt')
        with open(instructions, 'w', encoding='utf-8') as fh:
            fh.write(system_prompt)
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stderr:
            stdin.write(user_message.encode('utf-8', 'replace'))
            stdin.seek(0)
            proc = subprocess.Popen(codex_argv(exe, instructions, out, model, effort), stdin=stdin,
                                    stdout=subprocess.DEVNULL, stderr=stderr, cwd=work,
                                    creationflags=NO_WINDOW, env=codex_env())
            job = contain(proc)
            try:
                deadline = time.monotonic() + TIMEOUT
                while proc.poll() is None:
                    if cancel.wait(0.2) or time.monotonic() > deadline:
                        kill_tree(proc, job)
                        if cancel.is_set():
                            raise EngineError('cancelled', STOPPED)
                        raise EngineError('timeout', f'ChatGPT did not answer within {TIMEOUT} seconds. '
                                                     'Try again, or use a shorter text or a lower effort.')
            finally:
                release(job)                  # and with it anything Codex left running
            stderr.seek(0)
            said = stderr.read().decode('utf-8', 'replace').replace('\r\n', '\n')
            lines = said.replace(user_message.replace('\r\n', '\n'), '').split('\n')
        try:
            with open(out, encoding='utf-8') as fh:
                reply = fh.read()
        except OSError:
            reply = ''
    finally:
        for _ in range(10):               # a killed process can hold its folder for a moment
            shutil.rmtree(work, ignore_errors=True)
            if not os.path.exists(work):
                break
            time.sleep(0.2)
    # The header ends at the "user" line that opens the echoed prompt.
    head = lines[:lines.index('user')] if 'user' in lines else []
    name = next((m[1] for m in map(re.compile(r'model: ([\w.\-]{1,64})').fullmatch, head) if m), '')
    if seen is not None:
        seen['model'] = name
    # A setting of CODEX_FLAGS this Codex ignored: the reply came from a model with that tool
    # back on, so it is not used. Only key names the app itself passes reach the log.
    refused = ''
    if any(CODEX_IGNORING.fullmatch(line) for line in lines):
        ours = {flag.split('=', 1)[0] for flag in CODEX_FLAGS if '=' in flag}
        keys = sorted({m[1] for m in map(CODEX_IGNORED_KEY.fullmatch, lines) if m and m[1] in ours})
        log.error('codex ignored settings this app passes: %s', ', '.join(keys) or '?')
        refused = 'Codex ignored settings SlopMonster uses to switch its tools off, so the reply was not used.'
    elif 'user' not in lines:
        found = next((words.format(m[1]) for line in lines for pattern, words in CODEX_REFUSED
                      for m in [pattern.fullmatch(line.strip())] if m), '')
        if found:
            log.error('codex refused the command line: %s', found)
            refused = f'Codex refused the command: {found}.'
    if refused:
        version = cli_version(exe)
        if version and tuple(int(n) for n in version.split('.')) < TESTED_CODEX:
            raise EngineError('cli_old', f'{refused} Codex {version} is older than this app needs. Update '
                                         'Codex, then try again.')
        raise EngineError('engine_changed', f'{refused} This version of Codex'
                                            f'{f" ({version})" if version else ""} needs a newer SlopMonster.')
    if proc.returncode == 0 and reply.strip():
        return reply
    errors = [line for line in lines if line.startswith('ERROR: ')]
    log.error('codex exited %s with %d characters of reply and %d lines on stderr, %d of them errors',
              proc.returncode, len(reply), len(lines), len(errors))
    if any(CODEX_SIGNED_OUT.search(line) for line in errors):
        _codex_status['until'] = 0
        raise EngineError('not_logged_in', CODEX_NOT_LOGGED_IN)
    if any(CODEX_SPENT.search(line) for line in errors):
        raise EngineError('usage_limit', CODEX_LIMIT)
    if model and any(CODEX_NO_MODEL.search(line) for line in errors):
        raise EngineError('bad_model', f'ChatGPT does not offer the model "{model}" on your plan. Pick '
                                       "another in Settings, or use the plan's default.")
    if proc.returncode == 0:
        raise EngineError('empty_output', 'ChatGPT sent back an empty reply. Try again.')
    raise EngineError('engine_failed', 'Codex stopped with an error before ChatGPT answered. Try again, '
                                       'or run "codex" in a terminal to see what it says.')


def call_engine(system, message, engine, cancel):
    """The one place a model is chosen: Claude's run_model or ChatGPT's run_codex, as the run asked.

    `engine` is the run's {'engine', 'model', 'effort'}. run_codex adds 'seen', the model that answered.
    """
    if engine['engine'] == 'chatgpt':
        seen = {}
        reply = run_codex(system, message, engine['model'], engine['effort'], cancel, seen)
        engine['seen'] = seen.get('model') or engine.get('seen', '')
        return reply
    return run_model(system, message, engine['model'], cancel)


def call_model(system, message, engine, cancel, emit):
    """call_engine on a worker thread, with a heartbeat to the page while it works.

    The heartbeat is also how a page that has gone away gets noticed: its write fails, and
    the run is cancelled rather than left to finish a rewrite nobody will read.
    """
    box = {}

    def work():
        try:
            box['reply'] = call_engine(system, message, engine, cancel)
        except EngineError as e:
            box['error'] = e
        except Exception:
            log.exception('the model call failed')
            box['error'] = EngineError('engine_failed', UNEXPECTED)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(1)
            if worker.is_alive():
                emit({'event': 'tick'})
    except CLIENT_GONE:
        cancel.set()
        worker.join(5)                # long enough for the CLI to be killed
        raise
    if cancel.is_set():
        raise EngineError('cancelled', STOPPED)
    if 'error' in box:
        raise box['error']
    return box['reply']


# The checker matches shapes, not meaning, and upstream says to review a match before
# editing it. Without this the model is ordered to rename the unlock button.
UNLESS = (' The exception is a match that is a literal, technical or otherwise necessary use '
          '(a product term, a real list of three things, a quotation). Leave those as they '
          'are and say why in your notes.')


# Text from a Word file is written back into the file block by block, so its shape is fixed.
KEEP = ('This text comes from a Word document and goes back into it block by block. A block is '
        'a paragraph, a heading, a list item or a table row, and blocks are separated by one '
        'blank line. Return exactly the same number of blocks, in the same order. Do not merge, '
        'split, add, remove or reorder blocks, and do not put a blank line inside one. Keep the '
        '"# ", "- " and "1. " markers that open a block and the " | " separators in a table row '
        'as they are. Return a block that needs no change as it is. If the text is a chat reply '
        'around a draft, its framing stays word for word, even where the check above names it: '
        'the opening line to the requester, the option or version labels, and the notes and '
        'offers after the draft. Do not reword any of it, cut a sentence from it or write a new '
        'line about the draft in its place.')


def blocks_of(text):
    """The blocks of a text: what blank lines separate. Paragraphs, headings, list items, rows."""
    return [block for block in re.split(r'\n[ \t]*\n', text) if block.strip()]


def shape_of(text):
    """What must come back as it went out, block for block: the kind of marker and the cell bars.

    The count alone is not enough. A table row returned as "| a | b |", or a list item opened
    with "* ", has the right number of blocks and would be typed into the document as text.
    """
    shape = []
    for block in blocks_of(text):
        marker = re.match(r'\s*(#+ |- |\d+\. )?', block)[1] or ''
        kind = '#' if marker.startswith('#') else '1' if marker[:1].isdigit() else marker[:1]
        shape.append((kind, block.count('|')))
    return shape


def hit_key(category, h):
    """One hit, as it can be looked for in another check of the text."""
    return category['key'], h['label'], str(h.get('detail')), str(h.get('match'))


def user_message(copy, result, proof, context, first, keep=False, again='', drafted=None):
    """What the model is asked this round: the checker's findings, the hard rules, the text.

    `drafted` holds the hit_key() of each hit in the author's draft. After the first round a
    hit that is not among them came from the model's own edit, so the exception for a real
    list of three is not offered for it: a model asked to fix what is left defended its own.
    """
    def hit(h):
        where = f", matched: {h['match']}" if h.get('match') else ''
        return f"{h['label']} ({h['detail']}{where})"

    def listed(added):
        return [f"- {c['title']}: " + '; '.join(hit(h) for h in hits)
                for c in result['categories'] if not c['advisory']
                for hits in [[h for h in c['hits'] if added(c, h)]] if hits]

    ours = drafted is None or first
    todo = listed(lambda c, h: ours or hit_key(c, h) in drafted)
    added = listed(lambda c, h: not ours and hit_key(c, h) not in drafted)
    if added:
        added.insert(0, ('Your last edit added the following, which the draft did not have. '
                         'Reword each one, with no exception, and leave everything else as it is.'))
    if not todo and not added:
        ask = ('An automated pattern check found nothing to flag in this draft. '
               'Edit it as instructed.')
    elif first:
        ask = ('An automated pattern check flagged the following in this draft. '
               'Fix each one as part of your edit.' + UNLESS)
    elif todo:
        ask = ('This text has already been edited once and the pattern check still flags the '
               'following. Fix each one and leave everything else as it is.' + UNLESS)
        todo += [''] + added if added else []
    else:
        ask, todo = 'This text has already been edited once.', added
    rules = ('Never invent a number and never delete one from the draft. A number that appears '
             'only in deleted chat framing goes with it.')
    if proof:
        # Always the numbers from the author's draft, never from a rewrite, so a number the
        # model made up in an earlier round is not handed back to it as one to protect.
        rules = ('The check also found these numbers beside a noun such as "users" or '
                 '"customers": ' + '; '.join(proof) + '. Only the author can verify them. '
                 'Keep each one exactly as written. ' + rules)
    # The checker skips inline code, fenced blocks, struck text and image descriptions,
    # which makes wrapping a flagged phrase in backticks the cheapest way to pass it.
    rules += (' Never move prose into code formatting, strikethrough or an image description '
              'to get it past the check.')
    parts = [f'Context: {context}'] if context else []
    parts += ['\n'.join([ask] + todo), rules]
    if keep:
        parts.append(KEEP + (' ' + again if again else ''))
    parts.append('TEXT TO EDIT:\n' + copy)
    return '\n\n'.join(parts)


def split_reply(reply):
    """(copy, notes, marked) from a model reply, divided at its last marker line.

    The last, so a draft that happens to contain the marker is not cut at its own marker
    line when the model adds one after it. No marker at all means everything is copy, as in
    tools/cleanse.sh: a stray note is something the user can see and delete, and a
    swallowed last paragraph is not. `marked` is False then, and such a result is shown but
    never copied by itself: the text may end in the model's notes, or stop short.
    """
    lines = reply.replace('\r\n', '\n').split('\n')
    at = max((i for i, line in enumerate(lines) if line.rstrip() == MARKER), default=None)
    if at is None:
        return unfenced('\n'.join(lines)), ['The model did not mark where its change notes start. '
                                            'Check the end of the text for stray notes.'], False
    notes = [re.sub(r'^[-*+•·]\s+', '', line.strip()) for line in lines[at + 1:]]
    return unfenced('\n'.join(lines[:at])), [n for n in notes if n], True


def unfenced(copy):
    """The copy, taken out of the code fence when the model wrapped all of it in one.

    The checker skips fenced blocks, so a rewrite delivered that way would score as empty.
    """
    # Blank lines go from the start, but not the spaces that indent a first line: a part of a
    # long text can open with a nested list item.
    copy = re.sub(r'^(?:[ \t]*\n)+', '', copy).rstrip()
    whole = re.fullmatch(r'```[\w-]*\n(.*)\n```', copy, flags=re.S)
    return re.sub(r'^(?:[ \t]*\n)+', '', whole.group(1)).rstrip() if whole and '```' not in whole.group(1) else copy


# What has to come through a rewrite as it was. Each pattern is bounded so that it cannot
# backtrack over a long unbroken run and hold the server, as explained in check().
# A link ends at white space, a bracket, a quote, a dash or an ellipsis, so that tidying the
# punctuation after one is not reported as the link having changed.
_LINK = re.compile(r'https?://[^\s<>()\[\]"\'`|\u2014\u2013\u2026\u201c\u201d\u2018\u2019]+')
_ADDRESS = re.compile(r'(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}')
_PLACEHOLDER = re.compile(r'\[[^\]\[\n]{1,200}\](?![(\[])')    # [needs number], not [a](link)
_LIST_NUMBER = re.compile(r'(?m)^[ \t]*\d{1,3}[.)][ \t]')        # "12. " yes, a wrapped "2026. " no
_ENTITY = re.compile(r'&#[xX]?[0-9A-Fa-f]{1,7};')                # &#8212; is a dash, not the number 8212
_NUMBER = re.compile(r'\d[\d,.]*\d|\d')


def facts(text):
    """The numbers, links, addresses and [placeholders] in a text:
    {comparison key: [as first written, how many times]}."""
    found = {}

    def add(key, shown):
        found.setdefault(key, [shown, 0])[1] += 1

    for pattern in (_LINK, _ADDRESS, _PLACEHOLDER):
        for item in pattern.findall(text):
            item = item.rstrip('.,;:!?*_~')
            add(item, item)
        text = pattern.sub(' ', text)             # so the digits inside a link are not counted again
    text = _ENTITY.sub(' ', text)
    text = _LIST_NUMBER.sub(' ', text)            # "1." opening a line numbers a list, it is not a fact
    for item in _NUMBER.findall(text):
        add(item.replace(',', ''), item)          # 4,000 and 4000 are the same number
    return found


STILL_LINES = 1_000_000   # draft lines times rewrite lines one diff of kept framing may compare


def _still_there(draft, rewrite):
    """The draft's chat framing that the rewrite still has: each piece of it whose whole line
    the rewrite kept word for word, in its place. The draft's lines are lined up with the
    rewrite's, so that of a line in both versions ("The sale runs 8 to 2."), the copy the
    rewrite edited is the one that left, and a notes label that is part of a line is kept
    when its line is."""
    view, spans = deslop.framing_spans(checkable(draft))
    spans = [(start, end) for start, end in spans if view[start:end].strip()]
    if not spans:
        return ''
    starts = [m.start() for m in re.finditer(r'(?m)^', view)]
    lines = view.split('\n')
    worded = [n for n, line in enumerate(lines) if line.strip()]
    mine = [line.strip() for line in deslop.markdown_raw(checkable(rewrite)).split('\n') if line.strip()]
    kept = set()
    if len(worded) * len(mine) <= STILL_LINES:
        for m in difflib.SequenceMatcher(None, [lines[n].strip() for n in worded], mine).get_matching_blocks():
            kept.update(worded[m.a:m.a + m.size])
    else:
        # ponytail: a line diff costs the square of the line count on repetitive lines (62 s
        # at 12,000), so a long text only counts which lines are still there, not where.
        left = collections.Counter(mine)
        for n in worded:
            if left[lines[n].strip()] > 0:
                left[lines[n].strip()] -= 1
                kept.add(n)
    out = []
    for start, end in spans:
        for n in range(bisect.bisect_right(starts, start) - 1, bisect.bisect_left(starts, end)):
            piece = view[max(start, starts[n]):min(end, starts[n] + len(lines[n]))]
            if n in kept and piece.strip():
                out.append(piece.strip())
    return '\n'.join(out)


def carried(draft, rewrite):
    """What the rewrite lost from the draft and what it added.

    The model is told never to invent a number and never to delete one, and to mark a gap
    with a [placeholder] rather than guess. This is that rule checked, for every number,
    link and address. A regex cannot see a changed meaning.

    Counted, not only looked up: "3 days and 3 weeks" turned into "3 days and longer" has
    lost a 3, though a 3 is still there. Such an item is listed as "3 (1 of 2)".

    A draft pasted with the chat reply around it loses that reply on purpose: the option
    labels, the second version, the notes. An item the reply holds as often as the rewrite
    lost it is listed under 'framing' instead of 'lost', a key there only when it has one.
    Only the reply that left counts: one left in place, as in a Word file, explains no loss.
    That is read line by line, from the draft's framing, and not by running the rules on the
    rewrite again: those see a fenced note only beside another tell, and a rewrite that
    rewords the opener would hide the number its notes still hold.
    """
    had, has = facts(draft), facts(rewrite)
    framing = deslop.chat_framing(checkable(draft))
    around, still = facts(framing), facts(_still_there(draft, rewrite))

    def short(a, b, skip=None):
        out = []
        for key, (shown, n) in a.items():
            m = b[key][1] if key in b else 0
            if m < n and (skip is None or skip(key, n - m)):
                out.append(f'{shown} ({n - m} of {n})' if m else shown)
        return out[:12]

    def framed(key, gone):
        return key in around and around[key][1] - (still[key][1] if key in still else 0) >= gone

    # A [placeholder] is compared like the rest, but not counted: it is no number or link.
    warnings = {'lost': short(had, has, lambda key, gone: not framed(key, gone)),
                'new': short(has, had),
                'checked': sum(n for key, (_, n) in had.items() if not key.startswith('['))}
    framing = short(had, has, framed)
    if framing:
        warnings['framing'] = framing
    return warnings


# ── what the rewrite changed ─────────────────────────────────────────────────

# A word or a mark carries the white space after it. Spaces as tokens of their own would
# match everywhere, and every change would be cut into pieces around them.
_TOKEN = re.compile(r'\s+|\w+\s*|[^\w\s]\s*')
_WORD = re.compile(r'\w+')
_SPACE = re.compile(r'\s*')
_INK = re.compile(r'\S*')
_EDGES = re.compile(r'\A(?:[ \t]*\n)+|(?:\n[ \t]*)+\Z')       # blank lines at either end of a block
# An exact diff (autojunk off) takes time that grows with the square of its length. Measured
# on lightly edited prose, 3,000 tokens took 0.23 s exact and 0.013 s with autojunk on, and
# a 200,000-character text that spends the whole budget in diffs that size took 2.4 s. With
# autojunk on, the words most often repeated cannot start a match, which over a long text
# costs a little accuracy. A diff longer than DIFF_EXACT, or past the budget, has it on.
DIFF_BUDGET = 40_000    # tokens of exact diffs for one run
DIFF_EXACT = 3_000
DIFF_MOST = 60_000      # tokens of draft and rewrite together, when they cannot be paired by block


def pieces(text):
    """(lead, blocks, seps, trail): a text cut at its blank lines, with nothing lost.

    blocks is blocks_of(text). The rest is the white space around and between them, so
    lead + blocks[0] + seps[0] + ... + blocks[-1] + trail is the text again.
    """
    lead, blocks, seps, glue = None, [], [], ''
    for i, part in enumerate(re.split(r'(\n[ \t]*\n)', text)):
        if i % 2 or not part.strip():
            glue += part                      # a blank line, or the white space between two
            continue
        if lead is None:
            lead = glue
        else:
            seps.append(glue)
        blocks.append(part)
        glue = ''
    return (text, [], [], '') if lead is None else (lead, blocks, seps, glue)


def _respaced(old, new):
    """Segments for two texts with the same words that differ in white space: every word kept."""
    segs, i, j = [], 0, 0
    while i < len(old) or j < len(new):
        s, t = _SPACE.match(old, i).end(), _SPACE.match(new, j).end()
        segs += [['=', old[i:s]]] if old[i:s] == new[j:t] else [['-', old[i:s]], ['+', new[j:t]]]
        i, j = s, t
        n = _INK.match(old, i).end() - i      # the same run of letters and marks is on both sides
        segs.append(['=', old[i:i + n]])
        i, j = i + n, j + n
    return segs


def diff_words(old, new, budget=None):
    """[[op, text], ...] that turn old into new: '=' kept, '-' taken out, '+' put in.

    Joined, the texts that are not '+' give old and the texts that are not '-' give new.
    `budget` is {'left': tokens} shared by the diffs of one run (see DIFF_BUDGET).
    """
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    budget = {'left': DIFF_BUDGET} if budget is None else budget
    exact = len(a) + len(b) <= min(DIFF_EXACT, budget['left'])
    if exact:
        budget['left'] -= len(a) + len(b)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=not exact).get_opcodes()
    segs, gone, came = [], [], []

    def change():
        taken, put = ''.join(gone), ''.join(came)
        if taken.split() == put.split():      # a line rewrapped, a space doubled: no word changed
            segs.extend(_respaced(taken, put))
        else:
            segs.extend([['-', taken], ['+', put]])
        gone.clear()
        came.clear()

    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        # A lone "a ", "the " or ", " left standing between two changes joins them, so that a
        # rewritten sentence reads as one deletion and one insertion, not a scatter of both.
        if tag == 'equal' and not (i2 - i1 == 1 and len(a[i1].rstrip()) <= 3 and 0 < k < len(ops) - 1):
            if gone or came:
                change()
            segs.append(['=', ''.join(a[i1:i2])])
        else:
            gone.append(''.join(a[i1:i2]))
            came.append(''.join(b[j1:j2]))
    if gone or came:
        change()
    joined = []
    for op, text in segs:
        if text and joined and joined[-1][0] == op:
            joined[-1][1] += text
        elif text:
            joined.append([op, text])
    return joined


def _pairs(text):
    """The word pairs of a block, in lower case."""
    words = [w.lower() for w in _WORD.findall(text)]
    return set(zip(words, words[1:]))


def _sim(a, b):
    """The share of word pairs two blocks have in common. Over the longer of the two: over the
    shorter, a heading whose words the paragraph under it repeats ("## Site induction") scores
    1.0 against that paragraph, more than its own block scores once edited at all."""
    return len(a & b) / max(1, len(a), len(b))


def _lines_up(old, new):
    """Whether same-count block lists pair by position. A rewrite that joined two blocks and
    split another has the same count, but then some changed block shares more word pairs
    with a neighbour's counterpart than with its own, and pairing would swap paragraphs.
    ponytail: neighbours only; a block moved further than that is not caught.
    """
    o, n = [_pairs(b) for b in old], [_pairs(b) for b in new]
    for i in range(len(o)):
        own = _sim(o[i], n[i])
        for j in (i - 1, i + 1):
            if 0 <= j < len(o) and (_sim(o[j], n[i]) > own or _sim(o[i], n[j]) > own):
                return False
    return True


FRAMING_MOST = 12       # blocks of a chat reply's lead-in and closing a rewrite may drop and still pair


def _framed(draft, old, new):
    """Where the rewrite's blocks start among the draft's, when the draft is a pasted chat reply
    whose rewrite dropped the lead-in and closing around it and the rest lines up; else None."""
    extra = len(old) - len(new)
    if not 0 < extra <= FRAMING_MOST or not new or not deslop.chat_reply(checkable(draft)):
        return None
    o, n = [_pairs(b) for b in old], [_pairs(b) for b in new]
    score = [sum(_sim(o[a + i], n[i]) for i in range(len(n))) for a in range(extra + 1)]
    a = max(range(extra + 1), key=score.__getitem__)
    # Every block kept shares words with its own, or is the same: a rule or a lone word set
    # against a sentence is a reply line the rewrite kept, and nothing is paired.
    alike = all(_sim(o[a + i], n[i]) > 0 or old[a + i].strip() == new[i].strip() for i in range(len(n)))
    # What is left over is the reply only when every line of it is, or a rule that fenced the
    # draft: a draft's own greeting or sign-off dropped with it would be shown as the reply,
    # with no way to put it back.
    framing = [line.strip() for line in deslop.chat_framing(checkable(draft)).split('\n') if line.strip()]
    dropped = [deslop.markdown_raw(line) for b in old[:a] + old[a + len(new):] for line in b.split('\n')]
    reply = all(not line or re.fullmatch(r'[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*', line)
                or any(f in line for f in framing) for line in dropped)
    return a if alike and reply and score.count(score[a]) == 1 and _lines_up(old[a:a + len(new)], new) else None


def changes(draft, out):
    """What the rewrite changed, for the page to mark, or None when there is nothing to show.

    When draft and rewrite have the same number of blocks that line up, they are paired block by block,
    which lets the page put any one paragraph back as it was: {'new'} for a block the
    rewrite left alone, {'old', 'new', 'segs'} for one it changed, and the rewrite's own
    white space around them. A pasted chat reply whose rewrite dropped the reply around the
    draft pairs the same way, with that reply as a block or two marked 'gone', which the page
    shows taken out and cannot put back. Otherwise the two texts are one block, diffed whole,
    unless that is too long to do in reasonable time. `kept` is the share of the draft's words, in
    percent, that the rewrite kept where they were.
    """
    if draft == out:
        return None
    budget = {'left': DIFF_BUDGET}
    _, old, dseps, _ = pieces(draft)
    lead, new, seps, trail = pieces(out)
    a = 0 if len(old) == len(new) else _framed(draft, old, new)
    paired = a is not None and len(new) > 0 and _lines_up(old[a:a + len(new)], new)
    if paired:
        # A third blank line between two blocks is left on the block after it, and is not a change to it.
        blocks = [{'new': nb} if _EDGES.sub('', ob) == _EDGES.sub('', nb) else
                  {'old': ob, 'new': nb, 'segs': diff_words(ob, nb, budget)} for ob, nb in zip(old[a:], new)]
        b = a + len(new)
        if a or b < len(old):
            # The chat reply around the draft, with the blank lines it took, before and after.
            head = ''.join(ob + sep for ob, sep in zip(old[:a], dseps))
            tail = ''.join(sep + ob for ob, sep in zip(old[b:], dseps[b - 1:]))
            ends = [{'old': text, 'new': '', 'segs': [['-', text]], 'gone': True} for text in (head, tail)]
            blocks = ends[:1] * bool(a) + blocks + ends[1:] * (b < len(old))
            seps = [''] * bool(a) + seps + [''] * (b < len(old))
            paired = any('segs' in block and 'gone' not in block for block in blocks)
    if not paired:
        if len(_TOKEN.findall(draft)) + len(_TOKEN.findall(out)) > DIFF_MOST:
            return None
        lead, seps, trail = '', [], ''
        blocks = [{'old': draft, 'new': out, 'segs': diff_words(draft, out, budget)}]
    changed = sum('segs' in block and 'gone' not in block for block in blocks)
    if not changed:
        return None                           # only the blank lines between blocks differ
    kept = sum(len(_WORD.findall(block['new'])) for block in blocks if 'segs' not in block)
    kept += sum(len(_WORD.findall(text)) for block in blocks for op, text in block.get('segs', ()) if op == '=')
    return {'paired': paired, 'lead': lead, 'seps': seps, 'trail': trail, 'blocks': blocks,
            'changed': changed, 'kept': round(100 * kept / max(1, len(_WORD.findall(draft))))}


# ── the clipboard ────────────────────────────────────────────────────────────

def _clipboard():
    """(user32, kernel32) with the clipboard open, or (None, None) when it cannot be had."""
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
    except AttributeError:                    # not Windows
        return None, None
    # Spelled out because a handle is 64 bits wide and ctypes assumes a 32-bit int.
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.argtypes, u.GetClipboardData.restype = [ctypes.c_uint], ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = [ctypes.c_uint, ctypes.c_size_t], ctypes.c_void_p
    k.GlobalLock.argtypes, k.GlobalLock.restype = [ctypes.c_void_p], ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalFree.argtypes = [ctypes.c_void_p]
    for _ in range(10):                       # another program may hold it for a moment
        if u.OpenClipboard(None):
            return u, k
        time.sleep(0.03)
    return None, None


class ClipboardBusy(OSError):
    """Another program has the clipboard open, so what is on it cannot be told."""


def clipboard_text():
    """The text on the clipboard, or None when there is none or it cannot be read.

    Raises ClipboardBusy when another program kept it open the whole time it was tried.
    """
    u, k = _clipboard()
    if not u:
        if hasattr(ctypes, 'windll'):
            raise ClipboardBusy('another program has the clipboard open')
        return None
    try:
        handle = u.GetClipboardData(13)       # CF_UNICODETEXT
        locked = k.GlobalLock(handle) if handle else None
        if not locked:
            return None
        try:
            return ctypes.wstring_at(locked)
        finally:
            k.GlobalUnlock(handle)
    finally:
        u.CloseClipboard()


def set_clipboard(text):
    """Put text on the clipboard. True only when reading it back shows it is there."""
    text = text.replace('\r\n', '\n').replace('\n', '\r\n')       # what Windows programs expect
    raw = text.encode('utf-16-le') + b'\0\0'
    u, k = _clipboard()
    if not u:
        return False
    try:
        handle = k.GlobalAlloc(0x0002, len(raw))                  # GMEM_MOVEABLE
        locked = k.GlobalLock(handle) if handle else None
        if not locked:
            if handle:
                k.GlobalFree(handle)
            return False                      # and what was on the clipboard is still there
        ctypes.memmove(locked, raw, len(raw))
        k.GlobalUnlock(handle)
        u.EmptyClipboard()                    # only now, with the new text ready to go in
        if not u.SetClipboardData(13, handle):
            k.GlobalFree(handle)              # on success the clipboard owns the memory
    finally:
        u.CloseClipboard()
    return clipboard_text() == text


# ── the hotkey ───────────────────────────────────────────────────────────────

# Ctrl+Alt+U in any program sends what was just copied to the page for a run. NOREPEAT:
# held down, it fires once. Registered only when serve() is given keys, which only the
# launch from the shortcut does, and only while the page's switch is on.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
HOTKEY = (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord('U'))
WM_QUIT, WM_HOTKEY = 0x0012, 0x0312
EVENT_WAIT = 25          # seconds a GET /api/events waits for something to happen
EVENT_LIFE = 60          # seconds an event waits for the page before it is dropped

_events = []                          # (when, event) for the page, taken by GET /api/events
_events_cond = threading.Condition()
_hotkey = {'keys': None, 'state': 'off', 'thread': None, 'tid': 0}
_hotkey_lock = threading.Lock()


def queue_event(event):
    """Hand the page an event. A newer one replaces any it has not picked up yet: each
    press replaces the text, so only the last would be seen anyway."""
    with _events_cond:
        _events[:] = [(time.monotonic(), event)]
        _events_cond.notify_all()


def take_events(stop, gone=lambda: False):
    """The events for the page, each handed out once, waiting up to EVENT_WAIT seconds for one.

    `gone` says the page that asked is no longer there. A reload leaves the old page's
    request waiting here, and what it took would be lost with it.
    """
    with _events_cond:
        _events_cond.wait_for(lambda: _events or stop.is_set(), EVENT_WAIT)
        if gone():
            return []
        now = time.monotonic()
        taken = [event for when, event in _events if now - when < EVENT_LIFE]
        _events.clear()
        return taken


def hotkey_pressed():
    try:
        text = clipboard_text()
    except ClipboardBusy:
        # Not an empty clipboard: the user did copy something, and a second press may work.
        log.info('hotkey pressed: the clipboard was busy')
        queue_event({'event': 'hotkey', 'busy': True})
        return
    except Exception:
        log.exception('reading the clipboard failed')
        text = None
    log.info('hotkey pressed: chars=%d', len(text or ''))      # the length only, never the text
    queue_event({'event': 'hotkey', 'text': text} if text and text.strip()
                else {'event': 'hotkey', 'empty': True})


def _user32():
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                               wintypes.UINT, wintypes.UINT]
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                              wintypes.UINT]
    u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM]
    return u, wintypes


def _listen(keys, ready):
    """The hotkey's thread. A hotkey belongs to the thread that registers it, and arrives
    in that thread's message queue."""
    u, wintypes = _user32()
    msg = wintypes.MSG()
    u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)    # gives the thread its queue
    _hotkey['tid'] = ctypes.windll.kernel32.GetCurrentThreadId()
    registered = u.RegisterHotKey(None, 1, *keys)
    _hotkey['state'] = 'on' if registered else 'taken'
    ready.set()
    if not registered:
        log.info('the hotkey is held by another program')
        return
    try:
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:     # 0 is WM_QUIT, -1 a fault
            if msg.message == WM_HOTKEY:
                hotkey_pressed()
    finally:
        u.UnregisterHotKey(None, 1)


def set_hotkey(on):
    """Register the hotkey, or let other programs have it, as the page's switch says.

    Returns the state for the page: 'on', 'off', 'taken' when another program holds the
    keys, or 'none' in a launch that has no hotkey.
    """
    with _hotkey_lock:
        if not _hotkey['keys'] or not hasattr(ctypes, 'windll'):
            return 'none'
        thread = _hotkey['thread']
        if thread and not on:
            _user32()[0].PostThreadMessageW(_hotkey['tid'], WM_QUIT, 0, 0)
            thread.join(5)
            _hotkey['thread'] = None
        elif on and not thread:               # off, or taken the last time it was tried
            ready = threading.Event()
            thread = threading.Thread(target=_listen, args=(_hotkey['keys'], ready), daemon=True)
            thread.start()
            ready.wait(5)
            if _hotkey['state'] == 'on':
                _hotkey['thread'] = thread
        if not on:
            _hotkey['state'] = 'off'
        return _hotkey['state']


def split_parts(text, whole=False, joins=None):
    """A long text as parts of about PART_CHARS, cut between paragraphs. A short one is one part.

    With `whole`, a paragraph is never cut, however long: it is going back into a document.
    `joins`, when given, is filled with what stood between each part and the next.

    One model call rewrites a thousand words well and in seconds. Asked for thirty thousand
    at once it is slow, runs past the time limit, and starts to summarise.
    """
    if len(text) <= PART_CHARS * 1.5:
        return [text]
    # A blank line inside a fenced code block divides nothing: the checker pairs the fences,
    # so a part that holds half a block is scored with its code as prose.
    fences = [m.span() for m in re.finditer(r'```.*?```', text, flags=re.S)]
    pieces, start, k = [], 0, 0
    for gap in re.finditer(r'\n[ \t]*\n', text):
        while k < len(fences) and fences[k][1] <= gap.start():
            k += 1
        if k < len(fences) and fences[k][0] < gap.start():
            continue
        pieces.append(text[start:gap.start()])
        start = gap.end()
    pieces.append(text[start:])
    blocks = []                                   # (block, what stood before it)
    for block in pieces:
        before = '\n\n'
        # one enormous paragraph: cut after a sentence or a line, else between two words
        while len(block) > PART_CHARS and not whole and '```' not in block:
            cut = max(block.rfind('. ', 0, PART_CHARS), block.rfind('\n', 0, PART_CHARS)) + 1
            if cut < PART_CHARS // 2:
                cut = block.rfind(' ', PART_CHARS // 2, PART_CHARS) + 1 or PART_CHARS
            piece, rest = block[:cut].rstrip(), block[cut:].lstrip()
            blocks.append((piece, before))
            before, block = block[len(piece):len(block) - len(rest)], rest
        if block.strip():
            blocks.append((block, before))

    def glue(held):
        return held[0][0] + ''.join(before + block for block, before in held[1:])

    parts, held, size = [], [], 0
    for block, before in blocks:
        if held and size + len(block) > PART_CHARS:
            # A heading belongs with what follows it, not at the foot of the part before.
            carry = [held.pop()] if len(held) > 1 and held[-1][0].lstrip().startswith('#') else []
            parts.append(glue(held))
            if joins is not None:
                joins.append((carry or [(block, before)])[0][1])
            held, size = carry, sum(len(b) for b, _ in carry)
        held.append((block, before))
        size += len(block)
    parts.append(glue(held))
    return parts


CUT_REACH = 40          # draft words a reply word may skip ahead to, after the first match


def in_order(words, mine):
    """Where the reply's words fall in the draft's, in order: one index into `words` for each
    word of `mine` that was found, walking the draft once.

    An exact diff takes time that grows with the square of its length: 38 s for a 200,000
    character block of "Yes No N/A", with Stop waiting on it. This takes each reply word's
    next place in the draft, so it finds fewer words than a diff could, never more. A word
    whose next place lies more than CUT_REACH words ahead is taken as edited, and skipped,
    so that a changed word that turns up again in the notes does not carry the walk there."""
    at = collections.defaultdict(list)
    for i, word in enumerate(words):
        at[word].append(i)
    out, here = [], 0
    for word in mine:
        places = at.get(word)
        k = bisect.bisect_left(places, here) if places else 0
        if not places or k == len(places) or (out and places[k] - here > CUT_REACH):
            continue
        out.append(places[k])
        here = places[k] + 1
    return out


def cut_of(draft, reply):
    """Whether a reply is the draft's own blocks up to where the chat reply around them starts:
    a short message kept from above tips that no rule cuts off, lightly edited. Most of its
    words are the draft's, in the draft's order, eight at least, so that "Here is the text:"
    over a code block is not one. It starts and ends with a block of the draft, so that the
    first sentence of a letter is not one, and the draft's next block is the chat reply, so
    that the first paragraph of a letter is not one either."""
    blocks = blocks_of(checkable(draft))
    words, block = [], []                     # the draft's words, and the block each is in
    for n, text in enumerate(blocks):
        found = re.findall(r'\w+', deslop.markdown_prose(text).lower())
        words += found
        block += [n] * len(found)
    mine = re.findall(r'\w+', deslop.markdown_prose(checkable(reply)).lower())
    matched = in_order(words, mine)
    same = len(matched)
    if same < 8 or same < 0.6 * len(mine):
        return False
    # Two words of slack at either end, for an edited first or last word.
    first, last = matched[0], matched[-1]
    start, end = block.index(block[first]), len(block) - 1 - block[::-1].index(block[last])
    if first - start > 2 or end - last > 2:
        return False
    worded = set(block)
    rest = [text for n, text in enumerate(blocks) if n > block[last] and n in worded]
    return bool(rest) and deslop.chat_reply(rest[0])


def rewrite(text, before, engine, context, cancel, emit, system, keep=False):
    """The round loop on one text: up to MAX_ROUNDS model calls, each one checked.

    Returns the best candidate, {'text', 'check', 'notes', 'rounds', 'marked', 'stopped'},
    or raises EngineError when a round failed and none before it had produced one. With
    `keep`, a reply with a different number of blocks is not a candidate, and when no round
    keeps the shape the text comes back as it was. `stopped` is 'no_progress' when the
    rounds ended early because one left as much flagged as a round before it, 'kept' when
    the model sent back unchanged the still-flagged text that is returned, 'added' when the
    draft had nothing flagged and every round added something, so the draft is returned,
    else ''. `rounds` counts the rounds that finished.
    """
    proof = [h['label'] for c in before['categories'] if c['key'] == 'proof' for h in c['hits']]
    drafted = {hit_key(c, h) for c in before['categories'] for h in c['hits']}
    copy, result, notes, best, marked = text, before, [], None, True
    want, again, shape = len(blocks_of(text)), '', shape_of(text)
    fewest, stopped = None, ''                # the fewest flags any round has left so far
    # The draft without the chat reply around it, which PASS 0 deletes: three short options
    # under notes and offers leave a first option far below a quarter of what was pasted.
    # At least one word, so a reply of notes alone is still empty however much was framing.
    floor = max(before['words'] - len(deslop.markdown_prose(deslop.chat_framing(checkable(text))).split()), 1)
    # The rules do not see every end of the draft (tips under a message, copied as plain text),
    # so in a chat answer a reply that is the draft's blocks up to that reply passes too.
    chat = deslop.chat_reply(checkable(text))
    for n in range(1, MAX_ROUNDS + 1):
        emit({'event': 'round', 'round': n, 'of': MAX_ROUNDS})
        sent = copy
        try:
            # "First" until a round is accepted: a reply sent back for its shape edited nothing.
            reply = call_model(system, user_message(copy, result, proof, context, best is None, keep, again, drafted),
                               engine, cancel, emit)
            new, said, found = split_reply(reply)
            checked = check(new)
            if cancel.is_set():               # Stop pressed while the reply was checked
                raise EngineError('cancelled', STOPPED)
            # Counted in scored words, so that a reply with the rewrite tucked inside a code
            # span, or with none at all, is not waved through as having nothing to flag.
            if checked['words'] * 4 < floor and not (chat and cut_of(text, new)):
                raise EngineError('empty_output', 'The model returned almost none of the text '
                                                  'as prose. Try again.')
        except EngineError as e:
            if best is None or cancel.is_set():
                raise
            # A later round failing does not throw away a rewrite that is already in hand.
            best['notes'].append(f'Round {n} did not finish, so this is the result of an '
                                 f'earlier round. {e.message}')
            n -= 1                            # the rounds counted are the ones that finished
            break
        again = ''
        if keep and not found:
            # Without the marker the notes cannot be told from the text, and they would be
            # written into the document as a paragraph.
            again = (f'Your last reply did not have the line {MARKER} after the text. Put it on '
                     'a line of its own, then your notes.')
        elif keep and len(blocks_of(new)) != want:
            # Merged or split paragraphs have no place to go back to.
            again = (f'Your last reply had {len(blocks_of(new))} blocks. It must have exactly '
                     f'{want}, one for each block of the text below.')
        elif keep and shape_of(new) != shape:
            again = ('Your last reply changed how a block opens ("# ", "- ", "1. ") or the " | " '
                     'separators of a table row. Return each block with the marker and the '
                     'separators it has in the text below.')
        if again:
            continue                          # ask again, from the text that still had the shape
        copy, result = new, checked
        # A round that leaves as much flagged as the best one before it has fixed nothing on
        # balance, and the next would most likely do the same.
        flags = flag_count(result)
        stuck = fewest is not None and flags >= fewest
        fewest = flags if fewest is None else min(fewest, flags)
        marked = marked and found             # a later round keeps an earlier round's stray notes
        notes += said
        emit({'event': 'checked', 'round': n, 'check': result})
        flagged = sum(1 for c in result['categories'] if c['hits'] and not c['advisory'])
        if best is None or flagged <= best['flagged']:      # a later round wins a tie
            best = {'flagged': flagged, 'text': copy, 'check': result, 'notes': list(notes)}
        if result['clean']:
            break
        # Text sent back unchanged is the model saying what is left is necessary. Asking
        # again would spend another call on the same answer, and so would Run again on it.
        if copy.strip() == sent.strip():
            stopped = 'kept' if best['text'] == copy else ''
            break
        if stuck and n < MAX_ROUNDS:
            stopped = 'no_progress'
            break
    if best is None:                          # only with `keep`: no round kept the shape
        best = {'text': text, 'check': before, 'as_it_was': True, 'notes': [
            'The rewrite did not keep the paragraphs as they are in the document, so this '
            'text was left as it was.']}
    elif before['clean'] and not best['check']['clean']:
        # The draft is a candidate too: with nothing flagged, it beats a round that added a pattern.
        best, stopped = {'text': text, 'check': before, 'notes': [
            'Each rewrite added a pattern the checker flags, so your text is shown as it was.']}, 'added'
    best.update(rounds=n, marked=marked, stopped=stopped)
    return best


def rewrite_parts(parts, engine, context, cancel, emit, system, keep=False, joins=None):
    """A long text, one part at a time: (text, notes, most rounds a part took, marked, counts).

    A part with nothing flagged is kept as it is, with no model call: over a long document
    that is most of the saving, though a Thorough run rewrites every part. A part that fails
    is kept as it was and named in the notes, unless nothing has been rewritten yet, which
    means the engine is not working at all.
    After three failures the remaining parts are left alone, and so they are after one failure
    that says the engine cannot answer at all now (a usage limit, a lapsed sign-in): the parts
    already rewritten are kept, not thrown away with the run. counts['stopped'] is how many
    rewritten parts ended their rounds early because a round fixed nothing. After a failure
    of that second kind, counts['gone'] is its code, so that the page can say what to do.
    """
    texts, notes, problems, rounds, marked, gone = [], [], [], 0, True, False
    counts = {'of': len(parts), 'rewritten': 0, 'unchanged': 0, 'failed': 0, 'stopped': 0}
    for i, part in enumerate(parts, 1):
        if cancel.is_set():
            raise EngineError('cancelled', STOPPED)
        emit({'event': 'part', 'part': i, 'of': len(parts)})
        before = check(part)
        if before['clean'] and engine.get('strength') != 'thorough' or counts['failed'] >= 3 or gone:
            texts.append(part)
            counts['unchanged'] += 1
            continue
        where = (f'{context} ' if context else '') + (
            f'This is part {i} of {len(parts)} of one long document. Edit this part only, and '
            'do not add an opening or a closing of your own.')
        try:
            best = rewrite(part, before, engine, where, cancel, emit, system, keep)
            if best.get('as_it_was'):
                raise EngineError('shape', best['notes'][0])
        except EngineError as e:
            # A first failure with nothing rewritten yet means the engine is not working. A
            # reply in the wrong shape is not that: the model answered.
            if cancel.is_set() or not counts['rewritten'] and e.code != 'shape':
                raise
            texts.append(part)
            counts['failed'] += 1
            problems.append(f'Part {i} could not be rewritten and is unchanged. {e.message}')
            gone = e.code in ('not_logged_in', 'missing', 'engine_changed', 'cli_old', 'usage_limit',
                              'bad_model')
            if gone:
                counts['gone'] = e.code
                _status['until'] = _codex_status['until'] = 0     # it may be a login problem, so ask again
                if i < len(parts):
                    problems.append('The parts after this one were not tried.')
            elif counts['failed'] == 3 and i < len(parts):
                problems.append('Three parts failed, so the parts after this one were not tried.')
            continue
        texts.append(best['text'])
        counts['rewritten'] += 1
        if best.get('stopped') == 'no_progress':
            counts['stopped'] += 1
        rounds = max(rounds, best['rounds'])
        marked = marked and best['marked']
        notes += [f'Part {i}: {note}' for note in best['notes']]
    if len(notes) > 40:
        notes = notes[:40] + [f'And {len(notes) - 40} more notes.']
    joins = joins if joins and len(joins) == len(texts) - 1 else ['\n\n'] * (len(texts) - 1)
    # What went wrong comes first and is never cut: it says which parts are not rewritten.
    return (texts[0] + ''.join(j + t for j, t in zip(joins, texts[1:])), problems + notes, rounds,
            marked, counts)


def prompt_copy():
    """Where the user's edited rewrite instructions are kept. prompts/cleanse.txt is never
    written: tools/cleanse.sh and its tests read it too."""
    return os.path.join(DATA_DIR, 'cleanse.txt')


def default_instructions():
    """prompts/cleanse.txt as the model is given it."""
    with open(os.path.join(REPO, 'prompts', 'cleanse.txt'), encoding='utf-8') as fh:
        # The prompt ends by announcing the text. Here the text arrives in the user message.
        return fh.read().rstrip().removesuffix('TEXT TO EDIT:').rstrip()


def instructions():
    """(text, custom): the rewrite instructions in use, the user's copy when there is one."""
    try:
        with open(prompt_copy(), encoding='utf-8', errors='replace') as fh:
            text = fh.read().replace('\r\n', '\n').strip()
        if text:
            return text, True
    except FileNotFoundError:
        pass
    except OSError:
        log.exception('reading the edited instructions failed, so the default ones are used')
    return default_instructions(), False


def prompt_base():
    """The default the user's copy was made from, kept beside it, so that a later change to
    prompts/cleanse.txt can be told apart from the user's own edit."""
    return os.path.join(DATA_DIR, 'cleanse.base.txt')


# A copy saved before the base was kept has none. prompts/cleanse.txt did not change while
# the app could save copies without one, so such a copy was made from this text.
UNTRACKED_BASE = '466daa855da9652d'     # sha256 of default_instructions(), cut to 16


def stale_copy():
    """Whether prompts/cleanse.txt has changed since the user's copy was made from it."""
    default = default_instructions()
    try:
        with open(prompt_base(), encoding='utf-8', errors='replace') as fh:
            return fh.read().replace('\r\n', '\n').strip() != default
    except FileNotFoundError:
        return hashlib.sha256(default.encode('utf-8')).hexdigest()[:16] != UNTRACKED_BASE
    except OSError:
        log.exception('reading the default the edited instructions came from failed')
        return False


def guides():
    """references/principles.md and references/signs-of-ai-writing.md, for a Thorough run,
    without what GUIDE_SKIP finds or a heading left with nothing under it. A guide that
    cannot be read is left out: the run still has THOROUGH itself."""
    texts = []
    for name in GUIDES:
        try:
            with open(os.path.join(REPO, 'references', name), encoding='utf-8') as fh:
                blocks = [b.strip() for b in re.split(r'\n[ \t]*\n', fh.read().replace('\r\n', '\n'))]
        except OSError:
            log.exception('reading references/%s failed, so a Thorough run goes without it', name)
            continue
        blocks = [b for b in blocks if b and not GUIDE_SKIP.search(b)] + ['#']
        # A heading's level, or 9 for a block that is not one: a heading followed by one of its
        # own level or above has lost everything under it.
        level = lambda b: len(b) - len(b.lstrip('#')) if b.startswith('#') and '\n' not in b else 9
        texts.append('\n\n'.join(b for b, after in zip(blocks, blocks[1:]) if level(after) > level(b)
                                 or level(b) == 9))
    return texts


def system_prompt(strength='light'):
    """(system prompt, custom) for a run: the instructions in use, and CONTRACT after them
    when they no longer ask for the marker line on its own. A Thorough run has THOROUGH and
    the guides after the instructions, and CONTRACT after those whatever the instructions say."""
    text, custom = instructions()
    text = text.removesuffix('TEXT TO EDIT:').rstrip()
    marked = any(line.rstrip() == MARKER for line in text.split('\n'))   # as split_reply() reads it
    if strength == 'thorough':
        text = '\n\n'.join([text, THOROUGH] + guides())
    if strength == 'thorough' or not marked:
        text += '\n\n' + CONTRACT
    return text, custom


def unslop(text, before, engine, context, cancel, emit, auto=False, may_copy=lambda: False,
           keep=False):
    """Rewrite `text` until the checker passes. Emits every stream event after "start".

    `engine` is {'engine', 'model', 'effort', 'strength'}: which AI rewrites, as call_engine()
    reads it. The done event names the engine and the model that answered. The instructions
    are the user's edited copy when there is one, as system_prompt() gives them.

    `auto` marks a run that started by itself on a paste. Text with nothing flagged is then
    handed back as it is, in no rounds: nobody asked for an edit, and a model call for one
    costs ten seconds or more. Pressing Unslop still gets it, and so does a run sent with the
    page's switch for text that already passes, which arrives with `auto` off, and a Thorough
    run, which always gets at least one round. A [Your Name]
    left to fill in still starts the run: it is what a chat reply's draft most often has, and
    a person's own text rarely does.

    `may_copy` is asked at the end: True when the page wanted the result copied and is still
    there to see it. The result is then copied only when it is safe to use unread: the model
    marked its notes in every round, the text changed and passes the checker, every number and link
    carried over, and the clipboard still holds the pasted draft, so that something the user
    copied in the meantime is never replaced. A Thorough rewrite is never copied: it rewrites
    most sentences and can change how firm a rule or a promise is, which no check here sees.

    `keep` is for text that came out of a Word file and will be written back into it: the
    rewrite must have the same blocks, in the same order.
    """
    started = time.monotonic()
    # Thorough is a request for a real rewrite, which a text that passes still gets.
    if auto and before['clean'] and not placeholders(before) and engine.get('strength') != 'thorough':
        log.info('run skipped: already passes, words=%d score=%s', before['words'], before['score'])
        emit({'event': 'done', 'text': text, 'notes': [], 'before': before, 'after': before,
              'warnings': carried(text, text), 'copied': False, 'rounds': 0,
              'engine': engine['engine'], 'model': engine['model'], 'ms': 0})
        return
    system, custom = system_prompt(engine.get('strength', 'light'))
    joins = []
    parts, counts = split_parts(text, whole=keep, joins=joins), None
    if len(parts) == 1:
        best = rewrite(text, before, engine, context, cancel, emit, system, keep)
        out, after, notes = best['text'], best['check'], best['notes']
        rounds, marked, stopped = best['rounds'], best['marked'], best['stopped']
    else:
        out, notes, rounds, marked, counts = rewrite_parts(parts, engine, context, cancel, emit,
                                                           system, keep, joins)
        after = check(out)
        stopped = counts['stopped']           # for the log: how many parts stopped early
    model = engine.get('seen') or engine['model']      # ChatGPT's default is named once it answers
    if engine['engine'] == 'claude' and _cli_model.get(model) != CLI_MODEL.get(model):
        notes = [ALIAS_NOTE] + notes          # says why Fast was slow
    warnings = carried(text, out)
    copied = False
    # Text that comes back as it was is already on the clipboard: copying it changes nothing.
    if (marked and after['clean'] and out != text and not (warnings['lost'] or warnings['new'])
            and engine.get('strength') != 'thorough' and not cancel.is_set() and may_copy()):
        try:
            held = clipboard_text()
            # The page has made LF of every CRLF and lone CR, as a textarea does.
            same = lambda s: re.sub(r'\r\n?', '\n', s).strip()
            if held is not None and same(held) == same(text):
                copied = set_clipboard(out)
        except ClipboardBusy:
            log.info('copying the result: the clipboard was busy')
        except Exception:                     # a clipboard problem costs the copy, never the result
            log.exception('copying the result failed')
    try:
        change = changes(text, out)
    except Exception:                         # a fault here costs the Changes view, never the result
        log.exception('marking the changes failed')
        change = None
    took = time.monotonic() - started
    done = {'event': 'done', 'text': out, 'notes': notes, 'before': before, 'after': after,
            'warnings': warnings, 'copied': copied, 'rounds': rounds, 'engine': engine['engine'],
            'model': model, 'ms': int(took * 1000)}
    if counts:
        done['parts'] = counts
    elif stopped:
        done['stopped'] = stopped
    if change:
        done['changes'] = change
    # One line per run, for working out later how runs went. Counts and scores only: never
    # the text, the notes or what the checker flagged in it.
    log.info('run done: engine=%s model=%s strength=%s instructions=%s rounds=%d stopped=%s parts=%s '
             'seconds=%.1f words=%d score=%s->%s clean=%s copied=%s word_file=%s', engine['engine'],
             model or '-', engine.get('strength', 'light'), 'edited' if custom else 'default', rounds,
             stopped or '-',
             f"{counts['rewritten']}/{counts['of']}" if counts else '-', took, before['words'],
             before['score'], after['score'], after['clean'], copied, keep)
    emit(done)


# ── HTTP ─────────────────────────────────────────────────────────────────────

_run_lock = threading.Lock()                  # held by the one run allowed at a time
_swap_lock = threading.Lock()
_run = {'cancel': threading.Event()}          # the newest run's cancel switch


class Handler(BaseHTTPRequestHandler):
    timeout = 30      # a connection that never sends a request does not keep its thread

    def log_message(self, fmt, *args):
        # pythonw has no stderr for the default to write to. ascii() because the request
        # line is whatever the client sent, control characters included.
        log.info('%s', ascii(fmt % args))

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        super().end_headers()

    def send(self, status, body, content_type='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, code, message):
        self.send(status, {'error': {'code': code, 'message': message}})

    def send_error(self, code, message=None, explain=None):
        # http.server's own refusals (a malformed request, an unsupported method) are HTML
        # by default. Every error from this server is JSON.
        self.fail(code, 'bad_request', message or 'Bad request.')

    def do_GET(self):
        try:
            self.route()
        except CLIENT_GONE as e:
            log.info('client went away during %s %r: %r', self.command, self.path, e)
        except Exception:
            log.exception('%s %r failed', self.command, self.path)
            with contextlib.suppress(OSError):
                self.fail(500, 'internal', UNEXPECTED)

    do_POST = do_GET

    def route(self):
        path = self.path.split('?')[0]
        body = b''
        if self.command == 'POST':
            # Read the body before refusing anything. Answering while the client is still
            # sending resets the connection on Windows and the refusal never arrives.
            length = self.headers.get('Content-Length', '')
            if not length.isdecimal():
                return self.fail(411, 'bad_request', 'Content-Length is required.')
            # A file to read, or a file plus the text to put back in it, is allowed to be big,
            # but only from the page: a request without the token is refused anyway, and must
            # not be able to make this process hold 30 MB first.
            ours = hmac.compare_digest(self.headers.get('X-Slop-Token', '').encode(), TOKEN.encode())
            big = {'/api/extract': MAX_FILE, '/api/docx': MAX_FILE + MAX_BODY}.get(path, MAX_BODY)
            if int(length) > (big if ours else MAX_BODY):
                # ponytail: refused unread, so a browser may see a reset instead of this 413.
                return self.fail(413, 'too_long', 'That is too much text for one request.')
            body = self.rfile.read(int(length))

        # A DNS-rebinding page reaches this socket under its own hostname, so the Host
        # header must name this server. Browsers attach Origin to cross-site POSTs, and no
        # other site has business here. No CORS headers are ever sent, which leaves every
        # response unreadable to other origins, the page with the token in it included.
        port = self.server.server_address[1]
        hosts = (f'127.0.0.1:{port}', f'localhost:{port}')
        if self.headers.get('Host') not in hosts:
            return self.fail(403, 'forbidden', 'This server only answers to 127.0.0.1.')
        if self.headers.get('Origin', 'http://' + hosts[0]) not in ['http://' + h for h in hosts]:
            return self.fail(403, 'forbidden', 'Requests from other sites are not allowed.')
        # A link, a redirect or window.open from another site, or from another port here, opens
        # the page in the owner's own browser, token and all, and that page could turn the
        # hotkey back on. Browsers say where a request came from; a typed address, the app
        # window's own start and a reload are 'none'. Callers that send no such header pass.
        if self.headers.get('Sec-Fetch-Site', 'none') not in ('none', 'same-origin'):
            return self.fail(403, 'forbidden', 'Requests from other sites are not allowed.')
        if path.startswith('/api/') and path != '/api/ping' and not hmac.compare_digest(
                self.headers.get('X-Slop-Token', '').encode(), TOKEN.encode()):
            return self.fail(403, 'forbidden', 'Missing or wrong token. Reload the page.')
        if self.server.stop.is_set():         # nothing new starts once shutdown has begun
            return self.fail(503, 'shutting_down', 'SlopMonster is shutting down.')

        where = (self.command, path)
        if where in (('GET', '/'), ('GET', '/icon.png')):
            name = 'index.html' if path == '/' else 'icon.png'
            try:
                with open(os.path.join(APP_DIR, name), 'rb') as fh:
                    data = fh.read()
            except FileNotFoundError:
                return self.fail(404, 'not_found', f'app/{name} is missing.')
            if path == '/':
                self.send(200, data.replace(b'__SLOP_TOKEN__', TOKEN.encode()).replace(
                    b'__SLOP_VERSION__', VERSION.encode()), 'text/html; charset=utf-8')
            else:
                self.send(200, data, 'image/png')
        elif where == ('GET', '/api/ping'):
            # The ping needs no token. With a nonce it proves this copy holds the key in
            # instance.json, and says nothing else about the key or whose copy it is.
            reply = {'app': 'slopmonster'}
            nonce = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('n', [])
            key = getattr(self.server, 'key', None)
            if key and len(nonce) == 1 and re.fullmatch(r'[0-9a-fA-F]{32,64}', nonce[0]):
                reply['proof'] = proof(key, nonce[0], port)
            self.send(200, reply)
        elif where == ('GET', '/api/status'):
            self.send(200, {'engines': {'claude': engine_status(), 'chatgpt': chatgpt_status()},
                            'hotkey': _hotkey['state'] if _hotkey['keys'] else 'none'})
        elif where == ('GET', '/api/events'):
            # A long poll: the page asks again as soon as this answers. Shutdown wakes it. The
            # page sends nothing more on this connection, so a readable socket is a closed one.
            self.send(200, {'events': take_events(self.server.stop, lambda: select.select(
                [self.connection], [], [], 0)[0])})
        elif where == ('POST', '/api/hotkey'):
            try:
                data = json.loads(body)
            except (ValueError, RecursionError):
                data = None
            if not isinstance(data, dict) or not isinstance(data.get('on'), bool):
                return self.fail(400, 'bad_request', 'Send a JSON object with "on" set to true or false.')
            self.send(200, {'hotkey': set_hotkey(data['on'])})
        elif where == ('POST', '/api/check'):
            data = self.text_request(body)
            if not data:
                return
            if len(data['text']) > MAX_CHARS:
                return self.fail(413, 'too_long', TOO_LONG)
            if 'draft' not in data:
                return self.send(200, check(data['text']))
            # A result with paragraphs put back as they were: checked again, and compared with
            # the draft for numbers and links as the run's own result was.
            draft = data['draft']
            if not isinstance(draft, str):
                return self.fail(400, 'bad_request', '"draft" must be a string.')
            if len(draft) > MAX_CHARS:
                return self.fail(413, 'too_long', TOO_LONG)
            reply = check(data['text'])
            reply['warnings'] = carried(draft, data['text'])
            self.send(200, reply)
        elif where == ('POST', '/api/unslop'):
            data = self.text_request(body)
            if not data:
                return
            text, engine = data['text'], data.get('engine', 'claude')
            model = data.get('model', 'sonnet' if engine == 'claude' else '')
            effort = data.get('effort', '')
            context = data.get('context') or ''
            if not isinstance(context, str):
                return self.fail(400, 'bad_request', '"context" must be a string.')
            if len(text) > MAX_CHARS:
                return self.fail(413, 'too_long', TOO_LONG)
            before = check(text)
            if not before['words']:
                # Letters the checker did not count are all inside ``` blocks, which stay as they are.
                if re.search(r'[^\W_]', text):
                    return self.fail(400, 'empty', NO_PROSE)
                return self.fail(400, 'empty', 'There is no text to rewrite. Paste some first.')
            if engine not in ENGINES:
                return self.fail(400, 'bad_engine', 'Unknown engine. Pick Claude or ChatGPT.')
            # A name goes on a command line, so only one of the expected shape: Claude's own
            # three or a full Claude model name, and for ChatGPT '' (the plan's default) or a model name.
            if engine == 'claude':
                known = lambda: model in MODELS or CLAUDE_ID.fullmatch(model)
            else:
                known = lambda: model == '' or CODEX_ID.fullmatch(model)
            if not isinstance(model, str) or len(model) > 64 or not known():
                return self.fail(400, 'bad_model', 'Unknown model. Pick another in Settings.')
            if effort not in ('',) + EFFORTS:
                return self.fail(400, 'bad_effort', 'Unknown effort. Pick low, medium, high or xhigh.')
            strength = data.get('strength', 'light')
            if strength not in STRENGTHS:
                return self.fail(400, 'bad_strength', 'Unknown amount of change. Pick light or thorough.')
            # `always` is the page's "Rewrite text that already passes" switch: an automatic
            # run is then never skipped, as if Unslop had been pressed.
            auto = data.get('auto') is True and data.get('always') is not True
            run = {'engine': engine, 'model': model, 'effort': effort if engine == 'chatgpt' else '',
                   'strength': strength}
            self.stream(text, before, run, ' '.join(context.split()),
                        auto=auto, to_clipboard=data.get('copy') is True,
                        keep=data.get('structure') is True)
        elif where == ('GET', '/api/prompt'):
            text, custom = instructions()
            self.send(200, {'text': text, 'custom': custom, 'stale': custom and stale_copy()})
        elif where == ('POST', '/api/prompt'):
            self.save_prompt(body)
        elif where == ('POST', '/api/extract'):
            # The name only picks a parser. It is cut down to a bare file name first.
            name = urllib.parse.unquote(self.headers.get('X-Slop-Name', ''))
            self.extract(os.path.basename(name.replace('\\', '/'))[-200:] or 'file', body)
        elif where == ('POST', '/api/docx'):
            self.save_docx(body)
        elif where == ('POST', '/api/login'):
            try:
                data = json.loads(body) if body.strip() else {}
            except (ValueError, RecursionError):
                data = None
            if not isinstance(data, dict) or data.get('engine', 'claude') not in ENGINES:
                return self.fail(400, 'bad_request', 'Send a JSON object with "engine" set to "claude" or "chatgpt".')
            try:
                opened = start_sign_in(data.get('engine', 'claude'))
            except EngineError as e:
                return self.fail(409, e.code, e.message)
            self.send(200, {'ok': True, 'opened': opened})
        elif where == ('POST', '/api/shutdown'):
            self.send(200, {'ok': True})
            self.server.stop.set()            # serve() does the rest, off this thread
        else:
            self.fail(404, 'not_found', 'No such page.')

    def save_prompt(self, body):
        """Answer POST /api/prompt: {"text"} keeps the user's copy of the rewrite instructions,
        and {"reset": true} deletes it, so that runs use prompts/cleanse.txt again. Text that
        is the default word for word is a reset too: there is then nothing of the user's to keep."""
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or not (data.get('reset') is True or isinstance(data.get('text'), str)):
            return self.fail(400, 'bad_request', 'Send a JSON object with a "text" string, or "reset" set to true.')
        text = '' if data.get('reset') is True else data['text'].replace('\r\n', '\n').strip()
        if data.get('reset') is not True:
            if not text:
                return self.fail(400, 'empty_prompt', 'The instructions are empty. Write some, or reset them '
                                                      'to the default.')
            if len(text) > MAX_PROMPT:
                return self.fail(413, 'too_long', f'The instructions are {len(text):,} characters long. The '
                                                  f'limit is {MAX_PROMPT:,}.')
        default = default_instructions()
        try:
            if text and text != default:
                os.makedirs(DATA_DIR, exist_ok=True)
                # A new copy is made from the default in use now, and that is kept as its base.
                # A later save of the same copy keeps the old base: an edit to a copy that is
                # out of date does not bring it up to date.
                files = [(prompt_copy(), text)]
                if not instructions()[1]:
                    files.insert(0, (prompt_base(), default))
                for path, content in files:
                    with open(path + '.new', 'w', encoding='utf-8', newline='\n') as fh:
                        fh.write(content + '\n')
                    os.replace(path + '.new', path)     # never half a file
            else:
                for path in (prompt_copy(), prompt_base()):
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(path)
        except OSError:
            log.exception('saving the instructions failed')
            return self.fail(500, 'not_saved', f'The instructions could not be saved. The details are in '
                                               f'{LOG_AT}.')
        custom = bool(text) and text != default
        self.send(200, {'text': text or default, 'custom': custom, 'stale': custom and stale_copy()})

    def extract(self, name, body):
        """Answer /api/extract: the text of an uploaded Word, PDF or text file.

        A file is somebody else's bytes, so it is read in a process of its own. One that
        hangs or eats memory is killed there, and this server and its Turn off carry on.
        """
        try:
            done = subprocess.run(DOCUMENTS_CMD + ['extract', name], input=body,
                                  capture_output=True, timeout=90, cwd=DATA_DIR,
                                  creationflags=NO_WINDOW)
            reply = json.loads(done.stdout)
            if not isinstance(reply, dict):
                raise ValueError('not an object')
        except subprocess.TimeoutExpired:
            return self.fail(422, 'unreadable', 'That file took too long to read.')
        except (OSError, ValueError):
            log.exception('reading %r failed', name)
            return self.fail(422, 'unreadable', 'That file could not be read.')
        text = reply.get('text')
        if reply.get('ok') is not True or not isinstance(text, str):
            return self.fail(422, str(reply.get('code') or 'unreadable'),
                             str(reply.get('message') or 'That file could not be read.'))
        if len(text) > MAX_CHARS:
            return self.fail(413, 'too_long', f'That file holds {len(text):,} characters of text. '
                                              f'The limit is {MAX_CHARS:,}.')
        self.send(200, {'text': text, 'kind': str(reply.get('kind') or ''),
                        'summary': str(reply.get('summary') or ''),
                        'notes': [str(n) for n in reply.get('notes') or []]})

    def save_docx(self, body):
        """Answer /api/docx: the rewrite as a Word file.

        A JSON body {"text"} asks for a new document, and {"text", "rows": true} one whose
        text was read from a Word file, where "cell | cell" paragraphs are a table's rows. Any other body is the Word file the
        text came from, behind a four-byte length and that same JSON: the answer is then a
        copy of that file with only its text changed. The file is somebody else's bytes, so
        that work is done in a process of its own, as in extract().
        """
        if self.headers.get('Content-Type', '').startswith('application/json'):
            data = self.text_request(body)
            if data and len(data['text']) > MAX_CHARS:
                return self.fail(413, 'too_long', TOO_LONG)
            if data:
                import documents              # here, so a fault in it cannot stop the app starting
                self.send(200, documents.write_docx(data['text'], word_rows=data.get('rows') is True), DOCX)
            return
        try:
            done = subprocess.run(DOCUMENTS_CMD + ['rewrite'], input=body,
                                  capture_output=True, timeout=120, cwd=DATA_DIR,
                                  creationflags=NO_WINDOW)
            head, _, docx = done.stdout.partition(b'\n')
            reply = json.loads(head)
            if not isinstance(reply, dict):
                raise ValueError('not an object')
        except subprocess.TimeoutExpired:
            return self.fail(422, 'unreadable', 'That file took too long to write.')
        except (OSError, ValueError):
            log.exception('writing the Word file failed')
            return self.fail(422, 'unreadable', 'The Word file could not be written.')
        if reply.get('ok') is not True or not docx:
            return self.fail(422, str(reply.get('code') or 'unreadable'),
                             str(reply.get('message') or 'The Word file could not be written.'))
        self.send_response(200)
        self.send_header('Content-Type', DOCX)
        self.send_header('Content-Length', str(len(docx)))
        # What could not be put back, for the page to show. A header holds only plain ASCII.
        self.send_header('X-Slop-Notes', urllib.parse.quote(json.dumps(
            [str(n) for n in reply.get('notes') or []])))
        self.end_headers()
        self.wfile.write(docx)

    def text_request(self, body):
        """The JSON body as a dict with a "text" string, or None once it has been refused."""
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):  # RecursionError: a body of 100,000 open brackets
            data = None
        if not isinstance(data, dict) or not isinstance(data.get('text'), str):
            return self.fail(400, 'bad_request', 'Send a JSON object with a "text" string.')
        return data

    def stream(self, text, before, engine, context, auto, to_clipboard, keep):
        """Answer /api/unslop with one JSON line per event, written as each one happens."""
        started = time.monotonic()
        cancel = threading.Event()
        with _swap_lock:
            previous, _run['cancel'] = _run['cancel'], cancel
        previous.set()                        # one run at a time, and the newest wins
        # The length is unknown until the last round, so this is an HTTP/1.0-style reply:
        # no Content-Length, and the end of the stream is the connection closing.
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.send_header('Connection', 'close')
        self.end_headers()

        def emit(event):
            self.wfile.write(json.dumps(event).encode() + b'\n')
            self.wfile.flush()

        try:
            emit({'event': 'start', 'before': before, 'rounds': MAX_ROUNDS})
            with _run_lock:                   # waits here while the run it replaced winds down
                if cancel.is_set():
                    raise EngineError('cancelled', STOPPED)
                # The page sends nothing after its request, so a readable socket is a closed
                # one: Cancel, a closed window. A write only notices that a second late.
                unslop(text, before, engine, context, cancel, emit, auto, lambda: to_clipboard
                       and not select.select([self.connection], [], [], 0)[0], keep)
        except EngineError as e:
            _status['until'] = _codex_status['until'] = 0     # it may be a login problem, so ask again
            # The code only: a message can quote the model's reply, which can quote the text.
            log.info('run failed: code=%s engine=%s model=%s seconds=%.1f', e.code, engine['engine'],
                     engine.get('seen') or engine['model'] or '-', time.monotonic() - started)
            emit({'event': 'error', 'error': {'code': e.code, 'message': e.message}})
        except CLIENT_GONE:
            raise
        except Exception:
            log.exception('unslop failed')
            emit({'event': 'error', 'error': {'code': 'engine_failed', 'message': UNEXPECTED}})


# ── lifecycle ────────────────────────────────────────────────────────────────

# Chrome first. On a new profile Edge signs itself in to the Windows user's Microsoft
# account and starts syncing their passwords and history into it; Chrome has no such step.
# A Chrome installed without admin rights is in the account's own LOCALAPPDATA.
_CHROME = os.path.join('Google', 'Chrome', 'Application', 'chrome.exe')
_EDGE = os.path.join('Microsoft', 'Edge', 'Application', 'msedge.exe')
_PF = os.environ.get('ProgramFiles') or r'C:\Program Files'
_PF86 = os.environ.get('ProgramFiles(x86)') or r'C:\Program Files (x86)'
BROWSERS = tuple(os.path.join(root, exe) for root, exe in (
    (_PF, _CHROME), (_PF86, _CHROME), (os.environ.get('LOCALAPPDATA'), _CHROME),
    (_PF86, _EDGE), (_PF, _EDGE)) if root)


def launch_window(url):
    """Open the page in an app window. True when watch_window can follow it, False for a tab."""
    exe = next((p for p in BROWSERS if os.path.isfile(p)), None)
    if not exe:
        webbrowser.open(url)                  # a tab in the default browser
        return False
    profile = os.path.join(DATA_DIR, 'browser-profile')
    # Sessions is where the browser keeps its windows to restore them, with what was typed in
    # them: the page's boxes now say not to keep that, but versions before that did, and the
    # files carry it from one launch to the next. Nothing here restores a window, so the folder
    # goes, while no browser holds the profile.
    if not os.path.exists(os.path.join(profile, 'lockfile')):
        shutil.rmtree(os.path.join(profile, 'Default', 'Sessions'), ignore_errors=True)
    # A profile of its own: the window is then the only thing that browser instance does,
    # and its lock file belongs to this app alone.
    # msImplicitSignin off is what stops Edge signing the profile in (measured: without it
    # the account is attached within seconds and a "now syncing" dialog covers the window;
    # --disable-sync alone does not prevent that). Do not drop either flag.
    # Component updates and OptimizationHints are what a browser downloads into a new profile
    # for itself (measured on a new profile: Chrome 103 MB and Edge 476 MB with them, about
    # 20 MB without).
    # Every feature goes in the one --disable-features: a second one replaces the first.
    subprocess.Popen(
        [exe, f'--app={url}', '--user-data-dir=' + profile,
         '--disable-features=msImplicitSignin,OptimizationHints', '--disable-sync',
         '--disable-component-update',
         '--no-first-run', '--no-default-browser-check', '--disable-background-mode',
         '--hide-crash-restore-bubble', '--window-size=1180,820'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def watch_window(stop):
    """Shut the server down when the app window is closed.

    The process launch_window starts is no use for this: Edge re-launches itself and that
    first process exits at once. What does track the window is <profile>/lockfile. The
    browser holds it open for as long as it runs on the profile, and Windows deletes it
    when the browser exits, however it exits.
    """
    lock = os.path.join(DATA_DIR, 'browser-profile', 'lockfile')
    while not os.path.exists(lock):       # no time limit: a late window still gets watched
        if stop.wait(0.5):
            return
    while os.path.exists(lock):
        if stop.wait(0.5):
            return
    log.info('the app window was closed')
    stop.set()


def bind(port):
    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler, bind_and_activate=False)
    # http.server asks for SO_REUSEADDR, and on Windows that lets a second process listen
    # on a port that is already taken. A second launch has to fail here to find the first.
    httpd.allow_reuse_address = False
    # A connection dropped before its request arrived is routine (browsers open spares),
    # and the default handler prints a traceback to a stderr that pythonw does not have.
    httpd.handle_error = lambda *client: log.info('connection dropped: %r', sys.exc_info()[1])
    try:
        httpd.server_bind()
        httpd.server_activate()
    except OSError:
        httpd.server_close()
        raise
    return httpd


def proof(key, nonce, port):
    """What the copy holding `key` answers a ping on `port` that carries `nonce`.

    The port is part of it, so a program on another port that passes the nonce on to the
    real copy gets an answer that does not fit its own port.
    """
    return hmac.new(key, f'{nonce}:{port}'.encode('ascii'), hashlib.sha256).hexdigest()


def read_instance(path=None):
    """(port, key) from instance.json: the copy of this account that is running, or that
    stopped without removing it. None when there is no such file or it cannot be read."""
    try:
        with open(path or os.path.join(DATA_DIR, 'instance.json'), encoding='utf-8') as fh:
            data = json.loads(fh.read(4096))  # the real one is under 120 characters
        port, key = data['port'], bytes.fromhex(data['key'])
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    return (port, key) if type(port) is int and 0 < port < 65536 and len(key) == 32 else None


def write_instance(path, port, key):
    """Note this copy's port and key in instance.json, the whole file or none of it.

    DATA_DIR is in the user's own profile, which other Windows accounts cannot read. Every
    account shares 127.0.0.1, and any program can answer a ping, so the key is how a launch
    tells this account's copy from everything else.
    """
    fd, part = tempfile.mkstemp(prefix='instance.', suffix='.tmp', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump({'port': port, 'key': key.hex()}, fh)
        for attempt in range(10):         # a launch reading it holds it for a moment
            try:
                os.replace(part, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
    except OSError as e:
        log.warning('instance.json could not be written, so a second launch will not find this copy: %r', e)
        with contextlib.suppress(OSError):
            os.remove(part)


def forget_instance(path, key):
    """Remove instance.json, if it still names this copy and not a newer one."""
    # ponytail: a copy that starts between the read and the remove loses its file. Relaunch.
    held = read_instance(path)
    if held and hmac.compare_digest(held[1], key):
        with contextlib.suppress(OSError):
            os.remove(path)


def running_copy(port, wait=2):
    """True when this account's copy of this app holds this port; False when something else
    holds it (it answered, or kept quiet for `wait` seconds); None when the connection was
    refused or dropped, as by a copy that is stopping.

    Only the copy that instance.json names, and that proves it holds the key noted there for
    a nonce chosen here, is this account's. A reply with no proof or a wrong one, from an
    older build, or with no instance.json to check it against, is something else.

    `wait` bounds the whole exchange, not each read: a holder that sends its reply a byte at
    a time is something else too, and the launch goes on without it.
    """
    nonce = secrets.token_hex(32)
    got, socks = [], []

    def ask():
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=wait)
        try:
            conn.connect()
            socks.append(conn.sock)
            conn.request('GET', '/api/ping?n=' + nonce)
            got.append(conn.getresponse().read(4097))
        except socket.timeout:
            got.append(False)
        except OSError:
            got.append(None)
        except Exception:                      # a reply HTTP cannot read, or one cut off here
            got.append(False)
        finally:
            conn.close()

    asking = threading.Thread(target=ask, daemon=True)
    asking.start()
    asking.join(wait)
    reply = got[0] if got else False          # taken before the shutdown below cuts off a read
    for sock in socks:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)    # frees a read still stuck on a slow holder
    if not isinstance(reply, bytes):
        return reply
    if len(reply) > 4096:                      # a real reply is about 100 bytes
        return False
    try:
        data = json.loads(reply)
    except (ValueError, RecursionError):
        return False
    # Read only now. The copy that answers wrote its file before it began to answer, and one
    # that started a moment before this launch may not have written it when the ping went.
    held = read_instance()
    answer = data.get('proof') if isinstance(data, dict) else None
    return bool(held and held[0] == port and isinstance(answer, str) and hmac.compare_digest(
        answer.encode('utf-8'), proof(held[1], nonce, port).encode('ascii')))


def already_running(port):
    """Is whatever holds this port this account's copy of this app?"""
    return running_copy(port) is True


def bind_soon(port):
    """bind(port), tried for a second. None if it stays taken."""
    for _ in range(10):
        time.sleep(0.1)
        with contextlib.suppress(OSError):
            return bind(port)
    return None


def serve(port=PORT, open_window=True, hotkey=None):
    """Run the app until the page asks it to shut down or its window is closed.

    `hotkey` is the keys the page's switch registers, as (modifiers, virtual key). Only the
    launch from the shortcut passes HOTKEY. Without it the switch does nothing, so tests and
    a development run never take Ctrl+Alt+U from the user.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    # Under pythonw sys.stdout and sys.stderr are None. The log file is the only place an
    # error can go, so everything is sent there.
    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.handlers.RotatingFileHandler(
            os.path.join(DATA_DIR, 'server.log'), maxBytes=500_000, backupCount=1,
            encoding='utf-8', delay=True)])
    def join(at):
        """Open one more window on this account's copy that is there, and leave."""
        url = f'http://127.0.0.1:{at}/'
        log.info('already running at %s', url)
        if open_window:
            # ponytail: if that instance is mid-shutdown the new window finds nothing. Relaunch.
            launch_window(url)
        else:
            print(f'SlopMonster is already running at {url}', flush=True)

    # The port instance.json names first: when every port below was taken, the copy took
    # any free one, and this is the only way to find it.
    held = read_instance() if port else None
    if held and running_copy(held[0]) is True:
        return join(held[0])
    # PORT, or when something else has it, the first free one of the few after it: a second
    # launch has to find the first, and the page keeps its settings per port. Each one is
    # looked at before one is kept, since a copy on PORT+1 outlives whatever had PORT.
    httpd = None
    for candidate in range(port, port + FALLBACKS + 1) if port else [0]:
        try:
            bound = bind(candidate)
        except OSError as err:
            if err.errno != errno.EADDRINUSE:
                continue                      # a port Windows keeps back for itself
            # A copy here answers in milliseconds. Once a port is in hand, a listener that keeps
            # quiet is not worth the full wait on every launch.
            found = running_copy(candidate, 0.3 if httpd else 2)
            if found is True:
                if httpd:
                    httpd.server_close()
                return join(candidate)
            # A copy that is stopping refuses or drops the ping, and holds its port for half a
            # second more: worth the wait. Anything that answered or kept quiet is not a
            # copy that is stopping, and a port in hand already needs no wait either.
            bound = None if found is not None or httpd else bind_soon(candidate)
            if not bound:
                continue
        if httpd:
            bound.server_close()
        else:
            httpd = bound
    if not httpd:
        httpd = bind(0)                       # every one of them is taken: any free port
    url = f'http://127.0.0.1:{httpd.server_address[1]}/'
    httpd.stop = stop = threading.Event()
    # The socket already listens, so the page's first requests, and a second launch's ping,
    # wait for serve_forever, by when the key is in place.
    httpd.key = secrets.token_bytes(32)
    noted = os.path.join(DATA_DIR, 'instance.json')
    write_instance(noted, httpd.server_address[1], httpd.key)
    windowed = open_window and launch_window(url)
    if open_window and not windowed:
        hotkey = None                         # a tab: its closing tells this process nothing
    with _hotkey_lock:
        _hotkey.update(keys=hotkey, state='off', thread=None)
    with _events_cond:
        _events.clear()
    log.info('serving %s', url)
    if not open_window:
        print(f'SlopMonster is running at {url}', flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if windowed:
        threading.Thread(target=watch_window, args=(stop,), daemon=True).start()

    try:
        while not stop.wait(0.5):             # polling keeps Ctrl+C working in a Windows console
            pass
    except KeyboardInterrupt:
        stop.set()
    log.info('shutting down')
    with _events_cond:
        _events_cond.notify_all()             # a waiting GET /api/events answers now
    set_hotkey(False)
    _hotkey['keys'] = None
    httpd.shutdown()                          # safe here: serve_forever is on another thread
    httpd.server_close()                      # a relaunch can have the port from here on
    forget_instance(noted, httpd.key)
    _run['cancel'].set()
    if _run_lock.acquire(timeout=5):          # free once the run has killed its model process
        _run_lock.release()
    log.info('stopped')


def sign_in_window(argv):
    """SlopMonster.exe --sign-in claude|chatgpt <the CLI's sign-in command>: what python.exe
    runs from SIGN_IN in a source checkout, for an exe that has no python.exe beside it."""
    if sys.stdout is None:
        # The exe is a windowed program, so Windows gave it no console. It opens its own,
        # and the CLI started below shares it.
        ctypes.windll.kernel32.AllocConsole()
        sys.stdout = sys.stderr = open('CONOUT$', 'w')
        sys.stdin = open('CONIN$')
    sys.argv = [sys.argv[0]] + argv[1:]
    exec(CODEX_SIGN_IN if argv[:1] == ['chatgpt'] else SIGN_IN, {'__name__': '__main__'})


def attach_console():
    """Take on the console SlopMonster.exe was typed in, if there is one. The one-file exe
    runs as the child of PyInstaller's unpacker, which has no console, so it is the
    unpacker's parent's."""
    class Entry(ctypes.Structure):            # PROCESSENTRY32W
        _fields_ = [('size', ctypes.c_ulong), ('usage', ctypes.c_ulong), ('pid', ctypes.c_ulong),
                    ('heap', ctypes.c_size_t), ('module', ctypes.c_ulong), ('threads', ctypes.c_ulong),
                    ('ppid', ctypes.c_ulong), ('priority', ctypes.c_long), ('flags', ctypes.c_ulong),
                    ('exe', ctypes.c_wchar * 260)]
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k.CreateToolhelp32Snapshot(2, 0)   # TH32CS_SNAPPROCESS
    if snap in (None, ctypes.c_void_p(-1).value):
        return False
    entry, unpacker = Entry(), os.getppid()
    entry.size = ctypes.sizeof(Entry)
    try:
        found = k.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while found and entry.pid != unpacker:
            found = k.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    finally:
        k.CloseHandle(ctypes.c_void_p(snap))
    return bool(found and k.AttachConsole(entry.ppid))


def sweep_unpacked(own, age=600):
    """Remove the folders earlier copies of the one-file exe left in TEMP. Its unpacker
    removes the folder it unpacked to once the app ends, but not if the unpacker itself was
    ended first, from Task Manager or by a power cut. A folder younger than age seconds may
    still be unpacking, and one in use is left alone: Windows will not delete the python3NN.dll
    a running copy has loaded."""
    base = os.path.dirname(own)
    try:
        names = os.listdir(base)
    except OSError:
        return []
    swept = []
    for name in names:
        folder = os.path.join(base, name)
        if (not name.startswith('_MEI') or os.path.normcase(folder) == os.path.normcase(own)
                or not os.path.isfile(os.path.join(folder, 'app', 'index.html'))
                or not os.path.isfile(os.path.join(folder, 'prompts', 'cleanse.txt'))):
            continue
        try:
            if time.time() - os.path.getmtime(folder) < age:
                continue
            for dll in os.listdir(folder):
                if re.fullmatch(r'python3\d+\.dll', dll, re.I):
                    os.remove(os.path.join(folder, dll))
        except OSError:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        swept.append(folder)
    return swept


def main(argv):
    """Start the app, or do one of the two jobs SlopMonster.exe runs itself for."""
    global DATA_DIR
    if argv[1:2] == ['--documents']:          # DOCUMENTS_CMD, in the exe
        import documents
        return documents.main(argv[:1] + argv[2:])
    if argv[1:2] == ['--sign-in']:            # start_sign_in, in the exe
        return sign_in_window(argv[2:])
    import argparse
    parser = argparse.ArgumentParser(prog='SlopMonster', description='Paste text, get it unslopped.')
    parser.add_argument('--no-window', action='store_true',
                        help='open no window and register no hotkey; print the address instead')
    parser.add_argument('--port', type=int, default=PORT,
                        help=f'for testing: the first port to try (default {PORT}; 0 takes any free port)')
    parser.add_argument('--data-dir', help='for testing: keep the log, instance.json, the browser '
                                           'profile and saved instructions here instead')
    parser.add_argument('--version', action='version', version=f'SlopMonster GUI {VERSION}')
    # The exe is a windowed program, so typed in a console it has no stdout: --version, --help,
    # a wrong option and --no-window's address would go nowhere. It writes to that console.
    attached = FROZEN and sys.stdout is None and attach_console()
    if attached:
        sys.stdout = sys.stderr = open('CONOUT$', 'w')
    args = parser.parse_args(argv[1:])
    if not 0 <= args.port <= 65535 - FALLBACKS:
        parser.error(f'--port takes 0 to {65535 - FALLBACKS}')
    if args.data_dir:
        DATA_DIR = os.path.abspath(args.data_dir)
    window = not args.no_window
    if attached and window:
        # The window prints nothing, and closing that console or Ctrl+C in it must not stop it.
        sys.stdout.close()
        sys.stdout = sys.stderr = None
        ctypes.windll.kernel32.FreeConsole()
    if FROZEN:
        threading.Thread(target=sweep_unpacked, args=(sys._MEIPASS,), daemon=True).start()
    serve(port=args.port, open_window=window, hotkey=HOTKEY if window else None)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except Exception:
        log.exception('fatal')
        raise
