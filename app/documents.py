#!/usr/bin/env python3
"""Turn a Word or PDF file into text the app can rewrite, and rewritten text into a Word file.

    python documents.py extract <name> < the-file     # one JSON object on stdout, exit 0

The server runs that command instead of calling extract() itself. A file is somebody
else's bytes, and one that hangs or crashes its reader should take down a process the
server can kill, not the server. Standard library only, except that reading a PDF needs
pypdf, which is imported when the first PDF arrives.

The text is Markdown-flavoured, because that is what the checker and the rewriting prompt
already understand. Every paragraph, heading, list item and table row is a block, and one
blank line parts two blocks: "# " to "### " opens a heading, "- " or "1. " a list item (two
spaces deeper per level), and a table row is "cell | cell | cell". write_docx() reads the
same, with or without the blank line between list items, and builds a new Word file from it.
"""
import contextlib
import ctypes
import io
import json
import logging
import math
import os
import re
import sys
import struct
import threading
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from xml.sax.saxutils import escape

import wordfile

MAX_PART = 40_000_000                  # bytes of one unpacked part of a .docx
MAX_PAGES = 2_000

OLD_DOC = 'This is an old-format Word file (.doc). Save it as .docx in Word first.'
OTHER = ('Only Word (.docx), PDF, .txt and .md files can be read. Save it as one of those, '
         'or paste the text.')
UNSUPPORTED = {
    '.doc': OLD_DOC,
    '.rtf': 'Rich Text (.rtf) files cannot be read. Open it in Word and save it as .docx first.',
    '.odt': 'OpenDocument (.odt) files cannot be read. Save it as .docx or PDF first.'}
UNREADABLE = {
    'docx': 'This Word file is damaged or is not a real .docx. Open it in Word and save it again.',
    'pdf': 'This PDF is damaged or is not a real PDF. Print it to a new PDF, or paste the text.',
    'text': 'This file could not be read as text.'}
NO_TEXT = {
    'docx': 'This Word file has no text in its body.',
    'pdf': ('This PDF has no text in it. It is probably a scan: run it through OCR first, or '
            'paste the text.'),
    'text': 'This file is empty.'}
BOXES_ONLY = ('All the text in this Word file is in text boxes, which this app does not read. Copy the text '
              'from Word and paste it here instead.')
IMPORTED_ONLY = ('The text of this Word file was imported from another file, which this app does not read. '
                 'Open it in Word, save it, and try again.')
LOCKED_DOCX = ('This Word file has a password. Remove it in Word (File, Info, Protect Document) '
               'and try again.')
LOCKED_PDF = ('This PDF needs a password to open. Save a copy without the password and try '
              'that, or paste the text.')
NEEDS_CRYPTO = ('This PDF is protected, and opening it needs one more package. Install it with: '
                'python -m pip install cryptography. Or print the PDF to a new PDF and try that.')
if getattr(sys, 'frozen', False):         # SlopMonster.exe, where nothing can be installed
    NEEDS_CRYPTO = ('This PDF is protected in a way SlopMonster cannot open. Print it to a new PDF '
                    'and try that, or paste the text.')
NEEDS_PYPDF ='Reading PDFs needs the pypdf package. Install it with: python -m pip install pypdf'
BIG_DOCX = 'This Word file is too large to read. Split it and try each part.'
FORM_NOTE = ('Text typed into the form fields of this PDF is not shown. Copy it from the form and paste it, '
             'or print the PDF to a new PDF and open that.')
FORM_ONLY = ('The only text in this PDF is typed into its form fields, which are not read. Copy it from the '
             'form and paste it, or print the PDF to a new PDF and open that.')
# wordfile accepts the tracked changes in a paragraph it rewrites (see TRACKED there).
TRACKED_NOTE = ('This file has tracked changes, and the text here shows them accepted. Accept or reject them in '
                'Word before you unslop it, or the copy will accept them in each paragraph that is rewritten.')
CONTENTS_NOTE = 'The table of contents was left out.'

# What XML cannot hold and JSON should not carry: control characters and lone surrogates.
_UNSAFE = re.compile('[^\t\n\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')


class DocumentError(Exception):
    """The file could not be turned into text, with a code and a message the page can show."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def extract(name, data):
    """{'text', 'kind', 'summary', 'notes'} for the bytes of a file, or DocumentError."""
    ext = os.path.splitext(name or '')[1].lower()
    head = data[:1024].lstrip()
    # What the bytes say comes first: a Word file renamed to .pdf is still a Word file.
    if data[:4] == b'PK\x03\x04':
        kind = 'docx'
    elif head.startswith(b'%PDF'):
        kind = 'pdf'
    elif data[:4] == b'\xd0\xcf\x11\xe0':
        # The container of .doc, .xls and .ppt, and of a .docx saved with a password.
        if 'EncryptedPackage'.encode('utf-16-le') in data:
            raise DocumentError('encrypted', LOCKED_DOCX)
        raise DocumentError('unsupported', OTHER if ext in ('.xls', '.ppt', '.msg') else OLD_DOC)
    elif head.startswith(b'{\\rtf'):
        raise DocumentError('unsupported', UNSUPPORTED['.rtf'])
    elif ext in ('.docx', '.pdf'):
        kind = ext[1:]
    elif ext in ('.txt', '.md', '.markdown'):
        kind = 'text'
    else:
        raise DocumentError('unsupported', UNSUPPORTED.get(ext, OTHER))
    try:
        if kind == 'docx':
            text, summary, notes = _read_docx(data, ext)
        else:
            text, summary, notes = (_read_pdf if kind == 'pdf' else _read_text)(data)
    except DocumentError:
        raise
    except Exception:
        # zipfile, the XML parser and pypdf each fail on a damaged file in more ways than
        # anyone has listed, a recursion or memory error on a hostile one included.
        raise DocumentError('unreadable', UNREADABLE[kind])
    text = _UNSAFE.sub('', text).strip('\n')
    if not text.strip():
        raise DocumentError('no_text', NO_TEXT[kind])
    return {'text': text, 'kind': kind, 'summary': summary, 'notes': notes}


def _count(n, noun):
    return f'{n:,} {noun}' + ('' if n == 1 else 's')


def _read_text(data):
    notes = []
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):        # what Notepad called "Unicode"
        text = data.decode('utf-16', 'replace')
    else:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('cp1252', 'replace')
            notes.append('This file is not UTF-8, so it was read as Windows-1252. '
                         'Check any accented letters.')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return text, 'Text file, ' + _count(len(text.split()), 'word'), notes


# ── .docx in ─────────────────────────────────────────────────────────────────
# Elements are matched by name without namespace, so a file saved as "Strict Open XML",
# which uses different namespaces for the same names, reads the same.

# Never walked into: properties (a tab stop is a <w:tab> too), tracked deletions, the old
# place of moved text, and the second copy of a text box, which Word writes after the first
# as the Fallback of an AlternateContent.
_LEFT_OUT = (('headers', {'headerReference'}), ('footers', {'footerReference'}),
             ('footnotes', {'footnoteReference'}), ('endnotes', {'endnoteReference'}),
             ('comments', {'commentReference'}), ('images', {'pic', 'imagedata'}),
             ('text boxes', {'txbxContent'}), ('parts imported from other files', {'altChunk'}))


def _tag(el):
    return el.tag.rsplit('}', 1)[-1]


def _raw(z, name):
    """The bytes of one part of the package, read up to the limit and no further."""
    with z.open(name) as fh:
        raw = fh.read(MAX_PART + 1)       # the size in the zip header is the file's own claim
    if len(raw) > MAX_PART:
        raise DocumentError('too_big', BIG_DOCX)
    return raw


def _tree(raw):
    """A part of the package, parsed."""
    # Decoded here and parsed as text, so that the parser reads the very characters this
    # test read. Handed bytes it would follow the file's own encoding declaration, and a
    # DTD written in UTF-16 walks straight past a search for ASCII. No DTD, no entity bomb.
    text = raw.decode('utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig')
    if '<!DOCTYPE' in text or '<!ENTITY' in text:
        raise DocumentError('unreadable', UNREADABLE['docx'])
    return ET.fromstring(text)


def _word(fn, *args):
    """A call into wordfile, its refusals turned into DocumentError. Its messages are for the user."""
    try:
        return fn(*args)
    except ValueError as e:
        code = {wordfile.LOCKED: 'encrypted', wordfile.TOO_BIG: 'too_big'}.get(str(e), 'unreadable')
        raise DocumentError(code, str(e))


def _read_docx(data, ext):
    """The body of a Word file as text: one block to a paragraph or table row, a blank line between.

    The blocks are wordfile's, because rewrite_docx() writes the edited text back by lining
    it up against the same list.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        main = wordfile._main_part(z)       # word/document.xml, unless the package names another
        if main not in z.namelist():
            # Some other kind of zip. A spreadsheet is not a damaged Word file.
            if ext in ('', '.docx', '.docm', '.dotx', '.dotm'):
                raise DocumentError('unreadable', UNREADABLE['docx'])
            raise DocumentError('unsupported', UNSUPPORTED.get(ext, OTHER))
        raw = _raw(z, main)
    found, toc_notes = _word(wordfile.read, data)     # before any parsing here: it refuses a hostile file fast
    # A block can hold line breaks of its own. An empty line inside one would read as the
    # end of the block, so those go; wordfile ignores a difference that is only white space.
    # (A carriage return in the text is a line end too. Dropped, it would join two words.)
    out = ['\n'.join(line.rstrip() for line in block.replace('\r\n', '\n').replace('\r', '\n').split('\n')
                     if line.strip()) for block in found]
    try:
        present = {_tag(el) for el in _tree(raw).iter()}
    except Exception:                         # a part in an encoding only wordfile reads: no notes
        present = set()
    if not any(block.strip() for block in out):
        if 'altChunk' in present:             # Word turns these into its own paragraphs when it saves
            raise DocumentError('no_text', IMPORTED_ONLY)
        if 'txbxContent' in present:
            raise DocumentError('no_text', BOXES_ONLY)
    notes = []
    gone = [what for what, tags in _LEFT_OUT if present & tags]
    if gone:
        listed = ', '.join(gone[:-1]) + ' and ' + gone[-1] if len(gone) > 1 else gone[0]
        notes.append(listed.capitalize() + ' are not shown here and stay in the file as they are.')
    notes += toc_notes
    if present & {'ins', 'del', 'moveFrom', 'moveTo'}:
        notes.append(TRACKED_NOTE)
    if present & {'oMath', 'sym', 'object'}:
        notes.append('Equations and symbols are not shown, so a paragraph that holds one keeps its old wording '
                     'when you save as Word.')
    if 'tbl' in present:
        notes.append('A table row is one line, with | between the cells.')
    return '\n\n'.join(out), 'Word document, ' + _count(len(out), 'paragraph'), notes


def rewrite_docx(data, text):
    """(a copy of the Word file `data` holding `text`, notes for the user), or DocumentError.

    `text` is what extract() gave for this file, edited: the same blocks in the same order.
    Only the words change. Fonts, images, tables and layout stay, and so does every other
    part of the file. See wordfile.py for what that takes.
    """
    # An odd blank line (one Enter too many above a heading) leaves a block opening with a
    # line end, and its "# " would then be typed into the paragraph as text. Lines only.
    return _word(wordfile.rewrite, data, ['\n'.join(line for line in block.split('\n') if line.strip())
                                          for block in re.split(r'\n[ \t]*\n', text) if block.strip()])


# ── .pdf in ──────────────────────────────────────────────────────────────────
# pypdf has two ways to read a page. Its plain mode is the one used here. Tried on
# documents written in Word and in PyMuPDF, the layout mode put the two columns of a page
# side by side on one line, opened gaps inside words ("drop      -in") and could not tell
# a heading from the paragraph under it. Plain mode keeps each column whole and in order.
# What it lacks is any sign of where a paragraph ends, and its visitor callback supplies
# that: the position and type size of every piece of text.

_PDF_CHARS = str.maketrans({'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi', 'ﬄ': 'ffl',
                            'ﬅ': 'st', 'ﬆ': 'st', '\xa0': ' '})
_BULLET = re.compile(r'[•·‣◦▪▫■□●○◆◇►▸➢➤✓✔\u2043\uf0a7\uf0b7\uf0d8\uf076]\s*|[-–—*]\s+')
_NUMBERED = re.compile(r'(\d{1,3})[.)]\s+')
_ENDS = re.compile(r'[.!?:;…]["\'”’)\]]*$')
_PAGE_NO = re.compile(r'\W*(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\W*', re.I)
_HYPHENS = ('-', '\xad', chr(0x2010))     # the keyboard's, the soft one, and Unicode's own
_MARK = re.compile(r'\d{1,3}|[ivx]{1,4}|[*†‡]')     # a footnote's or an endnote's number
_RAISED = str.maketrans('0123456789+-−', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻')   # m², 10⁶, 10⁻³
_UNITS = {'cm', 'mm', 'km', 'dm', 'nm', 'µm', 'ft', 'yd', 'mi', 'in', 'sq'}
# A line of a table of contents: "Scope ..... 4", with Word's dot leaders or spaced ". . . 4".
# Five dots or more, so that "And then.... 5 people" is not one.
_CONTENTS = re.compile(r'(?:#{1,3} )?\S.*?(?: ?\.){5,} ?(?:\d{1,4}|[ivxlc]{1,7})')
_CONTENTS_TITLE = re.compile(
    r'(?:#{1,3} )?(?:table of contents|contents(?: page)?|inhalt(?:sverzeichnis)?|sommaire'
    r'|table des mati[eè]res|[íi]ndice|inhoud(?:sopgave)?|innhold|inneh[åa]ll|indholdsfortegnelse'
    r'|sis[äa]llys(?:luettelo)?|spis tre[śs]ci)\s*:?', re.I)
_TURN = '\f'      # a line that says: what follows starts a new column, or a new page


def _read_pdf(data):
    try:
        import pypdf
    except ImportError:
        raise DocumentError('needs_pypdf', NEEDS_PYPDF)
    logging.getLogger('pypdf').setLevel(logging.CRITICAL)     # it warns about most real PDFs
    # AES is left to a package of its own, and pypdf may first need it anywhere from opening
    # the file to reading a page's text, which is why this covers all of them.
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        # Many PDFs are locked against editing only and open with no password at all.
        if reader.is_encrypted and not reader.decrypt(''):
            raise DocumentError('encrypted', LOCKED_PDF)
        count = len(reader.pages)
        if count > MAX_PAGES:
            raise DocumentError('too_big', f'This PDF has {count:,} pages and the limit is '
                                           f'{MAX_PAGES:,}. Split it and try each part.')
        pages = []
        for n in range(count):
            try:
                pages.append(_pdf_lines(reader.pages[n]))
            except pypdf.errors.DependencyError:
                raise
            except Exception:             # one broken page is a page with no text
                pages.append([])
    except pypdf.errors.DependencyError:
        raise DocumentError('encrypted', NEEDS_CRYPTO)
    # A table of contents is left out, as it is from a Word file: its page numbers would mean
    # nothing in the new document. Its title stays.
    laid = _layout(pages)
    contents = _contents(laid)
    # Any other dotted line is a row of a price or fee list, and a line of its own.
    for p, lines in enumerate(laid):
        laid[p] = [part for i, line in enumerate(lines) for part in (
            [''] if (p, i) in contents else ['', line, ''] if _leader(line) else [line])]
    text, dropped, rejoined = reflow(laid)
    notes = [CONTENTS_NOTE] if contents else []
    # What is typed into a form's fields is not part of any page's text. A damaged form costs
    # only this note.
    try:
        typed = any(f.get('/FT') == '/Tx' and isinstance(f.get('/V'), str) and f['/V'].strip()
                    for f in (reader.get_fields() or {}).values())
    except Exception:
        typed = False
    if typed:
        if not text.strip():
            raise DocumentError('no_text', FORM_ONLY)
        notes.append(FORM_NOTE)
    empty = sum(1 for lines in pages if not lines)
    if 0 < empty < count:
        notes.append(f'{empty} of {count} pages had no text and may be '
                     + ('a scan.' if empty == 1 else 'scans.'))
    if dropped:
        notes.append('Page numbers and repeated page headers or footers were left out.')
    if rejoined:
        notes.append('Words split by a hyphen at the end of a line were joined up. '
                     'Check any that should keep the hyphen.')
    return text, 'PDF, ' + _count(count, 'page'), notes


def _contents(laid):
    """Where a table of contents is, as {(page, line)}.

    A dotted line that ends in a number is a line of one only in a run of them: under a title
    such as "Contents", or on the first three pages, before any paragraph of text, three or
    more with page numbers that never go down. Elsewhere such lines are a price or fee list,
    and they stay. A run carries on over a page turn and past running headers and page numbers.
    """
    furniture = _furniture(laid)
    found, run, titled, body = set(), [], False, False

    def close():
        numbers = [int(laid[p][i].split()[-1]) for p, i in run if laid[p][i].split()[-1].isdigit()]
        if run and (titled or (not body and run[0][0] < 3 and len(run) >= 3
                               and numbers == sorted(numbers))):
            found.update(run)
        run.clear()

    for p, lines in enumerate(laid):
        for i, raw in enumerate(lines):
            line = ' '.join(raw.split())
            if not line or raw == _TURN or (p, i) in furniture:
                continue
            if _leader(line):
                run.append((p, i))
                continue
            close()
            titled = bool(_CONTENTS_TITLE.fullmatch(line))
            body = body or (len(line) >= 50 and bool(_ENDS.search(line)))
    close()
    return found


def _leader(line):
    """Is this a dotted line that ends in a number, as in a table of contents?"""
    line = ' '.join(line.split())
    return len(line) <= 300 and bool(_CONTENTS.fullmatch(line))


def _note_mark(before):
    """Would a small raised number after the text `before` be a note's? After a word, or the
    stop or bracket that ends one, it would. After a digit, a sign, a single letter or a unit
    it is a power: m², 10⁶, x²."""
    before = before.rstrip()
    word = re.search(r'[^\W\d_]+$', before)
    if word:
        return len(word[0]) >= 2 and word[0].lower() not in _UNITS
    return before[-1:] in tuple('.,;:!?)]"\'’”')


def _pdf_lines(page):
    """The lines of a page, in pypdf's reading order, as (text, x, y, type size)."""
    lines, line = [], None

    def visit(text, cm, tm, font, size):
        nonlocal line
        for i, piece in enumerate(text.split('\n')):
            if i:
                line = None
            if not piece.strip():
                if line:
                    line[0] += piece              # the space between two words
                continue
            # Where this piece sits, from the text matrix and the page's own transform.
            # Measured along and across the text's own baseline, so that lines still run
            # "down" on a page whose text is turned on its side.
            a, b = tm[0] * cm[0] + tm[1] * cm[2], tm[0] * cm[1] + tm[1] * cm[3]
            c, d = tm[2] * cm[0] + tm[3] * cm[2], tm[2] * cm[1] + tm[3] * cm[3]
            e, f = tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            along, across = math.hypot(a, b) or 1, math.hypot(c, d) or 1
            # A note's number, set small and raised after a word: "sites" and a raised "1" would
            # read "sites1", which is no word, and the checker would not see "14 sites" in it.
            # A raised "st" and a lowered "2" (CO2) are kept. So is a power, raised as one:
            # "400 m2" or "106" would misread "400 m²" and "10⁶".
            if line:
                usual = max(line[3], key=line[3].get)
                if size * across < 0.8 * usual and (e * c + f * d) / across - line[2] > 0.15 * usual:
                    if _MARK.fullmatch(piece.strip()) and _note_mark(line[0]):
                        continue
                    if re.fullmatch(r'[\d+\-−]{1,4}', piece.strip()):
                        piece = piece.strip().translate(_RAISED)
            if line is None:
                line = ['', (e * a + f * b) / along, (e * c + f * d) / across, Counter()]
                lines.append(line)
            line[0] += piece
            line[3][round(size * across, 1)] += len(piece)

    page.extract_text(visitor_text=visit)
    # A matrix can hold anything. A position that is not a number counts as none at all.
    return [(text,) + tuple(v if math.isfinite(v) else 0 for v in (x, y, max(sizes, key=sizes.get)))
            for text, x, y, sizes in lines]


def _layout(pages):
    """Positioned lines turned into the plain ones reflow() reads.

    A '' goes where the gap above a line, or a change of type size, says a paragraph has
    ended. An indent becomes leading spaces. A line in larger type than the body gets
    "# ", and a jump back up the page (the next column) is marked as a turn.
    """
    sizes, gaps = Counter(), Counter()
    for lines in pages:
        for above, line in zip([None] + lines, lines):
            sizes[line[3]] += len(line[0])
            if above and above[3] == line[3] > 0 and 0 < above[2] - line[2] < 4 * line[3]:
                gaps[round((above[2] - line[2]) / line[3], 1)] += 1
    body = max(sizes, key=sizes.get, default=0)
    usual = max(sorted(gaps), key=gaps.get, default=0)    # line spacing, in type sizes
    bigger = sorted((s for s in sizes if body and s >= body * 1.15), reverse=True)
    out = []
    for lines in pages:
        # A column is the run of lines up to the next jump back up the page. Indents are
        # measured from its own left edge.
        columns = [[]]
        for line in lines:
            if columns[-1] and line[2] - columns[-1][-1][2] > line[3]:
                columns.append([])
            columns[-1].append(line)
        page = []
        for column in columns:
            if page:
                page.append(_TURN)
            left = min((x for _, x, _, _ in column), default=0)
            above = None
            for text, x, y, size in column:
                text = ' '.join(text.translate(_PDF_CHARS).split())
                if above and (abs(size - above[1]) > 0.1 * max(size, above[1])
                              or (usual and above[0] - y > usual * size * 1.25)):
                    page.append('')
                if size in bigger and len(text) <= 150 and any(ch.isalpha() for ch in text):
                    page.append('#' * min(bigger.index(size) + 1, 3) + ' ' + text)
                else:
                    page.append(' ' * max(0, min(40, round((x - left) / (size / 2 or 1)))) + text)
                above = (y, size)
        out.append(page)
    return out


def _furniture(pages):
    """Where the page numbers and running headers or footers are, as {(page, line)}.

    Looked for among the first three and last three blocks of each page, in the order
    the PDF draws them, and only in blocks of one or two lines: a header stands apart
    from the text, and the last lines of a paragraph do not. Such a line counts as
    running when, digits aside, it is at the edge of two pages in five (and of at least
    two), which also catches headers that alternate between left and right pages.
    """
    def key(line):
        return re.sub(r'\d+', '#', ' '.join(line.lower().split()))

    edges = []
    for lines in pages:
        blocks, block = [], []
        for i, line in enumerate(lines + ['']):
            if line.strip():
                block.append(i)
            elif block:
                blocks.append(block)
                block = []
        edges.append({i for block in blocks[:3] + blocks[-3:] if len(block) < 3 for i in block})
    seen = Counter(k for lines, at in zip(pages, edges) for k in {key(lines[i]) for i in at})
    enough = max(2, 0.4 * sum(1 for at in edges if at))
    return {(p, i) for p, (lines, at) in enumerate(zip(pages, edges)) for i in at
            if _PAGE_NO.fullmatch(lines[i].strip()) or seen[key(lines[i])] >= enough}


def reflow(pages):
    """Paragraphs out of the visual lines of a PDF. Returns (text, lines dropped, words joined).

    `pages` is a list of pages, each a list of lines as they were drawn. A '' line is a
    gap: the paragraph above it has ended. Leading spaces are an indent. Where nothing
    of the kind marks the end of a paragraph, the words decide: a line that stops short
    at the end of a sentence ends its paragraph, and so does one that stops far short
    anywhere. Over a page turn only the words can tell, so a paragraph that runs on to
    the next page is put back together.
    """
    furniture = _furniture(pages)
    paras, para, numbers = [], None, []   # numbers: where each numbered list still open has got to
    for p, lines in enumerate(pages):
        lines = ['' if (p, i) in furniture else raw for i, raw in enumerate(lines)]
        gap, turned = False, True
        for i, raw in enumerate(lines):
            if raw == _TURN or i == 0:
                # How long a full line is in the column that starts here. Not the very
                # longest: one stray long line would make every other line look short.
                end = i + 1
                while end < len(lines) and lines[end] != _TURN:
                    end += 1
                lengths = sorted(len(l.strip()) for l in lines[i:end] if l.strip())
                full = lengths[len(lengths) * 4 // 5] if lengths else 0
            if raw == _TURN:
                gap, turned = False, True
                continue
            line = ' '.join(raw.split())
            if not line:
                gap = not turned          # a gap at the top of a page says nothing
                continue
            indent = len(raw) - len(raw.lstrip(' '))
            head = re.match(r'#{1,3} ', line)
            bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
            last = para and para['lines'][-1]
            ended = not para or gap or bool(_ENDS.search(last))
            # "1." opens a list, perhaps inside another, and "4." carries on from a "3."
            # that is still open. Any other number that opens a line straight under
            # another is part of a sentence that happened to wrap there.
            n = numbered and int(numbered[1])
            if numbered and n != 1 and n - 1 not in numbers and para and not gap:
                numbered = None
            # A dash opens a list item only where a sentence could not be carrying on.
            if bullet and line[0] in '-–—*' and not (ended or para['kind'] == 'item'):
                bullet = None
            # How much of a full line the line above would fill with this line's first
            # word added. Had there been room for the word, it would have been put there.
            # Over a turn the wider column is the measure, or a one-line header would
            # count as a full line of its own column and run on into the text.
            room = para and (len(last) + 1 + len(line.split()[0])) / (max(para['full'], full) or 1)
            if not para or gap:
                new = True
            elif head or para['kind'] == 'head':
                new = not (head and head[0] == para['head'])      # a heading of two lines
            elif bullet or numbered:
                new = True
            elif last[-1] in _HYPHENS:
                new = False               # a word split to fit the line: more is coming
            elif room <= 0.6:
                new = True                # far too short to be a wrapped line: it was ended
            elif turned:
                new = ended
            elif indent >= para['indent'] + 2 and para['kind'] != 'item':
                new = True                # the first line of a paragraph, set in from the rest
            elif len(para['lines']) > 1 and indent <= para['indent'] - 2:
                new = True                # back out to the margin after a hanging indent
            else:
                new = ended and room <= 0.85      # counted in letters, so not much to spare
            if new:
                if numbered:
                    if n - 1 in numbers:
                        del numbers[numbers.index(n - 1):]        # a list inside this one is over
                    numbers = numbers[-8:] + [n]
                    line = f'{n}. ' + line[numbered.end():]
                elif bullet:
                    line = ('- ' + line[bullet.end():]).strip()
                para = {'kind': 'head' if head else 'item' if bullet or numbered else 'text',
                        'head': head and head[0], 'lines': [line]}
                paras.append(para)
            else:
                para['lines'].append(line[len(head[0]):] if head else line)
            para['indent'], para['full'] = indent, full
            gap = turned = False

    out, rejoined = [], 0
    for para in paras:
        pieces = para['lines'][:1]
        for line in para['lines'][1:]:
            before, end = pieces[-1][-2:-1], pieces[-1][-1:]
            if end == '\xad' or (end in _HYPHENS and before.isalpha() and line[0].islower()):
                pieces[-1] = pieces[-1][:-1]      # a word split to fit the line
                rejoined += 1
            elif not (end in _HYPHENS and before.isalnum()):          # "Anglo-" keeps its hyphen
                line = ' ' + line
            pieces.append(line)
        out.append(''.join(pieces))
    return '\n\n'.join(out).replace('\xad', ''), len(furniture), rejoined


# ── .docx out ────────────────────────────────────────────────────────────────

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_PACKAGE = 'http://schemas.openxmlformats.org/package/2006/'
_OFFICE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
_WORDML = 'application/vnd.openxmlformats-officedocument.wordprocessingml.'
_CONTENT_TYPES = (
    f'{_XML}<Types xmlns="{_PACKAGE}content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/word/document.xml" ContentType="{_WORDML}document.main+xml"/>'
    f'<Override PartName="/word/styles.xml" ContentType="{_WORDML}styles+xml"/>'
    f'<Override PartName="/word/numbering.xml" ContentType="{_WORDML}numbering+xml"/>'
    f'<Override PartName="/word/settings.xml" ContentType="{_WORDML}settings+xml"/>'
    '</Types>')
_RELS = (f'{_XML}<Relationships xmlns="{_PACKAGE}relationships">'
         f'<Relationship Id="rId1" Type="{_OFFICE}officeDocument" Target="word/document.xml"/>'
         '</Relationships>')
_DOCUMENT_RELS = (f'{_XML}<Relationships xmlns="{_PACKAGE}relationships">'
                  f'<Relationship Id="rId1" Type="{_OFFICE}styles" Target="styles.xml"/>'
                  f'<Relationship Id="rId2" Type="{_OFFICE}numbering" Target="numbering.xml"/>'
                  f'<Relationship Id="rId3" Type="{_OFFICE}settings" Target="settings.xml"/>'
                  '</Relationships>')
# The one setting. Without it Word opens the file in "Compatibility Mode", as if Word 2007
# had written it, and says so in the title bar.
_SETTINGS = (f'{_XML}<w:settings {_W}><w:compat><w:compatSetting w:name="compatibilityMode" '
             'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>')
_HEADING = ('<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
            '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/>'
            '<w:qFormat/><w:pPr><w:keepNext/><w:keepLines/>'
            '<w:spacing w:before="{before}" w:after="80"/><w:outlineLvl w:val="{outline}"/></w:pPr>'
            '<w:rPr><w:b/><w:bCs/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:style>')
_STYLES = (
    f'{_XML}<w:styles {_W}><w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
    '<w:spacing w:after="160" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    '</w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    '<w:qFormat/></w:style>'
    + ''.join(_HEADING.format(n=n, outline=n - 1, before=before, size=size)
              for n, before, size in ((1, 360, 32), (2, 240, 28), (3, 200, 26)))
    + '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
    '<w:basedOn w:val="Normal"/><w:uiPriority w:val="34"/><w:qFormat/>'
    '<w:pPr><w:ind w:left="720"/><w:contextualSpacing/></w:pPr></w:style></w:styles>')
_LIST_LEVEL = ('<w:lvl w:ilvl="{n}"><w:start w:val="1"/><w:numFmt w:val="{kind}"/>'
               '<w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/>'
               '<w:pPr><w:ind w:left="{left}" w:hanging="360"/></w:pPr></w:lvl>')
_INLINE = re.compile(r'(?<![\w*])(\*{1,3})(?=\S)(.{1,2000}?)(?<=\S)\1(?![\w*])')    # bounded, so it cannot crawl


def _page_size():
    """Paper width and height in twentieths of a point: US Letter where Windows says so, else A4."""
    try:
        paper = ctypes.c_uint()
        # LOCALE_USER_DEFAULT, LOCALE_IPAPERSIZE | LOCALE_RETURN_NUMBER. 1 is Letter, 9 is A4.
        ctypes.windll.kernel32.GetLocaleInfoW(0x0400, 0x2000100A, ctypes.byref(paper), 2)
        letter = paper.value == 1
    except (AttributeError, OSError):     # not Windows
        letter = False
    return (12240, 15840) if letter else (11906, 16838)


def _run(text, props=''):
    if not text:
        return ''
    body = '<w:tab/>'.join(f'<w:t xml:space="preserve">{escape(part)}</w:t>' if part else ''
                           for part in text.split('\t'))
    return '<w:r>' + (f'<w:rPr>{props}</w:rPr>' if props else '') + body + '</w:r>'


def _runs(text, bold=False):
    """The runs of a paragraph: **bold**, *italic* and ***both*** become run properties.
    With `bold`, every run is bold, as in a table's header row."""
    out, at, b = [], 0, '<w:b/><w:bCs/>'
    for m in _INLINE.finditer(text):
        stars = len(m[1])
        out.append(_run(text[at:m.start()], b if bold else ''))
        out.append(_run(m[2], (b if stars > 1 or bold else '')
                        + ('<w:i/><w:iCs/>' if stars != 2 else '')))
        at = m.end()
    return ''.join(out) + _run(text[at:], b if bold else '')


_MONO = '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/><w:sz w:val="20"/><w:szCs w:val="20"/>'
_BORDERS = ''.join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
                   for side in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'))
_RULE_ROW = re.compile(r'\|?(\s*:?-+:?\s*\|)+\s*(:?-+:?\s*)?')      # |---|:--:|
# Only backticks fence code, as chat models write it: a ~~~ line in prose is a scene break.
_FENCE = re.compile(r'(`{3,})[^`]*')                                # "```npm i```" is inline code


def _table(rows, header=True):
    """A table as a Word table: the cells of each row, the first row a bold header unless `header` is false."""
    width = max(len(row) for row in rows)

    def cell(text, header):
        text = text.replace('\\|', '|')
        return ('<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr><w:p><w:pPr><w:spacing w:after="0"/>'
                '</w:pPr>' + _runs(text, header) + '</w:p></w:tc>')
    return ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>'
            f'<w:tblBorders>{_BORDERS}</w:tblBorders></w:tblPr>'
            '<w:tblGrid>' + f'<w:gridCol w:w="{9000 // width}"/>' * width + '</w:tblGrid>'
            + ''.join('<w:tr>' + ('<w:trPr><w:tblHeader/></w:trPr>' if header and i == 0 else '')
                      + ''.join(cell(text, header and i == 0) for text in row + [''] * (width - len(row)))
                      + '</w:tr>'
                      for i, row in enumerate(rows)) + '</w:tbl>')


def write_docx(text, word_rows=False):
    """A new .docx from Markdown-flavoured text, as bytes.

    "# " to "### " lines are headings, "- " and "* " lines bullet items, "1. " lines
    numbered items, two spaces of indent one level deeper. A blank line ends a paragraph
    and the lines between two blank lines are one paragraph. Lines between ``` fences are
    code, kept line by line in a fixed-width font. Lines that open with | and have a
    |---| row second are a table. With `word_rows`, for text read from a Word file, so are two or
    more paragraphs in a row of the form "cell | cell", with as many cells each, which is how
    _read_docx() gives a Word table. Without it, such lines are sentences that hold a | (a
    shell pipeline, "small | medium | large"), and stay text. Everything else is body text.
    """
    paras, para = [], None                # each [paragraph properties, text], or [None, xml] built already
    lists, following = [], {}             # numbered lists; per level, (the list, its next number)
    fence, code, rows = None, [], []      # an open ``` and the lines inside it; lines that open with |
    text = _UNSAFE.sub('', text.replace('\r\n', '\n').replace('\r', '\n'))
    for raw in text.split('\n') + [None]:                # None: the end, where an open block is closed
        line = '' if raw is None else raw.strip()
        if fence:
            # No markup is read inside, and the lines keep their indent.
            if raw is None or (line.startswith(fence) and not line.strip(fence[0])):
                paras.append([None, '<w:p><w:pPr><w:spacing w:line="240" w:lineRule="auto"/></w:pPr>'
                              + '<w:r><w:br/></w:r>'.join(_run(part.rstrip(), _MONO) for part in code) + '</w:p>'])
                fence, code = None, []
            else:
                code.append(raw)
            continue
        if line.startswith('|'):
            rows.append(line)
            para = None
            continue
        if rows:
            if len(rows) > 1 and _RULE_ROW.fullmatch(rows[1]):
                paras.append([None, _table([[part.strip() for part in re.split(r'(?<!\\)\|', row.strip('|'))]
                                            for row in rows[:1] + rows[2:]])])
            else:
                paras.extend(['', row] for row in rows)     # not a table: a paragraph to a line
            rows = []
        if raw is None:
            break
        opening = _FENCE.fullmatch(line)
        if opening:
            fence, para = opening[1], None
            continue
        level = min((len(raw) - len(raw.lstrip(' '))) // 2, 8)
        heading = re.match(r'(#{1,6})\s+(.+)', line)
        bullet = re.match(r'[-*+]\s+(.+)', line)
        numbered = re.match(r'(\d{1,3})[.)]\s+(.+)', line)
        if numbered and para and not para[0] and numbered[1] != '1':
            numbered = None               # "12. " on the second line of a paragraph is a sentence
        if not line:
            para = None
        elif heading:
            paras.append([f'<w:pStyle w:val="Heading{min(len(heading[1]), 3)}"/>', heading[2]])
            para = None
        elif bullet or numbered:
            num_id, body = 1, (bullet or numbered)[bullet and 1 or 2]
            if numbered:
                # Word does the counting. A number that does not follow from the item
                # before it starts a list of its own, told where to begin.
                n = int(numbered[1])
                num_id, expected = following.get(level, (0, None))
                if n != expected:
                    lists.append((level, n))
                    num_id = len(lists) + 1
                following[level] = (num_id, n + 1)
                following = {k: v for k, v in following.items() if k <= level}
            para = ['<w:pStyle w:val="ListParagraph"/><w:numPr>'
                    f'<w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>', body]
            paras.append(para)
        elif para:
            para[1] += ' ' + line
        else:
            para = ['', line]
            paras.append(para)

    # A Word table comes from _read_docx() as one "cell | cell" paragraph to a row. A run of
    # them with as many cells each is that table again, with no header: the file did not say
    # which row is one. Cells are counted by '|' alone, because a row whose first or last cell
    # is empty (" | West pier") has lost the space on that side; one row of the run must
    # hold a " | ", so that "| not a table" lines stay text. A | inside `code` is no cell bar.
    cells = [len(body.split('|')) if word_rows and props == '' and '|' in body
             and not re.search(r'`[^`]*\|[^`]*`', body) else 0 for props, body in paras]
    built, i = [], 0
    while i < len(paras):
        end = i + 1
        while cells[i] and end < len(paras) and cells[end] == cells[i]:
            end += 1
        run = paras[i:end]
        if len(run) > 1 and any(' | ' in body for _, body in run):
            rows = [[part.strip() for part in body.split('|')] for _, body in run]
            if all(not row[0] and not row[-1] for row in rows):     # | a | b | with no |---| row
                rows = [row[1:-1] for row in rows]
            run = [[None, _table(rows, header=False)]]
        for block in run:
            if built and all(b[0] is None and b[1].startswith('<w:tbl>') for b in (built[-1], block)):
                built.append(['', ''])            # Word joins two tables that touch
            built.append(block)
        i = end
    paras = built

    width, height = _page_size()
    document = (
        f'{_XML}<w:document {_W}><w:body>'
        + (''.join(body if props is None else
                   '<w:p>' + (f'<w:pPr>{props}</w:pPr>' if props else '') + _runs(body) + '</w:p>'
                   for props, body in paras) or '<w:p/>')
        # Word wants a paragraph after a table that ends the document.
        + ('<w:p/>' if paras and paras[-1][0] is None and paras[-1][1].startswith('<w:tbl>') else '')
        + f'<w:sectPr><w:pgSz w:w="{width}" w:h="{height}"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>')
    numbering = f'{_XML}<w:numbering {_W}>' + ''.join(
        f'<w:abstractNum w:abstractNumId="{i}"><w:multiLevelType w:val="hybridMultilevel"/>'
        + ''.join(_LIST_LEVEL.format(n=n, kind=kind, text=mark or f'%{n + 1}.', left=720 * (n + 1))
                  for n in range(9)) + '</w:abstractNum>'
        for i, (kind, mark) in enumerate((('bullet', '•'), ('decimal', '')))
    ) + '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>' + ''.join(
        f'<w:num w:numId="{i}"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="{level}">'
        f'<w:startOverride w:val="{start}"/></w:lvlOverride></w:num>'
        for i, (level, start) in enumerate(lists or [(0, 1)], 2)) + '</w:numbering>'

    packed = io.BytesIO()
    with zipfile.ZipFile(packed, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, part in (('[Content_Types].xml', _CONTENT_TYPES), ('_rels/.rels', _RELS),
                           ('word/document.xml', document), ('word/styles.xml', _STYLES),
                           ('word/numbering.xml', numbering), ('word/settings.xml', _SETTINGS),
                           ('word/_rels/document.xml.rels', _DOCUMENT_RELS)):
            z.writestr(name, part.encode('utf-8'))
    return packed.getvalue()


# ── command line ─────────────────────────────────────────────────────────────

def main(argv):
    """extract <name>: file bytes in, one JSON object out.
    rewrite: four bytes of length, that much JSON {"text"}, then a Word file in; one JSON
    line and then the rewritten Word file out. Exit 0 either way: the JSON says how it went."""
    if argv[1:2] not in (['extract'], ['rewrite']) or len(argv) != (3 if argv[1:2] == ['extract'] else 2):
        sys.stderr.write('usage: python documents.py extract <name> < file\n'
                         '       python documents.py rewrite < length + json + file\n')
        return 2
    # The bytes, not sys.stdout: the parent may be pythonw with a console encoding that
    # is not UTF-8. And anything a library prints must not land beside the JSON.
    out, docx = sys.stdout.buffer, b''
    # The server ends this process after 90 or 120 seconds, but only while the server is
    # there. A reader stuck in a loop must not outlive the app that started it.
    watchdog = threading.Timer(150, os._exit, [3])
    watchdog.daemon = True
    watchdog.start()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            raw = sys.stdin.buffer.read()
            if argv[1] == 'extract':
                result = dict(extract(argv[2], raw), ok=True)
            else:
                size = struct.unpack('>I', raw[:4])[0]
                docx, notes = rewrite_docx(raw[4 + size:], json.loads(raw[4:4 + size])['text'])
                result = {'ok': True, 'notes': notes}
    except DocumentError as e:
        result = {'ok': False, 'code': e.code, 'message': e.message}
    except Exception:
        result = {'ok': False, 'code': 'unreadable', 'message': 'This file could not be read.'}
    out.write(json.dumps(result).encode('utf-8'))
    if argv[1] == 'rewrite':
        out.write(b'\n' + docx)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
