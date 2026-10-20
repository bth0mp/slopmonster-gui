#!/usr/bin/env python3
"""Tests for wordfile.py: rewriting a Word document in place. Run: python app/test_wordfile.py

The documents in testdata/ were written by Word itself, so they hold the XML real documents
hold. Word is not needed to run this: every check reads the XML that wordfile wrote. (The
module was scored against Word's own reading of 21 documents before it was taken in; a_ to d_
are the small ones kept from that. e_toc came later: a table of contents, which is left out
of the blocks and must come through byte for byte.)

Each testdata/<name>.json beside a <name>.docx holds
    blocks     what blocks(docx) must return, exactly
    edit       one light edit: new_blocks (the list handed to rewrite), blocks_after (what blocks() must return for
               the result), notes (what rewrite must return as notes) and checks, a list of assertions on the XML of
               the result.  Every check is a dict with a "check" key; the kinds are the functions named check_* below.

Standard library only.  The XML that is parsed is the fixtures' own and what the module wrote from them; a file with
a DOCTYPE is refused before any parser sees it.
"""
import io
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wordfile  # noqa: E402

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'testdata')
NS = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'mc': 'http://schemas.openxmlformats.org/markup-compatibility/2006',
    'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
    'v': 'urn:schemas-microsoft-com:vml',
    'wps': 'http://schemas.microsoft.com/office/word/2010/wordprocessingShape',
}
DOC = 'word/document.xml'
RELS = 'word/_rels/document.xml.rels'


def q(name):
    """'w:b' -> '{namespace}b'"""
    prefix, local = name.split(':')
    return '{%s}%s' % (NS[prefix], local)


def parts_of(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return [(i.filename, i.compress_type, z.read(i)) for i in z.infolist()]


def tree(xml):
    if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:
        raise ValueError('a DOCTYPE in a fixture')
    return ET.fromstring(xml)


def text_of(el):
    """The text of an element as blocks() counts it, tracked deletions and text boxes left out."""
    out = []

    def walk(node):
        for child in node:
            if child.tag in (q('w:del'), q('w:txbxContent'), q('mc:Fallback'), q('w:instrText'), q('w:delText')):
                continue
            if child.tag == q('w:t'):
                out.append(child.text or '')
            elif child.tag == q('w:tab'):
                if node.tag == q('w:r'):
                    out.append('\t')
            elif child.tag in (q('w:br'), q('w:cr')):
                if child.get(q('w:type')) not in ('page', 'column'):
                    out.append('\n')
            else:
                walk(child)
    walk(el)
    return ''.join(out)


def body_paragraphs_raw(xml):
    """The XML text of every w:p that is not inside another w:p, in document order (a text box's paragraphs
    belong to the paragraph that anchors it).  Plain string scanning, so the pieces can be compared byte for byte.
    The fixtures all use the prefix w:."""
    s = xml.decode('utf-8')
    out, pos, depth, start = [], 0, 0, -1
    while True:
        a = s.find('<w:p', pos)
        b = s.find('</w:p>', pos)
        while a >= 0 and s[a + 4] not in ' >/':            # <w:pPr>, <w:pStyle ...>: not a paragraph
            a = s.find('<w:p', a + 4)
        if b < 0:
            break
        if 0 <= a < b:
            end = s.find('>', a)
            if s[end - 1] == '/':                           # <w:p/> or <w:p .../>
                if depth == 0:
                    out.append(s[a:end + 1])
            else:
                if depth == 0:
                    start = a
                depth += 1
            pos = end + 1
        else:
            depth -= 1
            if depth == 0:
                out.append(s[start:b + 6])
            pos = b + 6
    return out


def runs_with(root, text):
    return [r for r in root.iter(q('w:r')) if text in ''.join(t.text or '' for t in r.findall(q('w:t')))]


def paragraph_with(root, text):
    hits = [p for p in root.iter(q('w:p')) if text_of(p) == text]
    if len(hits) != 1:
        raise AssertionError('%d paragraphs read %r' % (len(hits), text))
    return hits[0]


class Case(object):
    def __init__(self, before, after, notes, module):
        self.before, self.after, self.notes, self.module = before, after, notes, module
        self.parts0, self.parts1 = parts_of(before), parts_of(after)
        self.xml0 = dict((n, d) for n, _, d in self.parts0)[DOC]
        self.xml1 = dict((n, d) for n, _, d in self.parts1)[DOC]
        self.root0, self.root1 = tree(self.xml0), tree(self.xml1)

    # ---- the package and the parts nobody edited
    def check_other_parts_identical(self, c):
        """Every part but word/document.xml is byte-identical, with the same names, order and compression."""
        a = [(n, m, d if n != DOC else b'') for n, m, d in self.parts0]
        b = [(n, m, d if n != DOC else b'') for n, m, d in self.parts1]
        assert a == b, 'the other parts of the package changed'

    def check_root_tag_identical(self, c):
        """The start tag of w:document (every xmlns declaration, mc:Ignorable) is byte-identical."""
        def root_tag(xml):
            at = xml.index(b'<w:document')
            return xml[:xml.index(b'>', at) + 1]
        assert root_tag(self.xml0) == root_tag(self.xml1), 'the root element changed'

    def check_paragraphs_unchanged_except(self, c):
        """Body paragraphs (see body_paragraphs_raw) are byte-identical, but for those numbered in "paragraphs"."""
        a, b = body_paragraphs_raw(self.xml0), body_paragraphs_raw(self.xml1)
        assert len(a) == len(b), 'the document has %d body paragraphs, had %d' % (len(b), len(a))
        changed = [k for k, (x, y) in enumerate(zip(a, b)) if x != y]
        assert changed == c['paragraphs'], 'paragraphs that changed: %r, expected %r' % (changed, c['paragraphs'])

    def check_markup_between_paragraphs_identical(self, c):
        """Everything outside the body paragraphs (tables, section properties, wrappers) is byte-identical."""
        def skeleton(xml):
            s = xml.decode('utf-8')
            for raw in body_paragraphs_raw(xml):
                s = s.replace(raw, '<P/>', 1)
            return s
        assert skeleton(self.xml0) == skeleton(self.xml1), 'the markup between paragraphs changed'

    # ---- runs, links, paragraphs
    def check_run(self, c):
        """Every w:r whose text contains "text" (there must be one) has the rPr children listed in "has" and none
        of those in "lacks", e.g. {"check": "run", "text": "budget", "has": ["w:b"]}."""
        runs = runs_with(self.root1, c['text'])
        assert runs, 'no run holds %r' % c['text']
        for r in runs:
            props = r.find(q('w:rPr'))
            names = set(child.tag for child in props) if props is not None else set()
            for name in c.get('has', []):
                assert q(name) in names, 'the run holding %r has no %s' % (c['text'], name)
            for name in c.get('lacks', []):
                assert q(name) not in names, 'the run holding %r has %s' % (c['text'], name)

    def check_hyperlink(self, c):
        """A w:hyperlink whose text is exactly "text" points at "target" (through the relationships part) or at
        the bookmark "anchor"."""
        rels = dict((n, d) for n, _, d in self.parts1).get(RELS)
        targets = {}
        if rels:
            for rel in tree(rels):
                targets[rel.get('Id')] = rel.get('Target')
        for link in self.root1.iter(q('w:hyperlink')):
            if text_of(link) == c['text']:
                if 'target' in c:
                    assert targets.get(link.get(q('r:id'))) == c['target'], 'the link %r points at %r' % (
                        c['text'], targets.get(link.get(q('r:id'))))
                if 'anchor' in c:
                    assert link.get(q('w:anchor')) == c['anchor']
                return
        raise AssertionError('no hyperlink reads %r' % c['text'])

    def check_not_in_hyperlink(self, c):
        """No w:hyperlink holds "text"."""
        for link in self.root1.iter(q('w:hyperlink')):
            assert c['text'] not in text_of(link), '%r is inside a link' % c['text']

    def check_paragraph(self, c):
        """The one paragraph whose text is "text" has the style id "style" (null: none), and with "list" its
        numbering: {"numId": "2", "ilvl": "0"}, or null for no numbering."""
        p = paragraph_with(self.root1, c['text'])
        props = p.find(q('w:pPr'))
        if 'style' in c:
            style = props.find(q('w:pStyle')) if props is not None else None
            got = style.get(q('w:val')) if style is not None else None
            assert got == c['style'], 'the paragraph %r has style %r' % (c['text'], got)
        if 'list' in c:
            num = props.find(q('w:numPr')) if props is not None else None
            got = None
            if num is not None:
                got = {'numId': num.find(q('w:numId')).get(q('w:val')), 'ilvl': num.find(q('w:ilvl')).get(q('w:val'))}
            assert got == c['list'], 'the paragraph %r has numbering %r' % (c['text'], got)

    def check_paragraph_holds(self, c):
        """The one paragraph whose text is "text" still holds "count" elements named "tag" (e.g. w:drawing)."""
        p = paragraph_with(self.root1, c['text'])
        n = sum(1 for _ in p.iter(q(c['tag'])))
        assert n == c['count'], 'the paragraph %r holds %d %s' % (c['text'], n, c['tag'])

    # ---- counts, tables, text boxes
    def check_count(self, c):
        """document.xml holds "equals" elements named "tag"."""
        n = sum(1 for _ in self.root1.iter(q(c['tag'])))
        assert n == c['equals'], '%d %s, expected %d' % (n, c['tag'], c['equals'])

    def check_cell(self, c):
        """The cell at table/row/cell (counted from 1, top-level tables) reads "text" and holds "paragraphs" paragraphs."""
        body = self.root1.find(q('w:body'))
        tables = [t for t in body.iter(q('w:tbl'))]
        row = tables[c['table'] - 1].findall(q('w:tr'))[c['row'] - 1]
        cell = row.findall(q('w:tc'))[c['cell'] - 1]
        paras = cell.findall(q('w:p'))
        got = '\n'.join(text_of(p) for p in paras)
        assert got == c['text'], 'the cell reads %r' % got
        if 'paragraphs' in c:
            assert len(paras) == c['paragraphs']

    def check_text_boxes_unchanged(self, c):
        """Every w:txbxContent holds the text it held ("count" of them)."""
        def boxes(root):
            return [''.join(t.text or '' for t in box.iter(q('w:t'))) for box in root.iter(q('w:txbxContent'))]
        assert boxes(self.root0) == boxes(self.root1), 'text box text changed'
        if 'count' in c:
            assert len(boxes(self.root1)) == c['count'], '%d text boxes' % len(boxes(self.root1))

    def check_field_codes_unchanged(self, c):
        """The field instructions (w:instrText) and the number of w:fldChar are as they were."""
        def codes(root):
            return [t.text for t in root.iter(q('w:instrText'))], sum(1 for _ in root.iter(q('w:fldChar')))
        assert codes(self.root0) == codes(self.root1), 'the fields changed'


def run(module=wordfile):
    failed = total = 0
    for name in sorted(os.listdir(HERE)):
        if not name.endswith('.json'):
            continue
        with open(os.path.join(HERE, name), encoding='utf-8') as f:
            fx = json.load(f)
        with open(os.path.join(HERE, fx['file']), 'rb') as f:
            data = f.read()
        results = []

        def attempt(label, fn):
            try:
                fn()
                results.append((label, None))
            except Exception as e:                           # an assertion that does not hold, or a crash
                results.append((label, '%s: %s' % (type(e).__name__, e)))

        def same(got, want, what):
            assert got == want, '%s: got %r' % (what, [g for g, w in zip(got, want) if g != w][:2] or got[:2])
        attempt('blocks', lambda: same(module.blocks(data), fx['blocks'], 'blocks()'))
        attempt('identity', lambda: same(module.rewrite(data, fx['blocks'])[0], data, 'rewrite(blocks) must return the same document'))
        edit = fx['edit']
        try:
            out, notes = module.rewrite(data, edit['new_blocks'])
            case = Case(data, out, notes, module)
        except Exception as e:
            results.append(('rewrite', '%s: %s' % (type(e).__name__, e)))
            case = None
        if case is not None:
            attempt('blocks_after', lambda: same(module.blocks(out), edit['blocks_after'], 'blocks() of the result'))
            attempt('notes', lambda: same(notes, edit['notes'], 'notes'))
            for c in edit['checks']:
                attempt(json.dumps(c, ensure_ascii=True), lambda c=c: getattr(case, 'check_' + c['check'])(c))
        bad = [(label, why) for label, why in results if why]
        total += len(results)
        failed += len(bad)
        for label, why in bad:
            print('  · %s: %s\n      %s' % (fx['file'], label[:150], why[:300]))
    # A refusal is a ValueError with a sentence for the user, never anything else.
    for name, data in (('not a zip', b'not a zip at all'), ('a zip with no document', _zip({'a.txt': b'x'})),
                       ('a DOCTYPE', _zip({'word/document.xml': b'<!DOCTYPE x><w:document/>'}))):
        total += 1
        try:
            module.blocks(data)
            why = 'was read'
        except ValueError as e:
            why = None if str(e) else 'refused with no message'
        except Exception as e:
            why = 'raised %s' % type(e).__name__
        if why:
            failed += 1
            print('  · refusing %s: %s' % (name, why))
    # A paragraph that keeps two tables apart, or follows the last one, is emptied and stays:
    # Word joins two tables that touch, and wants a paragraph after the last table.
    with open(os.path.join(HERE, 'c_tables.docx'), 'rb') as f:
        data = f.read()
    for gone in ('Notes on individual projects are recorded in the final table.',
                 'All figures are provisional until the audit is complete.'):
        total += 1
        try:
            out, notes = module.rewrite(data, [b for b in module.blocks(data) if b != gone])
            xml = dict((n, d) for n, _, d in parts_of(out))[DOC].decode('utf-8')
            why = ('two tables touch' if re.search(r'</w:tbl>\s*<w:tbl[ >]', xml)
                   else 'the body ends with a table' if re.search(r'</w:tbl>\s*<w:sectPr[ >]', xml)
                   else 'no note' if not any('emptied rather than removed' in n for n in notes)
                   else 'still in blocks()' if gone in module.blocks(out) else None)
        except Exception as e:
            why = 'raised %s: %s' % (type(e).__name__, e)
        if why:
            failed += 1
            print('  · c_tables.docx, taking out %r: %s' % (gone[:40], why))
    # The fixtures are public: none may name a person. Word puts the user's name in core.xml,
    # app.xml's Company, people.xml and the w:author of comments and tracked changes. The
    # name found is never printed, only where it is.
    for name in sorted(n for n in os.listdir(HERE) if n.endswith('.docx')):
        total += 1
        with open(os.path.join(HERE, name), 'rb') as f:
            parts = dict((n, d) for n, _, d in parts_of(f.read()))
        core = parts.get('docProps/core.xml', b'').decode('utf-8')
        app = parts.get('docProps/app.xml', b'').decode('utf-8')
        where = [tag for tag in ('dc:creator', 'cp:lastModifiedBy')
                 if re.search(r'<%s>(?!SlopMonster</)[^<]+</%s>' % (tag, tag), core)]
        where += ['Company'] if re.search(r'<Company>[^<]+</Company>', app) else []
        where += [n for n, d in parts.items() if n.endswith('people.xml')
                  or (n.startswith('word/') and re.search(rb'\bw15?:(author|person)\b', d))]
        if where:
            failed += 1
            print('  · %s names a person in %s' % (name, ', '.join(where)))
    if failed:
        print('%d of %d FAILED' % (failed, total))
        return 1
    print('all tests passed')
    return 0


def _zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


if __name__ == '__main__':
    sys.exit(run())
