# SlopMonster app

A small Windows app around the SlopMonster checker. Paste text into the window, or open
a Word or PDF file. The text is scored out of 5 and then rewritten by ChatGPT or Claude,
whichever you choose, for up to three rounds, until the checker passes. A run also stops when a round leaves as much
flagged as the round before it. Copy the result, or save it back into the Word file it came
from with the formatting kept.

![The app after a run: the pasted text and its score on the left, the rewrite on the right](../docs/img/app-dark.png)

## What you need

- Windows 10 or 11.
- Chrome or Edge. Without either, the page opens as a tab in your default browser, with
  no Ctrl+Alt+U switch, and closing that tab leaves SlopMonster running in the
  background until you open it again and press Turn off.
- At least one of two command-line tools, each signed in. The app has a button to sign
  each one in.
  - OpenAI's Codex command-line tool, signed in with ChatGPT, to rewrite with ChatGPT.
    Rewrites count against your ChatGPT plan.
  - The [Claude Code](https://claude.com/claude-code) command-line tool, to rewrite with
    Claude. Rewrites count against your Claude plan.

SlopMonster.exe needs nothing else. Running it from source also needs Python; see
[Run from source](#run-from-source).

## Install the app

### 1. Install Claude Code, Codex, or both

SlopMonster does not include either tool. It runs the copy installed on your PC, under
your own account.

- **Claude Code.** Install it with its Windows installer, as the
  [Claude Code docs](https://code.claude.com/docs/en/setup) describe. In PowerShell
  that is `irm https://claude.ai/install.ps1 | iex`. `winget install Anthropic.ClaudeCode`
  works too. The app finds `claude.exe` in the folder each of them uses, even when that
  folder is not on PATH. The app cannot run the npm copy of Claude Code (`claude.cmd`),
  and says so when that is the only one found.
- **Codex.** Install OpenAI's Codex command-line tool as OpenAI describes it, for example
  `npm install -g @openai/codex`, which needs Node.js. The app finds the `codex.exe`
  inside that package, or the one from Codex's own Windows installer.

If SlopMonster is open while you install, it looks again when you come back to its
window. A terminal opened before the install does not see the new command, so open a
new one.

### 2. Sign in

You can sign in from the app: open Settings and press Sign in to Claude or Sign in to
ChatGPT. A console window opens and your browser asks you to approve the sign-in. To do
it in a terminal instead, run `claude auth login`, or `codex login` and choose to sign
in with ChatGPT. SlopMonster never sees your password or account details.

Claude Code's PowerShell installer puts `claude.exe` in `%USERPROFILE%\.local\bin` and
does not add that folder to PATH, so a terminal does not know `claude` until you add it.
Until then, run `& "$env:USERPROFILE\.local\bin\claude.exe" auth login` instead.

### 3. Download SlopMonster.exe

Download `SlopMonster.exe` from the latest release on the
[Releases page](https://github.com/bth0mp/slopmonster-gui/releases). There is no
installer: the file is the app. Put it in a folder where it can stay, such as
`%LOCALAPPDATA%\Programs\SlopMonster`, since a shortcut stops working when the file moves.

Each release also has `SHA256SUMS.txt`. To check the download, compare its line with:

```powershell
(Get-FileHash .\SlopMonster.exe -Algorithm SHA256).Hash.ToLower()
```

### 4. The first launch: "Windows protected your PC"

The exe is not code-signed, because a signing certificate costs money every year. Windows
SmartScreen therefore warns about it the first time you open it: a blue window says
"Windows protected your PC" and that it prevented an unrecognized app from starting. To
open it anyway:

1. Press **More info**. The window then shows the file name, `SlopMonster.exe`, and
   "Unknown publisher".
2. Press **Run anyway**.

Windows remembers the choice for that file, so later launches open straight away. Your
browser may also warn while downloading that the file is not commonly downloaded; in
Edge, open the download's menu and choose **Keep**, then **Keep anyway**. If you would
rather not trust a download, check its SHA-256 first, or build the exe yourself from this
repository as [packaging/README.md](../packaging/README.md) describes.

### 5. Make a shortcut

Right-click `SlopMonster.exe` and choose **Pin to Start**, or **Show more options**, then
**Send to**, then **Desktop (create shortcut)**. The exe unpacks itself each time it
starts, so it takes a second or two longer to open than a source run.

## Run from source

This needs Python 3.9 or newer, including `pythonw.exe`, on PATH or found by the `py`
launcher that python.org's installer adds. Nothing else needs installing, with one
exception: reading PDFs uses the `pypdf` package (`python -m pip install pypdf`).

Clone the repository, then create the shortcuts:

```powershell
powershell -ExecutionPolicy Bypass -File app\make-shortcut.ps1
```

This puts a SlopMonster shortcut on the Desktop and in the Start Menu. Run it again if
you move the folder.

## Use

1. Open SlopMonster from the shortcut. A window opens with no console behind it.
2. Press Ctrl+V anywhere in the window. The left pane shows the score and what the
   checker flagged. With the automatic switch on, the rewrite starts by itself.
3. When the run finishes, focus is on Copy, so Enter copies the result.
4. Turn off stops the background process and closes the window. Closing the app window
   with the X does the same.

Esc during a run goes back to the last result, and Ctrl+Z brings back the text that a
paste, an opened file or Clear replaced. The results of this session are listed above
the result. They are kept in memory only and are gone when you turn SlopMonster off.
Ctrl+Z cannot undo a replacement when either text is over 100,000 characters, or when
the new text is over 400 lines.

A run that starts by itself is skipped or held in four cases. Press Unslop or
Ctrl+Enter to run it anyway.

- Text that already passes the checker is not sent to the model, and the result offers an
  Unslop anyway button. Text that passes but still holds a placeholder such as
  `[Your Name]` is sent all the same, because a draft copied from a chat answer
  usually has one. Turn on Rewrite text that already passes, next to the automatic
  switch, to have such text rewritten anyway. It is off by default, because each paste
  then costs a model call. A Thorough run, set in Settings, is never skipped this way.
- A paste of fewer than five words is held.
- A text of more than about 9,000 characters is held.
- A paste that looks like it holds a key or a password is held. This is a short list of
  patterns and will not catch every secret.

Under the result, the app lists any number, link, email address or `[placeholder]` that
differs between your text and the rewrite. When something differs, focus stays off Copy
so that you read the result first.

A whole answer pasted from Claude is flagged for the wording around the draft: an
opening such as `Here's a draft you can adapt:`, offers to change it, or notes after a
`---` rule. The rewrite then returns the draft alone. A flagged line that speaks to the
text's own reader, such as `Would you like me to book the room?` in your own email,
stays, and the notes say why; the result then shows it as still flagged. When the answer
holds several versions, it keeps the first and names the others in the notes, and your
text stays in the left pane. A number that was only in the deleted wording, such as an
`Option 2` label or a tip under the draft, is not counted as missing. The result names
it as left out with the chat reply, and it does not stop Copy when it passes.
Placeholders such as `[Your Name]` are kept, and the result lists them under Fill these
in before sending. They never cost a point.

Changes, beside the scores, marks what the rewrite changed: your words struck through,
the new ones highlighted. Keep original puts one paragraph back as you wrote it. The
score and the lists below it update, and Copy and Save as Word use the text as it now
stands. In a Word file, a paragraph put back is written into the copy exactly as it was.
When you paste a chat reply and the rewrite drops the reply around the draft, that reply
is shown struck through and cannot be put back, but each paragraph of the draft can. When
the rewrite joined or split paragraphs, the words are still marked, but no paragraph can
be put back.

The window title, which the taskbar shows, follows a run: the round, or the part of a
long text, while it goes, then how it ended, such as `Passes 5/5` or `Still flagged 4/5`.
It reads `Check before copying` when a number or link differs. A result that is still
flagged has a Run again on this button. It puts the result in the box, still linked to
its Word file, and runs it again. When the model sent the text back unchanged, it kept what
is flagged on purpose and the notes say why, so the button is not shown. When your text
passed and every rewrite added something the checker flags, you get your text back as it
was, and the notes say so.

Copy when it passes is a second switch, off by default. With it on, a rewrite is copied
for you when it passes the checker and nothing differs, provided the clipboard still
holds the text you pasted. If the window is in the background, its title changes to
"Copied" and a notification appears if you allowed one. When placeholders are left, the
title reads "Copied - fill in 2" and the notification names them. A Thorough rewrite is
never copied for you, because it can change how firm a rule or a promise sounds and no
check sees that. Read it and its notes, then press Copy.

## Choosing which AI rewrites

SlopMonster's rule is that a draft goes for its second pass to a model from a different
company than the one that wrote it, so that the rewrite is not a model checking its own
habits. Rewrite text that Claude wrote with ChatGPT, and text that ChatGPT wrote with
Claude. A new install uses ChatGPT when the Codex command-line tool is installed and
signed in with ChatGPT, and Claude otherwise. A Codex signed in with an API key is used
only after you choose ChatGPT in Settings. The pill at the top of the window names the AI
the next run goes to, and the line under each result names the AI and model that wrote
it, such as `Took 12 seconds with ChatGPT (gpt-6.1-sol)`.

Settings, at the top of the window, holds the choices. Each one is kept as you make it,
in the app window's own browser profile, and goes with the next run.

- **Rewrite with.** ChatGPT or Claude, with whether each tool is installed and signed in.
  Sign in to ChatGPT opens `codex login` in a console window and your browser, the same
  way Sign in to Claude opens `claude auth login`. Being signed in to ChatGPT in a
  browser or the ChatGPT app does not sign the command-line tool in. When Codex was
  signed in with an API key instead, Settings says so, and runs are billed to that key
  and not to your ChatGPT plan.
- **Claude model.** Fast, Balanced, Best, or Custom with a full Claude model name.
- **ChatGPT model.** Plan default, the model your ChatGPT plan gives Codex, or Custom
  with a model name. Reasoning effort is Default, Low, Medium or High. A higher effort
  can take longer.
- **How much to change.** Light or Thorough. Light edits under the instructions and fixes
  each thing the checker flags. Thorough adds a section after the instructions that asks
  the model to go through the text sentence by sentence and write again what still reads
  as machine-written: repeated sentence shapes, an even rhythm, openings that restate the
  question, paragraphs that end on a summary. It gives the model SlopMonster's two guides,
  `references/principles.md` and `references/signs-of-ai-writing.md`, without their links
  and file references. The model is told to keep the facts and the Markdown structure as
  they are, and the result lists any number or link that changed, as with Light. Thorough
  rewrites text that already passes too, so with the automatic switch on each paste costs
  a model call. In a test on Claude's chat answers rewritten by ChatGPT, Thorough changed
  9 and 13 more words in a hundred than Light in an explanation and an email, and a few
  fewer in a meeting summary made of short bullets. No number or link was lost.
- **Rewrite instructions.** The instructions the model is given before your text. Edit
  them and press Save instructions. Your copy is kept in
  `%LOCALAPPDATA%\SlopMonster\cleanse.txt`, and every run uses it from then on.
  `prompts/cleanse.txt` in the repository is never changed, so your copy does not pick up
  later updates to it. When an update changes the default, Settings says so under your
  copy. Reset to default deletes your copy, and so does deleting the file.
  If you close Settings with an edit you have not saved, it stays open once and says so.
  Close it again to leave the edit unsaved.
  The limit is 60,000 characters. The app splits each reply at a line that reads
  `<<<SLOPMONSTER-NOTES>>>` by itself, starting at the left edge. If your instructions
  no longer ask for that line, the app adds a short OUTPUT section of its own after
  them. A Thorough run always ends with it.

## Ctrl+Alt+U from any program

While SlopMonster is running, copy text in any program and press Ctrl+Alt+U. The text
goes in the box and is run as Unslop would run it, even when it already passes. If the
rewrite passes and every number and link carried over, it is put on the clipboard, so
you can paste it where the draft was. Nothing is copied if you have copied something
else since, or with Thorough chosen. A notification says "The unslopped text is on the
clipboard" and names any placeholder such as `[Your Name]` still to fill in, or it says
that the rewrite needs a look. Click it to bring the window up. Without notifications,
the window title on the taskbar says how the run ended. A short text, a long one, or one
that looks like it holds a password is held, as a paste is, until you press Unslop.

The switch for it is the last of the four under the checker and is on by default.
Turned off, SlopMonster lets go of the keys, so another program can use them. If
another program already holds Ctrl+Alt+U, the switch says so. Close that program, then
turn the switch off and on again. If a program has the clipboard open at the moment you
press the keys, the window says so; press them again. The window asks once whether it may show
notifications, the first time you click or press a key in it.

On keyboards with an AltGr key, AltGr is the same as Ctrl+Alt. If AltGr+U types a
character on your layout, you cannot type that character while the switch is on. Turn
the switch off to type it.

## Word and PDF files

Press Open file, or drop a file on the window. Word (`.docx`), PDF, `.txt` and `.md`
files can be read. Save an old `.doc` file as `.docx` in Word first. With the automatic
switch on, an opened file is run at once, as a paste is.

Save as Word opens in the folder the file came from, with the name filled in. Ctrl+S
saves and Ctrl+O opens a file. Saving over the original file is refused. If you choose
it anyway, the browser has already emptied it by the time SlopMonster refuses, so
SlopMonster writes it back as it was. Its modified time changes. The Word file is made
before the dialog opens, so when it cannot be made, no file on disk is touched. If the
dialog cannot open, or the chosen file cannot be written, the document goes to your
Downloads folder and the message under the box says so. Pasted text is saved as a new
document, in which Markdown headings, lists, tables and code blocks become Word's own.

**Word.** The body text is shown one paragraph to a block, with `#` in front of a
heading and `-` or `1.` in front of a list item. A table row is one line with `|`
between its cells. The rewrite has to return the same blocks in the same order, and a
reply that merges or splits them is asked for again.

Save as Word then writes a copy of your file in which only the words have changed. The
original file is never modified. Only the three Word files opened most recently are kept
for this. For an older one, Save as Word writes a new document with its headings, lists
and tables but none of its other formatting, and the message under the box says so.

- Fonts, styles, lists, tables, images, headers, footers and page layout stay as they
  were. Every part of the file except the body text is copied byte for byte.
- In a paragraph that was edited, the words that were kept keep their formatting. A
  bold word that survives stays bold, and a link whose words survive stays a link.
- In a sentence rewritten from end to end, formatting follows position, so bold can
  land on a different word. A link whose words are all gone is removed, and a note
  under the result says so.
- Headers, footers, footnotes, comments, text boxes and parts imported from other files
  are not shown for rewriting.
- Equations, and characters from symbol fonts such as Wingdings, are not shown either.
  A paragraph that holds one keeps its old wording when saved, and a note says so.
- A footnote, endnote or comment mark stays where it stands in a rewritten paragraph.
  When the words beside it change, a note asks you to check it in Word.
- Text that Word fills in from a document property, such as the title on a cover page,
  is left as it was, because Word puts the property back when it opens the file. A note
  says so.
- A table row with a merged cell, or a cell holding two paragraphs, is left as it was.
- A paragraph of chat-reply wording, such as notes after the draft, cannot be deleted,
  because every paragraph has to come back. It stays flagged, and the rounds stop early
  once one fixes nothing more. Delete it in Word.
- Changes are written directly, not as tracked changes, even when tracking is on.
  Tracked changes already in a rewritten paragraph are accepted in the copy, and a note
  says so. A move to or from another paragraph is accepted at both ends. Accept or reject
  them in Word first to keep them.
- A table of contents or of figures is not shown for rewriting. Word rebuilds it from the
  headings or captions when you update it: right-click it, choose Update Field, then
  Update entire table. A new paragraph typed just below its title goes after the table,
  as body text.
- Other text that Word generates, such as a date field, comes back the next time Word
  updates its fields. A field whose words the rewrite drops is removed, and a note says
  so.

**PDF.** The text is read out and put back into paragraphs. A PDF cannot be rewritten in
place, so Save as Word writes a new document with headings and lists. Read the text
before you run it, with the automatic switch off:

- A scanned PDF has no text to read.
- Text typed into a form's fields is not read, and a note says so. Print the PDF to a
  new PDF to have it read.
- Where a paragraph ends is worked out from the layout. On a page of short paragraphs,
  list items or table rows, some of them can run together into one paragraph.
- A bulleted list printed to PDF from a web browser loses its bullets, and its items
  can run together.
- Tables come out as plain lines, or run into the text around them.
- A table of contents is left out, and a note says so. A price or fee list with dotted
  lines stays, one row to a line. A note's number after a word is left out too. A raised
  unit or power, as in m² or 10⁶, is kept. The notes themselves land at the foot of their
  page's text, with their numbers.
- Two-column pages can come out interleaved, or split into one paragraph per line.
- A real hyphen at the end of a line is lost when the next line starts in lower case.

**Long files.** A text of more than about 9,000 characters is rewritten in parts of
roughly a thousand words. Only the parts with something flagged are sent to the model, or
every part with Thorough, and a long text waits for you to press Unslop. If the AI stops
answering part-way, for example at your plan's usage limit, you keep the parts already
rewritten, and the notes say which parts were left as they were.

If the chosen tool is signed out, the right pane says so and offers Sign in to Claude or
Sign in to ChatGPT. That opens the tool's own sign-in in a console window and your
browser. Being signed in to the Claude desktop app, or to ChatGPT, does not sign the
command-line tool in. Open settings, beside it, is where you switch to the other AI.

With Claude, Fast uses the Haiku model, Balanced uses Sonnet and Best uses Opus. A paragraph took
10 to 20 seconds in testing, whichever was chosen. If Claude Code stops accepting the model
name the app gives for Fast, the app uses Claude Code's own "haiku" name until it is
turned off, and the notes under each Fast result say so. That name picked a slower model
in testing.

A Claude Code update can drop or rename an option the app passes to it. A run then stops
with "SlopMonster needs an update", naming the option and the Claude Code version, and
your text is left as it was. A Claude Code older than 2.1.292, the version the app was
tested with, can refuse an option too. The run then stops with "Claude Code needs an
update" instead. A copy installed with WinGet is updated with
`winget upgrade Anthropic.ClaudeCode`.

Codex is handled the same way. If a Codex update turns down an option the app passes, or
ignores a setting the app uses to switch Codex's tools off, the run stops with
"SlopMonster needs an update" and the reply is not used. A Codex older than 0.163.0 gets
"Codex needs an update" instead.

The optional "Where it will appear" field tells the model the audience and format, for
example "homepage for a roofing company".

## What goes where

- The score comes from `tools/deslop.py`, which runs on your machine.
- With ChatGPT chosen, a rewrite sends your text to OpenAI through the `codex`
  command-line tool, on your ChatGPT plan, with Codex's shell, web search and other tools
  switched off. With Claude chosen, it sends your text to Anthropic through the `claude`
  command-line tool, under your own account. Either way the rewrite instructions go with
  it. The app sends it nowhere else, and the page loads nothing from the internet.
- Your edited rewrite instructions, if you saved any, are in
  `%LOCALAPPDATA%\SlopMonster\cleanse.txt`, and the default they were made from is in
  `cleanse.base.txt` beside it.
- The app window's own browser profile is `%LOCALAPPDATA%\SlopMonster\browser-profile`.
  It keeps your Settings choices. The browser is told not to save what you type or paste
  in the boxes, and the window's saved session, where older versions left that text, is
  cleared each time SlopMonster starts.
- With Copy when it passes on, and on every Ctrl+Alt+U run whether it is on or not, the
  server reads the clipboard at the end of a Light run that passes and writes the
  rewrite to it. It does not write when the clipboard holds anything other than the
  text you pasted.
- Ctrl+Alt+U also reads the clipboard when you press it. The log notes how long the
  text was, never the text.
- The server listens on `127.0.0.1:47821`, or on the first free port of the four after
  it when another program or another signed-in account has that one. It only answers the
  page it served. It does not guard against other programs or other signed-in accounts
  on the same PC.
- The running copy writes its port and a key to `%LOCALAPPDATA%\SlopMonster\instance.json`.
  A second launch opens a window only on a copy that proves it holds that key. It finds
  the copy there even when all five ports were taken, and it never opens a window on
  another program.
- The log is `%LOCALAPPDATA%\SlopMonster\server.log`. It holds request lines, errors and
  one line per run with the engine, model, how much to change, whether your own
  instructions were used, rounds, seconds and scores. Your text is not written to it.

## Uninstall

1. Turn SlopMonster off.
2. Delete the shortcuts: `SlopMonster.lnk` on the Desktop and in
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs`, or the ones you made for the exe.
   Unpin it from Start if you pinned it.
3. Delete `%LOCALAPPDATA%\SlopMonster`. It holds the window's browser profile, the log
   and your saved instructions.
4. Delete `SlopMonster.exe`, or the repository folder for a source install.

The Codex and Claude Code command-line tools, and their sign-ins, are left as they are.

## Limits

- The checker matches English patterns. A score of 5/5 means none matched. It does not
  mean the text is clear or true, so read the result before you use it.
- Numbers next to words such as "customers" are listed under "Verify these claims". The
  checker cannot tell whether they are true.
- The model may leave a flagged phrase in place when it is a literal or necessary use,
  and says so in the notes.
- One text can be up to 200,000 characters, and one file up to 30 MB.
- The Word support was checked against Word 16 on documents written by Word. Files from
  LibreOffice or Google Docs have not been tried.
- Windows only. The server is plain Python, but the shortcut and the window handling are
  written for Windows.

## Tests

```powershell
python app\test_server.py
python app\test_documents.py
python app\test_wordfile.py
python app\test_page.py
```

The tests start the real server on a spare port with a stand-in for the model. They
make no model calls, open no windows and do not need Word. `test_page.py` also runs the
page in a headless Edge or Chrome. It needs Node 22 or newer, and without Node or a
browser it says so and skips.

To try a copy without touching the one you use, start it with a port and a data folder
of its own: `python app\server.py --no-window --port 48100 --data-dir %TEMP%\slop-test`,
or the same options after `SlopMonster.exe`. `SLOPMONSTER_DATA_DIR` does the same as
`--data-dir`. `packaging\smoke_exe.py` tests a built exe this way.

## Files

| File | What it is |
|---|---|
| `server.py` | The local server: checker, rewrite loop, sign-in, window lifecycle. Standard library only. |
| `index.html` | The whole page, with its styles and script inline. It has a dark theme and a light one. |
| `make-shortcut.ps1` | Creates the Desktop and Start Menu shortcuts. |
| `version.py` | The version number, shown in Settings and written into the exe. |
| `documents.py` | Reads Word, PDF and text files, and writes a new Word file from text. |
| `wordfile.py` | Rewrites the text of a Word file in place, keeping its formatting. |
| `test_server.py`, `test_documents.py`, `test_wordfile.py`, `test_page.py`, `test_page.mjs` | Tests. `test_page.mjs` is the part of the page test that drives the browser. |
| `testdata/` | Five small documents written by Word, with the results expected of each, used by the tests. |
| `icon.png`, `slopmonster.ico` | The mascot, cut from the upstream artwork. |

The checker, the editing prompt and the mascot come from
[ItsssssJack/SlopMonster](https://github.com/ItsssssJack/SlopMonster), under the MIT
license in the repository root. This fork adds the chat-reply rules to `tools/deslop.py`
and `prompts/cleanse.txt`, and a rule against added hedges to `prompts/cleanse.txt`. The
app depends on them. When you pull upstream changes, merge those two files by hand,
keeping both sides, and run `tools/test_deslop.py` and `app/test_server.py` afterwards.
