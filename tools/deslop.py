#!/usr/bin/env python3
"""Hunt AI tells in the visible copy of a built page. Regex, no opinions, exits red.

    python3 deslop.py index.html
    python3 deslop.py index.html --view view-site     # score one tab only
    python3 deslop.py --text "some copy to check"

This reads only what a visitor can SEE: it strips <script>, <style>, and every HTML
tag, so it scores the words on the page rather than the markup around them.

Scoring is out of 5. Below 5 exits non-zero. That is deliberate — "mostly clean"
copy is how a page ends up sounding like every other AI page on the internet.
"""
import html as _html   # aliased: `visible_text` takes a parameter named `html`
import re
import sys

# ── the catalogue ────────────────────────────────────────────────────────────
# Grouped by why they are a tell, because the fix differs per group.

# Matched by root, so every inflection fires: `elevate` also catches elevates,
# elevated, elevating, elevation. Landing-page copy is written in the third
# person ("Acme elevates your workflow"), so exact-string matching missed the
# single most common surface form of every word here.
VOCAB = [
    'delve', 'leverage', 'seamless', 'elevate', 'robust', 'unlock', 'unleash',
    'empower', 'streamline', 'cutting-edge', 'state-of-the-art', 'game-changer',
    'game-changing', 'revolutionize', 'revolutionise', 'transformative',
    'transformation', 'innovate', 'holistic', 'synergy', 'synergies', 'paradigm', 'bespoke',
    'meticulous', 'tapestry', 'testament', 'beacon', 'unparalleled', 'supercharge',
    'turbocharge', 'effortless', 'next-level',
    # second tier: fine once in a long page, damning in every section
    'pivotal', 'foster', 'showcase', 'compelling', 'intuitive', 'world-class',
    'best-in-class',
]

# Words with an ordinary literal sense — "we craft furniture", "harness the
# horse", "the landscape of the valley". Matched exactly, never by root, so the
# innocent use survives and only the marketing inflection is caught.
VOCAB_EXACT = [
    'crafted', 'curated', 'harnessing', 'harness the power', 'journey', 'realm', 'landscape',
    'navigate the', 'in the world of', "in today's", 'ever-evolving', 'fast-paced',
    'look no further', 'dive in', "let's dive", 'deep dive', 'embark',
    'unlock the power', 'buckle up', 'the secret sauce', 'level up',
]


def _root_pattern(word):
    """A regex matching `word` and its inflections.

    Strip a trailing e/ed/ing/ly to get the root, then allow the suffixes back.
    The bare `e?` alternative is load-bearing: without it, stripping the `e` from
    `elevate` leaves `elevat`, which no longer matches the base form itself.
    """
    root = re.sub(r'(ed|ing|ly|e)$', '', word)
    if len(root) < 4:                     # too short to stem safely
        return rf"(?<!\w){re.escape(word)}(?!\w)"
    return rf"(?<!\w){re.escape(root)}(?:e|es|ed|ing|ion|ions|ional|ive|al|ally|s|ly|ness)?(?!\w)"

# Negated "just/only/merely/simply", in either register.
#
# `\bnot\b` cannot match inside `isn't` — there is no standalone `not` token there — so
# an uncontracted-only pattern misses every contracted form, which is the register a
# model reaches for when it is trying hardest to sound human. Matching the `n't` suffix
# covers isn't / aren't / wasn't / doesn't / don't / won't / can't without enumerating
# them. Straight and curly apostrophes both.
_NEG_JUST = r"(?:\bnot|n['’]t)\s+(?:just|only|merely|simply)\b"

# The Y clause of the swap, reached across a comma or a single full stop. It is not
# always a copula — "it doesn't just park you, it GETS you there" is the same move — so
# this takes a pronoun subject and lets any verb follow. The punctuation carries the
# precision: "It's not just about money." has no Y clause and stays clean, and so does
# "Do not just take my word for it."
_XY_TAIL = r"[^!?]{0,80}?[,.]\s*(?:it|this|that|they|we|you|he|she|i)\b"

PHRASES = [
    # constructions, not words — these are the loudest tells
    # The contracted and uncontracted forms both matter: formal register is not an
    # adversarial rewrite, it is the default thing a model emits.
    (_NEG_JUST + r"[^.!?]{0,80}\bbut\b", "the 'not just X, but Y' construction"),
    (_NEG_JUST + _XY_TAIL, "the 'not just X, it's Y' construction"),
    (r"\bwhether you(?:'?re| are)\b[^.!?]{0,40}\bor\b", "the 'whether you're X or Y' opener"),
    (r"\bmore than just\b",                        "'more than just'"),
    (r"\b(that|this)(?:'?s| is) where\b[^.!?]{0,30}\bcomes? in\b", "'that's where X comes in'"),
    (r"\bsay goodbye to\b",                        "'say goodbye to'"),
    (r"\bimagine (a|an|the)\b",                    "the 'imagine a…' opener"),
    (r"\bin conclusion\b|\bto sum up\b",           "essay-summary phrasing"),
    (r"\bwhen it comes to\b",                      "'when it comes to' filler"),
    (r"\bat the end of the day\b",                 "'at the end of the day'"),
    (r"\bthe key is\b|\bthe truth is\b",           "throat-clearing opener"),
    (r"\bhelps? you to\b|\bcan help you\b",        "hedged benefit ('helps you to…')"),
    (r"\bmay potentially\b|\bcould potentially\b|\bmight possibly\b", "stacked hedging"),
    (r"\bvery unique\b|\bquite literally\b",       "intensifier padding"),
    # Openers and self-answering questions. Cheap literals, near-zero false
    # positives, and they cover the register a pure vocabulary list cannot see.
    (r"\bhere'?s the thing\b|\blet'?s break (it|this) down\b|\bthe best part\b",
                                                   "throat-clearing opener"),
    (r"\bready to get started\b|\blet'?s get started\b", "boilerplate CTA"),
    (r"\bthe (result|answer|catch|kicker|upshot)\?\s", "self-answering question"),
]

# The chat answer around a draft. Pasted straight from a chat window, a reply carries the
# assistant's own voice as well as the deliverable: the "Here's a draft you can adapt:" in
# front, the "Want me to make it warmer?" behind, the notes on what it changed. None of that
# is marketing slop, so the catalogue above never saw it, and a raw paste scored 5/5. These
# run over the prose, lower-cased, like PHRASES, and report under the same category.
#
# Every rule was measured against human email lines as well as human docs, and each one is
# narrower than the obvious version for that reason: "Here's a draft of the Q3 plan", "If
# you send me the file, I'll look", "A couple of notes:" and "Would you like me to walk you
# through it?" are all ordinary mail. What is left needs the edit itself in the sentence.
CHAT_PHRASES = [
    # Not "here's the latest version" or "here's a draft of": a person handing over a file.
    # Nor "here's a minimal template:" or "here's a starting point that should work": a README.
    # Nor "here's a version you can print for the meeting:", "here's a draft that should cover
    # everything from Monday:" or "here's a revision that I think fixes it:": colleagues' mail.
    (r"\bhere'?s (?:a|an|one) (?:[\w-]+ ){0,3}?(?:draft|version|rewrite|revision|toast)"
     r'\b(?:[ ,]+(?:you (?:can|could) (?:adapt|tailor|tweak|edit|customi[sz]e|personali[sz]e'
     r'|use|send|copy|paste|post|adjust)\b|(?:that|which)(?:\'s| is| are)? (?:(?:a bit |more '
     r'|still |both )?(?:warm|short|brief|concise|clear|polite|firm|friendly|professional|casual'
     r'|formal|direct|confident|grounded|personal|upbeat|simple)\b|keeps|stays|sounds|reads))'
     r'[^.!?\n]{0,80}?)?(?::|\byou can\b|\bthat (?:keeps|stays|sounds|reads)\b)'
     r"|\bi'?ll give you (?:a|an|two|three) (?:[\w-]+ ){0,2}?(?:draft|version|options?"
     r'|alternatives?)\b'
     r'|\ba (?:slightly )?(?:shorter|longer|warmer|punchier|tighter|more formal|more casual'
     r'|less formal) (?:version|alternative|option)\b(?! (?:of|to)\b)'
     # Not "here are three options you can choose from": that is a person offering a choice.
     r'|\bhere are (?:a few|some|two|three|four|five|several|\d) (?:[\w-]+ )?(?:options|versions'
     r'|drafts|alternatives|variations)\b[^.!?\n]{0,40}?\byou can (?:use (?:it |them )?(?:as[- ]is'
     r'|directly|verbatim)|mix|adapt|tweak|tailor|customi[sz]e|copy)\b'
     r'|\b(?:draft|version) you can (?:adapt|tailor|tweak|edit|customi[sz]e|personali[sz]e)\b',
     "draft handover ('here's a draft you can adapt:')"),
    # The closing question has to be the last thing in the text and has to offer an edit, in
    # the sentence that asks it: not "I want the draft out by Friday. Can you look?", nor
    # "Should I send you the latest version?" or "Should I rework the budget?".
    # The comparative has to be about the text: "want it warmer", "make it shorter", never
    # "would you like a shorter slot" or "should I book a longer table". Nor "want it warmer?"
    # by a thermostat, "should I make it shorter?" of a meeting or "want it shorter on the
    # sides?" at the barber's: "me to" or the text by name has to be there.
    (r'(?<![\w,] )\b(?:want|would you like|shall i|should i|do you want)\b'
     r'(?=[^?]{0,300}\?\s*\Z)[^.!?]{0,200}?\b(?:(?:me to \w+ (?:it|this)|(?:me to \w+ )?'
     r'(?:the|this) (?:draft|email|message|note|letter|post|toast|caption|bio|card|reply|speech'
     r'|intro|opening|ending|wording|text|copy|version))'
     r' (?:a (?:bit|little|touch) |slightly |even )?(?:shorter|longer|warmer|cooler|funnier'
     r'|punchier|tighter|friendlier|simpler|more (?:formal|casual|playful|minimal|personal'
     r'|professional|sentimental|concise|direct|detailed|upbeat|polished|conversational)'
     r'|less formal)'
     # Not "do you want to set the tone with the numbers?", "would you like a PDF version?",
     # "would you like a blank template?" or "should I bring the talking points?".
     r'|(?:adjust|tweak|change|shift|soften|lighten|alter|rework) (?:the )?tone|a different tone'
     r'|(?:a|an|another) (?:(?:shorter|longer|warmer|cooler|funnier|punchier|tighter|friendlier'
     r'|simpler|different|new|second|fresh|revised|(?:more|less|slightly|a bit) \w+) )?(?:version'
     r'|draft|rewrite)|me to (?:rework|condense|tailor|sketch)|(?:draft|write|prepare|sketch'
     r'|put together) (?:some |a few )?talking points|help sketching)\b'
     # Not "would you like me to make a reservation?" or "... to turn the heating up".
     r"|\b(?:would you like me to|let me know if you(?:'d| would) like me to) (?:adjust|tweak"
     r'|make (?:it|this|the (?:draft|email|message|note|letter|post|toast|caption|bio|card|reply'
     r'|speech|intro|opening|ending|wording|text|copy|version|tone))|shorten|lengthen|expand'
     r'|tighten|trim|rework|rewrite|revise|tailor|adapt|turn (?:it|this|that|these|them) into'
     r'|punch up|tone down|go deeper|dig deeper)\b'
     r"|\blet me know if you(?:'d| would) like (?:a |an )?(?:shorter|longer|deeper"
     r'|more detailed|different|simpler|quick|warmer|more formal) (?:version|explanation'
     r'|breakdown|take|comparison|summary|dive|tone)\b'
     r'|\b(?:warmer|more formal|more casual|punchier|funnier|more playful|more personal'
     r'|more professional|more sentimental|more minimal|shorter|friendlier|more upbeat'
     # A menu of tones, said as a choice: "or", or a question. Not "our winters are getting
     # shorter, warmer and wetter" or "the new form is shorter and friendlier".
     r'|more direct|more conversational)(?:,? or |(?:,? and |, )(?=[^.!?\n]{0,80}(?:\?|\bor\b)))'
     r'(?:a bit |slightly |even )?(?:warmer'
     r'|more formal|more casual|punchier|funnier|more playful|more personal|more professional'
     r'|more sentimental|more minimal|shorter|friendlier|more upbeat|more direct'
     r'|more conversational)\b',
     "offer to redo ('want me to make it shorter?', 'warmer, more formal, or shorter?')"),
    # An offer to edit the text itself. "I can make Tuesday work" is not one, nor "I can make
    # the meeting shorter", "I can drop it off on Friday", "I can cut this short" or "I can do
    # a quick call at 4".
    (r'\bi can (?:also )?(?:make (?:it|this|the (?:wording|tone|draft|language|opening|ending'
     r'|intro|subject line|email|message|letter|post|bio|toast|paragraph|note|reply|caption'
     r'|speech|text|copy|version|headline|tagline|summary|sign-off))'
     r' (?:a bit |slightly |even )?(?:warmer|shorter|longer|more|less|punchier|funnier'
     r'|friendlier|tighter|simpler)|(?:tighten|trim'
     r'|shorten|soften|rework|tweak|adjust|tailor|personali[sz]e|polish|condense|lengthen'
     r'|punch up|tone down|drop|cut) (?:it|this|that|the (?:wording|tone|draft|language|opening'
     r'|ending|intro|subject line|email|message|letter|post|bio|toast|paragraph)))\b'
     r'(?<!drop it)(?<!drop this)(?<!drop that)(?<!cut it)(?<!cut this)(?<!cut that)'
     r'|\bi can (?:also )?(?:drop|cut) (?:it|this|that)\b(?! (?:off|short|by|at|round|over|on'
     r'|into|in(?! half))\b)'
     r'|\bi can (?:also )?do a (?:warmer|shorter|longer|funnier|punchier|quick|more|less|formal'
     r'|casual)(?:,? (?:or )?[\w-]+){0,4}? (?:version|draft|take|pass|rewrite|revision|edit)\b'
     r'|\bi can (?:also )?draft (?:talking points|a (?:shorter|longer|follow-up|reply'
     r'|version))\b'
     # The edit needs the text as its object: "if you tell me the dates, I can adjust the
     # schedule" and "... I can point you to the right person" are a person at work.
     r"|\bif you (?:tell|share|paste|give|show) me\b[^.!?]{0,160}\bi(?:'ll| can"
     r'| could) (?:also )?(?:help (?:you )?)?(?:(?:tailor|tighten|adjust|rework|personali[sz]e'
     r'|refine|sharpen|tweak|polish|customi[sz]e) (?:it|this|that|these|them|the (?:[\w-]+ )?'
     r'(?:wording|tone|draft|message|email|letter|note|post|bio|caption|paragraph|opening'
     r'|closing|intro|ending|abstract|speech|toast|card|reply|version|language|text|copy))\b'
     r'|do a (?:quick|rough)|make (?:it|this|these|them) (?:more|less|warmer|shorter|longer'
     r'|punchier|funnier|friendlier|tighter|simpler)\b)'
     # Or to refine the answer, as its last sentence: "If you tell me your recipe, I can pinpoint
     # it more precisely." Not "... I can point you to the right person", nor one with a sign-off
     # or another sentence after it: "If you send me the logs, I can pinpoint it. Thanks, Pat".
     r"|\bif you (?:tell|share|give|send|show|let) me\b[^.!?]{0,200}\bi(?:'ll| can| could) (?:also )?"
     r'(?:help (?:you )?)?(?:pinpoint|narrow (?:it|this|that|things|them) down|be more (?:specific'
     r'|precise)|give (?:you )?(?:a |an )?(?:more )?(?:specific|precise|tailored|targeted)'
     r'|point you to (?:their|its|the) (?:specific|exact))\b(?=[^.!?]{0,120}[.!?]?\s*\Z)',
     "offer to edit ('I can make it warmer', 'if you tell me X, I can...')"),
    # Not "I'm happy to adjust it if that date doesn't suit you": the object is the wording.
    # Nor a tutor's "add an example to make it more specific", or "I'm happy to adjust the
    # length of the session".
    (r"\bi'?m happy to (?:work (?:it|that|them|those|any of (?:those|these|that|them)) in"
     r'|(?:tailor|adjust|tweak) (?:it|this) (?:further|more)|(?:tailor|adjust|tweak) the'
     r' (?:tone|wording|draft))\b'
     r'|\byou can add\b[^.!?]{0,120}\bto make it (?:stronger|more personal|punchier)\b',
     "offer to edit ('I'm happy to work any of those in')"),
    # Not "a couple of notes:" on its own: people write that in mail every day. Nor "a few
    # things to check:" or "a few tips for your first week:"; "tips for making this fair:" is,
    # but "tips for getting there:", "tips for making the most of it:" and "tips for using the
    # new expense system:" are not.
    (r'\ba (?:few|couple(?: of)?) (?:quick |optional |small )?(?:tips|suggestions|tweaks)\b'
     r'(?! from\b)(?! for (?!(?:\w+ing (?:it|this|that)\b|(?:delivering|sending|giving|tweaking'
     r'|personali[sz]ing|customi[sz]ing|finali[sz]ing)\b)))[^.!?:]{0,60}:'
     r'|\ba (?:few|couple(?: of)?) notes on what i changed\b',
     "notes to the requester ('a few tips since you're nervous:', 'Notes for tweaking:')"),
    # Not a support reply's "a few things you may want to check before we escalate", nor "a few
    # things you might want to adjust in your settings before the migration:". The list follows.
    (r'\ba (?:few|couple(?: of)?) (?:\w+ )?things you (?:may|might|could) want to'
     r' (?:fill in|adjust|tweak|personali[sz]e|customi[sz]e)(?: (?:or|and) (?:consider|check'
     r'|change|fill in|adjust|tweak|personali[sz]e|customi[sz]e))?(?: before (?:sending'
     r'|posting|you send(?: it)?))?[ \t]*:',
     "notes to the requester ('a couple of things you may want to fill in')"),
    (r"\bi(?:'ve)? (?:kept|left|used|framed|combined|pulled|swapped|tightened|softened"
     # "I left your original contract in your mailbox" is a person; the draft's words are not.
     # Nor "I kept your original text of the lease": the text of a document, not of the draft.
     r'|trimmed|went with)\b[^.!?]{0,80}\b(?:placeholders?|brackets?|your (?:original (?:draft'
     r'|text|wording|phrasing|structure|version|meaning|tone|voice|ending|opening)|voice'
     r'|tone|wording))\b(?! of\b)'
     # Nor "I've added your wording to section 3": a person merging a colleague's text.
     r"|\bi(?:'ve)? added\b[^.!?]{0,80}\b(?:placeholders?|brackets?)\b"
     r'|\bi kept (?:it|this|the tone)\b[^.!?]{0,40}\b(?:concise|warm|short|brief|simple'
     r'|professional|friendly|casual|light)\b'
     # Not 'your original email said "Friday"' or 'your original message said': mail.
     r'|\byour original (?:(?:draft|text|wording|version|post|note|notice|letter|bio|line'
     r'|sentence|paragraph|essay|copy|caption) )?(?:says|said|reads|had) [\"“]'
     # Not a README's "fill in the placeholders", "edit the paths to match your own setup" or
     # "change it to match your own style", nor "if that's not right, let me know and I'll
     # update the calendar", "... just change it in the booking page" or "... just change the
     # date on the booking page".
     # Nor a form's "fill in the bracketed fields".
     r'|\bfill in the bracketed (?:parts|bits|details|info|information|spots|sections|text'
     r'|placeholders|words)\b'
     r'|\b(?:adjust|tweak|change) (?:the example above|the tone) to match your\b'
     r'|\b(?:adjust|tweak|change) (?:them|it|this) to match your (?:actual|own|usual|real)'
     r' (?:tone|voice|wording)\b'
     r"|\bif (?:it|that|this)(?:'s| is) not (?:right|accurate|correct"
     r'|your style),? (?:just )?(?:(?:adjust|change|swap|edit|tweak|cut|replace) (?:that'
     r' (?:line|sentence|part|bit)|this (?:line|sentence|part|bit)|the (?:line|sentence'
     r'|wording))|swap in)\b'
     r'|\bi guessed at\b'
     r'|\b(?:that|which|still|while|and) keeps? your voice\b',
     "remark on the edit ('I kept it warm but concise', 'fill in the bracketed parts')"),
    (r'\b(?:my|the|an) honest (?:answer|caveat|take)\s*:'
     r'|\bthe honest (?:answer|caveat|truth) is\b',
     "candour label ('my honest answer:', 'the honest truth is')"),
    (r"n't (?:\w+ ){1,2}(\w{3,})[.;,] (?:it|they|this|that)(?:'s| is| are"
     r"|'re)? (?:\w+ ){1,2}\1\b",
     "the 'doesn't make heat. it moves heat' reframe"),
    (r'\bnot because\b[^.!?]{0,80}?\bbut because\b'
     r"|(?:\bnot|n't)(?: really| just| only)? about\b[^.!?]{0,80}?(?:[,;.]\s*(?:it'?s|it is"
     r"|this is|that'?s)|\bbut)\s+(?:really |all |more )?about\b",
     "the 'not about X, it's about Y' / 'not because X, but because Y' reframe"),
    # "The joke didn't land" is plain English, and so are "the cat lands better" and "so it
    # lands in the centre". "Makes the thanks land harder" and "so the takeaway lands" are tells.
    # Nor "a heavier battery makes the drone land harder".
    (r'\bmakes? (?:the |your |it |this |that )?(?:(?:takeaway|joke|line|point|thanks|feedback'
     r'|message|praise|apology|ending|punchline|opening|ask|request|compliment|story) )?land'
     r' (?:better|harder|softer'
     r'|differently)\b'
     r'|\b(?:takeaway|joke|line|point|thanks|feedback|message|praise|apology|ending'
     r'|punchline) lands\b(?! (?:on|in|at|into)\b)',
     "'land' as a verb of effect ('so the takeaway lands', 'makes it land harder')"),
    (r'\bbest of both worlds\b'
     r'|\bno two (?:[\w-]+ ){0,2}(?:are|is|look|will be) (?:exactly |quite |ever )?(?:alike'
     r'|the same|identical)\b'
     r'|\bpart of the charm\b'
     r'|\b(?:learned|learnt|taught me) more (?:from (?:you|them|each of you'
     r'|all of you) )?than i (?:ever )?(?:taught|could teach|gave|could give)\b'
     r'|\bnot the (?:experience|impression) (?:we|i) (?:want|wanted|hoped)\b',
     "stock cliché ('best of both worlds', 'no two are alike', 'taught me more than I taught')"),
]
PHRASES += CHAT_PHRASES
# The first seven speak to the requester: the handover, the offers, the notes, the remarks.
# The rest (candour, reframes, 'land', clichés) are copy a person could write as well.
REPLY_PHRASES = CHAT_PHRASES[:7]

# The same, for tells that live in the markdown itself: a line of its own, the first line, a
# `---` rule with a note under it, bold around a subject line. markdown_prose throws all of
# that away, so these run over markdown_raw(), the text with only its code taken out. Case
# and flags are in each pattern; re.M is on. A hit counts under 'phrases', so a chat answer
# costs one point at most, the same as one "not just X, but Y".
_TONE = (r'(?:(?:a bit|a little|slightly) )?(?:more |less )?(?:short|shorter|longer|simple|punchy|punchier'
         r'|concise|professional|warm|warmer|casual|formal|friendly|playful|fun|funny|direct'
         r'|brief|detailed|heartfelt|polished|sweet|minimal|personal|upbeat|light|lighthearted'
         r'|straightforward|enthusiastic|conversational|bold|witty|sincere|sentimental|cozy|cosy'
         r'|cheerful|catchy|cute|quirky|cheeky|clever|elegant|gentle|polite|informal|neutral'
         r'|humorous|heartwarming|inspirational|motivational|nostalgic|emotional|romantic'
         r'|uplifting|inviting|welcoming|festive|clear|crisp|soft|firm|calm|honest|encouraging'
         r'|supportive|grateful|appreciative|apologetic|to the point)\b')
# One tone or several: "Short and sweet", "Warm & friendly", "Short & to the point".
_TONES = _TONE + r'(?:[ \t]*(?:&|\+|/|,|and\b|or\b)[ \t]*' + _TONE + r')*'
# How an opener can say that what follows is a menu of drafts.
_MENU_SAID = (r' in (?:a few |several )?different (?:tones|styles|lengths)'
              r'|,? (?:ranging )?from [^\n:]{1,60} to [^\n:]{1,60}')
RAW_PHRASES = [
    # "Sure!" alone is a Slack reply, and "Sure, here's the agenda:", "Sure, here's the summary
    # from today's standup:" or "Sure, let me know" is mail. It counts only in front of the
    # draft handover below, or of an explainer: "Sure! Here's a quick overview of how DNS works:".
    # Not "Sure! Here's a quick overview of how the new rota works:": the team's own news.
    (r'(?i)\A\s*(?:\*\*)?(?:sure|absolutely|certainly|of course|gladly|great question'
     r"|good question|happy to help)\s*[!.,]\s*(?:here['’]?s|here is|below is) (?:a|an)"
     r' (?:quick |brief |simple |clear |high-level |detailed |step-by-step )?(?:overview'
     r'|breakdown|explanation|rundown|summary) of (?:(?:how|why|what) (?!(?:the new|the current'
     r'|our|your|my|this|these|those|we|you|i|they)\b)|the (?:key |main )?(?:differences'
     r'|pros and cons|trade-?offs)\b)'
     r'|\A\s*(?:\*\*)?(?:great question|good question)\s*!'
     # Not "Here's a draft of the Q3 plan:", "Here's a draft for your review:", "Here's a
     # draft that reflects Monday's discussion:" or "Here's a version you can print for the
     # meeting:". A that/which clause has to say how it reads.
     r'|\A\s*(?:\*\*)?(?:(?:sure|absolutely|certainly|of course|gladly|happy to help)\s*[!.,]'
     r"\s*)?(?:here['’]?s|here is|here are|below is|below are) (?:a |an )?(?:quick "
     r'|brief |simple |clear |high-level |detailed |step-by-step |full |concise |more '
     r'|slightly )?(?:polished |cleaned-up )?(?:draft|version|rewrite|revision|toast'
     r'|template)\b(?:[ \t,]+(?:you (?:can|could) (?:adapt|tailor|tweak|edit|customi[sz]e'
     r"|personali[sz]e|use|send|copy|paste|post|adjust)\b|(?:that|which)(?:['’]s| is| are)? "
     r'(?:(?:a bit |more |still |both )?(?:warm|short|brief|concise|clear|polite|firm|friendly'
     r'|professional|casual|formal|direct|confident|grounded|personal|upbeat|simple)\b|keeps'
     r'|stays|sounds|reads))[^\n]{0,150})?:[ \t]*(?:\*\*[ \t]*)?$'
     # A menu of drafts, said as nothing else: "Here are three options for lunch:" stays clean,
     # and so does "Absolutely, here are the options we discussed:". Plain "options" need a
     # draft under them: a sentence, four words or more, a label, a quote or a table. Not
     # "Sure, here are a few options:" over "- Tue 3pm".
     r'|\A\s*(?:\*\*)?(?:(?:sure|absolutely|certainly|of course|gladly|happy to help)\s*[!.,]'
     r'\s*)?(?:here are|below are) (?:a few |some |two |three |four |five |several '
     r'|\d )?(?:more |other |different |possible |alternative |short |shorter )?(?:(?:versions'
     r'|drafts|variations|captions)(?:' + _MENU_SAID + r')?|(?:options|alternatives)(?:'
     + _MENU_SAID + r'|(?=[ \t]*:[ \t]*(?:\*\*[ \t]*)?\n(?:[ \t]*\n)*(?:[^\n]*[.!?]["”’)]?'
     r'[ \t]*$|[ \t]*(?:[-*+][ \t]+|\d{1,2}[.)][ \t]+)?(?:[^ \t\n]+[ \t]+){3}[^ \t\n]'
     r'|[ \t]*(?:#|\*\*|>|\||(?:option|version|alternative)\b|' + _TONES
     + r'[ \t]*(?::[ \t]*)?$)))))[ \t]*:[ \t]*(?:\*\*[ \t]*)?$',
     "chat opener ('Sure! Here's a draft:', 'Here are a few options:')"),
    # Each blank run follows its own mark. Two runs side by side split a line of spaces every
    # way there is before `$` fails: 'Option 1', 130 spaces and a word took ten seconds.
    (r'(?im)^[ \t]*(?:#{1,4}[ \t]*)?(?:\*\*)?(?:option|version|draft|alternative'
     r'|variation)(?:[ \t]+(?:\d{1,2}|[a-c]|one|two|three)\b(?:\*\*)?[ \t]*'
     r'(?:\([^)\n]{1,40}\)[ \t]*)?(?::[ \t]*)?|[ \t]*(?:\([^)\n]{1,40}\)[ \t]*)?:[ \t]*)'
     r'(?:\*\*[ \t]*)?(?::[ \t]*)?$'
     # With a name after the number, only tones, and nothing after them but a note in brackets:
     # "**Option 1: Short and punchy**", "Version 2: Warm & friendly" copied as plain text,
     # never "### Option 1: Homebrew", "## Version 2.1.0", "**Option A** runs on one VM.",
     # "### Option 1: Personal access token" or "## Version 2 - light theme".
     r'|^[ \t]*(?:#{1,4}[ \t]*|\*\*)?(?:option|version|alternative|variation)[ \t]+(?:\d{1,2}'
     r'|[a-c]|one|two|three)\b(?:\*\*)?[ \t]*(?:[:(—–-]|\*\*)[ \t]*(?:\*\*[ \t]*)?\(?'
     + _TONES + r'[ \t]*(?:[):*][ \t]*)*(?:\([^)\n]{0,60}\)[ \t]*(?:[:*][ \t]*)*)?$'
     # Or a bold line of tones and nothing else: "**Simple and sincere**", "**A bit warmer**".
     r'|^[ \t]*\*\*' + _TONES + r'[ \t]*(?::[ \t]*)?\*\*(?:[ \t]*:)?[ \t]*$'
     # That line copied as plain text, under a blank line: "Simple and sincere", "Warm & simple".
     # Never the first line, so that a text which opens with "Simple" is no menu by that alone.
     r'|(?<=\n\n)[ \t]*' + _TONES + r'[ \t]*(?::[ \t]*)?$',
     "option menu ('Option 1 (concise):', 'Version 2:')"),
    (r'(?im)^[ \t]*(?:#{1,4}[ \t]*)?(?:\*\*)?(?:(?:a |an )?(?:slightly )?(?:shorter|longer'
     r'|more casual|more formal|warmer|punchier|tighter|softer|firmer|condensed|alternate|one-line'
     r"|one-liner)[ \t]+(?:version|option|take|alternative)\b[^\n]{0,70}?|if you(?:'d)? (?:want"
     r'|like|prefer) (?:it |something |a |an )?(?:shorter|longer|more \w+|less \w+|warmer'
     r'|punchier|tighter|softer|firmer)\b[^\n]{0,60}?)[ \t]*:(?:\*\*)?[ \t]*$',
     "alternative-version label ('Shorter version (for apps):', 'If you want it more casual:')"),
    # Not a bare "**Note:**", which READMEs use. The notes, tips and "what changed" labels end
    # their line: "What changed: the cookie now lasts 30 minutes" is a PR description, and
    # "Notes on delivery: the dock closes at 5" a delivery note. "One thing to check:" is
    # written inline in a chat answer.
    (r'(?im)^[ \t]*(?:\*\*)?(?:(?:notes? (?:for|on) (?:tweaking|delivery|using|the draft'
     r'|this draft|editing|customi[sz]ing|personali[sz]ing)|tips? (?:for|on) (?:delivery'
     r'|delivering|giving it|the toast|the speech|using (?:it|this)|tweaking|personali[sz]ing)'
     r'|what (?:i )?changed)(?:\*\*)?:(?:\*\*)?[ \t]*$|one thing to (?:check|confirm|double-check'
     r'|verify)[^\n:]{0,30}(?:\*\*)?:)',
     "notes label on its own line ('What changed:', 'One thing to check:')"),
    # The email disclaimer under a rule ("If you received this in error") is not a note, and
    # a line of dashes right under text is a heading's underline or the end of front matter.
    # A footer such as "If you have any questions, call..." is left to the gate in raw_rules():
    # Claude's notes start the same way ("If you have any numbers from the internship...").
    (r'(?i)^(?<![^\n]\n)[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*\n(?:[ \t]*\n){0,2}[ \t]*(?:\*\*|\*'
     r"|_)?(?:a (?:few|couple)|one (?:thing|note)|if (?!you(?:\s(?:are|were|have)|['’](?:re"
     r'|ve))?\s(?:not the intended|received|receiving|recieved))(?:you|this|the|your)'
     r'|want (?:me|it)|would you|i (?:can|could|used|added|kept|wrote|left|assumed|guessed'
     r"|changed|made|trimmed|swapped|cut)|i'(?:ve|d)|notes?\b|tips?\b|what (?:changed"
     r"|i changed)|here'?s|feel free|let me know|happy to|word count|suggested|your (?:original"
     r'|draft|version|notice|email|text))',
     "rule fencing off a note to the requester ('---' then 'A few notes:')"),
    # A plain-copied answer loses the rule, but the note still follows "Best,\n[Your name]".
    # "A few notes:" on its own is normal in mail, so it counts only under that sign-off.
    (r'(?im)^[ \t]*(?:\*\*)?(?:best|thanks|thank you|cheers|sincerely|regards|best regards'
     r'|kind regards|warm regards|warmly|all the best|talk soon|thanks again|many thanks'
     r'|yours truly|respectfully|with gratitude)[,!.]?(?:\*\*)?[ \t]*\n'
     r'(?:\*\*)?\[[^\]\n]{0,30}\bname\b[^\]\n]{0,30}\](?:\*\*)?[ \t]*\n'
     r'(?:[^\n]{0,80}\n){0,4}?[ \t]*\n(?:[ \t]*(?:(?:-{3,}|\*{3,}|_{3,})[ \t]*)?\n)*[ \t]*(?P<note>'
     r'(?:\*\*|\*|_)?[A-Za-z][^\n]{8,})',
     "note to the requester after a placeholder sign-off ('Best, [Your name]' then 'A few notes:')"),
    # Bold only: a plain "Subject:" is the user's own email, and a rewrite has to keep it.
    (r'^\*\*Subject:',
     "email subject line in bold ('**Subject:** ...')"),
    # Not "Next steps", "Action items" or "Bottom line": meeting notes use them.
    (r'(?im)^(?:#{1,6}[ \t]+|\*\*)?(?:key (?:takeaways?|considerations)|the short version'
     r'|my (?:take|recommendation|verdict)|practical (?:takeaways|points|tips|considerations)'
     r'|quick (?:summary|recap|version|answer)|short answer|overall summary'
     r'|the basic (?:difference|idea))\b[^\n]{0,30}?(?:\*\*)?:?(?:\*\*)?[ \t]*$',
     "signpost heading on its own line ('## Key takeaways', 'The short version')"),
]
# Named for chat_framing(), by position: a rule added above without a name here fails at import.
_OPENER, _OPTION, _ALTERNATIVE, _NOTES, _FENCE, _SIGNOFF, _SUBJECT, _SIGNPOST = (
    pat for pat, _ in RAW_PHRASES)
# Human docs have these two as well: "## Option A: pip", "**Option 1: Auth file**", a "---"
# and then "If you like this project". They count in a text that opens with a version label,
# as a menu of drafts does, or that has another chat tell, and in nothing else.
_WEAK = (_OPTION, _FENCE)
# Unless the note under the rule is itself an offer: "If you tell me his name, I can work it in",
# "If you'd like me to make it shorter, say so". Not "If you want me to add a feature, open an
# issue" or "If you'd like me to water the plants, text me": the offer is to change the text.
_FENCED_OFFER = (r"(?im)^(?<![^\n]\n)[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*\n(?:[ \t]*\n){0,2}"
                 r"[ \t]*if you(?:\b[^\n]{0,200}?\bi(?:'ll| can| could)\b"
                 r"|(?:['’]d| would)? (?:like|want) me to (?:make|change|adjust|tweak|shorten"
                 r'|lengthen|rewrite|trim|tighten|soften|add (?:a|an|more)\b[^\n]{0,40}?)'
                 r' (?:it|this|the (?:tone|draft|wording|note|message|email|letter|post))\b)')


# A text that opens with an option label is a menu of drafts, unless the label is a plain
# "Option 1: Minimal" with a tone after the colon: Slack proposals and notes open that way.
# That one counts beside another chat tell only. Bold, a heading or a bare "Option 1:" still
# open the gate.
_PLAIN_TONE_LEAD = (r'(?i)[ \t]*(?:option|version|alternative|variation)[ \t]+(?:\d{1,2}|[a-c]'
                    r'|one|two|three)\b[ \t]*[:—–-][ \t]*[a-z]')
# Nor a heading with the number alone, or a date after it: release notes and design docs open
# with "## Version 2", "### Version 2 (2 May 2024)" or "## Option 1". "## Option 1 (concise)",
# "**Version 1:**" and a bare "Version 1:" still open the gate.
_RELEASE_LEAD = (r'(?im)[ \t]*#{1,4}[ \t]*(?:option|version|draft|alternative|variation)[ \t]+'
                 r'(?:\d{1,2}|[a-c]|one|two|three)\b[ \t]*(?:\([^)\n]{0,40}\d[^)\n]{0,40}\)[ \t]*)?$')
# A rule over "A couple of optional additions..." or "One thing to check" is a note to the
# requester by itself: no human doc in the corpora has one. "If you...", "Notes" and "Tips"
# under a rule are footers too, and count only beside another chat tell. The nouns are named:
# "A few weeks later", "A couple of known issues" and "One note from Dana" open human sections.
_FENCED_NOTE = (r'(?im)^(?<![^\n]\n)[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*\n(?:[ \t]*\n){0,2}'
                r'[ \t]*(?:\*\*|\*|_)?(?:a (?:few|couple(?: of)?) (?:optional |quick |small )?'
                r'(?:notes?|tips|things|suggestions|additions|tweaks|ideas|changes|options)\b'
                r'|one (?:thing|note) (?:to (?:check|confirm|double-check|verify|note|consider|add'
                r'|keep in mind)|you)\b)')


def _chat_tell(view, low):
    return bool((re.match(_OPTION, view) and not re.match(_PLAIN_TONE_LEAD, view)
                 and not re.match(_RELEASE_LEAD, view))
                or re.search(_FENCED_OFFER, view) or re.search(_FENCED_NOTE, view)
                or any(re.search(pat, low) for pat, _ in REPLY_PHRASES)
                or any(re.search(pat, view, re.M) for pat, _ in RAW_PHRASES if pat not in _WEAK))


def raw_rules(view, low):
    """The RAW_PHRASES that apply to a text: `view` is markdown_raw() of it, `low` its prose
    lower-cased. Without a chat tell elsewhere, an option label or a rule is a human doc's.
    A cliché, a reframe or a candour label is copy a person writes too, so only the phrases
    that speak to the requester (REPLY_PHRASES) count as that tell."""
    if _chat_tell(view, low):
        return RAW_PHRASES
    return [(pat, label) for pat, label in RAW_PHRASES if pat not in _WEAK]


def chat_reply(raw):
    """Whether a pasted text has the chat reply around a draft: a handover, an offer, notes to
    the requester, or a raw rule. A cliché or a reframe alone is copy, not a reply."""
    return _chat_tell(markdown_raw(raw), markdown_prose(raw).lower())

# Slots left for the sender to fill in. Advisory, like --allow-proof makes proof: they are
# listed so nobody sends "[Your Name]", but they cost nothing, because a rewrite is told to
# keep placeholders exactly and so could never clear them. The app also lets them start an
# automatic run that the score alone would skip. Never a link, inline or by reference, a
# checkbox, `[X]`, `[URL]`, anything with `=` or `,` inside, or the app's own `[needs number]`
# marker: all of those turn up in human docs.
PLACEHOLDERS = [
    (r"(?<![\w\]`])\[(?!(?i:needs?)\b)(?:(?i:your|their|recipient(?:'s)?|customer|client"
     r'|company|manager|hiring manager|candidate|first name|last name|full name|date|day|time'
     r'|phone|email|e-mail|portfolio|linkedin|signature|address)\b[^\]\n=,]{0,60}'
     # One bare word, "[Name]" or "[amount]", but not "[name]@[version]", "[name].txt" or
     # "add [name] [url]".
     r'|(?i:name|number|amount|code|year|title|airline|city)(?=\](?!\s?[\[@:/(]|\.\w))'
     # Someone else's details: "[Boss's name]", "[Hal's email/phone]".
     r"|[A-Za-z]+(?: [A-Za-z]+)?'s (?:[a-z]+ )?(?i:name|email|e-mail|phone|number|address)\b"
     r'[^\]\n=,]{0,40}'
     # A choice left open: "[Spring/Summer]", "[work / the kids]", never "[y/N]", "[PATCH 1/3]"
     # or a reference link's "[deflate/inflate][zlib]".
     r'|[A-Za-z][a-z.]+(?: [a-z]+){0,2} ?/ ?[A-Za-z][a-z.]+[^\]\n=,]{0,40}(?=\](?!\[))'
     r'|[A-Z][a-z]+(?: [A-Z][a-z]+){0,3} (?:Name|Date|Email|Phone|Title|URL|Address|Number|City'
     r'|Company))\](?!\(|\[\])',
     "fill-in placeholder ('[Your Name]', '[Customer Name]', '[day]')"),
    (r'(?<![\w\]`])\[(?:(?i:add|insert|describe|briefly|mention|include|give|specific)\b'
     r'|(?i:e\.g\.))[^\]\n]{0,200}\](?!\(|\[\])',
     "instruction placeholder ('[Add a specific example]')"),
    (r'\((?:Add|Insert|Fill in|Include|Describe|Mention) (?:details|detail|a specific'
     r'|an example|specifics|your|the (?:name|date|number|details|specifics))\b[^)\n]{0,200}\)',
     "instruction in parentheses ('(Add details: ...)')"),
]

# Invented social proof. Deliberately broad: this is the one mistake with no
# route back, so recall matters more than precision. If your number is real and
# you can evidence it, pass --allow-proof and the rule drops to advisory.
# Hyphenated compound modifiers. One is ordinary English: "a 25-year warranty".
# Four in a sentence is a model reaching for authority it has not earned, and the
# giveaway is that they stack in front of one noun: "our industry-leading,
# context-aware, best-in-class, AI-powered platform". The floor is high on purpose.
# Real trade copy runs one to three per hundred words, and stacked slop runs sixty.
COMPOUND = re.compile(r'\b[a-z]{2,}-[a-z]{2,}(?:-[a-z]{2,})*\b', re.I)
COMPOUND_FLOOR = 4

PROOF = re.compile(
    # Digits, thousands separators and a decimal point, but never a trailing
    # full stop. Absorbing it let "Don't Make Me Think, 2000. The reader…" read
    # as a proof claim, because the year swallowed the sentence break and then
    # reached across it for a noun. A number and its noun live in one sentence.
    # It starts at a number's first digit, never inside one: tried from every digit of a
    # long constant, the noun search made a 2,000-digit run take 17 seconds.
    r"(?<!\d)(?<!\d,)([\d][\d,]*(?:\.\d+)?)\s*\+?\s*"
    r"((?:happy|early|active|satisfied|verified|trusted|delighted)\s+)?"
    r"(?:\w+\s+){0,1}"
    r"(users?|customers?|learners?|students?|teams?|members?|companies|businesses"
    r"|homeowners?|subscribers?|clients?|patients?|readers?|sites?|projects?)"
    # Closed with a word boundary, like `_root_pattern`. Without it `sites?`
    # matched inside "sitemaps", `teams?` inside "teamsters" and `projects?`
    # inside "projectors", and "12 sitemaps" scored as invented social proof.
    r"(?!\w)",
    re.I)


def normalise(t):
    """Fold the typographic variants back to the plain ones the rules match.

    A non-breaking hyphen is not `-`, \xa0 is not a space, and a curly
    apostrophe is not `'`. Miss any of them and `cutting-edge`,
    `it's not just X` and `whether you're` all stop matching on real copy,
    because real copy is exactly where the pretty characters come from.

    Every input path runs through here. Markdown skipped it once, and the
    construction rules went blind on every .md file with a smart quote in it.
    """
    t = t.replace('‑', '-').replace('\xa0', ' ').replace('’', "'")
    return re.sub(r'\s+', ' ', t).strip()


def visible_text(html):
    """What a visitor actually reads. Script/style stripped, tags removed."""
    t = re.sub(r'<(script|style)\b.*?</\1>', ' ', html, flags=re.S | re.I)
    t = re.sub(r'<!--.*?-->', ' ', t, flags=re.S)
    t = re.sub(r'<[^>]+>', ' ', t)
    # Decode every entity, not a hand-written six. Numeric entities used to leak
    # through as literal text: each `&#x27;` donated a phantom semicolon to the
    # punctuation rule, and every apostrophe-encoded page went blind to the
    # `it's not just X` and `whether you're` patterns.
    t = _html.unescape(t)
    return normalise(t)


def read_utf8(path):
    """Read a file as UTF-8, whatever the machine's locale says.

    open() with no encoding= uses the locale's, which is cp1252 on a stock
    Windows install. A UTF-8 page then decodes its em-dashes and curly
    apostrophes into mojibake, .replace('\u2019', "'") never fires, and
    window.count('\u2014') counts zero. The punctuation and construction rules
    go silently blind: the same copy scores 4/5 through --text and 5/5 CLEAN
    from a file path, and the file path is what CI wires in.

    A gate that passes because it cannot read is worse than no gate at all.
    Reported by @Azrael259 in #3, who hit it on Windows.
    """
    return open(path, encoding='utf-8', errors='replace').read()


def markdown_prose(md):
    """The prose of a Markdown file, with the specimens removed.

    A literal is not copy. A README that documents `delve` has not shipped the
    word, it has quoted it, and a linter that cannot tell the difference makes
    every catalogue score zero. So four things come out before scoring:

      fenced blocks   ```…```      commands and code, never prose
      inline code     `delve`      the specimen being named
      struck text     ~~before~~   the line being shown as wrong, on purpose
      images          ![alt](src)  alt text is metadata, not body copy

    Link text stays, because that is read as part of the sentence. Everything
    else is scored exactly as before: this strips markup, it does not soften a
    single rule.
    """
    md = re.sub(r'```.*?```', ' ', md, flags=re.S)
    md = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' . ', md)
    md = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', md)
    # Stand-ins, not deletions. Removing `[needs number]` outright welds
    # "you do not have, write ... and move on" into a false rule-of-three, and
    # a linter that invents a hit is worse than one that misses. Two letters
    # keep the clause intact without ever matching a rule itself.
    md = re.sub(r'`[^`]*`', ' it ', md)
    md = re.sub(r'~~.*?~~', ' it ', md, flags=re.S)
    # Headings, table cells and rows are separate copy, not one long sentence.
    # Joined, two em-dashes from two table rows read as one machine cadence.
    md = re.sub(r'^\s{0,3}#{1,6}\s*(.*)$', r' . \1 . ', md, flags=re.M)
    md = re.sub(r'\|', ' . ', md)
    # A bullet is its own line of copy. Left joined, two list items donate one
    # em-dash each and read as a single machine cadence that nobody wrote.
    # Indentation only before the mark: `^\s*` crossed blank lines, and a run of them was
    # tried from every line in it.
    md = re.sub(r'^[^\S\n]*(?:[-*+]|\d+\.)\s+', ' . ', md, flags=re.M)
    md = re.sub(r'^[^\S\n]*>\s?', ' . ', md, flags=re.M)
    md = re.sub(r'[*_>]', ' ', md)
    return normalise(md)


def markdown_raw(md):
    """The pasted text with its code taken out and every line kept where it was.

    RAW_PHRASES need what markdown_prose removes: line starts, the first line, `---` rules,
    `**`. A fenced block or an inline span is a specimen, not copy, so it goes, but as blank
    lines and ' it ' so that line anchors still line up. Typographic variants are folded the
    way normalise() folds them, without collapsing whitespace. So are Windows line ends: `$`
    and `\\n` in the rules stop short of a '\\r', and --text "$(cat reply.txt)" keeps them.
    """
    md = md.replace('\r\n', '\n').replace('\r', '\n')
    md = re.sub(r'```.*?```', lambda m: '\n' * m.group(0).count('\n'), md, flags=re.S)
    md = re.sub(r'`[^`\n]*`', ' it ', md)
    return md.replace('‑', '-').replace('\xa0', ' ').replace('’', "'").strip()


def chat_framing(raw):
    """The chat reply around a pasted draft, as one string: each line a raw rule matched, and
    the rest of the text from a second version, a fenced note or a note under the sign-off.

    A rewrite deletes all of it (PASS 0 in prompts/cleanse.txt), and a number that was only
    there goes with it. The app reads this to tell that from a number lost from the draft.
    Human text has none of it, so for anything but a chat answer this is ''.
    """
    view, spans = framing_spans(raw)
    return '\n'.join(view[start:end] for start, end in spans).strip()


def framing_spans(raw):
    """chat_framing() as places: (markdown_raw(raw), [(start, end), ...]) in that view. A rule
    can match part of a line ("One thing to check for the 14 seats:"), and a line can be in
    the draft and in a second version both, so the app reads which framing a rewrite kept
    from these places and not from the text alone."""
    view = markdown_raw(raw)
    found = [(pat, m) for pat, _ in raw_rules(view, markdown_prose(raw).lower())
             for m in re.finditer(pat, view, re.M)]
    # "Here's a draft:", a rule, the letter, a rule, the offer: the first rule opens the draft,
    # whatever its first word is ("I'd like to let you know..."). Only the last one fences a note.
    top = re.match(r'\s*(?:[^\n]*:[ \t*]*\n)?\s*', view).end()     # under the lead-in, if any
    found = [(pat, m) for pat, m in found
             if not (pat == _FENCE and m.start() <= top and re.compile(_RULE).search(view, m.end()))]
    starts = [m.start('note') if pat == _SIGNOFF else m.start() for pat, m in found
              if pat in (_ALTERNATIVE, _FENCE, _SIGNOFF)]
    options = [m.start() for pat, m in found if pat == _OPTION]
    menu = [m for pat, m in found if pat == _OPENER and re.search(r'(?i)\b(?:here|below) are\b', m.group(0))]
    labels = []
    if menu and not options:
        second, labels = _second_version(view, menu[0].end())
        starts += second
    cut = min(starts + options[1:], default=len(view))
    return view, [m.span() for _, m in found if m.end() <= cut] + labels + [(cut, len(view))]


_RULE = r'(?m)^[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$'
_ITEM = r'(?:\d{1,2}[.)]|[-*+•])[ \t]'


def _trimmed(pos, line):
    """The place of a line without the white space at either end."""
    lead = len(line) - len(line.lstrip())
    return pos + lead, pos + len(line.rstrip())


def _second_version(view, at):
    """Where the second draft starts in a menu with no labels ("Here are a few options:" over
    a numbered list, table rows, quotes or plain paragraphs): the next item, the next row, or
    the next block that opens the way the first one does, as a list of none or one place. A
    heading or a bold line is the label of what is under it, so the next one starts a draft.
    Also where the lines are that label the drafts without being one: a table's header and its
    |---| row, as (start, end) places, white space left out."""
    lines = [(m.start(), m.group(0)) for m in re.finditer(r'(?m)^.*$', view) if m.start() > at]
    k = next((k for k, (_, line) in enumerate(lines) if line.strip()), None)
    if k is None:
        return [], []
    head, indent = lines[k][1].strip(), re.match(r'[ \t]*', lines[k][1]).group(0)
    if head.startswith('|'):
        rows, header = [], [_trimmed(*lines[k])]
        for pos, line in lines[k + 1:]:
            if not line.lstrip().startswith('|'):
                break
            if re.fullmatch(r'[|:\s-]+', line):
                header.append(_trimmed(pos, line))
            else:
                rows.append(pos)
        return rows[1:2], header
    if re.match(_ITEM, head):
        return [pos for pos, line in lines[k + 1:] if re.match(indent + _ITEM, line)][:1], []
    label = re.match(r'#+[ \t]|>|\*\*(?=[^*\n]+\*\*:?$)', head)
    for (pos, line), (_, above) in zip(lines[k + 1:], lines[k:]):
        line = line.strip()
        if line and not above.strip() and (not label or line.startswith(label.group(0))):
            return [pos], []
    return [], []


def audit(text, raw=None):
    """Hits per category. `text` is the prose; `raw`, when given, is the text as pasted
    (markdown and all), for RAW_PHRASES and PLACEHOLDERS. HTML has no raw view: pass None."""
    hits = {'vocab': [], 'phrases': [], 'punctuation': [], 'rhythm': [], 'proof': [],
            'placeholders': []}
    low = text.lower()
    if raw is not None:
        view = markdown_raw(raw)
        for pat, label in raw_rules(view, low):
            n = len(re.findall(pat, view, re.M))
            if n:
                hits['phrases'].append((label, n))
        # A reference link's text is no slot either: "[your settings]", "[your settings][]" or
        # "[your settings][s]" when a line "[your settings]: https://..." or "[s]: ..." defines
        # it, and that line itself. The destination has to look like one, so a script's
        # "[Customer Name]: Sure." stays a slot, and so does "[First Name][Last Name]".
        refs = {' '.join(label.lower().split()) for label in re.findall(
            r'''^ {0,3}\[([^\]\n]+)\]:[ \t]*(?:<[^>\n]*>|[a-z][\w+.-]*:\S+|[./#~]\S*|\S*/\S*'''
            r'''|\S+\.\w+)[ \t]*(?:["'(].*)?$''', view, re.M | re.I)}
        # Each slot once: "[Your Name]" in the greeting and the sign-off is one thing to fill.
        for pat, _ in PLACEHOLDERS:
            for m in re.finditer(pat, view, re.M):
                label = re.match(r'\[([^\]\n]+)\]', view[m.end():])
                if ' '.join((label[1] if label else m.group(0)[1:-1]).lower().split()) in refs:
                    continue
                if m.group(0) not in hits['placeholders']:
                    hits['placeholders'].append(m.group(0))

    for w in VOCAB:
        n = len(re.findall(_root_pattern(w), low))
        if n:
            hits['vocab'].append((w, n))

    for w in VOCAB_EXACT:
        n = len(re.findall(rf"(?<!\w){re.escape(w)}(?!\w)", low))
        if n:
            hits['vocab'].append((w, n))

    for pat, label in PHRASES:
        found = re.findall(pat, low)
        if found:
            hits['phrases'].append((label, len(found)))

    # Em-dash density: two or more in one sentence reads as machine cadence.
    # Bounded to a 220-char window on purpose — UI strings (nav items, quiz options,
    # labels) carry no terminal punctuation, so a naive sentence split merges the
    # whole page into one "sentence" and this rule fires on every page. Ask me how
    # I know. A linter that cries wolf gets switched off.
    for s in re.split(r'(?<=[.!?])\s+', text):
        for i in range(0, max(1, len(s)), 220):
            window = s[i:i + 220]
            if window.count('—') >= 2:
                hits['punctuation'].append(('two or more em-dashes in one sentence',
                                            window[:70].strip()))
                break
    for s in re.split(r'(?<=[.!?])\s+', text):
        for i in range(0, max(1, len(s)), 220):
            window = s[i:i + 220]
            found = COMPOUND.findall(window)
            if len(found) >= COMPOUND_FLOOR:
                hits['punctuation'].append((f'{len(found)} hyphenated compounds stacked '
                                            'in one sentence', ', '.join(found[:4])))
                break

    # Floor of 3: two semicolons in a long technical page is a style, not a tell.
    if text.count(';') > max(3, len(text) // 1200):
        hits['punctuation'].append(('semicolon-heavy for web copy', f"{text.count(';')} found"))

    # Tricolon: the rule-of-three reflex. Two shapes, deliberately narrow.
    #
    # With the Oxford comma, three single words: "faster, smarter, and better".
    # Without it, the third item must be a 2–3 word phrase that ends the clause:
    # "Trusted, reliable and built to last". That phrase requirement is what
    # separates a rhetorical flourish from a plain list of services —
    # "Inspection, repair and replacement for homes and commercial buildings"
    # is three real things a roofer does, its third item runs long, and
    # flagging it would be exactly the wolf-crying that gets a linter switched
    # off. Note the shape is what is judged, not the meaning: a three-word
    # closing item ("replacement for homes") does fire, by design.
    for pat in (r'\b(\w{4,}),\s+(\w{4,}),\s+and\s+(\w{4,})\b',
                r'\b(\w{4,}),\s+(\w{4,})\s+and\s+((?:\w+\s+){1,2}\w+)\s*[.!?,;:]'):
        for m in re.finditer(pat, text):
            hits['rhythm'].append(('rule-of-three list', m.group(0)[:60]))

    for m in PROOF.finditer(text):
        hits['proof'].append(m.group(0).strip())

    return hits


def report(hits, label='', allow_proof=False):
    # placeholders weigh nothing: listed, never scored (see PLACEHOLDERS).
    weights = {'vocab': 1, 'phrases': 1, 'punctuation': 1, 'rhythm': 1, 'proof': 1,
               'placeholders': 0}
    if allow_proof:
        # The one rule a regex cannot judge: it sees a number beside a noun, not
        # whether you can evidence it. --allow-proof still prints the hits, but
        # stops a true, defensible claim from blocking a green build forever.
        weights['proof'] = 0
    failed = [k for k, v in hits.items() if v]
    score = 5 - sum(weights[k] for k in failed)
    score = max(0, score)

    titles = {
        'vocab': 'AI vocabulary',
        'phrases': 'AI constructions',
        'punctuation': 'punctuation cadence',
        'rhythm': 'rule-of-three rhythm',
        'proof': 'possible invented proof',
        'placeholders': 'placeholders to fill in (not scored)',
    }

    if label:
        print(f'── {label}')
    for k in ('proof', 'phrases', 'vocab', 'punctuation', 'rhythm', 'placeholders'):
        if not hits[k]:
            continue
        print(f'  {titles[k]}:')
        for item in hits[k][:8]:
            print(f'    · {item[0] if isinstance(item, tuple) else item}'
                  + (f'  ({item[1]})' if isinstance(item, tuple) and len(item) > 1 else ''))
        if len(hits[k]) > 8:
            print(f'    · …and {len(hits[k]) - 8} more')

    print(f'\n  score {score}/5', end='  ')
    print('CLEAN' if score == 5 else 'needs a cleanse')
    return score


if __name__ == '__main__':
    # Reading UTF-8 correctly means real non-ASCII now reaches print(), and a
    # Windows console is cp1252: one CJK character or emoji inside a flagged
    # snippet would end the run in a UnicodeEncodeError traceback. Replace
    # rather than raise. Naming the tell is the job; echoing it byte-for-byte
    # is not, and the suite already asserts this tool never shows a traceback.
    try:
        sys.stdout.reconfigure(errors='replace')
    except (AttributeError, ValueError):      # already-wrapped or exotic stream
        pass

    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)

    allow_proof = '--allow-proof' in args
    args = [a for a in args if a != '--allow-proof']

    as_md = '--markdown' in args
    args = [a for a in args if a != '--markdown']

    raw = None                    # HTML has no lines to read: visible_text joins them all
    if args[0] == '--text':
        text = raw = ' '.join(args[1:])
        if as_md:
            text = markdown_prose(text)
    elif args[0].endswith('.md') or as_md:
        try:
            raw = read_utf8(args[0])
            text = markdown_prose(raw)
        except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
            sys.exit(f'deslop: cannot read {args[0]}: {e.strerror}')
    else:
        try:
            html = read_utf8(args[0])
        except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
            sys.exit(f'deslop: cannot read {args[0]}: {e.strerror}')
        if '--view' in args:
            # score one element only: slice from its id= to the next id="view-…"
            vid = args[args.index('--view') + 1]
            start = html.find(f'id="{vid}"')
            if start < 0:
                sys.exit(f'no element with id "{vid}"')
            nxt = html.find('id="view-', start + 1)
            html = html[start:nxt if nxt > 0 else len(html)]
        text = visible_text(html)

    # Nothing to score is a failure, not a pass. A cleanse that times out leaves a
    # zero-byte file, and a gate that stamps an empty file CLEAN reports slop as
    # clean at exactly the moment the pipeline broke.
    if not text.split():
        sys.exit('deslop: no visible copy to score — empty input')

    print(f'{len(text.split())} words of visible copy\n')
    sys.exit(0 if report(audit(text, raw), allow_proof=allow_proof) == 5 else 1)
