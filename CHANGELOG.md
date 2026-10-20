# Changelog

## 1.1.0

SlopMonster can now stay open for days, and a few results tell you more.

Left open all day:

- The window keeps working when the connection drops for a moment. Check again reconnects
  the same window and keeps your draft and the results of the session.
- The moving background stops when the window is idle or out of sight, so an open window
  no longer keeps the processor busy.
- After a crash, the next start cleans up the files the last run left behind and opens at
  once.
- Ctrl+Alt+U goes to the SlopMonster window you used last.
- The log keeps the lines about runs and leaves out routine polling, so it covers weeks.
- The window opens at the size you left it.
- `make-shortcut.ps1 -Startup` starts SlopMonster when you sign in to Windows. Both
  READMEs say to minimise the window, not close it, to keep Ctrl+Alt+U working.
- F5 or Ctrl+R asks before a reload throws away your text or results. Press it again to
  reload.
- The box keeps the last 20 swaps for Ctrl+Z, so its memory use stays flat.

Runs and results:

- Try with Claude, or Try with ChatGPT, sends the same text once to the other AI. It
  shows under a result and on an error the other AI might get past, such as a usage
  limit. Your choice in Settings stays as it is. Ctrl+Shift+Enter presses it.
- Less sure than your text names any sentence where the rewrite added a caveat your text
  did not have.
- A list of three still costs a point but no longer stops a pass or drives extra rounds.
  Any left are listed under Lists of three.
- A chat answer with bold run-in labels, such as `- **Cost:** ...`, is asked to fold them
  into its sentences, because Copy pastes the asterisks too. A bold title or price stays a
  short line.
- The window shows the Where it will appear text while it applies, and the × in the field
  clears it.
- The pill says Thorough when it is on.
- The list of results names the AI and the score of each one. After Keep original it
  gives the score of the text Copy copies, once that text is checked.
- A sentence-pattern chip shows the words it matched, with the rule on hover.
- A note the model repeats in a later round is listed once.
- After Ctrl+Alt+U holds a text, Enter runs it.
- `packaging\build.ps1` also builds from a source zip, which is not a git checkout.

## 1.0.0

First public release.
