# SlopMonster

> This is the README of [SlopMonster](https://github.com/ItsssssJack/SlopMonster), the
> project SlopMonster GUI is built on, kept here for its checker and agent skill. The
> commands below are written for macOS and Linux. On Windows, type `python` where they
> say `python3`. For the Windows app, see the [main README](../README.md).

![The five SlopMonster mascots, one for each scoring category](img/hero.png)

SlopMonster checks English writing for common AI writing patterns and provides
instructions for editing the draft. Use it on a README, a landing page, an email or
a script.

The Python checker reports matches in five categories and gives the text a score
out of 5. The editing workflow asks an AI agent to review the draft for its intended
reader, revise it, and request another pass from a different model family.

A score of 5/5 means the checker found no scored matches. You still need to review
whether the text makes sense, supports its claims and gives the reader what they
came for. The previous version of this README passed while using repetitive
metaphors and vague contrasts. That exposed a gap in the editing guidance.

## Quick start

The checker uses Python 3 and its standard library. It needs no API key or packages
to install.

```bash
git clone https://github.com/ItsssssJack/SlopMonster
cd SlopMonster

# Check a short draft.
python3 tools/deslop.py --text "It's not just a tool, it's a game-changing journey."
# Scores 3/5 and exits with status 1.

# Check a Markdown file.
python3 tools/deslop.py README.md

# Check text extracted from an HTML file.
python3 tools/deslop.py index.html
```

Each category with a match costs one point, regardless of how many matches it
contains. The command exits with status 0 at 5/5 and status 1 below 5/5. Empty input
also fails. Checking a file leaves its contents unchanged.

For Markdown, the checker skips fenced code blocks, inline code, images and
struck-through examples. For HTML, it removes tags and script/style contents. It
doesn't render the page, so text hidden by CSS can still be included.

## Edit a draft

Give your agent [SKILL.md](../SKILL.md) and the draft. Include the intended audience
and where the writing will appear if those are unclear from the text.

1. Run the checker to identify the patterns it recognizes.
2. Review the draft's purpose and order. A README should explain the tool and show
   how to use it. An email should make its reason for writing clear.
3. Rewrite unclear or repetitive passages. Check that each sentence adds useful
   information and that claims match the supplied facts.
4. Request a second editing pass from a different model family, then inspect the
   changes and run the checker again.

The second pass uses [prompts/cleanse.txt](../prompts/cleanse.txt). With a supported AI
command-line tool (CLI) installed and authenticated, run:

```bash
# If Claude wrote the draft, use Codex for the second pass.
DESLOP_WRITER=claude tools/cleanse.sh draft.md > cleansed.md

# If GPT wrote the draft, use Claude for the second pass.
DESLOP_WRITER=gpt tools/cleanse.sh draft.md > cleansed.md

# Read the edited file, then check it as Markdown.
python3 tools/deslop.py cleansed.md
```

Run the command for the family that wrote your draft. The script writes edited
copy to standard output and change notes to standard error. It uses the installed
CLI's configured model.

The prompt also handles a whole chat answer pasted with the draft inside it. When
the checker findings sent with the draft name chat-reply wording, the editor returns
the draft alone. It deletes the opening and the notes and offers around the draft,
keeps the first of several versions, and leaves placeholders such as `[Your Name]`
in place. `cleanse.sh` sends no findings, so only the [Windows app](../app/README.md)
uses this step.

If the required CLI is missing, the script prints the editing prompt and draft,
then exits with status 127. Paste that output into the other model family's chat.
A timeout exits with status 124. Check the exit status before treating the output
file as an edited draft. A warning about a missing notes marker means the response
may include change notes in the copy.

Using another model provides another editing pass. It doesn't guarantee better
writing. Review the result for changed facts and lost context before accepting it.

### Use as an agent skill

For Claude Code, copy this folder to `~/.claude/skills/slopmonster/` and invoke
`/slopmonster`. For Codex or another agent, ask it to follow [SKILL.md](../SKILL.md).

## What the checker flags

The current score has five categories:

| Category | What it checks |
|---|---|
| Vocabulary | Listed words such as `delve` and `seamless`, including selected word forms. |
| Sentence patterns | Listed constructions such as `not just X, but Y`, stacked hedges and certain self-answering questions. Also the wording a chat reply puts around a draft, such as `Here's a draft you can adapt:`, `Want me to make it warmer?` or a `---` rule followed by notes to the person who asked. |
| Punctuation | Repeated em dashes or stacked hyphenated compounds within a 220-character window in a sentence, plus semicolon density. |
| Three-item lists | Two specific list patterns that often occur in promotional copy. |
| Possible unsupported proof | Numbers near nouns such as `users` or `customers`. The checker cannot verify the claim. |

Some chat-reply checks read the text line by line, looking for a bold `**Subject:**`
line or an `Option 1:` label, for example. Install guides and changelogs use such labels
and `---` rules too, so those two count only when the text opens with a label or has
another sign of a reply to the person who asked, such as an offer to change the draft.
A stock phrase or a reframe alone is not such a sign, and neither is an opening heading
with only a number or a date after the label, such as `## Version 2`. On Markdown files
and `--text`, the checker runs them and also lists placeholders such as `[Your Name]` or
`[Add an example]` so you can fill them in. Placeholders don't change the score or
the exit status. HTML input skips both.

The patterns are in [tools/deslop.py](../tools/deslop.py). The broader editing
catalogue is in [references/signs-of-ai-writing.md](../references/signs-of-ai-writing.md).
Some entries in that catalogue require editorial judgment and have no automated
check.

If a flagged customer count is accurate and you have evidence for it, use:

```bash
python3 tools/deslop.py index.html --allow-proof
```

This option still reports proof matches but removes their score penalty. It
doesn't check the evidence. In this mode, 5/5 can include proof warnings.

## Review beyond the score

Read the draft in the context where someone will encounter it. Check these points
before accepting an edit:

- **Audience and purpose:** Does it answer the reader's likely questions at the
  right level of detail? Explain technical terms where they first matter.
- **Opening:** Does the first paragraph explain the subject or establish a useful
  reason to keep reading? Avoid adding a second hook that delays the explanation.
- **Progression:** Does each sentence build on the previous one? Remove repeated
  metaphors and contrasts that add no information.
- **Claims:** Can you support each factual statement? Describe optional behavior
  with its conditions. For example, a command's failure status only stops a
  workflow if that workflow is configured to act on it.
- **Voice:** Does the wording suit the author and format? Contractions are fine
  where natural. Forced fragments and invented opinions can make the edit worse.

See [references/principles.md](../references/principles.md) for more editing guidance
and examples. Never add a statistic or testimonial to make a sentence sound
specific. If information is missing, flag it for the author.

### Limits

The checker uses English patterns and has no language detection. Text in another
language can score 5/5 because those patterns don't match.

It can flag ordinary wording and miss poor writing. It cannot determine who wrote
the text, verify facts or judge whether a hook suits the audience. Use its findings
as prompts for review. A passing score alone doesn't establish writing quality.

## Optional CI check

You can run the checker in continuous integration (CI), the automated checks a
repository runs when code changes. Because a score below 5/5 returns a failure
status, a workflow that runs the command can fail on flagged copy.

This repository's [workflow](../.github/workflows/slop.yml) runs the regression tests
and checks the repository README and a sample file. Copy it to your repository and set
the file paths you want checked. Blocking a merge or deployment requires your own
repository rules or deployment configuration.

```bash
python3 tools/test_deslop.py
bash tools/test_cleanse.sh
python3 tools/deslop.py README.md
```

## Examples and sources

- [Jasper homepage excerpt](../examples/jasper-live-run.md): a recorded editing run
  with the commands and output from that run. Scores can change as rules change.
- [Ridgeline Roofing](../examples/ridgeline-roofing.md): a longer worked example with
  before-and-after copy. Check any business facts before reusing a line.
- [Source list](../references/sources.md): Wikipedia's *Signs of AI writing* and the
  open-source humanizer projects used to assemble the catalogue. The marketing
  guidance also draws on Steve Krug, Daniel Priestley and Alex Hormozi.

[MIT license](../LICENSE).
