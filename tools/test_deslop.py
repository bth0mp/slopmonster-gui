#!/usr/bin/env python3
"""Regression tests for the linter. Run: python3 tools/test_deslop.py

Two directions, and the second is the one that matters. It is easy to widen a
regex until it catches everything, so every widening here is paired with a
must-stay-clean case that would break if the rule got greedy.
"""
import atexit
import re
import shutil
import subprocess
import sys
import tempfile
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deslop import (VOCAB, VOCAB_EXACT, audit, visible_text, _root_pattern,
                    markdown_prose, PROOF)

fails = []


def check(name, cond, detail=''):
    if not cond:
        fails.append(f'{name}: {detail}')


def groups(text):
    return {k for k, v in audit(text).items() if v}


# ── every catalogue word still fires in its own base form ───────────────────
# The obvious stemming fix (strip a trailing "e") silently kills `elevate`,
# `leverage`, `delve` and ten others. This test is what catches that.
for w in VOCAB:
    check(f'base form "{w}"', re.search(_root_pattern(w), w.lower()),
          'root pattern no longer matches its own base word')
for w in VOCAB_EXACT:
    check(f'exact form "{w}"', 'vocab' in groups(f'We {w} things.'), 'not detected')

# ── inflections fire too: the gap that made the tool half-blind ─────────────
for w in ('elevates', 'unlocks', 'empowers', 'streamlines', 'leverages',
          'harnessing', 'revolutionizes', 'innovation', 'transformational'):
    check(f'inflection "{w}"', 'vocab' in groups(f'Acme {w} your workflow.'), 'missed')

# ── literal senses must NOT fire ────────────────────────────────────────────
# Stemming `crafted` to `craft` would flag a furniture maker. These are the
# words that earn their place on the exact-match list.
for ok in ('We craft furniture by hand in Leeds.',
           'Harness the horse before you load the cart.',
           'They walked the length of the valley.'):
    check('literal sense clean', 'vocab' not in groups(ok), ok)

# ── constructions, contracted and not ───────────────────────────────────────
for bad in ("It's not just a tool, it's a platform.",
            'It is not just a tool, it is a platform.',
            'Whether you are a beginner or a pro, start here.',
            'This is where Acme comes in.',
            "Here's the thing. You need a plan.",
            'The result? Teams ship faster.',
            'Ready to get started?'):
    check('construction caught', 'phrases' in groups(bad), bad)

check('plain "whether you are" is clean',
      'phrases' not in groups('Check whether you are on the latest version.'))

# ── rhythm: the repo's own canonical bad example must fail ──────────────────
check('no-Oxford tricolon', 'rhythm' in groups('Trusted, reliable and built to last.'),
      'the tell quoted in examples/ridgeline-roofing.md scored clean')
check('Oxford tricolon', 'rhythm' in groups('It is faster, smarter, and better.'))
check('short items are not a tricolon', 'rhythm' not in groups('We shipped red, white, and blue.'))
check('fronted adverbial is not a tricolon',
      'rhythm' not in groups('On Tuesday, we shipped the release and went home.'))
# The discriminator that earns the no-Oxford rule its place: a rhetorical
# flourish ends in a phrase, a plain list of services does not. This is the
# repo's own shipped hero line — flagging it would be crying wolf.
check('service list is not a tricolon',
      'rhythm' not in groups('Inspection, repair and replacement for homes and commercial buildings.'))
check('or-list is not a tricolon',
      'rhythm' not in groups('We do not do gutters, siding, windows or conservatories.'))

# ── proof: the canonical fabricated line must fail ──────────────────────────
check('canonical invented proof', 'proof' in groups('Loved by 10,000+ happy homeowners.'),
      'the line quoted in references/principles.md scored clean')
check('small counts too', 'proof' in groups('Trusted by 25 businesses.'))

# ── entities: decoded, not leaked ───────────────────────────────────────────
t = visible_text("<p>It&#x27;s not just a tool, it&#x27;s a platform.</p>")
check('entity apostrophes decoded', "it's not just" in t.lower(), repr(t))
check('entities do not donate semicolons', t.count(';') == 0, repr(t))
check('phrase seen through entities', 'phrases' in groups(t))
check('nbsp becomes a space', visible_text('<p>a&nbsp;b</p>') == 'a b')

# ── the gate: empty input must fail, not pass ───────────────────────────────
here = os.path.dirname(os.path.abspath(__file__))
r = subprocess.run([sys.executable, f'{here}/deslop.py', '--text', ''],
                   capture_output=True, text=True)
check('empty input exits non-zero', r.returncode != 0, f'exit {r.returncode}')
r = subprocess.run([sys.executable, f'{here}/deslop.py', '/nope/missing.html'],
                   capture_output=True, text=True)
check('missing file exits non-zero', r.returncode != 0, f'exit {r.returncode}')
check('missing file has no traceback', 'Traceback' not in r.stderr, r.stderr[:80])

# ── files are read as UTF-8, whatever the locale ───────────────────────────
# open() with no encoding= uses the locale's, cp1252 on a stock Windows box. A
# UTF-8 page then decodes to mojibake and the em-dash and construction rules
# never fire, so a page full of tells scores CLEAN. Driven through the CLI on
# purpose: --text was always fine, the file paths were the blind spot. Both of
# them: .html goes through visible_text, .md through markdown_prose.
_dir = tempfile.mkdtemp()
atexit.register(shutil.rmtree, _dir, ignore_errors=True)    # on a failing run too
_tells = ('It\u2019s not just a tool, it\u2019s a platform. Whether you\u2019re a startup '
          'or an agency, we ship fast \u2014 really fast \u2014 every week.')

for name, body in (('utf8.html', f'<html><body><p>{_tells}</p></body></html>'),
                   ('utf8.md', _tells)):
    path = os.path.join(_dir, name)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(body)
    r = subprocess.run([sys.executable, f'{here}/deslop.py', path],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    check(f'{name} is not decoded as cp1252', r.returncode != 0,
          'a UTF-8 page full of tells scored CLEAN - locale decoding is back')
    check(f'{name}: construction rule fires', 'construction' in r.stdout, r.stdout[:140])
    check(f'{name}: em-dash rule fires', 'em-dash' in r.stdout, r.stdout[:140])

# A flagged snippet outside the console codepage must score, not traceback.
_cjk = os.path.join(_dir, 'cjk.html')
with open(_cjk, 'w', encoding='utf-8') as fh:
    fh.write('<html><body><p>Our seamless platform \u4f60\u597d \u2014 \u4e16\u754c '
             '\u2014 delivers robust value today.</p></body></html>')
r = subprocess.run([sys.executable, f'{here}/deslop.py', _cjk],
                   capture_output=True, text=True, encoding='utf-8', errors='replace')
check('non-cp1252 snippet does not traceback', 'Traceback' not in r.stderr, r.stderr[-160:])
check('non-cp1252 snippet still scores', 'score' in r.stdout, r.stdout[:140])

# ── clean human copy still scores 5/5 ───────────────────────────────────────
for ok in ('Six nails per shingle, every shingle.',
           'You get a written scope and a fixed number before anyone climbs a ladder.',
           'We do not do overlays. If the roof needs replacing, it gets stripped.'):
    check('clean copy stays clean', not groups(ok), f'{ok} -> {groups(ok)}')

# ── markdown: a literal is not copy ─────────────────────────────────────────
# Every strip below is paired with a case that must survive it, because the
# fastest way to make a catalogue score 5/5 is to stop reading the catalogue.
md = markdown_prose('Use `delve` here. See [the guide](u.md) for more.')
check('inline code is not scored', 'delve' not in md, repr(md))
check('link text survives', 'the guide' in md, repr(md))
check('surrounding prose survives', 'for more' in md, repr(md))

md = markdown_prose('> ~~Trusted, reliable and built to last.~~\n> **Six nails per shingle.**')
check('struck specimen is not scored', 'rhythm' not in groups(md), repr(md))
check('the shipped line survives', 'Six nails per shingle' in md, repr(md))

md = markdown_prose('Intro.\n```\nleverage seamless unlock\n```\nOutro.')
check('fenced block is not scored', not groups(md), repr(md))
check('prose around the fence survives', 'Intro' in md and 'Outro' in md, repr(md))

# Deleting a code span outright welds the clause into a false rule-of-three.
md = markdown_prose('If a claim needs a number you do not have, write `[needs number]` and move on.')
check('stripping code invents no tricolon', 'rhythm' not in groups(md), repr(md))

# Two table rows are two lines of copy, not one sentence with a dash pile-up.
md = markdown_prose('| a | **3/5** — one thing |\n| b | **5/5** — another thing |')
check('table rows do not merge into one cadence', 'punctuation' not in groups(md), repr(md))

# A heading must not run into the sentence beneath it.
md = markdown_prose('## Receipts, not claims\nA real run on seven sentences.')
check('heading does not weld to body', 'Receipts, not claims A real' not in md, repr(md))

# Markdown handling strips markup only. Real slop in real prose still fails.
md = markdown_prose('We leverage seamless, robust and cutting-edge tooling to empower teams.')
check('markdown does not soften vocabulary', 'vocab' in groups(md), repr(md))
md = markdown_prose('It is not just a tool, it is a journey.')
check('markdown does not soften constructions', 'phrases' in groups(md), repr(md))

md = markdown_prose('- Tier 1 — delete on sight\n- Tier 2 — usually cut')
check('bullets do not merge into one cadence', 'punctuation' not in groups(md), repr(md))
# but a genuine pile-up inside one bullet must still fire
md = markdown_prose('- Our team — trained, certified and local — is ready to help.')
check('dash pile-up inside a bullet still fires', 'punctuation' in groups(md), repr(md))

# ── hyphens: stacking, not the hyphen itself ────────────────────────────────
stack = 'Our industry-leading, context-aware, best-in-class, AI-powered platform ships.'
check('four stacked compounds fire', 'punctuation' in groups(stack), stack)
for ok in ('We fitted a 25-year warranty and a 4.2-hour call-out on a two-storey roof.',
           'The built-up roof needs a full-width strip before the tear-off.',
           'A well-known, long-standing trade name.'):
    check('ordinary hyphenated copy stays clean',
          'punctuation' not in groups(ok), f'{ok} -> {groups(ok)}')

md = markdown_prose('> ~~Em-dash pile-up~~\n> **Thirty-eight on the crew, factory-trained today.**')
check('blockquote lines do not merge', 'punctuation' not in groups(md), repr(md))

# ── proof: a number and its noun live in one sentence ───────────────────────
for hit in ('10,000+ happy users', '500 teams', '2,000 verified customers',
            '3.5 million customers'):
    check('real proof claim still caught', PROOF.search(hit), hit)
for clean in ('"Don\'t Make Me Think", 2000. The reader decides in seconds.',
              'Roofing, and only roofing, since 2001. Customers come back.',
              'Version 2.0. Readers can skip it.'):
    check('proof does not reach across a full stop', not PROOF.search(clean), clean)

# ── negated "just": the contracted register counts too ──────────────────────
# `\bnot\b` has no standalone token to match inside `isn't`, so an uncontracted-only
# pattern scored every contracted form clean — in the register a model reaches for
# when it is trying hardest to sound human. The full stop cases matter as much: the
# window could not previously cross one.
for hit in ("This isn't just a map, it's a plan.",
            'It is not just a map. It is a plan.',
            "SlopMonster isn't just a linter, it's a gate.",
            "These aren't just words, they're tells.",
            'This is not just a map, but a plan.',
            "It doesn't just score you, it fails the build."):
    check('negated-just construction caught', 'phrases' in groups(hit), hit)

# Paired must-stay-clean cases, per this file's rule. The Y clause needs a comma or a
# full stop before its pronoun, which is what stops a bare negation tripping the rule.
for clean in ("It's not just about money.",
              'Do not just take my word for it.',
              'We could not just sit there. The rain kept falling.',
              'The lot is not paved, it is grass.',
              'He said the price was not right, and we walked away.'):
    check('negated-just stays clean', 'phrases' not in groups(clean), clean)

# ── proof nouns need a closing boundary ─────────────────────────────────────
# `sites?` matched inside "sitemaps", `teams?` inside "teamsters". Real proof
# claims must still fire; ordinary nouns that merely start with one must not.
for clean in ('We generated 12 sitemaps last quarter.',
              'The hall seats 20 projectors.',
              'We shipped 40 userscripts.',
              'The union sent 300 teamsters.'):
    check('proof noun does not match inside a longer word', 'proof' not in groups(clean), clean)
for hit in ('Trusted by 10,000 teams.', 'We host 12 sites.', 'Over 300 clients served.'):
    check('proof claim still caught', 'proof' in groups(hit), hit)

# ── the README's own must-not-fire tricolon example ─────────────────────────
# The wolf-crying case the narrowness exists to prevent. If this fires, the
# README is lying about the rule.
check('README safe tricolon example stays clean',
      'rhythm' not in groups('Inspection, repair and replacement for homes and commercial buildings.'))
check('README tricolon example still fires',
      'rhythm' in groups('Trusted, reliable and built to last.'))

# ── a chat answer pasted whole: the wrapper around the draft is the tell ────
# Real Claude chat output scored 5/5: the catalogue was built for marketing copy, and a chat
# answer's tells are its handover line, its offers and its notes to the requester. Every
# true hit below is a line from a real answer; every near miss is human writing that shares
# a word with a rule, most of it from correspondence, the genre these rules are aimed at.
from deslop import markdown_raw, chat_reply  # noqa: E402


def md_groups(md):
    """Categories hit when a paste is scored the way the app scores it: prose plus raw."""
    return {k for k, v in audit(markdown_prose(md), raw=md).items() if v}


for bad in ("Sure! Here's a quick overview of how DNS works:\n\nDNS maps names to addresses.",
            "Great question! Remote work isn't inherently better or worse.",
            "Here's a draft you can adapt:\n\n---\n\nHi Priya,\n\nThe venue is booked.",
            "Here's a polished version that keeps your voice:\n\nI baked bread every Sunday.",
            "**Option 1 (concise):**\n\nI am a registered nurse.",
            "Version 2 (more casual):\nHey Sam,",
            "Hi team,\n\nThe launch moved to Friday.\n\nWant me to make it shorter, funnier, or more sentimental?",
            "Want help sketching out the tables (products, suppliers, stock movements, etc.)?",
            "If you'd like a different tone, I can do a warmer, funnier, or more formal version.",
            "If you tell me your current gas and electric rates, I can do a quick cost comparison.",
            "**Subject:** Rooms\n\nThanks,\nSam\n\n---\n\nI assumed the replacements arrive March 4.",
            "A few tips since you're nervous: pause after the first joke.",
            "The plan is done.\n\nWhat changed:\n- One-line summary up top",
            "A couple of notes: I kept it warm but concise.",
            "Your original notice said \"Thursday Oct 23,\" but Oct 23 is a Wednesday.",
            "My honest answer: probably not yet.",
            "## Key takeaways\n\nBoth work.",
            "**Subject:** Warehouse migration pushed to Nov 4\n\nHi team,",
            "A heat pump doesn't make heat. It moves heat.",
            "Not because you can't do it, but because quitting first is the riskiest order.",
            "One line about it would make the thanks land harder than anything general.",
            "Each mug is thrown by hand, so no two are exactly alike.",
            # The five added after the held-out answers.
            "**Shorter version (for apps with tight character limits):**\n\nTwo hosts, one bad idea.",
            "If you want it more casual:\n\nHey all, we're off Friday.",
            "Thanks,\n[Your name]\n\nA few notes:\n- Swap in the real date.",
            "A couple of things you may want to fill in or consider: the outage start time.",
            "I'm happy to work any of those in.",
            "You can add a guest's name or a listener question to make it stronger.",
            # And after a second set of held-out answers.
            "Here are a few options you can use as-is or mix:\n\nWishing you the best.",
            "Here are a few options:\n\n1. Two hosts, one bad idea.",
            "Here are a few options in different tones:\n\n**Warm:** Happy birthday.",
            "If you tell me a bit more about her, I can help tailor the wording.",
            # And a third: an offer to refine the answer, as the last thing in it.
            "Bake it to 205F.\n\nIf you tell me your recipe (flour, hydration), I can pinpoint it more precisely.",
            "File this week.\n\nIf you tell me the airline, I can point you to their specific claim page.",
            "Keep it out of drafts.\n\nIf you tell me how often you water, I can narrow it down.",
            # What the narrowed rules must still catch.
            "**Option 1: Short and punchy**\n\nFresh bread, every Saturday.",
            "**Version 1: Professional & concise**\n\nI am out until Monday.",
            "Hi Sam,\n\nThe van is booked.\n\nBest,\nAna\n\n---\n\nIf you'd like me to make it shorter, say so.",
            "Here's a draft you can adapt:\n\nHi Sam, the van is booked.",
            "Here's a version that keeps it short:\n\nVan booked.",
            "Thanks, Sam.\n\nWant me to make a shorter version?",
            "Thanks, Sam.\n\nWould you like me to tailor it for the client?",
            "Thanks, Sam.\n\nWould you like me to turn these into slides?",
            "Thanks, Sam.\n\nI can make the headline punchier if you want.",
            "If it's not right, just swap in the real date.",
            "Adjust the tone to match your own style.",
            "A few tips for making this fair: rotate the order.",
            "A slightly shorter version for the newsletter:\n\nVan booked.",
            "Here's a draft:\n\nCan anyone cover Saturday?\n\n"
            "A slightly shorter version if you prefer:\n\nSaturday cover, anyone?",
            # And what the third narrowing must still catch (review ADV-3 to ADV-7, E2E-2).
            # "Want it a bit warmer?" alone and a plain "Option 1: Short and punchy" menu with
            # nothing around it read as a thermostat and a Slack proposal now (review R3L-6).
            "Thanks, Sam.\n\nWant the email a bit warmer?",
            "Thanks, Sam.\n\nWant me to make it a bit warmer?",
            "Sure! Here's a draft:\n\nHi Sam, the van is booked.",
            "Here's a draft that's warm but firm:\n\nHi Sam, the van is booked.",
            "Here are a few options:\n\nOption 1: Short and punchy\n\nFresh bread, every Saturday.\n\n"
            "Option 2: Warm and detailed\n\nCome by for bread from 9.",
            "Option 1: Short and punchy\n\nFresh bread, every Saturday.\n\n"
            "Option 2: Warm and detailed\n\nCome by for bread from 9.\n\nWant me to tweak the tone?",
            "**Option 1: Minimal**\n\nKeep the page.\n\n**Option 2: Bold**\n\nRedesign it.",
            "Here are a few options:\n\n- Fresh bread every Saturday, from 9 until it's gone",
            "Here's a draft you can send:\n\nHi Sam, the van is booked.",
            "Here's a draft that stays under 50 words:\n\nHi Sam, the van is booked.",
            "Sure! Here's a quick overview of how compound interest works:\n\nInterest earns interest.",
            "I kept your original wording and only fixed the dates.",
            'Your original draft said "synergy" twice.',
            "If that's not right, just change the wording.",
            'Adjust it to match your own voice.',
            'A few things you might want to adjust before sending: the date and the room.',
            "I'm happy to adjust the tone if it reads too stiff.",
            "Hi Sam,\n\nThe van is booked.\n\n---\n\nIf you'd like me to make the tone warmer, say so.",
            "If that's not right, adjust that line.",
            "A few tips for keeping it that way: reply within a day.",
            # What the narrowing for ordinary mail and PR text must still catch (CHK-2, CHK-3).
            'If the meetup asks for a 150-word limit, I can also cut it.',
            'I can also cut it down to 100 words.',
            'I can also drop the subject line if this is going in a chat message.',
            'I can do a warmer, funnier, or more reflective take.',
            'Thanks, Sam.\n\nWant a different version for the client?',
            'Thanks, Sam.\n\nWould you like me to turn this into a LinkedIn post?',
            'Thanks, Sam.\n\nWould you like me to draft talking points for the call?',
            'Thanks, Sam.\n\nWant it warmer, more formal, or with a story included?',
            'If you want it shorter or more casual:\n\nHey Dev, sorry.',
            'I added a placeholder for her email, since readers need it.',
            'Fill in the bracketed parts with specific examples.',
            'Hi Sam, the van is booked.\n\n**Tips for delivery:**\n\n- Pause after the first line.',
            'Hi Sam, the van is booked.\n\nNotes for tweaking:\n- Swap in the real date.',
            'Hi Sam, the van is booked for 14 seats.\n\nOne thing to check: I guessed the 14 seats.'):
    check('chat answer caught', 'phrases' in md_groups(bad), bad)

for ok in ("Hi Priya,\n\nHere's the latest version of the deck. I cut slides 4 and 5.\n\nTom",
           "Here's my draft of the minutes, let me know if I missed anything.",
           'Thanks for the update. I can make Tuesday work.',
           "If you send me the file, I'll take a look tonight.",
           'I can also bring the projector if you need it.',
           'Would you like me to book the room for Friday?',
           'Want to grab lunch on Thursday?',
           "Let me know if you'd like to meet next week.",
           'A couple of notes: the meeting moved to 3pm, and Sam is out.',
           'Feel free to reach out if you have any questions.',
           'Honestly, I think we should wait until the spring.',
           'The short answer is no, but there is a workaround.',
           "The joke didn't land, and the room went quiet.",
           'There are trade-offs either way.',
           "Your original email said Thursday, so I booked Thursday.",
           'Subject: Q3 numbers\n\nHi all, the Q3 numbers are attached.',
           'Next steps:\n- Sam books the venue\n- Priya sends the invite',
           'Our options are limited. Option 2 is cheaper but slower.',
           'Version 3.12 adds a new parser.',
           # Ordinary mail that an early, wider version of the rules flagged.
           "Sure! I'll send it over after lunch.",
           'Absolutely. Thursday at 3 works for me.',
           "Of course. I'll loop in Dana.",
           "Here's a quick overview of where we are on the migration:\n\n- DB moved\n- API next week",
           "Here's a breakdown of the Q3 costs:\n\nHosting 4k, staff 12k.",
           "Hi Sam,\n\nHere's a draft of the Q3 plan for your review. Comments welcome.\n\nThanks,\nPat",
           "Here's a revised version of the budget with the changes from Tuesday.",
           "Attached is a template you can adapt for your team's reports.",
           'The shorter version of the report is attached; the full one is on the drive.',
           'If you share the doc with me, I can review it tonight.',
           'If you give me a call tomorrow I can explain the numbers.',
           "If you send me your availability I'll set something up.",
           'Would you like me to walk you through the setup on Friday?',
           'Do you want me to adjust the schedule for next week?',
           'Please fill in the blanks on the attached form and return it by Friday.',
           "The short version: we're two weeks late and need more budget.",
           'My honest opinion is that we should wait until Q2.',
           'The real question is whether the vendor can deliver by March.',
           'Bottom line:\nWe need two more engineers.',
           'Key points\nBudget is flat. Hiring is frozen.',
           "Thanks,\nPat\n\nP.S. I'll be out Friday.",
           'Best,\nPat\n\nIf you have questions, call me at the office.',
           'A few notes:\n- Room 4B is booked\n- Lunch provided',
           "Remote work isn't inherently better or worse, it depends on the team.",
           'Option 1: renew the lease.\nOption 2: move to the new building.',
           'Short version:\nThe client said no.',
           "This is really common with new hires, don't worry about it.",
           "---\n\nIf you're receiving this email in error, please delete it.",
           "Our trucks don't carry freight. They carry trust.",
           # A person offering a choice, or offering to help with something other than the text.
           'Here are three options you can choose from for the venue:\n\n- The Barn\n- Hall 2',
           'Here are three options for lunch:\n\n- Tacos\n- Pho',
           'Here are three venue options:\n\n- The Barn\n- Hall 2',
           'If you tell me which dates work, I can adjust the schedule.',
           'If you tell me which dates work, I can help adjust the schedule.',
           'If you tell me what you need, I can point you to the right person.',
           'If you tell me the dates, I can make the booking earlier.',
           'If you send me the logs, I can pinpoint the problem.\n\nThanks,\nPat',
           'If you send me the logs, I can pinpoint where it fails before Friday. See you then.',
           # Ordinary mail and README lines the first chat rules flagged (review F5).
           "Hi Sam,\n\nI want to get the draft out to the board by Friday. Can you take a look before then?",
           'Hi Sam,\n\nThanks for the notes. Should I send you the latest version?',
           'Hi Sam,\n\nThe report is nearly done. Would you like the draft now or after lunch?',
           "I've booked the room for Tuesday at 3. If that's not right, let me know and I'll update the calendar.",
           'Morning! I left your notes on the desk.',
           "The totals look fine, but I didn't verify the March figures yet.",
           'Copy config.example.yml to config.yml and edit the paths to match your own setup.',
           'Copy .env.example, fill in the placeholders and save it as .env.',
           "Thanks for the list. I'm happy to add those to the agenda.",
           'Before the release there are a few things to check: the backup logs, the disk space and the cron jobs.',
           'Welcome aboard! A few tips for your first week: bring a photo ID and ask for a desk key.',
           'There are a few things to consider when choosing a database: size, cost and support.',
           "Here's a minimal template:\n\n```\nname: demo\n```",
           "Here's a basic template you can adapt for any fruit.",
           "Here's a starting point that should work on most systems.",
           'If you need a shorter alternative to the full flag, use -v.',
           'Hold the piping bag upright so it lands in the centre.',
           'He swore the cat always lands better on carpet.',
           "Here's a draft of the Q3 plan:\n\n1. Hire two engineers.\n2. Ship the importer.",
           "Hi all,\n\nHere's a draft agenda for Thursday:\n\n- Budget\n- Hiring",
           "Here's a draft for your review:\n\nThe team met on Monday.",
           'Would you like help planning the offsite?',
           'Should I rework the budget before Friday?',
           'Do you want to tailor the deck for the client?',
           # Human docs and mail the first raw rules flagged (review behaviour-F3).
           "Sure! Here's the agenda for Thursday:\n\n1. Budget\n2. Hiring",
           'Sure, let me know when you are free next week.',
           "Of course, I'd be happy to cover your shift on Friday.",
           "Absolutely, here's what I found in the March logs:\n\nThe job failed twice.",
           '### Option 1: Homebrew\n\n    brew install tidyup\n\n### Option 2: From source\n\nRun make.',
           '**Option 1: Homebrew**\n\n**Option 2: From source**',
           '## Version 2.1.0\n\n- Fixed the crash.\n\n## Version 2.0.0\n\n- Dropped Python 3.8.',
           '## Version 1.0.9 (April 24th, 2025)\n\n- Fixed the pool timeout.',
           '**Option A** runs on a single VM.',
           'Thanks,\nPriya\n\n---\nIf you have any questions about this notice, call the front desk.',
           'Install\n-------\n\nIf you use pip, run the command below.\n\nUsage\n-----\n\n'
           "Here's the shortest example.",
           # From the human control: npm's and node-gyp's own docs.
           'A couple of options of note for those in a hurry:\n\n* `token` - can be passed in.',
           '**Note:** Configuration set via `npm` will only be used when `node-gyp`\nis run via `npm`.',
           "Here's how to create a new team `newteam` under the `org` org:",
           "Here's a summary of today's standup:\n\n- Sam: release notes",
           # This repo's own README: bold lead-ins are a house style, not a chat tell.
           "- **Audience and purpose:** Does it answer the reader's likely questions?\n"
           '- **Opening:** Does the first paragraph explain the subject?\n'
           '- **Progression:** Does each sentence build on the previous one?\n'
           '- **Voice:** Does the wording suit the author and format?',
           'Intro.\n\n---\n\nAfter the rule.',
           # Closing questions with a comparative that is not about the text (review ADV-3).
           'I have both decks ready. Do you want the longer version or the summary?',
           'Thursday is packed. Would you like a shorter slot on Friday?',
           'Six more people said yes. Should I book a longer table?',
           "It's going to be 2 degrees up there. Want a warmer jacket from the lost and found?",
           'The tart failed twice. Shall I try a simpler recipe for Sunday?',
           'The pub feels too loud for the client. Would you like a more formal venue?',
           # Replies that open with "Sure" and hand over no draft (ADV-4).
           "Sure, here's the summary from today's standup:\n\n- API is done\n- Tests next",
           'Absolutely, here are the options we discussed:\n\n1. Rent\n2. Buy',
           "Of course! Here's the rundown for Saturday:\n\nDoors at 6, food at 7.",
           "Sure! Here's the breakdown of the costs:\n\nVenue 400, food 300.",
           'Certainly. Here is an overview of the changes:\n\n- New logo\n- New colours',
           # Support replies, mail and README lines close to a narrowed rule (ADV-5).
           'Hi Dana,\n\nSorry the sync keeps failing. There are a few things you may want to check '
           'before we escalate: the VPN, the disk space and the clock.\n\nThanks,\nRob',
           'Hi Lee,\n\nYour original ticket said "the app crashes on login", but the log shows a timeout.',
           "Hi Max,\n\nI've set the renewal to March 3. I'm happy to adjust it if that date doesn't suit you.",
           "I've put you down for the 9:30 slot. If that's not right, just change it in the booking page.",
           'Hi Jo,\n\nI left your original contract in your mailbox, signed.\n\nCheers,\nTom',
           'I kept your original receipts in the blue folder for the audit.',
           'Hi all,\n\nThe offsite is on Friday. A few tips for getting there: take the 8:10 train.',
           'Welcome to the club! A few tips for making the most of your first session: arrive early.',
           "Hi team,\n\nHere's an updated draft that reflects Monday's discussion: the budget moves to Q2.",
           "Here's a revised version, which fixes the totals: the grand total is now 4,120.",
           'Copy config.sample.toml to config.toml and change it to match your own environment.',
           'The defaults assume a 4-core machine; adjust them to match your actual hardware.',
           'Hum for a minute before rehearsal. It keeps your voice warm.',
           'A heavier battery makes the drone land harder, so check the legs.',
           'You can add a concrete example to make it more specific.',
           # Footers under a rule that offer something other than an edit (ADV-6).
           '# tidyup\n\nSorts your Downloads folder.\n\n---\n\nIf you want me to add a feature, open an issue.',
           "Hi Sam,\n\nThe keys are under the mat.\n\n---\n\nIf you'd like me to water the plants, text me.",
           # Docs sections that open with an option heading named by a common word (ADV-7).
           '### Option 1: Personal access token\n\nCreate a token.\n\n### Option 2: SSH key\n\nAdd your key.',
           '**Option 1: Minimal install**\n\nRun pip install foo.\n\n**Option 2: Full install**\n\nRun it all.',
           '## Option A: Direct download\n\nGrab the zip.\n\n## Option B: Package manager\n\nUse winget.',
           '## Version 2 - light theme\n\nThe second version adds a light theme and a print view.',
           # Mail, Slack and notes that the third narrowing still flagged (review R3L-6).
           'Option 1: Minimal\nKeep the current page and fix the copy.\n\n'
           'Option 2: Bold\nRedesign the page before the spring sale.',
           'Option A: Soft\nThe one we tried in the shop, 900 dollars.\n\nOption B: Firm\nCheaper, and better for your back.',
           "Thanks for the invite. I'm happy to adjust the length of the session if the room is needed after 3.",
           "Hi Dana,\n\nHere's a draft that should cover everything we discussed on Monday:\n\n"
           "The budget stays at last year's level and the launch moves to May.\n\nThanks,\nLee",
           "Hi all,\n\nHere's a revision that I think addresses Maria's comments:\n\n"
           'The budget moves to Q3 and the timeline gets two more weeks.\n\nBest,\nSam',
           "Hi team,\n\nHere's a version you can print for the meeting:\n\nAgenda: budget, hiring, the offsite.\n\nSam",
           "Here's a draft that should cover everything we discussed on Monday:\n\nThe launch moves to May.",
           "Here's a version you can print for the meeting:\n\nAgenda: budget, hiring.",
           'Sure, here are a few options:\n\n- Tue 3pm\n- Wed 10am\n- Thu 2pm',
           "I'm by the thermostat. Want it warmer?",
           'The standup ran 40 minutes today. Should I make it shorter?',
           'Same as last time. Want it shorter on the sides?',
           'Hi all,\n\nA few tips for using the new expense system:\n\nSubmit receipts within 30 days, and tag the project code.',
           'A couple of suggestions for reading the report: start with the summary, then the tables.',
           "I've booked the room for the 14th. If that's not right, just change the date on the booking page.",
           'Your original email said "Friday", but the invite says Thursday. Which is it?',
           'A few things you might want to adjust in your settings before the migration: the time zone and the '
           'default calendar.',
           'Edit `theme.css` and change it to match your own style.',
           "Sure! Here's a quick overview of how the new rota works:\n\nMornings are 8 to 12 and afternoons 12 to 5.",
           'Dear Mr. Okafor,\n\nI kept your original text of the lease and only added the new rent of 900 pounds.\n\n'
           'Yours sincerely,\nAnne',
           # Mail whose offer, question or list is about something other than the text (CHK-3).
           'Thanks for the slides. I can do a quick call at 4.',
           'I can do a longer session next week.',
           'I can drop it off on Friday.',
           'I can cut this short if we run over.',
           'I can make the meeting shorter.',
           'Thanks for the notes. Would you like a PDF version?',
           'The report is attached. Would you like a blank template?',
           'Should I bring the talking points?',
           'Do you want to set the tone with the sales numbers?',
           'Would you like me to make the room warmer?',
           'Would you like me to make a reservation for Friday?',
           'Would you like me to turn the spare room into an office?',
           'Let me know if you would like me to turn the heating up.',
           'Our winters are getting shorter, warmer and wetter.',
           'The new checkout form is shorter and friendlier.',
           'A few suggestions from the retro:\n\n- Shorter standups',
           'A few tips from my trip to Lisbon:\n\n- Take the tram',
           "I've added your wording to section 3.",
           'Please fill in the bracketed fields on the form.',
           # A PR description, a commit note and a delivery note open with a label inline (CHK-2).
           'What changed: the session cookie now lasts 30 minutes instead of 5. Users on the old '
           'build will be signed out once after the deploy.',
           '**What changed:** the export job runs at 2am now, not at midnight, so the backups finish first.',
           'Notes on delivery: the loading dock closes at 5pm, so the truck has to arrive before then.',
           'Notes on using the printer: load paper face down and wait for the green light.'):
    check('human correspondence stays clean', 'phrases' not in md_groups(ok) and not chat_reply(ok),
          f'{ok!r} -> {md_groups(ok)}, chat_reply {chat_reply(ok)}')

# Raw rules see the markdown, but never inside code: a specimen is not copy.
check('raw rules skip fenced code', 'phrases' not in md_groups('```\n**Subject:** x\n```\nDone.'))
check('raw rules skip inline code', 'phrases' not in md_groups('Bold it as `**Subject:**` here.'))
check('raw view keeps line positions', markdown_raw('a\n```\nb\n```\nc').count('\n') == 4,
      repr(markdown_raw('a\n```\nb\n```\nc')))
# Windows line ends: --text "$(cat reply.txt)" keeps them, and `$` stops short of a '\r'.
_crlf = 'Option 1:\r\n\r\nHi Sam, it moved.\r\n\r\n---\r\n\r\nA few notes: none.\r\n'
check('CRLF scores like LF', md_groups(_crlf) == md_groups(_crlf.replace('\r\n', '\n')) == {'phrases'},
      md_groups(_crlf))
_md = os.path.join(_dir, 'chat.md')
with open(_md, 'w', encoding='utf-8') as fh:
    fh.write('**Subject:** Lunch on Friday\n\nHi all, lunch is at noon.\n')
r = subprocess.run([sys.executable, f'{here}/deslop.py', _md],
                   capture_output=True, text=True, encoding='utf-8', errors='replace')
check('a .md file gets the raw rules', r.returncode != 0 and 'subject line' in r.stdout, r.stdout)

# Placeholders are listed, never scored: a rewrite must keep them, so they cannot block.
for hole in ('Best,\n[Your Name]', 'Hi [Recipient\'s Name],', 'by [day]?', 'Dear [Name],',
             # Slots from real chat answers (DAILY-1).
             "Hey [name], I'm so sorry I missed your dinner.", "Hi [Boss's name],",
             "Please contact [Hal's email/phone] while I'm away.", 'Booked under confirmation [code].',
             'Sponsorships are $[amount] each.', 'for the [Spring/Summer] season',
             'I have known her for [number] years.', 'Start with [give 1-2 specific examples].',
             'It failed often [e.g., power failures, sensor drift].',
             'It dropped 12% [Add the exact figure if you have it].',
             'The outage ran for an hour (Add details: time spent diagnosing).'):
    h = audit(markdown_prose(hole), raw=hole)
    check('placeholder listed', h['placeholders'], hole)
    check('placeholder is not scored', not any(v for k, v in h.items() if k != 'placeholders'),
          f'{hole!r} -> {h}')
for not_hole in ('The [needs number] figure goes here.', 'Use list[X] and git remote add [name] [url].',
                 'See the [Example](#example) section.', 'Run `curl [URL]` from your shell.',
                 '- [x] book venue\n- [ ] send invites', '"bar" : [Stream, Number]',
                 'Add the cache (it saves a second per call).',
                 'Install it as [name]@[version] from the registry.',
                 'Register a remote with tool add [Name] [url] first.', 'Set [Name]: a string.',
                 'Save it as [name].txt first.', 'Overwrite it? [y/N]', 'Send it as [PATCH v2 1/3].',
                 'It runs over [TCP/IP] only.',
                 'It makes [raw deflate/inflate streams][zlib].\n\n[zlib]:\n  https://example.com/zlib',
                 # Reference links and the lines that define them (CHK-5).
                 'See [your account settings][settings] to change the plan.\n\n'
                 '[settings]: https://example.com/settings',
                 'Open [your dashboard][] to see the numbers.\n\n[your dashboard]: https://example.com/d',
                 'Open [Your Dashboard] to check it.\n\n[your dashboard]: https://example.com/d "Home"'):
    h = audit(markdown_prose(not_hole), raw=not_hole)
    check('not a placeholder', not h['placeholders'], f'{not_hole!r} -> {h["placeholders"]}')
# A slot beside a link is still a slot, and so is a dialogue template's "[Your Name]:".
for hole, slots in (("Sign it [Your Name] [here](https://example.com/sign).", ['[Your Name]']),
                    ('[Your Name]: Hi, is this a good time?\n\n[Customer Name]: Sure.',
                     ['[Your Name]', '[Customer Name]']),
                    ('Best,\n[Your Name]\n\nSee [docs][d].\n\n[d]: https://example.com/docs', ['[Your Name]']),
                    # Two slots back to back are no reference link: nothing defines the second.
                    ('Dear [First Name][Last Name],\n\nThanks for the call.', ['[First Name]']),
                    ('Best,\n[Your Name][Your Title]', ['[Your Name]'])):
    h = audit(markdown_prose(hole), raw=hole)
    check('a slot near a link is listed', h['placeholders'] == slots, f'{hole!r} -> {h["placeholders"]}')
check('a placeholder is listed once',
      audit('Hi [Your Name]', raw='Hi [Your Name]\n\nBest,\n[Your Name]')['placeholders'] == ['[Your Name]'])
r = subprocess.run([sys.executable, f'{here}/deslop.py', '--text', 'Thanks for the call.\n\nBest,\n[Your Name]'],
                   capture_output=True, text=True, encoding='utf-8', errors='replace')
check('a placeholder alone still scores 5/5', r.returncode == 0 and 'score 5/5' in r.stdout, r.stdout[-80:])
check('and is still listed', '[Your Name]' in r.stdout, r.stdout)

# The reply around the draft, which a rewrite deletes: the app tells its numbers from the draft's.
from deslop import chat_framing  # noqa: E402
_fenced = chat_framing('**Subject:** Rent for May\n\nHi Corinne,\n\nI was expecting $125,000.'
                       '\n\nBest,\n[Your name]\n\n---\n\n**A few tips:**\n\n- Ask for $128k. '
                       'Review it at 6 months.')
check('a fenced note is framing to the end',
      _fenced.endswith('Review it at 6 months.') and '125,000' not in _fenced, _fenced)
check('a bold subject line is framing, its subject is not',
      '**Subject:' in _fenced and 'Rent for May' not in _fenced, _fenced)
_signed = chat_framing('Hi Sam,\n\nBooked for May 4.\n\nBest,\n[Your name]\nRoom 12\n\n'
                       'A few notes: I guessed the 4th.')
check('a note under a sign-off is framing, the signature is not',
      _signed == 'A few notes: I guessed the 4th.', _signed)
_options = chat_framing('**Option 1**\n\nSeven years at Acme.\n\n**Option 2 (shorter)**\n\n'
                        'Seven years, 2 roles.')
check('the second version is framing, the first is not',
      _options == '**Option 1**\n**Option 2 (shorter)**\n\nSeven years, 2 roles.', _options)
# A plain copy loses the ** around the labels; bold tone names need no number (review E2E-2).
_plain = chat_framing('Option 1: Short and sweet\n\nBread at 9.\n\nOption 2: Warm\n\n'
                      'Bread from 9 to 12.\n\nWant me to tweak the tone?')
check('a plain second version is framing',
      _plain == 'Option 1: Short and sweet\nOption 2: Warm\n\nBread from 9 to 12.\n\n'
                'Want me to tweak the tone?', _plain)
_named = chat_framing('Here are a few options:\n\n**Simple and sincere**\n"22 years!"\n\n'
                      '**A bit warmer**\n"Thanks for 22 years."')
check('a second version under a bold tone name is framing',
      _named.endswith('**A bit warmer**\n"Thanks for 22 years."') and '"22 years!"' not in _named,
      _named)
check('CRLF framing is LF framing',
      chat_framing(_crlf) == chat_framing(_crlf.replace('\r\n', '\n')) != '', repr(chat_framing(_crlf)))
check('human text has no framing',
      chat_framing('Hi Sam,\n\n---\n\nThe 3 rooms are booked.\n\nPat') == '')
# A plain copy of "**Warm & simple**" is a line of tones alone (review E2E3-1).
_gift = chat_framing('Here are a few options in different tones:\n\nWarm & simple\nThanks for 9 years.\n\n'
                     'A little more personal\nNine years of parcels, 3 dogs and no complaints.\n\n'
                     'Short & sweet\nThanks, and happy holidays.')
check('a plain line of tones labels a version',
      _gift.startswith('Here are a few options in different tones:\nWarm & simple\nA little more personal\n')
      and '9 years' not in _gift and '3 dogs' in _gift, _gift)
check('a text that opens with a line of tones is no menu by that alone',
      chat_framing('Simple\n\nThe 3 rooms are booked.\n\nWarm\n\nPat') == '')
# "Here's a draft:", a rule, a letter that opens like a note, a rule, an offer (review R3L-3).
_letter = chat_framing("Here's a draft:\n\n---\n\nI'd like to let you know that I am resigning. My last day is "
                       'March 28.\n\nBest,\nRiley\n\n---\n\nWant me to make it more formal?')
check('a rule that opens the draft is not a fenced note',
      '28' not in _letter and _letter.endswith('Want me to make it more formal?'), _letter)
_noted = chat_framing('Hey team, the machine works again.\n\n---\n\nA few notes: it took 3 days.\n\n---\n\n'
                      'Want me to make it warmer?')
check('a rule under a one-line draft still fences the note', '3 days' in _noted, _noted)
# A menu with no labels: the second item, row, quote or paragraph starts the framing (R3L-2).
for _shape, _menu in (
        ('numbered', '1. Quick update on Friday\n2. Friday launch: 3 things to know\n3. See you at 3pm'),
        ('table', '| Tone | Caption |\n|---|---|\n| Short | Buns out. |\n| Warm | Bread at 8, 3 kinds. |'),
        ('quoted', '> Running late, sorry!\n\n> Running 10 minutes behind, save me a seat.'),
        ('plain', 'Running late, sorry!\n\nRunning 10 minutes behind, save me a seat.\n\nWant it warmer?')):
    _cut = chat_framing('Here are a few options:\n\n' + _menu)
    check(f'a {_shape} menu with no labels: the second version is framing, the first is not',
          _cut.startswith('Here are a few options:') and ('3' in _cut or '10' in _cut)
          and not any(w in _cut for w in ('Quick', 'Buns', 'late, sorry')), _cut)
check('a table menu: its header labels the versions', '| Tone | Caption |' in chat_framing(
    'Here are a few options:\n\n| Tone | Caption |\n|---|---|\n| Short | Buns out. |\n| Warm | Bread. |'))
# A pasted table pads "Option 1" out to its next column, and the app holds the GIL while it
# checks. The command line has no checkable() either, so blank lines under a sign-off count.
_began = time.time()
audit('', raw='Option 1' + ' ' * 5000 + 'Basic\n' + ('Version 2' + '\t' * 40 + '2024\n') * 4000)
audit('', raw="Here's a draft:" + ' ' * 20000 + 'x')
audit('', raw='Best,\n[Your name]\n' + ' \n' * 40 + '-- \n555 0100')
audit('', raw='Option 1: Short' + ' \t' * 3000 + '&' + ' ' * 3000 + 'x\n**Warm &' + ' ' * 6000 + 'x')
audit('', raw="Here's a draft:\n\nWarm" + ' ' * 20000 + 'x')
check('a run of blanks after a chat label is checked quickly', time.time() - _began < 2,
      f'{time.time() - _began:.1f}s')
# The command line and CI have no length cap: a long constant or a run of blank lines must not
# be tried from every position in it (CHK-6).
_began = time.time()
PROOF.search('Pi to 1000 places: ' + '7' * 1000 + '.')
markdown_prose('Intro.\n' + '\n' * 20000 + '- Item\n' + ' \n' * 5000 + '> Quote')
check('a long digit run and a long blank run are checked quickly', time.time() - _began < 1,
      f'{time.time() - _began:.1f}s')

# Docs have option headings and rules too. Alone, neither is a chat answer, and a doc that
# scored it as one would have its second option deleted as "framing", numbers and all.
for doc in ('# Install\n\n## Option 1: pip\n\nRun it.\n\n## Option 2: from source\n\nPort 8443.',
            '# Changelog\n\n## Version 2.1.0\n\n- Fixed 3 crashes.\n\n## Version 2.0.0\n\n- New.',
            'Pick one.\n\n**Option A** - Docker.\n\n**Option B** - run it with Node 18.',
            '---\ntitle: Setup\n---\n\nIf the build fails, wait 30 seconds and run it again.',
            'Installation\n------------\n\nIf you use pip, pin version 2.',
            'Usage\n-----\n\nNote that the port is 8443.',
            '# tinylog\n\nA logger.\n\n---\n\nIf you like this project, give it a star.',
            'The rain had not stopped.\n\n***\n\nI left the house at 5.',
            # Release notes under bare or dated version headings (CHK-4).
            '## Version 2\n\nUploads up to 50 MB.\n\n## Version 1\n\nFirst release, 14 March 2024.',
            '### Version 2 (2 May 2024)\n\nUploads up to 50 MB.\n\n### Version 1 (14 March 2024)\n\nFirst release.',
            # A section break, then a paragraph that opens like a chat note but is not one.
            '# Moving the shop\n\nWe signed the lease in March.\n\n---\n\nA few weeks later the roof leaked, '
            'and we lost 12 boxes.',
            'Hi everyone,\n\nThe offsite is on 12 May.\n\n---\n\nA few reminders: bring two forms of ID.',
            '# Installing Tally\n\n### Option A\n\npip install tally-cli. Needs Python 3.11.\n\n### Option B\n\n'
            'Build from source in 15 minutes.\n\n---\n\nA couple of known issues: the build fails on 32-bit Windows.',
            'My grandfather kept bees for 30 years.\n\n***\n\nOne thing he taught me was to move slowly.'):
    check('a doc is not a chat answer', 'phrases' not in md_groups(doc), doc)
    check('and has no framing', chat_framing(doc) == '', doc)
# A cliché is flagged as a sentence pattern, but it is copy, not the reply around a draft.
_cliche = 'The new office has the best of both worlds: a quiet floor and a cafe downstairs.'
check('a cliché alone is flagged but is no chat reply', 'phrases' in md_groups(_cliche) and not chat_reply(_cliche),
      f'{md_groups(_cliche)}, chat_reply {chat_reply(_cliche)}')
# A cliché or a reframe is copy a person writes too: it is no chat answer around the doc (CHK-1).
_guide = ('# Install\n\n{}\n\n### Option A (recommended)\n\nUse pip with Python 3.11.\n\n'
          '### Option B\n\nBuild it with make: 4 GB of RAM, about 15 minutes.\n\n---\n\n'
          'If you get stuck, open an issue.')
for _copy in ('It gives you the best of both worlds.', "It isn't about speed, it's about control.",
              'The honest answer is that most people want pip.'):
    check('a copy-level phrase does not make a doc a chat answer', chat_framing(_guide.format(_copy)) == '',
          chat_framing(_guide.format(_copy)))
check('a fenced "A couple of..." note is a chat answer on its own',
      'phrases' in md_groups('Each mug is thrown by hand.\n\n---\n\n'
                             'A couple of optional additions if they apply: dimensions.'))
# Labels that still open a menu of drafts beside the release-note exemption.
for _menu in ('## Option 1 (concise)\n\nHi Sam, thanks.\n\n## Option 2 (warm)\n\nHey Sam!',
              '**Version 1:**\n\nHi Sam, thanks.\n\n**Version 2:**\n\nHey Sam!',
              '## Version 1\n\nHi Sam, thanks.\n\n## Version 2\n\nHey Sam!\n\nWant me to make it shorter?'):
    check('a menu of drafts still has its framing', 'Hey Sam!' in chat_framing(_menu), _menu)
check('a fenced offer is a chat answer on its own',
      'phrases' in md_groups('Thanks for 35 years.\n\n---\n\n'
                             'If you tell me his name, I can work it in.'))

# HTML has no raw view: audit without raw behaves exactly as before.
check('audit without raw has no placeholders', audit('Best, [Your Name]')['placeholders'] == [])
check('audit without raw runs no raw rule',
      'phrases' not in groups('**Subject:** Lunch\n\nOption 1:\n\n## Key takeaways'))
r = subprocess.run([sys.executable, f'{here}/deslop.py', os.path.join(_dir, 'utf8.html')],
                   capture_output=True, text=True, encoding='utf-8', errors='replace')
check('HTML lists no placeholders', 'placeholders' not in r.stdout, r.stdout)

if fails:
    print(f'{len(fails)} FAILED\n')
    for f in fails:
        print(f'  · {f}')
    sys.exit(1)
print('all tests passed')
