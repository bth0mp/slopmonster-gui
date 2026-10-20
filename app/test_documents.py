#!/usr/bin/env python3
"""Tests for documents.py. Run: python app/test_documents.py

No network, no Word and nothing to install. The Word files are built here part by part,
and the PDFs are written out by hand, so every byte that goes in is in this file. Only the
checks that read a PDF need pypdf: without it they print a line and step aside.
"""
import io
import json
import os
import re
import struct
import subprocess
import sys
import time
import traceback
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import documents

# A failure detail can quote text the console codepage cannot print. Replace, do not raise.
try:
    sys.stdout.reconfigure(errors='replace')
except (AttributeError, ValueError):
    pass

try:
    import pypdf  # noqa: F401  only to know whether the PDF checks can run
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False

fails = []


def check(name, cond, detail=''):
    if not cond:
        fails.append(f'{name}: {detail}')


def section(fn):
    """Run one group of checks now. A crash inside it is one more failure, not the end of the run."""
    try:
        fn()
    except Exception as e:
        line = traceback.extract_tb(e.__traceback__)[1].lineno
        fails.append(f'{fn.__name__}: crashed at line {line} with {e!r}')


def code(name, data):
    """The DocumentError code extract() raises for this file, or 'no error'."""
    try:
        documents.extract(name, data)
    except documents.DocumentError as e:
        return e.code
    return 'no error'


def timed(name, data):
    """(seconds, error code) for one extract() call."""
    began = time.time()
    got = code(name, data)
    return time.time() - began, got


# ── Word files, built by hand ───────────────────────────────────────────────
NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W = (f'xmlns:w="{NS}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
     'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
     'xmlns:v="urn:schemas-microsoft-com:vml"')


def package(document=None, styles=None, numbering=None, extra=()):
    """A zip with these parts under word/. Text or bytes."""
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, part in (('document', document), ('styles', styles), ('numbering', numbering)):
            if part is not None:
                z.writestr(f'word/{name}.xml', part)
        for name, part in extra:
            z.writestr(name, part)
    return packed.getvalue()


def body(inner):
    return f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{inner}</w:body></w:document>'


def p(text, style=None, num=None, level=0):
    """One paragraph: plain text, or ready-made runs when it starts with a tag."""
    props = f'<w:pStyle w:val="{style}"/>' if style else ''
    if num:
        props += f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr>'
    runs = text if text.startswith('<') else f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'
    return '<w:p>' + (f'<w:pPr>{props}</w:pPr>' if props else '') + runs + '</w:p>'


def t(text):
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


# The ids are what a German Word writes. Only the names say what a style is.
STYLES = f'''<?xml version="1.0" encoding="UTF-8"?><w:styles {W}>
<w:style w:type="paragraph" w:default="1" w:styleId="Standard"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Titel"><w:name w:val="Title"/></w:style>
<w:style w:type="paragraph" w:styleId="berschrift1"><w:name w:val="heading 1"/><w:basedOn w:val="Standard"/></w:style>
<w:style w:type="paragraph" w:styleId="berschrift2"><w:name w:val="heading 2"/></w:style>
<w:style w:type="paragraph" w:styleId="berschrift5"><w:name w:val="heading 5"/></w:style>
<w:style w:type="paragraph" w:styleId="Eigen"><w:name w:val="Zwischentitel"/>
  <w:basedOn w:val="berschrift2"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Margin note"/></w:style>
<w:style w:type="paragraph" w:styleId="Aufzhlung"><w:name w:val="List Bullet"/>
  <w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Kreis"><w:name w:val="a"/><w:basedOn w:val="Kreis2"/></w:style>
<w:style w:type="paragraph" w:styleId="Kreis2"><w:name w:val="b"/><w:basedOn w:val="Kreis"/></w:style>
</w:styles>'''
# Lists 2 and 3 share one definition, so 3 carries on where 2 stopped. List 4 says to start again.
NUMBERING = f'''<?xml version="1.0" encoding="UTF-8"?><w:numbering {W}>
<w:abstractNum w:abstractNumId="0">
  <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="-"/></w:lvl>
  <w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/><w:lvlText w:val="o"/></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1">
  <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
  <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%2)"/></w:lvl>
</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
<w:num w:numId="3"><w:abstractNumId w:val="1"/></w:num>
<w:num w:numId="4"><w:abstractNumId w:val="1"/>
  <w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>
</w:numbering>'''
BOX = '<w:txbxContent><w:p><w:r><w:t>Boxed words.</w:t></w:r></w:p></w:txbxContent>'
DOCUMENT = body(
    p('Report title', 'Titel')
    + p('First heading', 'berschrift1')
    + p('Not a heading', 'Heading1')
    # A tab stop in the paragraph's properties is not a tab in its text.
    + '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="2880"/></w:tabs></w:pPr>'
    + t('Name:') + '<w:r><w:tab/></w:r>' + t('value') + '<w:r><w:br/></w:r>' + t('second line') + '</w:p>'
    + p('Step one', num=2) + p('Step two', num=2)
    + p('Detail a', num=2, level=1) + p('Detail b', num=2, level=1)
    + p('Step three', num=2)
    + p('Between.')
    + p('Step four', num=3) + p('Fresh one', num=4)
    + p('Bullet', num=1) + p('Nested bullet', num=1, level=1) + p('Bullet by style', 'Aufzhlung')
    + p('Custom heading', 'Eigen') + p('Deep heading', 'berschrift5')
    + '<w:tbl><w:tblPr/><w:tr><w:tc>' + p('Phase') + '</w:tc><w:tc>' + p('Cost') + '</w:tc></w:tr>'
    + '<w:tr><w:tc>' + p('One', 'berschrift1') + '</w:tc><w:tc>' + p('120,000') + p('estimated')
    + '</w:tc></w:tr></w:tbl>'
    + p(t('Kept ') + '<w:del w:id="1"><w:r><w:delText>deleted words </w:delText></w:r></w:del>'
        + '<w:ins w:id="2">' + t('inserted words ') + '</w:ins>' + t('end.'))
    + p('<w:moveFrom w:id="3">' + t('The moved sentence. ') + '</w:moveFrom>' + t('It used to stand here.'))
    + p('<w:moveTo w:id="4">' + t('The moved sentence. ') + '</w:moveTo>' + t('Now it stands here.'))
    + p(t('Anchor paragraph.') + '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:txbx>'
        + BOX + '</wps:txbx></w:drawing></mc:Choice><mc:Fallback><w:pict><v:textbox>' + BOX
        + '</v:textbox></w:pict></mc:Fallback></mc:AlternateContent></w:r>')
    + p(t('Footnoted claim') + '<w:r><w:footnoteReference w:id="2"/></w:r><w:r><w:commentReference w:id="0"/></w:r>'
        + '<w:r><w:instrText>PAGE</w:instrText></w:r>' + t('.'))
    + p('Loop', 'Kreis')
    + '<w:sectPr><w:headerReference w:type="default" r:id="rId9" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></w:sectPr>')
EXPECTED = '''# Report title

# First heading

Not a heading

Name:\tvalue
second line

1. Step one

2. Step two

  1. Detail a

  2. Detail b

3. Step three

Between.

4. Step four

1. Fresh one

- Bullet

  - Nested bullet

- Bullet by style

Custom heading

##### Deep heading

Phase | Cost

One | 120,000
estimated

Kept inserted words end.

It used to stand here.

The moved sentence. Now it stands here.

Anchor paragraph.

Footnoted claim.

Loop'''


@section
def hand_built_docx():
    got = documents.extract('bericht.docx', package(DOCUMENT, STYLES, NUMBERING))
    text = got['text']
    check('docx: the whole text', text == EXPECTED,
          next((f'got {a!r}, want {b!r}' for a, b in zip(text.split('\n'), EXPECTED.split('\n')) if a != b),
               f'{len(text)} characters, want {len(EXPECTED)}'))
    check('docx: kind and summary', (got['kind'], got['summary']) == ('docx', 'Word document, 25 paragraphs'),
          (got['kind'], got['summary']))
    check('docx: headings come from the style name, not its id',
          '# First heading' in text and '\nNot a heading' in text, text[:80])
    # A text box is not body text. It stays in the file as it is and is not offered for rewriting.
    check('docx: text in a text box is not shown', 'Boxed words.' not in text)
    check('docx: a cell with two paragraphs keeps them on two lines', 'One | 120,000\nestimated' in text)
    check('docx: a tracked deletion is left out and an insertion kept',
          'deleted words' not in text and 'inserted words' in text)
    check('docx: moved text is read where it went', text.count('The moved sentence.') == 1)
    check('docx: a field code is not text', 'PAGE' not in text)
    check('docx: notes say what was left out', got['notes'] == [
        'Headers, footnotes, comments and text boxes are not shown here and stay in the file as they are.',
        documents.TRACKED_NOTE,
        'A table row is one line, with | between the cells.'], got['notes'])

    plain = documents.extract('a.docx', package(body(p('Only one.'))))
    check('docx: no notes when nothing was left out',
          (plain['text'], plain['summary'], plain['notes']) == ('Only one.', 'Word document, 1 paragraph', []), plain)
    check('docx: a list with no numbering part still reads as a list',
          documents.extract('a.docx', package(body(p('Item', num=7))))['text'] == '1. Item')

    # "Strict Open XML" is the same document under other namespace names.
    strict = [part.replace(NS, 'http://purl.oclc.org/ooxml/wordprocessingml/main')
              for part in (DOCUMENT, STYLES, NUMBERING)]
    check('docx: a strict-format file reads the same',
          documents.extract('strict.docx', package(*strict))['text'] == EXPECTED)
    as_utf16 = DOCUMENT.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode('utf-16')
    check('docx: a part in UTF-16 reads the same',
          documents.extract('a.docx', package(as_utf16, STYLES, NUMBERING))['text'] == EXPECTED)
    # A part in another encoding is read as what it declares, as Word reads it. (The check
    # for a DTD is made after the part has been turned into UTF-8, so none can hide in one.)
    latin = body(p('Café')).replace('encoding="UTF-8"', 'encoding="ISO-8859-1"').encode('latin-1')
    check('docx: a part that declares Latin-1 is read as Latin-1',
          documents.extract('a.docx', package(latin))['text'] == 'Café')


# ── text in, Word file out, text back ───────────────────────────────────────
ROUND_TRIP = '''# Main title

The first paragraph is plain.

## A section

- First bullet

- Second bullet

  - Nested bullet

- Third bullet

Between the lists.

1. Step one

2. Step two

  1. A step inside step two

3. Step three

### A smaller heading

1. A second list starts at one again

2. And goes on

A list can pick up later:

5. Five

6. Six

Symbols: 5 < 6 > 4 & "quoted", café — 你好.'''
PARTS = ['[Content_Types].xml', '_rels/.rels', 'word/document.xml', 'word/styles.xml',
         'word/numbering.xml', 'word/settings.xml', 'word/_rels/document.xml.rels']


@section
def round_trip():
    packed = documents.write_docx(ROUND_TRIP)
    got = documents.extract('out.docx', packed)
    check('round trip: the text comes back as it went in', got['text'] == ROUND_TRIP,
          next((f'got {a!r}, want {b!r}' for a, b in zip(got['text'].split('\n'), ROUND_TRIP.split('\n'))
                if a != b), 'lengths differ'))
    check('round trip: nothing to warn about', got['notes'] == [], got['notes'])
    again = documents.extract('out.docx', documents.write_docx(got['text']))['text']
    check('round trip: a second trip changes nothing', again == ROUND_TRIP)
    blocks = ROUND_TRIP.split('\n\n')
    check('round trip: every paragraph, heading and list item is a block of one line',
          len(blocks) == 19 and got['summary'] == 'Word document, 19 paragraphs'
          and not any('\n' in block for block in blocks), (len(blocks), got['summary']))
    # The same lists the way Markdown is more often written, with no blank line between items.
    tight = ROUND_TRIP.replace('\n\n- ', '\n- ').replace('\n\n  ', '\n  ')
    for n in '236':
        tight = tight.replace(f'\n\n{n}. ', f'\n{n}. ')
    check('round trip: list items need no blank line between them going in',
          tight.count('\n\n') < ROUND_TRIP.count('\n\n') - 8
          and documents.extract('out.docx', documents.write_docx(tight))['text'] == ROUND_TRIP)

    with zipfile.ZipFile(io.BytesIO(packed)) as z:
        names = z.namelist()
        parts = {name: z.read(name).decode('utf-8') for name in names}
    check('write: the package has exactly its seven parts', names == PARTS, names)
    for part in ('document', 'styles', 'numbering', 'settings'):
        check(f'write: {part}.xml has a content type and, bar the document, a relationship',
              f'/word/{part}.xml' in parts['[Content_Types].xml'] and (
                  part == 'document' or f'Target="{part}.xml"' in parts['word/_rels/document.xml.rels']))
    for name, part in parts.items():
        try:
            ET.fromstring(part)
            well_formed = True
        except ET.ParseError as e:
            well_formed = str(e)
        check(f'write: {name} is well-formed XML', well_formed is True, well_formed)
    for style in ('Normal', 'heading 1', 'heading 2', 'heading 3', 'List Paragraph'):
        check(f'write: styles.xml defines {style}', f'<w:name w:val="{style}"/>' in parts['word/styles.xml'])
    numbering = parts['word/numbering.xml']
    check('write: one bullet definition and one decimal one', numbering.count('<w:abstractNum ') == 2
          and '<w:numFmt w:val="bullet"/>' in numbering and '<w:numFmt w:val="decimal"/>' in numbering)
    check('write: a list that starts at 5 is told to', '<w:startOverride w:val="5"/>' in numbering)

    marked = ('Plain **bold words** and *slanted* and ***both***, with a\ttab,\n'
              'a second line & <angle> brackets.\n\n* A star bullet\n\n2 * 3 * 4 = 24 and a lone * star')
    packed = documents.write_docx(marked)
    with zipfile.ZipFile(io.BytesIO(packed)) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    check('write: bold is a run of its own',
          '<w:r><w:rPr><w:b/><w:bCs/></w:rPr><w:t xml:space="preserve">bold words</w:t></w:r>' in xml, xml[:600])
    check('write: italic is a run of its own',
          '<w:r><w:rPr><w:i/><w:iCs/></w:rPr><w:t xml:space="preserve">slanted</w:t></w:r>' in xml)
    check('write: three stars are both',
          '<w:rPr><w:b/><w:bCs/><w:i/><w:iCs/></w:rPr><w:t xml:space="preserve">both</w:t>' in xml)
    check('write: every text run keeps its spaces', xml.count('<w:t ') == xml.count('<w:t xml:space="preserve">'))
    check('write: markup characters are escaped', '&amp; &lt;angle&gt; brackets.' in xml)
    back = documents.extract('out.docx', packed)['text']
    check('round trip: bold and italic text comes back without its stars, lines joined', back == (
        'Plain bold words and slanted and both, with a\ttab, a second line & <angle> brackets.\n\n'
        '- A star bullet\n\n2 * 3 * 4 = 24 and a lone * star'), back)

    odd = 'A\x00B\x0bC\x1f' + chr(0xfffe) + chr(0xd800) + '.\r\nSame paragraph.\r\rNext.'
    back = documents.extract('out.docx', documents.write_docx(odd))['text']
    check('write: characters XML cannot hold are dropped', back == 'ABC. Same paragraph.\n\nNext.', repr(back))
    check('write: an empty text is still a document', code('empty.docx', documents.write_docx('')) == 'no_text')
    wrapped = documents.write_docx('It ends by\n2026. After that it is off.\n12. Not a list.')
    back = documents.extract('out.docx', wrapped)['text']
    check('write: a number that opens a wrapped line is not a list item',
          back == 'It ends by 2026. After that it is off. 12. Not a list.', back)

    # Code between ``` fences keeps its lines, and a Markdown table is a table.
    fence = '`' * 3
    markdown = ('Run the steps below.\n\n' + fence + 'yaml\n# install the deps\n- name: build\n'
                '  run: make **not bold**\n\n\tdone\n' + fence + '\n\nUse ' + fence + 'npm i' + fence + ' once.\n\n'
                '| Setting | Default | Notes |\n|---|:-:|---|\n| timeout | 30 | seconds |\n| retries | 3 | a \\| pipe |\n\n'
                '| Second | table |\n| --- | --- |\n| x | *y* |\n\n| not a table\n| nor this\n\nThe end.\n\n'
                '| Last | table |\n|---|---|\n| a | b |')
    packed = documents.write_docx(markdown)
    with zipfile.ZipFile(io.BytesIO(packed)) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    back = documents.extract('out.docx', packed)['text']
    check('write: code keeps its lines, its indent and its markup characters', back.split('\n\n')[1] ==
          '# install the deps\n- name: build\n  run: make **not bold**\n\tdone'
          and 'w:ascii="Consolas"' in xml and xml.count('<w:br/>') == 4, back)
    check('write: three backticks inside a line are not a fence', 'Use ' + fence + 'npm i' + fence + ' once.' in back)
    check('write: a Markdown table is a table, its header bold, an escaped | kept as text',
          xml.count('<w:tbl>') == 3 and '<w:tblHeader/>' in xml
          and '<w:rPr><w:b/><w:bCs/></w:rPr><w:t xml:space="preserve">Setting</w:t>' in xml
          and 'Setting | Default | Notes\n\ntimeout | 30 | seconds\n\nretries | 3 | a | pipe' in back, back)
    check('write: two tables never touch, and a paragraph follows the last one',
          not re.search(r'</w:tbl><w:tbl>', xml) and '</w:tbl><w:p/><w:sectPr>' in xml, xml[-400:])
    check('write: lines that open with | but are no table are a paragraph each',
          '| not a table\n\n| nor this\n\nThe end.' in back, back)
    # A Word file's text saved as a new document, once the file itself is no longer held:
    # each table row is a "cell | cell" paragraph, and the rows make a table again.
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'testdata', 'c_tables.docx'), 'rb') as fh:
        shown = documents.extract('c_tables.docx', fh.read())['text']
    with zipfile.ZipFile(io.BytesIO(documents.write_docx(shown, word_rows=True))) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    check('write: the rows of a Word table, a paragraph each, are a table again, with no header',
          xml.count('<w:tbl>') == 4 and '<w:tblHeader/>' not in xml and '<w:b/>' not in xml
          and re.search(r'<w:tbl>(?:(?!</w:tbl>).)*West pier', xml)
          and not re.search(r'</w:tbl><w:tbl>', xml), shown)
    with zipfile.ZipFile(io.BytesIO(documents.write_docx('Home | About | Contact\n\nSome prose.\n\nGo | on',
                                                        word_rows=True))) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    check('write: a row on its own, or rows of other lengths, stay text', '<w:tbl>' not in xml, xml[:600])
    # Text that was not read from a Word file has no table rows: a | in it belongs to a sentence.
    for label, prose, rows in (
            ('shell pipelines', 'Run `ls | grep log` to find the logs.\n\nThen run `ps aux | sort` to see what is running.',
             False),
            ('choices', 'Pick one size: small | medium | large.\n\nPick one colour: red | green | blue.', False),
            ('a menu', 'Home | About | Contact\n\nPrivacy | Terms | Help', False),
            ('pipelines, even in text from a Word file',
             'Run `ls | grep log` to find the logs.\n\nThen run `ps aux | sort` to see what is running.', True)):
        packed = documents.write_docx(prose, word_rows=rows)
        with zipfile.ZipFile(io.BytesIO(packed)) as z:
            xml = z.read('word/document.xml').decode('utf-8')
        check(f'write: {label} stay text', '<w:tbl>' not in xml
              and documents.extract('out.docx', packed)['text'] == prose, xml[:600])
    check('write: code left open at the end is still code', documents.extract('out.docx', documents.write_docx(
        'Before.\n\n' + fence + '\nx = 1\n  y = 2'))['text'] == 'Before.\n\nx = 1\n  y = 2')
    for prose in ('Chapter one ends here.\n\n~~~\n\nChapter two begins.\n\nIt goes on.',
                  'One.\n\n~~~\n\nTwo.\n\n~~~\n\nThree.\n\n~~~\n\nFour.'):
        packed = documents.write_docx(prose)
        with zipfile.ZipFile(io.BytesIO(packed)) as z:
            mono = 'Consolas' in z.read('word/document.xml').decode('utf-8')
        back = documents.extract('out.docx', packed)['text']
        check('write: a ~~~ break in prose stays text, and what follows is no code',
              back == prose and not mono, repr(back))
    back = documents.extract('out.docx', documents.write_docx(
        '| **Feature** | *Plan A* |\n|---|---|\n| **Speed** | fast |'))['text']
    check('write: stars in a table header are formatting, not text', back == 'Feature | Plan A\n\nSpeed | fast', back)


# ── what kind of file it is ─────────────────────────────────────────────────
OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + bytes(600)      # how every .doc, .xls and .ppt begins


@section
def stars_that_are_not_emphasis():
    # Text from a paste or a PDF is not Markdown. A star between two characters is arithmetic
    # or code, and taking it for emphasis would change the numbers in the saved file.
    for plain in ('The area is 5*3 = 15 and the volume is 2*3*4 = 24.', 'g(x**2 + y**2)',
                  'Call f(*args, **kwargs) to pass them on.'):
        got = documents.extract('x.docx', documents.write_docx(plain))['text']
        check(f'write: {plain[:24]!r} keeps its stars', got == plain, got)
    got = documents.extract('x.docx', documents.write_docx('It is *very* **important** to (*check*).'))['text']
    check('write: real emphasis is still formatting, not stars', got == 'It is very important to (check).', got)
    # One Enter too many above a heading must not get its marker typed into the paragraph.
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'testdata', 'b_lists.docx'), 'rb') as fh:
        lists = fh.read()
    shown = documents.extract('b_lists.docx', lists)['text']
    out, notes = documents.rewrite_docx(lists, '\n' + shown.replace('\n\n', '\n\n\n') + '\n')
    check('rewrite: blank lines between blocks, however many, change nothing', out == lists and notes == [], notes)
    # A carriage return inside the text is a line end, not nothing.
    cr = package(body(p('Harbour Office&#13;Quay Street')))
    check('docx: a carriage return in the text does not join two words',
          documents.extract('a.docx', cr)['text'] == 'Harbour Office\nQuay Street',
          documents.extract('a.docx', cr)['text'])
    check('docx: and saving that text unchanged changes nothing',
          documents.rewrite_docx(cr, documents.extract('a.docx', cr)['text'])[0] == cr)


# ── a paragraph put back as it was, from the page's Changes view ────────────
def document_xml(docx):
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        return z.read('word/document.xml')


@section
def kept_paragraphs():
    # Keep original puts a paragraph's extracted text back between the rewrite's own blank
    # lines. Saved into the copy, that paragraph has to be the original's, byte for byte.
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'testdata')
    for name in sorted(n for n in os.listdir(here) if n.endswith('.docx')):
        with open(os.path.join(here, name), 'rb') as fh:
            data = fh.read()
        blocks = documents.extract(name, data)['text'].split('\n\n')
        wordy = [k for k, block in enumerate(blocks) if re.search(r'[A-Za-z]{4,}', block)]
        rewritten = [re.sub(r'[A-Za-z]{4,}', 'Altered', block, count=1) for block in blocks]

        def put_together(*still_rewritten):
            # The rewrite's blank lines, which are not the ones extract() gave: the page keeps them.
            chosen = [rewritten[k] if k in still_rewritten else block for k, block in enumerate(blocks)]
            return '\n' + '\n \n'.join(chosen[:2]) + '\n\n\n' + '\n\n'.join(chosen[2:]) + '\n'

        original = document_xml(data)
        check(f'kept: {name}: a paragraph changed and put back leaves document.xml as it was',
              len(wordy) >= 2 and rewritten[wordy[0]] != blocks[wordy[0]]
              and document_xml(documents.rewrite_docx(data, put_together())[0]) == original, wordy)
        # The first changed paragraph put back, the last still rewritten: every byte that differs
        # from the original lies inside one paragraph, the one still rewritten.
        mixed = document_xml(documents.rewrite_docx(data, put_together(wordy[-1]))[0])
        same = next((i for i, (a, b) in enumerate(zip(original, mixed)) if a != b), len(original))
        tail = next((i for i, (a, b) in enumerate(zip(original[::-1], mixed[::-1])) if a != b), len(original))
        between = original[same:len(original) - tail]
        check(f'kept: {name}: next to a paragraph still rewritten, the one put back is the original\'s',
              mixed != original and b'</w:p>' not in between and not re.search(rb'<w:p[ >/]', between),
              (same, between[:300]))


# ── a table of contents, which Word writes from the headings ────────────────
# Shown, its lines were scored by the checker, while the model was told to leave them be: a
# heading fixed in its own paragraph stayed flagged through its copy in the contents.
TOC_STYLES = STYLES.replace('</w:styles>', '''
<w:style w:type="paragraph" w:styleId="Verzeichnis1"><w:name w:val="toc 1"/></w:style>
<w:style w:type="paragraph" w:styleId="Verzeichnis2"><w:name w:val="toc 2"/></w:style>
<w:style w:type="paragraph" w:styleId="Inhaltsverzeichnisberschrift"><w:name w:val="TOC Heading"/></w:style>
<w:style w:type="paragraph" w:styleId="Eigenverzeichnis"><w:name w:val="Mein Verzeichnis"/>
  <w:basedOn w:val="Verzeichnis1"/></w:style>
</w:styles>''')
TAB = '<w:r><w:tab/></w:r>'


def field(kind, code=''):
    """The runs of a complex field: its begin (with its code), separate or end."""
    return (f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'
            + (f'<w:r><w:instrText xml:space="preserve">{code}</w:instrText></w:r>' if code else ''))


CONTENTS = ('<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/></w:docPartObj>'
            '</w:sdtPr><w:sdtContent>'
            + p('Contents', 'Inhaltsverzeichnisberschrift')
            + p(field('begin', r'TOC \o "1-3"') + field('separate') + t('Unlocking growth') + TAB + t('3'),
                'Verzeichnis1')
            + p(t('Next steps') + TAB + t('4') + field('end'), 'Verzeichnis2')
            + '</w:sdtContent></w:sdt>')


@section
def table_of_contents():
    data = package(body(CONTENTS + p('Unlocking growth', 'berschrift1') + p('We grew sales.')), TOC_STYLES)
    got = documents.extract('bericht.docx', data)
    check('toc: its lines are left out, and its title is kept',
          got['text'] == 'Contents\n\n# Unlocking growth\n\nWe grew sales.', got['text'])
    check('toc: a note says why it is missing and how to bring it up to date',
          got['notes'] == [documents.wordfile.TOC_NOTE] and documents.wordfile.TOC_NOTE == (
              'The table of contents is not shown. Word rebuilds it from the headings: in Word, '
              'right-click it, choose Update Field, then Update entire table.'), got['notes'])
    check('toc: the paragraphs counted are the ones shown', got['summary'] == 'Word document, 3 paragraphs',
          got['summary'])

    # The heading fixed: the contents still name the old one until Word updates them, as it
    # would after any edit made in Word itself.
    out, notes = documents.rewrite_docx(data, got['text'].replace('# Unlocking growth', '# Growing sales'))
    xml = document_xml(out)
    sdt = re.search(rb'<w:sdt>.*</w:sdt>', document_xml(data), re.S).group(0)
    check('toc: saving leaves the table of contents byte for byte', sdt in xml, xml[:400])
    heading = next((para for para in ET.fromstring(xml).iter(f'{{{NS}}}p')
                    if para.find(f'.//{{{NS}}}pStyle') is not None
                    and para.find(f'.//{{{NS}}}pStyle').get(f'{{{NS}}}val') == 'berschrift1'), None)
    check('toc: and writes the heading',
          heading is not None and ''.join(el.text or '' for el in heading.iter(f'{{{NS}}}t')) == 'Growing sales')
    check('toc: with nothing to report', notes == [], notes)
    check('toc: the saved file reads back with the new heading',
          documents.extract('out.docx', out)['text'] == 'Contents\n\n# Growing sales\n\nWe grew sales.')

    # A TOC field with no content control around it. The note comes after the list of what
    # stays in the file and before the one about tracked changes.
    loose = package(body(
        p(field('begin', r'TOC \o "1-2"') + field('separate') + t('Plan') + TAB + t('2'), 'Verzeichnis2')
        + p(t('Costs') + TAB + t('5') + field('end'), 'Verzeichnis2')
        + p('Plan', 'berschrift2')
        + p(t('Kept ') + '<w:del w:id="1"><w:r><w:delText>gone </w:delText></w:r></w:del>' + t('words.'))
        + p('Costs', 'berschrift2')
        + '<w:sectPr><w:headerReference w:type="default" r:id="rId9" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></w:sectPr>'),
        TOC_STYLES)
    got = documents.extract('plan.docx', loose)
    check('toc: a contents field without a content control is left out too',
          got['text'] == '## Plan\n\nKept words.\n\n## Costs', got['text'])
    check('toc: its note stands between the other two', got['notes'] == [
        'Headers are not shown here and stay in the file as they are.', documents.wordfile.TOC_NOTE,
        documents.TRACKED_NOTE], got['notes'])

    # A table of figures alone is left out too, and its note does not speak of a table of contents.
    figure_styles = TOC_STYLES.replace('</w:styles>', '<w:style w:type="paragraph" w:styleId="Abbildungen">'
                                                      '<w:name w:val="table of figures"/></w:style></w:styles>')
    figure_lines = (p(field('begin', r'TOC \h \z \c "Figure"') + field('separate') + t('Figure 1: Sales') + TAB
                      + t('3'), 'Abbildungen')
                    + p(t('Figure 2: Costs') + TAB + t('4') + field('end'), 'Abbildungen'))
    figures = package(body(figure_lines + p('We grew sales.')), figure_styles)
    got = documents.extract('figures.docx', figures)
    check('toc: a table of figures is left out, with a note of its own',
          got['text'] == 'We grew sales.' and got['notes'] == [documents.wordfile.FIGURES_NOTE], got)

    # New text typed where a table of contents is left out goes after the whole of it, as body text: not between
    # its title and its lines, where Word would keep it through every update, styled as the title.
    def placed(data, old, new):
        """(style, text, inside a content control) for each paragraph of the body saved with `old` replaced by
        `new` in the text, and whether that file reads back as the text saved."""
        text = documents.extract('in.docx', data)['text']
        out, _ = documents.rewrite_docx(data, text.replace(old, new))
        found = []

        def walk(el, boxed):
            for child in el:
                if child.tag == f'{{{NS}}}p':
                    style = child.find(f'{{{NS}}}pPr/{{{NS}}}pStyle')
                    found.append((style.get(f'{{{NS}}}val') if style is not None else '',
                                  ''.join(r.text or '' for r in child.iter(f'{{{NS}}}t')), boxed))
                else:
                    walk(child, boxed or child.tag == f'{{{NS}}}sdt')
        walk(ET.fromstring(document_xml(out)), False)
        return found, documents.extract('out.docx', out)['text'] == text.replace(old, new)

    lines = [('Verzeichnis1', 'Unlocking growth3', True), ('Verzeichnis2', 'Next steps4', True)]
    contents = [('Inhaltsverzeichnisberschrift', 'Contents', True)] + lines
    got = placed(data, 'Contents\n\n', 'Draft.\n\nContents\n\nIn brief.\n\nAnd more.\n\n')
    check('toc: text typed after its title goes after the content control, styled as body text, and text typed '
          'before the title stays before it', got == ([('', 'Draft.', False)] + contents + [
              ('', 'In brief.', False), ('', 'And more.', False), ('berschrift1', 'Unlocking growth', False),
              ('', 'We grew sales.', False)], True), got)
    middle = package(body(p('Report', 'Titel') + CONTENTS + p('Unlocking growth', 'berschrift1')
                          + p('We grew sales.')), TOC_STYLES)
    got = placed(middle, 'Contents\n\n', 'Draft.\n\nContents\n\nIn brief.\n\n')
    check('toc: the same with a title above it', got == ([('Titel', 'Report', False), ('', 'Draft.', False)]
                                                         + contents + [('', 'In brief.', False),
                                                                       ('berschrift1', 'Unlocking growth', False),
                                                                       ('', 'We grew sales.', False)], True), got)
    last = package(body(p('We grew sales.') + CONTENTS + '<w:sectPr/>'), TOC_STYLES)
    got = placed(last, 'Contents', 'Contents\n\nThe end.')
    check('toc: and with the table of contents last in the body',
          got == ([('', 'We grew sales.', False)] + contents + [('', 'The end.', False)], True), got)
    xml = document_xml(documents.rewrite_docx(last, 'We grew sales.\n\nContents\n\nThe end.')[0])
    check('toc: before the page setup', xml.find(b'</w:sdt>') < xml.find(b'The end.') < xml.find(b'<w:sectPr'), xml)

    # A field with no content control, its end in a paragraph of its own: the text goes after that paragraph,
    # outside the field, or updating the table would throw it away.
    unboxed = package(body(
        p('Contents', 'Inhaltsverzeichnisberschrift')
        + p(field('begin', r'TOC \o "1-3"') + field('separate') + t('Plan') + TAB + field('begin', 'PAGEREF _Toc1')
            + field('separate') + t('2') + field('end'), 'Verzeichnis1')
        + p(t('Costs') + TAB + t('5'), 'Verzeichnis1') + p(field('end'))
        + p('Plan', 'berschrift1') + p('We grew sales.')), TOC_STYLES)
    got = placed(unboxed, 'Contents\n\n', 'Contents\n\nIn brief.\n\n')
    check('toc: a field without a content control: after the paragraph where it ends', got == ([
        ('Inhaltsverzeichnisberschrift', 'Contents', False), ('Verzeichnis1', 'Plan2', False),
        ('Verzeichnis1', 'Costs5', False), ('', '', False), ('', 'In brief.', False),
        ('berschrift1', 'Plan', False), ('', 'We grew sales.', False)], True), got)
    # Its end in a paragraph with text of its own (text typed on the line below the table, or a table added at the
    # start of a heading): anything before that paragraph is in the field, so the text stays below the title.
    shared = package(body(
        p('Contents', 'Inhaltsverzeichnisberschrift')
        + p(field('begin', r'TOC \o "1-3"') + field('separate') + t('Plan') + TAB + t('2'), 'Verzeichnis1')
        + p(field('end') + t('Plan'), 'berschrift1') + p('We grew sales.')), TOC_STYLES)
    got = placed(shared, 'Contents\n\n', 'Contents\n\nIn brief.\n\n')
    check('toc: a field that ends in a paragraph with text: before the field, as body text', got == ([
        ('Inhaltsverzeichnisberschrift', 'Contents', False), ('', 'In brief.', False),
        ('Verzeichnis1', 'Plan2', False), ('berschrift1', 'Plan', False), ('', 'We grew sales.', False)], True), got)
    shared = package(body(CONTENTS.replace(field('end'), '').replace(
        '</w:sdtContent>', p(field('end') + t('Typed below.')) + '</w:sdtContent>')
        + p('Unlocking growth', 'berschrift1')), TOC_STYLES)
    got = placed(shared, 'Contents\n\n', 'Contents\n\nIn brief.\n\n')
    check('toc: the same in a content control', got == ([contents[0], ('', 'In brief.', True)] + lines + [
        ('', 'Typed below.', True), ('berschrift1', 'Unlocking growth', False)], True), got)

    # A title renamed with no word in common, and text typed below it: the title keeps its place and its style.
    got = placed(data, 'Contents\n\n', 'Table of contents\n\nIn brief.\n\n')
    check('toc: a renamed title stays the title, and the text below it goes after the table', got == (
        [('Inhaltsverzeichnisberschrift', 'Table of contents', True)] + lines + [
            ('', 'In brief.', False), ('berschrift1', 'Unlocking growth', False), ('', 'We grew sales.', False)],
        True), got)
    got = placed(unboxed, 'Contents\n\n', 'CONTENTS\n\nIn brief.\n\n')
    check('toc: the same without a content control', got == ([
        ('Inhaltsverzeichnisberschrift', 'CONTENTS', False), ('Verzeichnis1', 'Plan2', False),
        ('Verzeichnis1', 'Costs5', False), ('', '', False), ('', 'In brief.', False),
        ('berschrift1', 'Plan', False), ('', 'We grew sales.', False)], True), got)

    # A table of contents and a table of figures under a heading of the author's. Below the title of the first,
    # new text goes after the table; below the heading, which is not the title of a table of contents, it stays
    # where it was typed, before the lines and outside their field, as body text.
    both = (CONTENTS + p('Figures', 'berschrift1') + figure_lines + p('Unlocking growth', 'berschrift1')
            + p('We grew sales.'))
    got = placed(package(body(both), figure_styles), '# Figures\n\n', 'In brief.\n\n# Figures\n\nSee below.\n\n')
    want = contents + [
        ('', 'In brief.', False), ('berschrift1', 'Figures', False), ('', 'See below.', False),
        ('Abbildungen', 'Figure 1: Sales3', False), ('Abbildungen', 'Figure 2: Costs4', False),
        ('berschrift1', 'Unlocking growth', False), ('', 'We grew sales.', False)]
    check('toc: a table of contents and a table of figures', got == (want, True), got)
    # So with a table of contents that has no title: what is typed below the title of the document stays there.
    untitled = (p('Report', 'Titel') + p(field('begin', r'TOC \o "1-3"') + field('separate') + t('Plan') + TAB
                                         + t('2') + field('end'), 'Verzeichnis1')
                + p('Plan', 'berschrift1') + p('We grew sales.'))
    got = placed(package(body(untitled), TOC_STYLES), '# Report\n\n', '# Report\n\nIn brief.\n\n')
    alone = [('Titel', 'Report', False), ('', 'In brief.', False), ('Verzeichnis1', 'Plan2', False),
             ('berschrift1', 'Plan', False), ('', 'We grew sales.', False)]
    check('toc: one with no title of its own', got == (alone, True), got)
    # The same with the whole body in a content control, as some templates have it: that one is not the table's.
    wrap = '<w:sdt><w:sdtPr/><w:sdtContent>%s</w:sdtContent></w:sdt>'
    got = placed(package(body(wrap % both), figure_styles), '# Figures\n\n',
                 'In brief.\n\n# Figures\n\nSee below.\n\n')
    check('toc: both, in a content control around the body', got == ([(s, x, True) for s, x, _ in want], True), got)
    got = placed(package(body(wrap % untitled), TOC_STYLES), '# Report\n\n', '# Report\n\nIn brief.\n\n')
    check('toc: one with no title, in a content control around the body',
          got == ([(s, x, True) for s, x, _ in alone], True), got)

    # Not left out: a contents line in a table cell, and a style that is only based on "toc 1".
    kept = package(body(
        '<w:tbl><w:tblPr/><w:tr><w:tc>' + p(t('Unlocking growth') + TAB + t('3'), 'Verzeichnis1')
        + '</w:tc><w:tc>' + p('Page') + '</w:tc></w:tr></w:tbl>'
        + p('An entry of my own', 'Eigenverzeichnis')), TOC_STYLES)
    got = documents.extract('kept.docx', kept)
    check('toc: a line in a table cell stays in its row, and a style based on toc 1 is the author\'s',
          got['text'] == 'Unlocking growth\t3 | Page\n\nAn entry of my own', got['text'])
    check('toc: and with nothing left out there is no note', documents.wordfile.TOC_NOTE not in got['notes'],
          got['notes'])
    # A line left out still counts in the list it belongs to, so the numbers shown are Word's.
    numbered = package(body(p('First', num=2) + p(t('Second') + TAB + t('7'), 'Verzeichnis1', num=2)
                            + p('Third', num=2)), TOC_STYLES, NUMBERING)
    got = documents.extract('list.docx', numbered)['text']
    check('toc: a list goes on counting past a line that is left out', got == '1. First\n\n3. Third', got)


@section
def kinds_and_text_files():
    word = documents.write_docx('A Word file.')
    for name in ('a.docx', 'a.pdf', 'a.txt', 'a', 'a.DOCX'):
        got = documents.extract(name, word)
        check(f'kind: Word bytes named {name} are read as Word',
              (got['kind'], got['text']) == ('docx', 'A Word file.'), got)
    for name in ('a.txt', 'a.md', 'A.TXT', 'notes.markdown'):
        got = documents.extract(name, b'# Title\r\n\r\nSome *text*.\r\n')
        check(f'kind: {name} is text, as written',
              (got['kind'], got['text'], got['notes']) == ('text', '# Title\n\nSome *text*.', []), got)
    check('text: summary counts words', got['summary'] == 'Text file, 4 words', got['summary'])
    check('text: a text file that begins "PK" is still text',
          documents.extract('a.txt', b'PK is short for Phil Katz.')['kind'] == 'text')

    accents = 'Café — naïve, 你好'
    check('text: UTF-8 with a BOM',
          documents.extract('a.txt', b'\xef\xbb\xbf' + accents.encode('utf-8'))['text'] == accents)
    check('text: UTF-16 from Notepad', documents.extract('a.txt', accents.encode('utf-16'))['text'] == accents)
    old = documents.extract('a.txt', 'Café — naïve'.encode('cp1252'))
    check('text: not UTF-8 falls back to Windows-1252', old['text'] == 'Café — naïve', old['text'])
    check('text: and says so', len(old['notes']) == 1 and 'Windows-1252' in old['notes'][0], old['notes'])
    check('text: control characters are dropped',
          documents.extract('a.txt', b'One\x00 two\x0c.\n')['text'] == 'One two.')


@section
def refusals():
    for name, data, want in (
            ('old.doc', OLE, 'Save it as .docx in Word first.'),
            ('renamed.docx', OLE, 'Save it as .docx in Word first.'),
            ('named-only.doc', b'no magic here', 'Save it as .docx in Word first.'),
            ('letter.rtf', b'{\\rtf1\\ansi Hello}', 'save it as .docx'),
            ('rtf-inside.docx', b'{\\rtf1\\ansi Hello}', 'save it as .docx'),
            ('letter.odt', b'whatever', 'Save it as .docx or PDF'),
            ('sheet.xls', OLE, 'Only Word (.docx), PDF, .txt and .md'),
            ('photo.jpg', b'\xff\xd8\xff\xe0', 'Only Word (.docx), PDF, .txt and .md'),
            ('no-extension', b'plain words', 'Only Word (.docx), PDF, .txt and .md'),
            ('', b'plain words', 'Only Word (.docx), PDF, .txt and .md')):
        try:
            documents.extract(name, data)
            got = ('no error', '')
        except documents.DocumentError as e:
            got = (e.code, e.message)
        check(f'unsupported: {name!r} says what to do', got[0] == 'unsupported' and want in got[1], got)
    # A real OpenDocument or Excel file is a zip, which is what a .docx is too.
    other_zip = package(extra=[('content.xml', '<a/>')])
    check('unsupported: an .odt that is a zip', code('a.odt', other_zip) == 'unsupported', code('a.odt', other_zip))
    check('unsupported: an .xlsx that is a zip', code('a.xlsx', other_zip) == 'unsupported')
    check('encrypted: a Word file with a password',
          code('locked.docx', OLE + 'EncryptedPackage'.encode('utf-16-le')) == 'encrypted')

    # As small as a DTD gets, and well-formed: a parser left to itself reads this as "hello".
    # The dangerous ones differ only in what the entity holds.
    dtd = ('<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY greeting "hello">]>'
           f'<w:document {W}><w:body><w:p><w:r><w:t>&greeting;</w:t></w:r></w:p></w:body></w:document>')
    stored = io.BytesIO()
    with zipfile.ZipFile(stored, 'w') as z:       # stored, not packed, so one letter can be changed
        z.writestr('word/document.xml', body(p('Some text.')))
    check('a stored Word file reads', code('a.docx', stored.getvalue()) == 'no error')
    for name, data in (
            ('a zip cut short', documents.write_docx('Some text.')[:300]),
            ('a zip with a bad checksum', stored.getvalue().replace(b'Some text.', b'Sume text.')),
            ('bytes that are no zip at all', b'not a zip'),
            ('an empty zip', package()),
            ('a zip without word/document.xml', package(extra=[('word/other.xml', '<a/>')])),
            ('XML that is not well-formed', package(body('<w:p>'))),
            ('a DOCTYPE', package(dtd)),
            ('a DOCTYPE in UTF-16', package(dtd.replace('?>', ' encoding="UTF-16"?>', 1).encode('utf-16'))),
            ('a DOCTYPE in the styles', package(body(p('Fine.')), styles=dtd)),
            ('a DOCTYPE in the numbering', package(body(p('Fine.')), numbering=dtd)),
            ('an entity declaration', package(body(p('x')).replace('?>', '?><!ENTITY x "y">', 1))),
            ('a part that is not UTF-8', package(body(p('x')).encode('utf-8') + b'\xff')),
            ('tables nested 100,000 deep', package(body('<w:tbl><w:tr><w:tc>' * 100_000 + p('deep')
                                                        + '</w:tc></w:tr></w:tbl>' * 100_000)))):
        began = time.time()
        got = code('a.docx', data)
        check(f'unreadable: {name}', got == 'unreadable' and time.time() - began < 5,
              f'{got} in {time.time() - began:.1f}s')

    deleted = p('<w:del><w:r><w:delText>gone</w:delText></w:r></w:del>')
    for name, inner in (('an empty body', ''), ('only a deletion', deleted),
                        ('paragraphs with nothing in them', p('   ') + p('<w:r><w:br/></w:r>'))):
        got = code('a.docx', package(body(inner)))
        check(f'no_text: a Word file with {name}', got == 'no_text', got)
    for data in (b'', b'  \r\n\t\n', b'\xef\xbb\xbf'):
        check(f'no_text: a text file holding {data!r}', code('a.txt', data) == 'no_text', code('a.txt', data))

    limit = documents.MAX_PART
    try:
        documents.MAX_PART = 1000
        check('too_big: a part over the limit', code('a.docx', package(body(p('word ' * 400)))) == 'too_big')
        check('too_big: a part within the limit is read', code('a.docx', package(body(p('Short.')))) == 'no error')
    finally:
        documents.MAX_PART = limit

    e = documents.DocumentError('unsupported', 'Say what to do.')
    check('DocumentError carries a code and a message',
          (e.code, e.message, str(e)) == ('unsupported', 'Say what to do.', 'Say what to do.'))


# ── paragraphs out of lines ─────────────────────────────────────────────────
def flowed(*pages):
    return documents.reflow([list(page) for page in pages])[0]


@section
def reflow_lines():
    check('reflow: the lines of a paragraph are joined, and a gap parts two', flowed([
        'The council approved the new cycle route on Tuesday after a debate that ran for',
        'almost three hours.',
        '',
        'Work is expected to begin in March.']) == (
        'The council approved the new cycle route on Tuesday after a debate that ran for almost '
        'three hours.\n\nWork is expected to begin in March.'))
    check('reflow: with no gap, a short line that ends a sentence ends the paragraph', flowed([
        'The council approved the new cycle route on Tuesday after a debate that ran for',
        'almost three hours.',
        'Work is expected to begin in March, and the first phase covers the stretch from',
        'Mill Lane to the river bridge.']).count('\n\n') == 1)
    check('reflow: a full line that ends a sentence does not', '\n' not in flowed([
        'Supporters said that the cost was modest compared with the resurfacing budget.',
        'Opponents argued that the consultation had been far too short to mean anything',
        'at all.']))
    check('reflow: nor does a short line in the middle of a sentence', '\n' not in flowed([
        'Supporters said that the cost was modest compared with the resurfacing budget and',
        'that the unpronounceably-long-and-unbreakable-compound-word-here-fills-the-next-line',
        'was not a problem.']))
    check('reflow: a line that stops far short ends its paragraph, full stop or not', flowed([
        'What happens next',
        'Residents can see the plans at the library until the end of the month, and comments',
        'sent before the deadline will be summarised in a report to the planning committee.',
        ]).startswith('What happens next\n\nResidents can'))
    check('reflow: a first line set in from the rest starts a paragraph', flowed([
        '   The council approved the new cycle route on Tuesday after a debate that ran',
        'for almost three hours, and nobody in the room was surprised by the vote which',
        '   followed, although several people said afterwards that they had hoped for',
        'a longer consultation']).count('\n\n') == 1)

    text, dropped, rejoined = documents.reflow([[
        'The committee will publish its recom-', 'mendations in the spring, when the consul\xad',
        'tation closes. It was an Anglo-', 'American plan for the years 2025-',
        '26, and by all accounts a well -', 'meant one, the minutes say.']])
    check('reflow: a word split across two lines is joined, a real hyphen kept', text == (
        'The committee will publish its recommendations in the spring, when the consultation '
        'closes. It was an Anglo-American plan for the years 2025-26, and by all accounts a '
        'well - meant one, the minutes say.'), text)
    check('reflow: and the joins are counted', (dropped, rejoined) == (0, 2), (dropped, rejoined))

    book = [['Acme Ltd | Annual report', '', words, '', f'Page {n} of 3'] for n, words in enumerate((
        'The first page stops in the middle of a', 'sentence, which the second page carries on to the',
        'third, where it ends.'), 1)]
    text, dropped, _ = documents.reflow(book)
    check('reflow: running headers and page numbers go, and the text joins up across pages',
          text == 'The first page stops in the middle of a sentence, which the second page carries '
                  'on to the third, where it ends.', text)
    check('reflow: what was dropped is counted', dropped == 6, dropped)
    # Left and right pages with headers of their own: each is at the edge of half the pages.
    sides = [[('Acme Ltd Annual report' if n % 2 else 'Chapter 2 Results'), '',
              f'The {w} survey ran for six weeks.', '', str(n)]
             for n, w in enumerate(('alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot'), 1)]
    text = documents.reflow(sides)[0]
    check('reflow: headers that alternate between left and right pages go too',
          'Acme' not in text and 'Chapter' not in text and text.count('survey') == 6, text)
    for number in ('7', '- 7 -', 'Page 7', 'page 7 of 12', '7 / 12', '[7]'):
        check(f'reflow: a bare {number!r} at the foot of a page is a page number',
              flowed(['Some text here.', '', number]) == 'Some text here.', flowed(['Some text here.', '', number]))
    middle = flowed(['One.', '', 'Two.', '', 'Three.', '', '42', '', 'Five.', '', 'Six.', '', 'Seven.', '', '9'])
    check('reflow: a number in the middle of the page is not', middle
          == 'One.\n\nTwo.\n\nThree.\n\n42\n\nFive.\n\nSix.\n\nSeven.', middle)
    ending = flowed(['The last paragraph on this page is long enough to wrap around twice before it',
                     'reaches the year that it has been leading up to all along, and that year is', '1999'])
    check('reflow: nor is one that ends a paragraph', ending.endswith('that year is 1999'), ending)
    check('reflow: a header seen on one page only stays',
          flowed(['Chapter one', '', 'Text.'], ['', 'More text.']).startswith('Chapter one\n\n'))
    check('reflow: the last lines of a page are not a footer because they match another page\'s',
          flowed(*[['The last paragraph of this page is', 'the same as the last paragraph of',
                    'the page after it.']] * 2).count('the page after it.') == 2)
    check('reflow: a paragraph carries on over a page turn', flowed(
        ['It was a long debate that ran for'], ['almost three hours.'])
        == 'It was a long debate that ran for almost three hours.')
    check('reflow: a sentence that ends the page ends the paragraph', flowed(
        ['It ended there.'], ['A new paragraph starts the next page.']).count('\n\n') == 1)
    check('reflow: and over a column turn', flowed(
        ['The left column stops in the middle of a', documents._TURN, 'sentence, and the right one takes it up.'])
        == 'The left column stops in the middle of a sentence, and the right one takes it up.')

    listed = flowed([
        'Check each of these before you leave the house, because most problems start here.',
        '• Your passport must be valid for at least six months after the date you plan',
        '   to return.',
        '· Print the boarding pass.',
        chr(0xf0b7) + ' Pack the charger.',
        '- Lock the door.',
        'Boarding closes twenty minutes before departure, whatever the screens say then.'])
    check('reflow: bullets become "- " items, each with its wrapped lines', listed.split('\n\n') == [
        'Check each of these before you leave the house, because most problems start here.',
        '- Your passport must be valid for at least six months after the date you plan to return.',
        '- Print the boarding pass.', '- Pack the charger.', '- Lock the door.',
        'Boarding closes twenty minutes before departure, whatever the screens say then.'], listed)
    check('reflow: a bullet with its text on the next line', flowed(['•', 'First item.', '•', 'Second item.'])
          == '- First item.\n\n- Second item.', flowed(['•', 'First item.', '•', 'Second item.']))
    check('reflow: a dash inside a sentence is not a bullet', flowed([
        'The plan was simple and it had worked well for everyone involved in the scheme',
        '- or so they said at the time.']).count('\n') == 0)
    numbered = flowed([
        'Follow these steps in order.',
        '1. Drop your bags at the desk.',
        '2) Go through security, which can take up to forty minutes on a weekday morning',
        'in the summer.',
        '3. Find your gate.',
        'After all of that, the bill for the three of them together came to no less than',
        '12. That was more than anyone expected.'])
    check('reflow: numbered items are kept, a wrapped number is not one', numbered.split('\n\n') == [
        'Follow these steps in order.', '1. Drop your bags at the desk.',
        '2. Go through security, which can take up to forty minutes on a weekday morning in the summer.',
        '3. Find your gate.',
        'After all of that, the bill for the three of them together came to no less than 12. That '
        'was more than anyone expected.'], numbered)
    check('reflow: a numbered list inside another, and the outer one carrying on after it', flowed([
        '1. One', '2. Two', '1. Inner one', '2. Inner two', '3. Three']).split('\n\n')
        == ['1. One', '2. Two', '1. Inner one', '2. Inner two', '3. Three'])
    check('reflow: after a gap a list may start at any number', flowed([
        'A list can pick up where an earlier one stopped.', '', '5. Five', '6. Six']).split('\n\n')
        == ['A list can pick up where an earlier one stopped.', '5. Five', '6. Six'])
    check('reflow: a heading stands alone, and one of two lines is one heading', flowed([
        '# A long heading that runs', '# over two lines', 'Body text follows it.', '## Next', 'More.'])
        == '# A long heading that runs over two lines\n\nBody text follows it.\n\n## Next\n\nMore.')
    check('reflow: nothing in, nothing out', documents.reflow([]) == ('', 0, 0) and flowed([], ['', '']) == '')


# ── PDFs, written out by hand ───────────────────────────────────────────────
# One page, two lines of 12-point Helvetica. The numbers in the table at the end are the
# byte offsets of the five objects, which is why this one is spelled out rather than built.
TINY_PDF = (
    b'%PDF-1.4\n'
    b'1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n'
    b'2 0 obj\n<< /Type /Pages /Kids [4 0 R] /Count 1 >>\nendobj\n'
    b'3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\nendobj\n'
    b'4 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
    b'/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>\nendobj\n'
    b'5 0 obj\n<< /Length 113 >>\nstream\n'
    b'BT /F1 12 Tf 1 0 0 1 72 720 Tm (Hello from a PDF.) Tj ET\n'
    b'BT /F1 12 Tf 1 0 0 1 72 700 Tm (It has two lines.) Tj ET\n'
    b'endstream\nendobj\n'
    b'xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n'
    b'0000000212 00000 n \n0000000338 00000 n \n'
    b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n502\n%%EOF\n')


def pdf(*pages):
    """A PDF with these pages. Each page is a list of (x, y, type size, text)."""
    kids = ' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))
    objects = ['<< /Type /Catalog /Pages 2 0 R >>',
               f'<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>',
               '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>']
    for i, lines in enumerate(pages):
        stream = '\n'.join(f'BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET'
                           for x, y, size, text in lines)
        objects.append('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                       f'/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>')
        objects.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
    out, offsets = b'%PDF-1.4\n', []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f'{n} 0 obj\n{obj}\nendobj\n'.encode('latin-1')
    table = ''.join(f'{at:010d} 00000 n \n' for at in offsets)
    return out + (f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n{table}trailer\n'
                  f'<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{len(out)}\n%%EOF\n').encode()


def page(number, of, lines):
    """A page with a running header and a page number. Lines are 14 points apart:
    '' leaves a gap, "!" opens a heading in larger type, leading spaces are an indent."""
    out, y = [(72, 760, 9, 'Acme Ltd - Draft report'), (290, 30, 9, f'Page {number} of {of}')], 700
    for line in lines:
        if line:
            text = line.lstrip(' !')
            out.append((72 + 6 * (len(line) - len(line.lstrip(' '))), y, 18 if line[0] == '!' else 11, text))
        y -= 14 if line else 10
    return out


REPORT = pdf(
    page(1, 2, ['!Background', '',
                'The council approved the new cycle route on Tuesday after a debate that ran for',
                'almost three hours. Supporters said the cost was modest.', '',
                'Work is expected to begin in March. The first phase covers the stretch from Mill',
                'Lane to the river bridge, where the pavement will be wid-']),
    page(2, 2, ['ened and two parking bays removed.', '',
                '\\225 Parking near the shops must not get harder, especially for people who',
                '   cannot walk far.',
                '\\225 Nobody wants works during the school term.', '',
                'The \\223first\\224 phase costs \\243120,000 \\226 a caf\\351 owner called it fair.']))
REPORT_TEXT = '\n\n'.join((
    '# Background',
    'The council approved the new cycle route on Tuesday after a debate that ran for almost three '
    'hours. Supporters said the cost was modest.',
    'Work is expected to begin in March. The first phase covers the stretch from Mill Lane to the '
    'river bridge, where the pavement will be widened and two parking bays removed.',
    '- Parking near the shops must not get harder, especially for people who cannot walk far.',
    '- Nobody wants works during the school term.',
    'The “first” phase costs £120,000 – a café owner called it fair.'))


@section
def pdf_files():
    # Asking for a PDF without pypdf is an error with an instruction in it, not a crash.
    # None in sys.modules is how Python is told a module cannot be imported.
    absent = object()
    real = sys.modules.get('pypdf', absent)
    sys.modules['pypdf'] = None
    try:
        documents.extract('a.pdf', TINY_PDF)
        got = ('no error', '')
    except documents.DocumentError as e:
        got = (e.code, e.message)
    finally:
        if real is absent:
            del sys.modules['pypdf']
        else:
            sys.modules['pypdf'] = real
    check('pdf: without pypdf the message says how to get it', got == (
        'needs_pypdf', 'Reading PDFs needs the pypdf package. Install it with: python -m pip install pypdf'), got)
    if not HAVE_PYPDF:
        print('pypdf is not installed: the checks that read a PDF were skipped')
        return

    got = documents.extract('hello.pdf', TINY_PDF)
    check('pdf: the text of a hand-written PDF', got['text'] == 'Hello from a PDF. It has two lines.', got['text'])
    check('pdf: kind, summary, notes', (got['kind'], got['summary'], got['notes']) == ('pdf', 'PDF, 1 page', []), got)
    for name in ('a.docx', 'a.txt', 'a', 'A.PDF'):
        check(f'kind: PDF bytes named {name} are read as a PDF', documents.extract(name, TINY_PDF)['kind'] == 'pdf')
    check('kind: a .pdf name with other bytes is tried as a PDF', code('a.pdf', b'not a pdf at all') == 'unreadable')
    check('unreadable: a PDF cut short', code('a.pdf', TINY_PDF[:200]) == 'unreadable', code('a.pdf', TINY_PDF[:200]))

    got = documents.extract('report.pdf', REPORT)
    check('pdf: headings, paragraphs, a word split over two pages, bullets', got['text'] == REPORT_TEXT,
          next((f'got {a!r}, want {b!r}' for a, b in zip(got['text'].split('\n'), REPORT_TEXT.split('\n'))
                if a != b), f'{len(got["text"])} characters, want {len(REPORT_TEXT)}'))
    check('pdf: the notes say what was dropped and what was joined', got['summary'] == 'PDF, 2 pages'
          and got['notes'] == ['Page numbers and repeated page headers or footers were left out.',
                               'Words split by a hyphen at the end of a line were joined up. '
                               'Check any that should keep the hyphen.'], got)
    indented = documents.extract('book.pdf', pdf(page(1, 1, [
        '   The council approved the new cycle route on Tuesday after a debate that',
        'ran for almost three hours and that nobody in the room had expected to last so',
        '   Work is expected to begin in March and the first phase covers the stretch',
        'from Mill Lane to the river bridge'])))['text']
    check('pdf: paragraphs marked only by an indent, under a header that stays when seen once',
          [part[:16] for part in indented.split('\n\n')]
          == ['Acme Ltd - Draft', 'The council appr', 'Work is expected'], indented)

    # A note's number, small and raised after a word, is left out: "sites1" hid the claim
    # "14 sites" from the checker. A raised "st" and a lowered "2" are words, and stay.
    for label, lines, want in (
            ('a footnote number is left out', [(72, 700, 11, 'We counted 14 sites'), (170, 704, 6.4, '1'),
                                               (174, 700, 11, ' in the network.')],
             'We counted 14 sites in the network.'),
            ('a raised ordinal stays', [(72, 700, 11, 'It opens on the 1'), (152, 704, 6.4, 'st'),
                                        (157, 700, 11, ' of May.')], 'It opens on the 1st of May.'),
            ('a lowered number stays', [(72, 700, 11, 'Cut the CO'), (124, 697, 6.4, '2'),
                                        (128, 700, 11, ' we emit.')], 'Cut the CO2 we emit.')):
        got = documents.extract('n.pdf', pdf(lines))['text']
        check(f'pdf: {label}', got == want, got)
    # A unit or a power, raised as one, stays: "400 m2" and "106" would misread m² and 10⁶.
    for label, lines, want in (
            ('square metres', [(72, 700, 11, 'The hall covers 400 m'), (180, 704, 6.4, '2'),
                               (184, 700, 11, ' of floor.')], 'The hall covers 400 m² of floor.'),
            ('a power of ten', [(72, 700, 11, 'A budget of 10'), (140, 704, 6.4, '6'),
                                (146, 700, 11, ' euros a year.')], 'A budget of 10⁶ euros a year.'),
            ('a negative power', [(72, 700, 11, 'About 4.2 x 10'), (140, 704, 6.4, '-'), (143, 704, 6.4, '3'),
                                  (148, 700, 11, ' of the total.')], 'About 4.2 x 10⁻³ of the total.'),
            ('x squared', [(72, 700, 11, 'The area is x'), (135, 704, 6.4, '2'), (140, 700, 11, ' plus y.')],
             'The area is x² plus y.'),
            ('a note after a full stop is still left out', [(72, 700, 11, 'It was fixed.'), (130, 704, 6.4, '2'),
                                                            (134, 700, 11, ' Then more.')],
             'It was fixed. Then more.')):
        got = documents.extract('n.pdf', pdf(lines))['text']
        check(f'pdf: {label}', got == want, got)
    # A table of contents is left out, as it is from a Word file. Its title stays.
    got = documents.extract('toc.pdf', pdf([
        (72, 700, 11, 'Contents'), (72, 686, 11, 'Background . . . . . . . . 3'),
        (72, 672, 11, 'Costs ...................................... 12'),
        (72, 640, 11, 'The body text starts here. And then.... 5 people came.')]))
    check('pdf: the lines of a table of contents are left out, and a note says so',
          got['text'] == 'Contents\n\nThe body text starts here. And then.... 5 people came.'
          and got['notes'] == ['The table of contents was left out.'], got)
    # With no title: three lines or more on the first pages, before any paragraph, numbers going up.
    # It carries on over a page turn.
    got = documents.extract('toc.pdf', pdf([
        (72, 700, 11, 'Annual report 2025'), (72, 672, 11, 'Background ............ 3'),
        (72, 658, 11, 'Costs ............ 5'), (72, 644, 11, 'Staff ............ 9')],
        [(72, 700, 11, 'Plans ............ 12'), (72, 672, 11, 'The body text starts here, on the second page.')]))
    check('pdf: a table of contents with no title, over a page turn',
          got['text'] == 'Annual report 2025\n\nThe body text starts here, on the second page.'
          and got['notes'] == ['The table of contents was left out.'], got)
    # A fee or price list set with dot leaders is text, wherever it is: each row a line of its own.
    got = documents.extract('fees.pdf', pdf([
        (72, 720, 11, 'Harbour charges for 2026'),
        (72, 690, 11, 'Visiting boats pay the charges below on arrival at the harbour office.'),
        (72, 670, 11, 'Overnight stay, under 8 m .......... 18'),
        (72, 656, 11, 'Overnight stay, 8 m and over .......... 26'),
        (72, 642, 11, 'Weekly stay, any length .......... 130'),
        (72, 628, 11, 'Electricity, per night .......... 4'),
        (72, 600, 11, 'Charges are in pounds and include VAT.')]))
    check('pdf: a fee list in the body stays, row by row', got['text'] == (
        'Harbour charges for 2026\n\nVisiting boats pay the charges below on arrival at the harbour office.\n\n'
        'Overnight stay, under 8 m .......... 18\n\nOvernight stay, 8 m and over .......... 26\n\n'
        'Weekly stay, any length .......... 130\n\nElectricity, per night .......... 4\n\n'
        'Charges are in pounds and include VAT.') and got['notes'] == [], got)
    got = documents.extract('menu.pdf', pdf([(72, 700, 11, 'Coffee .......... 3'), (72, 686, 11, 'Tea .......... 2')]))
    check('pdf: a price list alone is text, not a table of contents',
          got['text'] == 'Coffee .......... 3\n\nTea .......... 2' and got['notes'] == [], got)

    words = [(72, 700, 11, 'Only this page has words on it.')]
    got = documents.extract('scan.pdf', pdf(words, [], []))
    check('pdf: pages with no text are counted', got['notes'] == ['2 of 3 pages had no text and may be scans.']
          and got['text'] == 'Only this page has words on it.', got)
    try:
        documents.extract('scan.pdf', pdf([], []))
        got = ('no error', '')
    except documents.DocumentError as e:
        got = (e.code, e.message)
    check('no_text: a PDF with no text says it is probably a scan', got[0] == 'no_text' and 'scan' in got[1], got)

    def locked(password):
        """TINY_PDF encrypted by pypdf itself, with a cipher it needs no other package for."""
        writer, out = pypdf.PdfWriter(clone_from=io.BytesIO(TINY_PDF)), io.BytesIO()
        writer.encrypt(password, 'the owner password', algorithm='RC4-128')
        writer.write(out)
        return out.getvalue()

    check('pdf: one locked only against editing opens with no password',
          documents.extract('a.pdf', locked(''))['text'] == 'Hello from a PDF. It has two lines.')
    check('encrypted: a PDF that needs a password to open', code('a.pdf', locked('secret')) == 'encrypted',
          code('a.pdf', locked('secret')))

    # An AES-encrypted PDF on a machine without the package pypdf needs for AES. Seen for
    # real with that package hidden; here pypdf's own error is raised in its place.
    def needs_aes(stream):
        raise pypdf.errors.DependencyError('cryptography>=3.1 is required for AES algorithm')

    reader = pypdf.PdfReader
    pypdf.PdfReader = needs_aes
    try:
        documents.extract('a.pdf', TINY_PDF)
        got = ('no error', '')
    except documents.DocumentError as e:
        got = (e.code, e.message)
    finally:
        pypdf.PdfReader = reader
    check('encrypted: a cipher pypdf cannot do alone says which package to add',
          got[0] == 'encrypted' and 'pip install cryptography' in got[1], got)

    # The same for real: a PDF locked against editing only, with AES-128, which pypdf opens
    # alone and first needs the package for when it reads the text. Written with the package,
    # read in a process that cannot import it.
    try:
        writer, aes = pypdf.PdfWriter(clone_from=io.BytesIO(TINY_PDF)), io.BytesIO()
        writer.encrypt('', 'the owner password', algorithm='AES-128')
        writer.write(aes)
    except pypdf.errors.DependencyError:
        aes = None
    if aes:
        blocked = ('import sys, documents\n'
                   'class Blocked:\n'
                   '    def find_spec(self, name, path=None, target=None):\n'
                   '        if name.split(".")[0] in ("cryptography", "Crypto", "Cryptodome"):\n'
                   '            raise ImportError(name)\n'
                   'sys.meta_path.insert(0, Blocked())\n'
                   'sys.exit(documents.main(["documents.py", "extract", "a.pdf"]))\n')
        done = subprocess.run([sys.executable, '-c', blocked], input=aes.getvalue(), capture_output=True,
                              timeout=120, cwd=os.path.dirname(os.path.abspath(__file__)))
        try:
            got = json.loads(done.stdout.decode('utf-8'))
        except ValueError:
            got = None
        check('encrypted: AES-128 locked against editing only, without the package, says which to add',
              isinstance(got, dict) and got.get('code') == 'encrypted'
              and 'pip install cryptography' in got.get('message', ''), done.stdout[:300])

    def form(data, **fields):
        """The PDF `data` with a form on its first page: a field for each name, typed into when it has text,
        and a ticked checkbox."""
        from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject
        writer, out = pypdf.PdfWriter(clone_from=io.BytesIO(data)), io.BytesIO()
        refs = []
        for name, (kind, value) in dict(fields, ticked=('/Btn', '/Yes')).items():
            field = DictionaryObject({NameObject('/Type'): NameObject('/Annot'),
                                      NameObject('/Subtype'): NameObject('/Widget'),
                                      NameObject('/FT'): NameObject(kind), NameObject('/T'): TextStringObject(name),
                                      NameObject('/Rect'): ArrayObject([NumberObject(n) for n in (72, 72, 300, 90)])})
            if value:
                field[NameObject('/V')] = NameObject(value) if kind == '/Btn' else TextStringObject(value)
            refs.append(writer._add_object(field))
        writer.pages[0][NameObject('/Annots')] = ArrayObject(refs)
        writer._root_object[NameObject('/AcroForm')] = DictionaryObject({NameObject('/Fields'): ArrayObject(refs)})
        writer.write(out)
        return out.getvalue()

    answer = 'The vessel is a 9.5 metre wooden sloop, moored in the harbour since 2019.'
    got = documents.extract('form.pdf', form(TINY_PDF, name=('/Tx', 'Jane Harbour'), about=('/Tx', answer)))
    check('pdf: a form with text typed into it says that text is not shown',
          got['text'] == 'Hello from a PDF. It has two lines.' and got['notes'] == [documents.FORM_NOTE], got)
    got = documents.extract('form.pdf', form(TINY_PDF, name=('/Tx', ''), about=('/Tx', '  ')))
    check('pdf: a form with nothing typed into it has no note', got['notes'] == [], got)
    try:
        documents.extract('form.pdf', form(pdf([]), about=('/Tx', answer)))
        got = ('no error', '')
    except documents.DocumentError as e:
        got = (e.code, e.message)
    check('no_text: a PDF whose only text is in its form fields says so, not that it is a scan',
          got == ('no_text', documents.FORM_ONLY), got)

    limit, lines = documents.MAX_PAGES, documents._pdf_lines
    try:
        documents.MAX_PAGES = 2
        check('too_big: a PDF over the page limit', code('a.pdf', pdf(words, words, words)) == 'too_big')
        check('too_big: a PDF at the limit is read',
              code('a.pdf', pdf(words, [(72, 700, 11, 'And so does this one.')])) == 'no error')
        seen = []

        def breaks_on_the_second(pdf_page):
            seen.append(pdf_page)
            if len(seen) == 2:
                raise ValueError('a damaged page')
            return lines(pdf_page)

        documents._pdf_lines = breaks_on_the_second
        got = documents.extract('a.pdf', pdf(words, words))
        check('pdf: a page that cannot be read counts as a page with no text',
              got['text'] == 'Only this page has words on it.'
              and got['notes'] == ['1 of 2 pages had no text and may be a scan.'], got)
    finally:
        documents.MAX_PAGES, documents._pdf_lines = limit, lines


# ── the command line the server uses ────────────────────────────────────────
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documents.py')


def run_cli(args, data, **env):
    """(exit code, stdout bytes, stderr bytes) of documents.py run as its own process."""
    done = subprocess.run([sys.executable] + args, input=data, capture_output=True, timeout=120,
                          env=dict(os.environ, **env))
    return done.returncode, done.stdout, done.stderr


@section
def command_line():
    # cp437 stands in for the console of a parent that is not UTF-8.
    status, out, err = run_cli([SCRIPT, 'extract', 'café.docx'], documents.write_docx('# Título\n\nCafé — 你好.'),
                               PYTHONIOENCODING='cp437')
    try:
        got = json.loads(out.decode('utf-8'))
    except ValueError:
        got = None
    check('cli: success is one JSON object on stdout and exit 0', status == 0 and isinstance(got, dict)
          and out.lstrip()[:1] == b'{' and not err, f'{status} {out[:200]!r} {err[:200]!r}')
    check('cli: the object', got == {'ok': True, 'text': '# Título\n\nCafé — 你好.', 'kind': 'docx',
                                     'summary': 'Word document, 2 paragraphs', 'notes': []}, got)
    check('cli: stdout is ASCII, whatever the console encoding', out.isascii(), out[:200])

    status, out, err = run_cli([SCRIPT, 'extract', 'old.doc'], OLE)
    try:
        got = json.loads(out.decode('utf-8'))
    except ValueError:
        got = None
    check('cli: a refusal is one JSON object too, and still exit 0', status == 0 and got == {
        'ok': False, 'code': 'unsupported',
        'message': 'This is an old-format Word file (.doc). Save it as .docx in Word first.'} and not err,
          f'{status} {out[:300]!r} {err[:200]!r}')

    # Something nobody planned for: extract() itself blows up, and prints on its way.
    crash = ('import sys, documents\n'
             'def boom(name, data):\n'
             '    print("a library printing to stdout")\n'
             '    raise RuntimeError("unplanned")\n'
             'documents.extract = boom\n'
             'sys.exit(documents.main(["documents.py", "extract", "a.txt"]))\n')
    status, out, err = run_cli(['-c', crash], b'text', PYTHONPATH=os.path.dirname(SCRIPT))
    try:
        got = json.loads(out.decode('utf-8'))
    except ValueError:
        got = None
    check('cli: an unexpected error is "unreadable" as JSON, with no traceback on stdout',
          status == 0 and isinstance(got, dict) and (got.get('ok'), got.get('code')) == (False, 'unreadable')
          and b'Traceback' not in out and b'a library printing' not in out, f'{status} {out[:300]!r}')
    status, out, err = run_cli([SCRIPT], b'')
    check('cli: wrong arguments print usage to stderr, nothing to stdout',
          status == 2 and not out and b'usage' in err, f'{status} {out!r} {err!r}')


# ── files meant to hurt ─────────────────────────────────────────────────────
def with_claimed_size(packed, size):
    """The zip, with both of its headers for the first file altered to claim this unpacked size."""
    out = bytearray(packed)
    local, central = out.find(b'PK\x03\x04'), out.find(b'PK\x01\x02')
    out[local + 22:local + 26] = out[central + 24:central + 28] = struct.pack('<I', size)
    return bytes(out)


@section
def hostile_files():
    flood = b'a' * 5_000_000
    for name, want in (('flood.docx', 'unreadable'), ('flood.txt', 'no error'),
                       ('flood.pdf', 'unreadable' if HAVE_PYPDF else 'needs_pypdf')):
        took, got = timed(name, flood)
        check(f'hostile: 5 MB of one character as {name}', got == want and took < 5, f'{got} in {took:.1f}s')
    took, got = timed('flood.pdf', b'%PDF-1.7\n' + flood)
    check('hostile: the same behind a PDF header', got != 'no error' and took < 5, f'{got} in {took:.1f}s')
    took, got = timed('flood.docx', b'PK\x03\x04' + flood)
    check('hostile: the same behind a zip header', got == 'unreadable' and took < 5, f'{got} in {took:.1f}s')

    # 45 MB that pack down to 44 KB. Read to the limit and no further.
    bomb = package(b'<a>' + b' ' * 45_000_000 + b'</a>')
    check('hostile: the bomb is small on disk', len(bomb) < 100_000, len(bomb))
    took, got = timed('bomb.docx', bomb)
    check('hostile: a part that unpacks past 40 MB is too big', got == 'too_big' and took < 5,
          f'{got} in {took:.1f}s')
    took, got = timed('bomb.docx', package(body(p('Fine.')), styles=b'<a>' + b' ' * 45_000_000 + b'</a>'))
    check('hostile: in the styles it is refused or passed over, quickly', took < 5, f'{got} in {took:.1f}s')
    # Too big is also what reading all 45 MB and then measuring would say. Watch the reads.
    asked, read = [], zipfile.ZipExtFile.read

    def watched(self, n=-1):
        asked.append(n)
        return read(self, n)

    zipfile.ZipExtFile.read = watched
    try:
        code('bomb.docx', bomb)
        documents.extract('fine.docx', package(body(p('Fine.')), STYLES, NUMBERING))
    finally:
        zipfile.ZipExtFile.read = read
    check('hostile: no read is ever without a bound',
          asked and all(0 < n <= documents.MAX_PART + 1 for n in asked), sorted(set(asked)))
    # The same bomb, now claiming to be 1,000 bytes. Nothing may trust that and read it all.
    took, got = timed('bomb.docx', with_claimed_size(bomb, 1000))
    check('hostile: a large part whose header claims a small size', got == 'unreadable' and took < 5,
          f'{got} in {took:.1f}s')
    honest = package(body(p('word ' * 2000)))
    took, got = timed('lie.docx', with_claimed_size(honest, 50))
    check('hostile: a small claim on an ordinary file', got == 'unreadable' and took < 5, f'{got} in {took:.1f}s')

    took, got = timed('long.docx', package(body(p('One short paragraph of text.') * 100_000)))
    check('hostile: 100,000 paragraphs', got == 'no error' and took < 20, f'{got} in {took:.1f}s')
    took, got = timed('deep.docx', package(body(p('Item', num=1, level=10 ** 9))))
    check('hostile: a list level of a billion is not a billion spaces', got == 'no error' and took < 5,
          f'{got} in {took:.1f}s')
    began = time.time()
    documents.reflow([['A line that ends in a hyphen-'] * 20_000, ['*a ' * 20_000], [documents._TURN] * 20_000])
    documents.write_docx('*a ' * 100_000 + '\n' + '**' * 100_000 + '\n' + '1. x\n' * 5_000)
    check('hostile: long runs of hyphens, stars and list items', time.time() - began < 20,
          f'{time.time() - began:.1f}s')


@section
def equations_symbols_and_odd_packages():
    math = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
    eq = f'<m:oMath xmlns:m="{math}"><m:r><m:t>m</m:t></m:r></m:oMath>'
    sym = '<w:r><w:sym w:font="Wingdings" w:char="F0FC"/></w:r>'
    doc = package(body(p(t('where ') + eq + t(' is the mass. It is robust.')) + p(t('Tick ') + sym + t(' means done.'))
                       + p('A plain paragraph.')))
    got = documents.extract('eq.docx', doc)
    check('equations: the text shows the gaps', got['text'] == (
        'where  is the mass. It is robust.\n\nTick  means done.\n\nA plain paragraph.'), got['text'])
    check('equations: a note says they are not shown', got['notes'] == [
        'Equations and symbols are not shown, so a paragraph that holds one keeps its old wording when you save '
        'as Word.'], got['notes'])
    # What a model does with "where  is the mass": it fills the gap. Typed beside the equation, the words would
    # repeat it, so the paragraph keeps its old words.
    out, notes = documents.rewrite_docx(doc, 'where m is the mass. It is sound.\n\nTick means the work is done.\n\n'
                                             'A plain one.')
    after = documents.extract('eq.docx', out)['text']
    check('equations: a paragraph holding one keeps its words, the others change', after == (
        'where  is the mass. It is robust.\n\nTick  means done.\n\nA plain one.'), after)
    check('equations: and a note says so for each', len(notes) == 2 and all('an equation or a symbol' in n
                                                                             for n in notes), notes)
    xml = zipfile.ZipFile(io.BytesIO(out)).read('word/document.xml').decode('utf-8')
    check('equations: the equation and the symbol are still there', '<m:t>m</m:t>' in xml and 'F0FC' in xml)
    same, quiet = documents.rewrite_docx(doc, 'where is the mass. It is robust.\n\nTick means done.\n\n'
                                              'A plain paragraph.')
    check('equations: white space alone is no change and needs no note', same == doc and quiet == [], quiet)
    kept, why = documents.rewrite_docx(doc, 'Tick  means done.\n\nA plain paragraph.')
    check('equations: a paragraph holding one is not deleted either',
          documents.extract('eq.docx', kept)['text'].startswith('where  is the mass.') and len(why) == 1, why)

    # The main part is the one the package's relationships name, as Word reads it.
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="/word/document2.xml"/></Relationships>')
    other = package(extra=[('_rels/.rels', rels), ('word/document2.xml', body(p('Under another name.')))])
    check('docx: a main part under another name is read', code('a.docx', other) == 'no error', code('a.docx', other))
    if code('a.docx', other) == 'no error':
        out, _ = documents.rewrite_docx(other, 'Under a new name.')
        check('docx: and written back where it was', documents.extract('a.docx', out)['text'] == 'Under a new name.'
              and 'word/document.xml' not in zipfile.ZipFile(io.BytesIO(out)).namelist())

    # A flyer: every word in a text box.
    boxed = ('<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:txbx><w:txbxContent>'
             + p('Summer fete on the green.') + '</w:txbxContent></wps:txbx></w:drawing></mc:Choice>'
             '</mc:AlternateContent></w:r></w:p>')
    try:
        documents.extract('flyer.docx', package(body(boxed)))
        said = None
    except documents.DocumentError as e:
        said = (e.code, e.message)
    check('docx: text only in text boxes says so', said is not None and said[0] == 'no_text'
          and 'text boxes' in said[1], said)

    # Imported content (w:altChunk), as export tools write it until Word saves the file.
    rels = ('word/_rels/document.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="c1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk" Target="c1.html"/>'
            '</Relationships>')
    chunk = ('word/c1.html', '<html><body><p>Imported words.</p></body></html>')
    alt = '<w:altChunk xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="c1"/>'
    chunked = package(body(p('Before.') + alt + p('After.')), extra=(rels, chunk))
    got = documents.extract('a.docx', chunked)
    check('docx: imported content is noted', got['text'] == 'Before.\n\nAfter.' and got['notes'] == [
        'Parts imported from other files are not shown here and stay in the file as they are.'], got)
    saved = documents.rewrite_docx(chunked, 'Before now.\n\nAfter.')[0]
    check('docx: imported content stays in the saved copy', b':altChunk ' in document_xml(saved)
          and documents.extract('a.docx', saved)['text'] == 'Before now.\n\nAfter.', document_xml(saved)[:300])
    try:
        documents.extract('a.docx', package(body(alt), extra=(rels, chunk)))
        said = None
    except documents.DocumentError as e:
        said = (e.code, e.message)
    check('docx: text only imported says so', said == ('no_text', documents.IMPORTED_ONLY), said)


@section
def notes_and_comments_in_a_rewritten_paragraph():
    # A footnote mark or the ends of a comment are not in the text shown, so they stay where
    # they stand among the runs. Sentences that move or go leave them by other words: a note.
    def foot(n):
        return f'<w:r><w:footnoteReference w:id="{n}"/></w:r>'
    first = 'Sales rose 20% in 2023. Costs fell by a third. The board approved the plan.'
    tail = 'A second paragraph keeps the file honest.'
    cited = package(body(p(t('Sales rose 20% in 2023.') + foot(1) + t(' Costs fell by a third.') + foot(2)
                           + t(' The board approved the plan.')) + p(tail)))
    commented = package(body(p(t('Sales rose 20% in 2023. ') + '<w:commentRangeStart w:id="7"/>'
                               + t('Costs fell by a third.') + '<w:commentRangeEnd w:id="7"/>'
                               + '<w:r><w:commentReference w:id="7"/></w:r>' + t(' The board approved the plan.'))
                             + p(tail)))
    for label, data in (('footnotes', cited), ('a comment', commented)):
        check(f'marks: {label}: the text shown has none',
              documents.extract('m.docx', data)['text'] == first + '\n\n' + tail)
        _, notes = documents.rewrite_docx(data, first.replace('rose', 'grew') + '\n\n' + tail)
        check(f'marks: {label}: a word changed beside them needs no note', notes == [], notes)
        for case, new, quoted in (('sentences swapped',
                                   'Costs fell by a third. Sales rose 20% in 2023. The board approved the plan.',
                                   'Costs fell by a third. Sales rose 20% in 2023. The board ap…'),
                                  ('a sentence dropped', 'Sales rose 20% in 2023. The board approved the plan.',
                                   'Sales rose 20% in 2023. The board approved the plan.')):
            out, notes = documents.rewrite_docx(data, new + '\n\n' + tail)
            # The note quotes the paragraph as it now reads, which is what Word shows.
            check(f'marks: {label}: {case} says so', notes == [
                'A footnote, endnote or comment mark in a rewritten paragraph now stands beside different words. '
                'Check in Word that it still follows the words it belongs to: "%s".' % quoted]
                and quoted.rstrip('…') in documents.extract('m.docx', out)['text'], notes)


@section
def text_bound_to_a_document_property():
    # A cover page's title is a content control bound to the file's Title property. Word puts
    # the property back over whatever document.xml says when it opens the file, so a rewrite
    # of it would be lost: it is left as it was, and a note says so.
    def tied(inner, path='/ns1:coreProperties[1]/ns0:title[1]'):
        return ('<w:sdt><w:sdtPr><w:alias w:val="Title"/><w:dataBinding w:prefixMappings="" '
                f'w:xpath="{path}" w:storeItemID="{{6C3C8BC8-F283-45AE-878A-BAB7291924A1}}"/><w:text/>'
                f'</w:sdtPr><w:sdtContent>{inner}</w:sdtContent></w:sdt>')

    def note(text):
        return ('Text that Word fills in from a document property, such as the title, was left as it was, because '
                f'Word puts the property back when it opens the file. Type the new wording over it in Word: "{text}".')
    cover = package(body(tied(p('Annual Report')) + p('The year went well.')))
    check('bound: a bound title is shown like any paragraph',
          documents.extract('c.docx', cover)['text'] == 'Annual Report\n\nThe year went well.')
    out, notes = documents.rewrite_docx(cover, 'Yearly Report\n\nThe year went very well.')
    check('bound: a bound title keeps its wording, and the paragraph after it changes',
          documents.extract('c.docx', out)['text'] == 'Annual Report\n\nThe year went very well.'
          and notes == [note('Annual Report')], notes)
    out, notes = documents.rewrite_docx(cover, 'The year went well.')
    check('bound: a bound title taken out stays, with the same note',
          documents.extract('c.docx', out)['text'] == 'Annual Report\n\nThe year went well.'
          and notes == [note('Annual Report')], notes)
    out, notes = documents.rewrite_docx(cover, 'Annual Report\n\nA line added under it.\n\nThe year went well.')
    xml = document_xml(out).decode('utf-8')
    check('bound: a paragraph added under a bound title goes after the control, not inside it',
          documents.extract('c.docx', out)['text'] == 'Annual Report\n\nA line added under it.\n\nThe year went well.'
          and re.search(r'<w:sdtContent><w:p>(?:(?!</w:p>).)*Annual Report(?:(?!</w:p>).)*</w:p></w:sdtContent>', xml)
          and notes == [], xml[-700:])

    inline = package(body(p(t('Prepared for ') + tied(t('Acme Ltd'), '/ns0:Properties[1]/ns0:Company[1]')
                            + t(' by the audit team.'))))
    out, notes = documents.rewrite_docx(inline, 'Prepared for Acme Ltd by our audit team.')
    check('bound: words around a bound name in a paragraph change',
          documents.extract('c.docx', out)['text'] == 'Prepared for Acme Ltd by our audit team.' and notes == [], notes)
    out, notes = documents.rewrite_docx(inline, 'Prepared for Acme Limited by the audit team.')
    check('bound: a bound name in a paragraph keeps its wording',
          documents.extract('c.docx', out)['text'] == 'Prepared for Acme Ltd by the audit team.'
          and notes == [note('Prepared for Acme Ltd by the audit team.')], notes)


def paragraphs_xml(docx):
    """The XML of each top-level w:p of document.xml, as text (the hand-built files nest none)."""
    return re.findall(r'<w:p>.*?</w:p>', document_xml(docx).decode('utf-8'))


TRACKED = 'Tracked changes in a rewritten paragraph were accepted in the copy: "%s".'
REVISION = re.compile(r'<w:(?:ins|del|moveFrom|moveTo|delText)[ >]')


@section
def tracked_changes_in_a_rewritten_paragraph():
    # The text shown has every tracked change accepted. Typed in among them, the new words
    # would land in a reviewer's insertion and leave a deletion in the middle of the new
    # wording, so that Reject All turned the paragraph into nonsense. A rewritten paragraph
    # has its changes accepted first, and a note says so. The others keep theirs.
    by = 'w:author="Ann Reviewer" w:date="2026-10-01T09:00:00Z"'
    pending = (t('Furthermore, ') + f'<w:del w:id="1" {by}><w:r><w:delText xml:space="preserve">it is worth '
               'noting that </w:delText></w:r></w:del>' + t('staffing levels')
               + f'<w:ins w:id="2" {by}>' + t(' (312 full-time staff)') + '</w:ins>' + t(' remained stable.'))
    untouched = p(t('Costs ') + f'<w:ins w:id="3" {by}>' + t('fell ') + '</w:ins>' + t('as planned.'))
    data = package(body(p(pending) + untouched))
    got = documents.extract('t.docx', data)
    check('tracked: the text shown has the changes accepted, and a note says what saving does',
          got['text'] == 'Furthermore, staffing levels (312 full-time staff) remained stable.\n\nCosts fell as planned.'
          and got['notes'] == [documents.TRACKED_NOTE], got)
    new = 'Staffing held steady at 312 full-time staff.'
    out, notes = documents.rewrite_docx(data, new + '\n\nCosts fell as planned.')
    first, second = paragraphs_xml(out)
    check('tracked: a rewritten paragraph reads as the rewrite, with no tracked change left in it',
          documents.extract('t.docx', out)['text'] == new + '\n\nCosts fell as planned.'
          and not REVISION.search(first) and 'Ann Reviewer' not in first, first)
    check('tracked: a paragraph that was not rewritten keeps its tracked changes byte for byte',
          second == paragraphs_xml(data)[1], second)
    check('tracked: a note says the changes were accepted', notes == [TRACKED % new], notes)
    out, notes = documents.rewrite_docx(data, got['text'])
    check('tracked: nothing rewritten, nothing accepted', out == data and notes == [], notes)

    # A move, an insertion a second reviewer deleted, and an insertion inside a link.
    moved = p(f'<w:moveFrom w:id="4" {by}>' + t('Moreover, ') + '</w:moveFrom>' + t('the first point stands.'))
    landed = p(f'<w:moveTo w:id="5" {by}>' + t('Moreover, ') + '</w:moveTo>' + t('the second point holds.'))
    undone = p(t('We ') + f'<w:ins w:id="6" {by}><w:del w:id="7" w:author="Bo"><w:r><w:delText>truly '
               '</w:delText></w:r></w:del></w:ins>' + t('agree.'))
    linked = p(t('Read ') + '<w:hyperlink w:anchor="guide">' + f'<w:ins w:id="8" {by}>' + t('the guide')
               + '</w:ins></w:hyperlink>' + t(' before you start, as it explains it all.'))
    data = package(body(moved + landed + undone + linked))
    new = ('The first point stands.', 'Moreover, the second point holds.', 'We all agree.',
           'Read the guide before you start.')
    out, notes = documents.rewrite_docx(data, '\n\n'.join(new))
    paras = paragraphs_xml(out)
    check('tracked: moves, nested changes and a link come out as the rewrite',
          documents.extract('t.docx', out)['text'] == '\n\n'.join(new), documents.extract('t.docx', out)['text'])
    check('tracked: only the rewritten paragraphs lose their changes',
          not REVISION.search(paras[0]) and paras[1] == paragraphs_xml(data)[1]
          and not REVISION.search(paras[2]) and not REVISION.search(paras[3]), paras)
    check('tracked: words of a link that were tracked stay in the link',
          re.search(r'<w:hyperlink w:anchor="guide">(?:(?!</w:hyperlink>).)*the guide', paras[3]), paras[3])
    check('tracked: one note for each paragraph whose changes were accepted',
          notes == [TRACKED % new[0], TRACKED % new[2], TRACKED % new[3]], notes)

    # A move between two paragraphs, its halves tied by the name of their ranges. With one side rewritten, the
    # whole move is accepted and its range markers go: left paired, Word's Reject All put the new wording back
    # where the words came from. Another move, in paragraphs that were not rewritten, stays as it was.
    def move(name, at, before, words, after_from):
        return (p(f'<w:moveFromRangeStart w:id="{at}" w:name="{name}" {by}/><w:moveFrom w:id="{at + 1}" {by}>'
                  + t(words + ' ') + f'</w:moveFrom><w:moveFromRangeEnd w:id="{at}"/>' + t(after_from)),
                p(t(before) + f'<w:moveToRangeStart w:id="{at + 2}" w:name="{name}" {by}/>'
                  + f'<w:moveTo w:id="{at + 3}" {by}>' + t(words) + f'</w:moveTo><w:moveToRangeEnd w:id="{at + 2}"/>'))
    first, second = move('move1', 10, 'The second point. ', 'The budget is fixed.',
                         'Moreover, the first point stands on its own.')
    other = move('move2', 20, 'Next. ', 'Staff stay.', 'Then a third point.')
    data = package(body(first + second + ''.join(other)))
    shown = documents.extract('t.docx', data)['text'].split('\n\n')
    for side, new in (('the side it went to', [shown[0], 'The second point. Our budget is fixed.'] + shown[2:]),
                      ('the side it came from', ['The first point stands on its own.'] + shown[1:])):
        out, notes = documents.rewrite_docx(data, '\n\n'.join(new))
        paras = paragraphs_xml(out)
        check(f'tracked: a move rewritten on {side} reads as the rewrite',
              documents.extract('t.docx', out)['text'] == '\n\n'.join(new), documents.extract('t.docx', out)['text'])
        check(f'tracked: a move rewritten on {side} is accepted whole, its range markers gone',
              not any(REVISION.search(x) or 'move1' in x or 'RangeStart' in x or 'RangeEnd' in x for x in paras[:2])
              and 'budget' not in paras[0] and paras[2:] == paragraphs_xml(data)[2:], paras)


@section
def a_field_whose_words_are_dropped():
    # A date or a cross-reference whose shown words the rewrite drops is taken out, as a
    # link is. Left in, Word shows it again, run into the words beside it ("2026by").
    def dated(inner):
        return package(body(p(t('Prepared on ') + inner + t(' by the team.')) + p('Second paragraph.')))
    complex_date = field('begin', r'DATE \@ "d MMMM yyyy"') + field('separate') + t('19 October 2026') + field('end')
    simple_date = '<w:fldSimple w:instr=" DATE "><w:r><w:t>19 October 2026</w:t></w:r></w:fldSimple>'
    taken = ('A field, such as a date or a cross-reference, was taken out, because the words it showed were all '
             'rewritten or moved: "Prepared by the team.".')
    for label, inner in (('a complex field', complex_date), ('a simple field', simple_date)):
        data = dated(inner)
        out, notes = documents.rewrite_docx(data, 'Prepared by the team.\n\nSecond paragraph.')
        xml = document_xml(out).decode('utf-8')
        check(f'field: {label} whose words are all gone is taken out',
              'fldChar' not in xml and 'instrText' not in xml and 'fldSimple' not in xml
              and documents.extract('f.docx', out)['text'] == 'Prepared by the team.\n\nSecond paragraph.', xml)
        check(f'field: {label} taken out says so', notes == [taken], notes)
        out, notes = documents.rewrite_docx(data, 'Prepared on 19 October 2026 by our team.\n\nSecond paragraph.')
        xml = document_xml(out).decode('utf-8')
        check(f'field: {label} whose words stay is kept',
              (xml.count('<w:fldChar ') == 3 and xml.count('instrText') == 2 if label == 'a complex field'
               else xml.count('<w:fldSimple ') == 1) and '19 October 2026' in xml and notes == [], (notes, xml))


if fails:
    print(f'{len(fails)} FAILED\n')
    for f in fails:
        print(f'  · {f}')
    sys.exit(1)
print('all tests passed')
