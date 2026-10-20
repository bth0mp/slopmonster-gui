"""Rewrite the text of a Word document in place: a copy of the .docx in which only the words have changed.

    blocks(data)              -> the body text, one string per paragraph or table row
    read(data)                -> (those blocks, notes for the user about what they leave out)
    rewrite(data, new_blocks) -> (a copy of the document holding the new text, notes for the user)

Standard library only.  The approach is an ElementTree round trip of word/document.xml, and nothing else in the
package is opened for writing: every other part is copied as the compressed bytes it already is.

Why not ET.fromstring() and ET.tostring()?  That pair renames every prefix (w: becomes ns0:), drops the namespace
declarations nothing in the tree seems to use (mc:Ignorable names prefixes, so Word then refuses the file), writes
<w:b /> for <w:b/> and &#10; for &#xA;, and forgets comments.  So the tree is built here by the same expat parser
ElementTree uses, driven directly, which lets every element remember three more things: the byte span it came from,
its parent, and the namespace declarations made on it.  Edits are made on the tree.  The writer then walks the tree
and, for any element nothing touched, copies its bytes from the source; only the elements that changed are written
out from the tree, and those take their prefixes from the declarations that are in scope where they stand.  The XML
declaration, the root start tag and everything else outside the edited paragraphs therefore come through verbatim.

Untrusted input: at most MAX_XML bytes of XML are ever inflated (whatever the zip header says), a document with more
than MAX_ELEMENTS elements or nested deeper than MAX_DEPTH is refused, a DOCTYPE or ENTITY declaration is refused,
no part other than the three that are read is decompressed at all, and nothing here can backtrack over a long run of
text or do work that grows with the square of the document.  Every refusal is a ValueError with a sentence for the
user.

Places where rewrite() reads its contract by the spirit rather than the letter, each marked where it happens:
  * New blocks of the same number as the old are taken block for block, unless lining them up shows that more
    blocks came back untouched that way: a paragraph removed here and another added there would otherwise rewrite
    every paragraph in between with its neighbour's text.
  * Added words take the formatting of the run before them, but they do not join a hyperlink, a field result, a
    tracked insertion or a content control that merely ends where they were typed.
  * White space belongs to the document.  A block that comes back trimmed, or with its tabs, line breaks and
    double spaces flattened to single spaces, is not a change; in a changed block the white space between words
    that did not change stays as it was.  (A text box or a language model flattens it without meaning to.)
  * A block that comes back empty was deleted, whatever the number of blocks says.
  * A new block is styled like the one before it when it carries that block's marker.  Otherwise its own marker
    says what it is: plain text added under a heading is an ordinary paragraph, '## Title' becomes a heading if the
    document has one to copy, '  - item' a list item of that depth.  See _model.
  * A new block beside a table row, with that row's number of ' | ' pieces, becomes a new row.  See row_like.
  * When part of a sentence moves, the words that carry a link or formatting of their own are the ones left in
    place, and the plain words are typed again.  A link or a field whose words are all gone is taken out, with a
    note.  See _anchors.
  * The text shown has the tracked changes accepted, so a paragraph that is rewritten has them accepted in the
    copy too, with a note: new words typed in among them would leave a pending deletion in the middle of the new
    wording.  Paragraphs that are not rewritten keep theirs.  See accept.
"""
import difflib
import io
import posixpath
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter
from xml.parsers import expat

__all__ = ['blocks', 'read', 'rewrite']

MAX_XML = 60_000_000                    # bytes of XML inflated from one document, over all the parts that are read
MAX_DEPTH = 256                         # elements nested deeper than this are not a Word document
MAX_ELEMENTS = 500_000                  # elements in word/document.xml: about ten times a 300-page document
MAX_NOTES = 12                          # notes of one kind spelled out for the user; the rest are counted
DOC_PART = 'word/document.xml'

NOT_DOCX = 'This file is not a Word document (.docx).'
DAMAGED = 'This Word file is damaged and cannot be read. Open it in Word and save it again.'
LOCKED = 'This Word file has a password. Remove it in Word and try again.'
TOO_BIG = 'This Word file is too large to work on. Split it and try each part.'
HAS_DTD = 'This Word file holds a document type declaration, which a Word file never needs. It was not opened.'
BAD_BLOCKS = 'The new text must be a list of strings, one for each block.'
# Word's Update Field on a table of contents asks what to update, and "page numbers only" leaves the old headings.
TOC_NOTE = ('The table of contents is not shown. Word rebuilds it from the headings: in Word, right-click it, '
            'choose Update Field, then Update entire table.')
FIGURES_NOTE = ('The table of figures is not shown. Word rebuilds it from the captions: in Word, right-click it, '
                'choose Update Field, then Update entire table.')
UNSEEN = ('A paragraph that holds an equation or a symbol was left as it was, because those are not in the text '
          'that was rewritten: "%s".')
BOUND = ('Text that Word fills in from a document property, such as the title, was left as it was, because Word '
         'puts the property back when it opens the file. Type the new wording over it in Word: "%s".')
# A footnote or endnote mark, a comment and the ends of the text a comment is on stay where they stand among the
# runs; the words around them are what the rewrite changes.
MARK_MOVED = ('A footnote, endnote or comment mark in a rewritten paragraph now stands beside different words. Check '
              'in Word that it still follows the words it belongs to: "%s".')
# The text shown has every tracked change accepted.  New words typed in among them would land in a reviewer's
# insertion, credited to that reviewer, and leave a deletion in the middle of the new wording.
TRACKED = 'Tracked changes in a rewritten paragraph were accepted in the copy: "%s".'
FIELD_GONE = ('A field, such as a date or a cross-reference, was taken out, because the words it showed were all '
              'rewritten or moved: "%s".')
_WORD = re.compile(r'\w+')

_MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
_W_URIS = ('http://schemas.openxmlformats.org/wordprocessingml/2006/main',
           'http://purl.oclc.org/ooxml/wordprocessingml/main')         # the second is "Strict Open XML"
_XML = 'http://www.w3.org/XML/1998/namespace'
_SPACE = '{%s}space' % _XML


# ====================================================================================================================
# The package: bounded reads, and a copy with one part replaced
# ====================================================================================================================
class _Package(object):
    def __init__(self, data):
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise ValueError('The document must be given as bytes.')
        self.data = data
        self.left = MAX_XML
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            self.name = _main_part(self.zip)
            self.info = self.zip.getinfo(self.name)
        except Exception:               # not a zip, a truncated zip, a zip with no main document part
            raise ValueError(NOT_DOCX) from None

    def xml(self, name):
        """One XML part as UTF-8 bytes, or None when the package has no such part.  Never inflates more than what
        is left of MAX_XML: the size in the zip header is the file's own claim and is not believed."""
        try:
            info = self.zip.getinfo(name)
        except KeyError:
            return None
        chunks, problem = [], None
        try:
            with self.zip.open(info) as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.left -= len(chunk)
                    if self.left < 0:
                        problem = TOO_BIG
                        break
                    chunks.append(chunk)
        except RuntimeError:            # zipfile's word for "this entry is encrypted"
            problem = LOCKED
        except Exception:               # bad CRC, broken deflate stream, unknown compression method, ...
            problem = DAMAGED
        if problem:
            del chunks
            raise ValueError(problem)
        return _as_utf8(b''.join(chunks))

    def with_document(self, xml):
        """The package again with word/document.xml replaced.  Every other entry is copied as it lies in the file
        (local header and compressed bytes), in the same order, so nothing else is inflated or re-deflated."""
        data, out, central, total = self.data, [], [], 0
        infos = self.zip.infolist()
        if len(infos) >= 0xFFFF:
            raise ValueError(TOO_BIG)
        for info in infos:
            at = info.header_offset
            head = data[at:at + 30]
            if len(head) < 30 or head[:4] != b'PK\x03\x04':
                raise ValueError(DAMAGED)
            flags, = struct.unpack_from('<H', head, 6)
            name_len, extra_len = struct.unpack_from('<HH', head, 26)
            body = at + 30 + name_len + extra_len
            names = data[at + 30:body]
            if info is self.info:
                if info.compress_type == zipfile.ZIP_DEFLATED:
                    deflate = zlib.compressobj(6, zlib.DEFLATED, -15)
                    packed = deflate.compress(xml) + deflate.flush()
                elif info.compress_type == zipfile.ZIP_STORED:
                    packed = xml
                else:
                    raise ValueError(DAMAGED)
                crc, size = zlib.crc32(xml) & 0xFFFFFFFF, len(xml)
            else:
                packed = data[body:body + info.compress_size]
                if len(packed) != info.compress_size:
                    raise ValueError(DAMAGED)
                crc, size = info.CRC, info.file_size
            if max(len(packed), size, total) >= 0xFFFFFFFF:
                raise ValueError(TOO_BIG)
            # The sizes go into the header, so an entry that kept them in a trailing data descriptor loses the flag.
            flags &= ~0x08
            local = (head[:6] + struct.pack('<H', flags) + head[8:14] + struct.pack('<III', crc, len(packed), size)
                     + head[26:30])
            central.append((info, head[8:14], crc, len(packed), size, total))
            out.append(local)
            out.append(names)
            out.append(packed)
            total += 30 + len(names) + len(packed)
            if total > len(data) + len(xml) + (1 << 16):        # entries that overlap: a zip built to explode
                raise ValueError(DAMAGED)
        start = total
        for info, method_time_date, crc, packed_size, size, offset in central:
            name = info.orig_filename.encode('utf-8' if info.flag_bits & 0x800 else 'cp437')
            extra = _without_zip64(info.extra)
            comment = info.comment or b''
            record = (struct.pack('<4sBBH', b'PK\x01\x02', info.create_version & 0xFF, info.create_system & 0xFF,
                                  info.extract_version)
                      + struct.pack('<H', info.flag_bits & ~0x08 & 0xFFFF) + method_time_date
                      + struct.pack('<IIIHHHHHII', crc, packed_size, size, len(name), len(extra), len(comment), 0,
                                    info.internal_attr & 0xFFFF, info.external_attr & 0xFFFFFFFF, offset))
            out.append(record + name + extra + comment)
            total += len(record) + len(name) + len(extra) + len(comment)
        comment = self.zip.comment or b''
        out.append(struct.pack('<4sHHHHIIH', b'PK\x05\x06', 0, 0, len(central), len(central), total - start, start,
                               len(comment)) + comment)
        return b''.join(out)


def _main_part(zf):
    """The name of the main document part, as the package's own relationships give it.  It is almost always
    word/document.xml, but the name is the package's to choose (tools built on the Open XML SDK have written
    word/document2.xml), and Word follows the relationship.  word/document.xml when the relationships say nothing
    usable."""
    try:
        with zf.open('_rels/.rels') as fh:
            raw = fh.read(1 << 20)          # a list of a few relationships; a longer one is not believed
        rels = ET.fromstring(_as_utf8(raw))
        for rel in rels:
            if (rel.get('Type') or '').endswith('/officeDocument') and rel.get('TargetMode') != 'External':
                name = posixpath.normpath((rel.get('Target') or '').lstrip('/'))
                zf.getinfo(name)
                return name
    except Exception:
        pass
    return DOC_PART


def _without_zip64(extra):
    """A central-directory extra field without its zip64 block: the sizes are written in the plain fields here."""
    out, at = [], 0
    while at + 4 <= len(extra):
        kind, size = struct.unpack_from('<HH', extra, at)
        if kind != 1:
            out.append(extra[at:at + 4 + size])
        at += 4 + size
    return b''.join(out)


_DECLARED = re.compile(r'(<\?xml[^>]{0,200}?encoding\s{0,20}=\s{0,20}["\'])([A-Za-z0-9._-]{1,40})(["\'])')
_OTHER_CODECS = ('utf-16', 'utf16', 'iso-8859-', 'iso8859-', 'latin', 'windows-125', 'cp125', 'us-ascii', 'ascii')


def _as_utf8(raw):
    """The part as UTF-8 (Word writes nothing else, and then this returns the very same bytes), refused if it holds
    a DTD.  The test is made on the bytes the parser will read: a DTD written in UTF-16 would walk past it."""
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        codec = 'utf-16'
    elif raw[:4] in (b'<\x00?\x00', b'\x00<\x00?'):
        codec = 'utf-16-le' if raw[:1] == b'<' else 'utf-16-be'
    else:
        found = _DECLARED.match(raw[3 if raw[:3] == b'\xef\xbb\xbf' else 0:][:260].decode('latin-1'))
        codec = found.group(2).lower() if found else 'utf-8'
    if codec not in ('utf-8', 'utf8'):
        if not codec.startswith(_OTHER_CODECS):
            raise ValueError(DAMAGED)
        try:
            text = raw.decode(codec)
        except (LookupError, ValueError):
            raise ValueError(DAMAGED) from None
        raw = (_DECLARED.sub(r'\1UTF-8\3', text[:260], count=1) + text[260:]).encode('utf-8')
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise ValueError(HAS_DTD)
    return raw


# ====================================================================================================================
# The tree: ElementTree elements that remember where they came from
# ====================================================================================================================
class _El(ET.Element):
    """lo, hi  where the element lies in the source: the offset of its start tag's '<', and the offset the parser
               reported when the element ended (the '<' of its end tag); -1 for an element made here
       up      its parent
       ns      the namespace declarations made on it, ((prefix, uri), ...), or None
       st      0 untouched; 1 something inside it changed; 2 its own text or attributes changed
       kids    the children it had in the source, kept from the moment its list of children is first changed"""
    __slots__ = ('lo', 'hi', 'up', 'ns', 'st', 'kids')


def _new(tag, text=None, attrib=None):
    el = _El(tag, attrib) if attrib else _El(tag)
    el.lo = el.hi = -1
    el.up = el.ns = el.kids = None
    el.st = 2
    el.text = text
    return el


def _refuse_dtd(*_):
    raise ValueError(HAS_DTD)


def _parse(src):
    """The tree of one XML part.  Comments and processing instructions become nodes, as ElementTree makes them.
    A part with more than MAX_ELEMENTS elements is refused: each one costs memory and time here, and a file can
    be small on disk and still hold millions of them."""
    parser = expat.ParserCreate(namespace_separator='}')
    parser.buffer_text = True
    parser.buffer_size = 1 << 16
    parser.ordered_attributes = True
    parser.StartDoctypeDeclHandler = parser.EntityDeclHandler = parser.ExternalEntityRefHandler = _refuse_dtd
    names = {}                          # 'uri}local' as expat spells it -> '{uri}local' as ElementTree does
    state = [None, None, None, 0, 0]    # root, the open element, the element that just closed, depth, elements
    text, declared, prefixes = [], [], {}

    def name(raw):
        fixed = names[raw] = '{' + raw if '}' in raw else raw
        return fixed

    def flush():
        data = text[0] if len(text) == 1 else ''.join(text)
        del text[:]
        if state[2] is not None:
            state[2].tail = data
        elif state[1] is not None:
            state[1].text = data

    def add(el, at):
        el.lo = el.hi = at
        el.up = state[1]
        el.ns = el.kids = None
        el.st = 0
        if state[1] is not None:
            state[1].append(el)

    def start(tag, attrs):
        if text:
            flush()
        if attrs:
            pairs = iter(attrs)
            el = _El(names.get(tag) or name(tag), {names.get(k) or name(k): v for k, v in zip(pairs, pairs)})
        else:
            el = _El(names.get(tag) or name(tag))
        add(el, parser.CurrentByteIndex)
        if declared:
            el.ns = tuple(declared)
            del declared[:]
        if state[0] is None:
            state[0] = el
        state[1], state[2] = el, None
        state[3] += 1
        state[4] += 1
        if state[3] > MAX_DEPTH:
            raise ValueError(DAMAGED)
        if state[4] > MAX_ELEMENTS:
            raise ValueError(TOO_BIG)

    def end(tag):
        if text:
            flush()
        el = state[1]
        el.hi = parser.CurrentByteIndex
        state[1], state[2] = el.up, el
        state[3] -= 1

    def comment(data):
        if text:
            flush()
        if state[1] is not None:        # one outside the root lies in the bytes around it, which are kept as they are
            el = _El(ET.Comment)
            el.text = data
            add(el, parser.CurrentByteIndex)
            state[2] = el

    def instruction(target, data):
        if text:
            flush()
        if state[1] is not None:
            el = _El(ET.PI)
            el.text = target + ' ' + data if data else target
            add(el, parser.CurrentByteIndex)
            state[2] = el

    def declare(prefix, uri):
        declared.append((prefix or '', uri or ''))
        prefixes.setdefault(uri or '', prefix or '')

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = text.append
    parser.CommentHandler, parser.ProcessingInstructionHandler = comment, instruction
    parser.StartNamespaceDeclHandler = declare
    try:
        parser.Parse(src, True)
    except expat.ExpatError:
        raise ValueError(DAMAGED) from None
    return state[0], prefixes


_TAG_STOP = re.compile(b'[>"' + b"'" + b']')          # '>' or either kind of quote


def _tag_close(src, lo):
    """The offset just past the '>' that ends the tag starting at lo.  A '>' inside a quoted value does not end it.
    One pass over the tag, however many attributes it has (the source has been parsed, so every quote is closed)."""
    search, find, at = _TAG_STOP.search, src.find, lo
    while True:
        stop = search(src, at)
        if stop is None:
            raise ValueError(DAMAGED)
        at = stop.end()
        if src[at - 1] == 62:           # '>'
            return at
        at = find(stop.group(), at) + 1
        if not at:
            raise ValueError(DAMAGED)


def _span_end(src, el):
    """The offset just past an element that came from the source."""
    if el.tag is ET.Comment:
        return src.index(b'-->', el.lo + 4) + 3
    if el.tag is ET.PI:
        return src.index(b'?>', el.lo + 2) + 2
    close = _tag_close(src, el.lo)
    if src[close - 2] == 47:            # '/>': an empty-element tag is the whole element
        return close
    return src.index(b'>', el.hi) + 1


def _escape(text):
    if '&' in text:
        text = text.replace('&', '&amp;')
    if '<' in text:
        text = text.replace('<', '&lt;')
    if '>' in text:
        text = text.replace('>', '&gt;')
    if '\r' in text:
        text = text.replace('\r', '&#xD;')
    return text.encode('utf-8')


def _escape_attribute(text):
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    return text.replace('\t', '&#x9;').replace('\n', '&#xA;').replace('\r', '&#xD;').encode('utf-8')


class _Writer(object):
    """Serialises the tree.  `pure` writes every element below the root from the tree, never from the source: it is
    there to measure how far a plain tree serialisation is from the original bytes, and is not used by rewrite()."""

    def __init__(self, src, root, prefixes, pure=False):
        self.src, self.root, self.prefixes, self.pure = src, root, prefixes, pure
        self.out = []
        self.names = {}

    def document(self):
        src, root, out = self.src, self.root, self.out
        out.append(src[:root.lo])
        if self.pure:
            close = _tag_close(src, root.lo)
            out.append(src[root.lo:close])
            if src[close - 2] != 47:
                self.inside(root)
                out.append(b'</' + src[root.lo + 1:close - 1].split(None, 1)[0] + b'>')
        else:
            self.element(root)
        out.append(src[_span_end(src, root):])
        return b''.join(out)

    def element(self, el):
        """An element that came from the source: its own bytes if nothing in it changed, and otherwise its own start
        tag, the bytes between its children, and each child in turn."""
        src, out, lo = self.src, self.out, el.lo
        if lo < 0 or self.pure:
            return self.fresh(el)
        if not el.st:
            out.append(src[lo:_span_end(src, el)])
            return
        close = _tag_close(src, lo)
        empty = src[close - 2] == 47
        tag = src[lo:close - 2] if empty else src[lo:close - 1]
        name = tag[1:].split(None, 1)[0]
        own = el.st == 2
        if own and el.get(_SPACE) == 'preserve' and b'xml:space' not in tag:
            tag += b' xml:space="preserve"'
        kids = list(el)
        was = el.kids if el.kids is not None else kids
        if own:
            lead = _escape(el.text or '')
        elif empty:
            lead = b''
        else:
            lead = src[close:was[0].lo if was else el.hi]
        if empty and not kids and not lead:
            out.append(tag + b'/>')
            return
        out.append(tag + b'>')
        out.append(lead)
        until = {}                      # child from the source -> where the text after it ends
        for i, child in enumerate(was):
            until[child] = was[i + 1].lo if i + 1 < len(was) else el.hi
        for child in kids:
            if child.lo < 0:
                self.fresh(child)
                if child.tail:
                    out.append(_escape(child.tail))
                continue
            after = _span_end(src, child)
            if child.st:
                self.element(child)
            else:
                out.append(src[child.lo:after])
            if own:
                if child.tail:
                    out.append(_escape(child.tail))
            elif until.get(child, after) > after:
                out.append(src[after:until[child]])
        out.append(b'</' + name + b'>')

    def fresh(self, el):
        """An element made here (or any element, when writing `pure`): written from the tree."""
        out, tag = self.out, el.tag
        if tag is ET.Comment:
            out.append(b'<!--' + (el.text or '').encode('utf-8') + b'-->')
            return
        if tag is ET.PI:
            out.append(b'<?' + (el.text or '').encode('utf-8') + b'?>')
            return
        name = self.qname(tag, el, False)
        attrs = [(self.qname(k, el, True), v) for k, v in el.attrib.items()]
        head = [b'<', name]
        for prefix, uri in el.ns or ():
            head.append(b' xmlns' + (b':' + prefix.encode('utf-8') if prefix else b'') + b'="'
                        + _escape_attribute(uri) + b'"')
        for key, value in attrs:
            head.append(b' ' + key + b'="' + _escape_attribute(value) + b'"')
        if not len(el) and not el.text:
            head.append(b'/>')
            out.append(b''.join(head))
            return
        head.append(b'>')
        out.append(b''.join(head))
        self.inside(el)
        out.append(b'</' + name + b'>')

    def inside(self, el):
        out = self.out
        if el.text:
            out.append(_escape(el.text))
        for child in el:
            self.element(child)
            if child.tail and (child.lo < 0 or self.pure):
                out.append(_escape(child.tail))

    def qname(self, tag, el, attribute):
        """prefix:local for a name in the tree, with the prefix that is bound where el stands.  A namespace with no
        prefix in scope there gets one, declared on el itself."""
        if tag[:1] != '{':
            return tag.encode('utf-8')
        scope = el
        while scope.ns is None and scope.up is not None:
            scope = scope.up
        key = (tag, scope, attribute)
        name = self.names.get(key)
        if name is None:
            uri, _, local = tag[1:].partition('}')
            hidden, node, prefix = set(), el, 'xml' if uri == _XML else None
            while node is not None and prefix is None:
                for known, bound in reversed(node.ns or ()):
                    if known not in hidden:
                        if bound == uri and (known or not attribute):
                            prefix = known
                            break
                        hidden.add(known)
                node = node.up
            if prefix is None:
                base = self.prefixes.get(uri) or 'ns'
                prefix, n = base, 0
                while prefix in hidden:
                    n += 1
                    prefix = '%s%d' % (base, n)
                el.ns = (el.ns or ()) + ((prefix, uri),)
                return (prefix + ':' + local).encode('utf-8')
            name = self.names[key] = (prefix + ':' + local if prefix else local).encode('utf-8')
        return name


def _touch(el):
    """Something in el changed: it and everything above it can no longer be copied from the source as they are."""
    while el is not None and not el.st:
        el.st = 1
        el = el.up


def _own(el):
    """Call before changing el's list of children: the writer needs to know what the list was in the source."""
    if el.lo >= 0 and el.kids is None:
        el.kids = list(el)
    _touch(el)


def _own_text(el):
    """The text of an element that has a comment or a processing instruction in the middle of it."""
    return (el.text or '') + ''.join([child.tail or '' for child in el])


def _local(tag):
    return tag.rsplit('}', 1)[-1] if tag.__class__ is str else ''


def _value(el, name='val'):
    """An attribute by its local name, whatever its namespace (styles.xml and numbering.xml are read this way, so a
    file saved as Strict Open XML reads the same)."""
    if el is not None:
        for key, value in el.attrib.items():
            if key.rsplit('}', 1)[-1] == name:
                return value
    return None


def _kid(el, name):
    if el is not None:
        for child in el:
            if _local(child.tag) == name:
                return child
    return None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ====================================================================================================================
# Reading the body
# ====================================================================================================================
_HEADINGS = dict([('title', 1)] + [('heading %d' % n, n) for n in range(1, 7)])
# The paragraph styles Word fills in from a TOC field: a table of contents and a table of figures.  Word writes
# these lines itself, and writes them again when the field is updated.  ("TOC Heading", the title above, is the
# author's own.)
_GENERATED = re.compile(r'(toc [1-9]|table of figures)$')
# Never walked into when looking for a paragraph's text.
_NO_TEXT = ('pPr rPr del moveFrom txbxContent instrText delText delInstrText drawing pict object rt sdtPr sdtEndPr '
            'fldData').split()
# Never walked into when looking for paragraphs and table rows.
_NO_BLOCKS = ('pPr rPr sectPr tblPr tblGrid tblPrEx trPr tcPr sdtPr sdtEndPr customXmlPr del moveFrom '
              'txbxContent').split()
# What a paragraph may hold and still be removed outright: text, and things that mean nothing without it.
_PLAIN = 'r t tab ptab cr noBreakHyphen softHyphen lastRenderedPageBreak proofErr hyperlink ins smartTag sym'.split()
# What a new paragraph does not take from the one it is styled like.
_NOT_COPIED = ('sectPr pPrChange rPrChange ins del moveFrom moveTo pageBreakBefore').split()
# ... and what a new table row does not take from the row it is modelled on.
_NOT_COPIED_ROW = 'tblHeader cnfStyle trPrChange tcPrChange tblPrExChange cellIns cellDel cellMerge'.split()

_TEXT, _TAB, _BREAK, _RETURN, _HYPHEN, _FIELD = 1, 2, 3, 4, 5, 6


class _Unit(object):
    """One block: a paragraph outside any table (p, kind, marker, and for a list item its list and level, for a
    heading its level), or a table row (tr, table, cells)."""
    __slots__ = ('p', 'text', 'marker', 'kind', 'num', 'depth', 'tr', 'table', 'cells')

    def __init__(self, text, p=None, marker='', kind='', num=None, depth=0, tr=None, table=None, cells=None):
        self.text, self.p, self.marker, self.kind, self.num, self.depth = text, p, marker, kind, num, depth
        self.tr, self.table, self.cells = tr, table, cells


class _Document(object):
    def __init__(self, data):
        self.package = package = _Package(data)
        self.src = package.xml(package.name)
        self.root, self.prefixes = _parse(self.src)
        root = self.root
        ns, _, local = root.tag[1:].partition('}') if root.tag[:1] == '{' else ('', '', root.tag)
        if local != 'document' or ns not in _W_URIS:
            raise ValueError(NOT_DOCX)
        self.q = q = ('{%s}' % ns).__add__ if ns else str
        self.kind = {q(name): 0 for name in _NO_TEXT}
        self.kind['{%s}Fallback' % _MC] = 0
        self.kind.update({q('t'): _TEXT, q('tab'): _TAB, q('ptab'): _TAB, q('br'): _BREAK, q('cr'): _RETURN,
                          q('noBreakHyphen'): _HYPHEN, q('fldChar'): _FIELD})
        self.no_blocks = frozenset([q(name) for name in _NO_BLOCKS] + ['{%s}Fallback' % _MC])
        self.plain = frozenset(q(name) for name in _PLAIN)
        self.not_copied = frozenset(q(name) for name in _NOT_COPIED)
        self.not_copied_row = frozenset(q(name) for name in _NOT_COPIED + _NOT_COPIED_ROW)
        # Marks that belong to the word before them (-1) or after them (1).
        self.anchored = {q('footnoteReference'): -1, q('endnoteReference'): -1, q('commentReference'): -1,
                         q('commentRangeEnd'): -1, q('commentRangeStart'): 1}
        self.styles = self._styles(package.xml('word/styles.xml'))
        self.lists = self._lists(package.xml('word/numbering.xml'))
        self.counts, self.restarted = {}, set()
        self.left_out = set()           # the styles of the lines units() left out because Word writes them
        self.tocs = {}                  # unit -> the body paragraphs after it that units() left out, lines among them
        self.notes = []                 # sentences for the user about what could not be done as asked
        self.body = next((c for c in root if c.tag == q('body')), None)

    # ---- styles and numbering ------------------------------------------------------------------------------------
    @staticmethod
    def _part(raw):
        if raw is None:
            return ()
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            raise ValueError(DAMAGED) from None

    def _styles(self, raw):
        """{style id: (name in lower case, the id it is based on, its list id, its list level)}"""
        styles = {}
        for style in self._part(raw):
            if _local(style.tag) == 'style':
                num = _kid(_kid(style, 'pPr'), 'numPr')
                styles[_value(style, 'styleId')] = ((_value(_kid(style, 'name')) or '').strip().lower(),
                                                    _value(_kid(style, 'basedOn')), _value(_kid(num, 'numId')),
                                                    _value(_kid(num, 'ilvl')))
        return styles

    def _lists(self, raw):
        """{numId: (the definition it counts with, {level: (format, first number)}, {level: restart at})}"""
        def level(lvl):
            return _value(_kid(lvl, 'numFmt')) or 'decimal', _int(_value(_kid(lvl, 'start')), 1)
        defined, lists = {}, {}
        for el in self._part(raw):
            kind = _local(el.tag)
            if kind == 'abstractNum':
                defined[_value(el, 'abstractNumId')] = {_int(_value(lvl, 'ilvl')): level(lvl)
                                                        for lvl in el if _local(lvl.tag) == 'lvl'}
            elif kind == 'num':
                base = _value(_kid(el, 'abstractNumId'))
                levels, restart = dict(defined.get(base, {})), {}
                for over in el:
                    if _local(over.tag) == 'lvlOverride':
                        ilvl = _int(_value(over, 'ilvl'))
                        if _kid(over, 'lvl') is not None:
                            levels[ilvl] = level(_kid(over, 'lvl'))
                        if _kid(over, 'startOverride') is not None:
                            restart[ilvl] = _int(_value(_kid(over, 'startOverride')), 1)
                lists[_value(el, 'numId')] = (base, levels, restart)
        return lists

    def properties(self, p):
        q = self.q
        for child in p:
            if child.tag == q('pPr'):
                return child
            if child.tag.__class__ is str:
                break
        return None

    def marker(self, p):
        """('## ', '#', None, 2) for a heading, ('  3. ', '1', list id, 1) or ('- ', '-', list id, 0) for a list
        item, ('', '', None, 0) for anything else: the marker, its kind, the list, the level.
        It counts the list items it is shown, so it wants every body paragraph once, in document order."""
        props = self.properties(p)
        if props is None:
            return '', '', None, 0
        q = self.q
        val = q('val')
        style = num = level = None
        for child in props:
            if child.tag == q('pStyle'):
                style = child.get(val)
            elif child.tag == q('numPr'):
                for item in child:
                    if item.tag == q('numId'):
                        num = item.get(val)
                    elif item.tag == q('ilvl'):
                        level = item.get(val)
        styles = self.styles
        heading = _HEADINGS.get(styles[style][0], 0) if style in styles else 0
        inherited, hops = style, 0
        while (num is None or level is None) and inherited in styles and hops < 12:
            _, inherited, style_num, style_level = styles[inherited]
            num = style_num if num is None else num
            level = style_level if level is None else level
            hops += 1
        found = ('', '', None, 0)
        if num is not None and num != '0':          # 0 is how a paragraph opts out of its style's list
            level = max(0, min(_int(level), 8))
            # Word counts per definition, not per numId: two lists that share a definition run on as one unless
            # the second says where to restart.  A numId nothing defines is numbered 1, 2, 3 as Word shows it.
            base, levels, restart = self.lists.get(num) or (None, {}, {})
            fmt, first = levels.get(level, ('decimal', 1))
            counts = self.counts.setdefault(base if base is not None else ('numId', num), {})
            if level in restart and (num, level) not in self.restarted:
                self.restarted.add((num, level))
                counts[level] = restart[level] - 1
            counts[level] = counts.get(level, first - 1) + 1
            for deeper in [k for k in counts if k > level]:
                del counts[deeper]                  # a sub-list starts again under each new item
            if fmt == 'bullet':
                found = '  ' * level + '- ', '-', num, level
            elif fmt != 'none':
                found = '  ' * level + '%d. ' % counts[level], '1', num, level
        if heading:
            return '#' * heading + ' ', '#', None, heading
        return found

    def style_name(self, p):
        """The name of p's own paragraph style, in lower case, or ''."""
        props = self.properties(p)
        if props is not None:
            style = self.q('pStyle')
            for child in props:
                if child.tag == style:
                    found = self.styles.get(child.get(self.q('val')))
                    return found[0] if found else ''
        return ''

    def generated(self, p):
        """The name of p's own paragraph style when p is a line Word fills in from a TOC field, else ''.  A style
        based on "toc 1" is the author's, and is not followed."""
        name = self.style_name(p)
        return name if _GENERATED.match(name) else ''

    # ---- text ----------------------------------------------------------------------------------------------------
    def atoms(self, p, marks=None):
        """The pieces of a paragraph's text, in order, as [element, text, wrappers].  A w:t holds its text; a tab, a
        line break and a non-breaking hyphen stand for one character each.  `wrappers` are the elements between the
        paragraph and the run (a hyperlink, a tracked insertion, a content control) and the complex fields the piece
        lies in: it says where newly typed text does not belong.  Given a list as `marks`, it adds (mark, how many
        pieces come before it) for each footnote, endnote and comment mark."""
        kinds, out, fields = self.kind, [], []
        run, break_type = self.q('r'), self.q('type')
        anchored = self.anchored

        def walk(el, wrappers):
            for child in el:
                tag = child.tag
                kind = kinds.get(tag)
                if kind is None:
                    if marks is not None and tag in anchored:
                        marks.append((child, len(out)))
                    if len(child):
                        walk(child, wrappers if tag == run else wrappers + (child,))
                elif kind == _TEXT:
                    text = (child.text or '') if not len(child) else _own_text(child)
                    out.append([child, text, wrappers + tuple(fields) if fields else wrappers])
                elif kind == 0:
                    continue
                elif kind == _FIELD:
                    state = _value(child, 'fldCharType')
                    if state == 'begin':
                        fields.append(child)
                    elif state == 'end' and fields:
                        fields.pop()
                elif kind == _BREAK and child.get(break_type) in ('page', 'column'):
                    continue
                else:
                    out.append([child, '\t' if kind == _TAB else '-' if kind == _HYPHEN else '\n',
                                wrappers + tuple(fields) if fields else wrappers])
        walk(p, ())
        return out

    def text(self, p):
        return ''.join([atom[1] for atom in self.atoms(p)]) if len(p) else ''

    def unseen(self, p):
        """True for a paragraph whose text runs around something that is not text: an equation, an embedded object
        (an old-style or MathType equation), or a character from a symbol font.  None of them is in the text shown
        for rewriting, so new words typed beside one would only repeat what it already shows."""
        kinds, sym, obj = self.kind, self.q('sym'), self.q('object')

        def walk(el):
            for child in el:
                tag = child.tag
                if tag.__class__ is not str:
                    continue
                if tag == sym or tag == obj or _local(tag) in ('oMath', 'oMathPara'):
                    return True
                if kinds.get(tag) != 0 and len(child) and walk(child):
                    return True
            return False
        return walk(p)

    def single(self, el):
        """True for a content control that holds one paragraph at most: one bound to a document property or other
        data (w:dataBinding), or a plain-text one."""
        if el.tag != self.q('sdt'):
            return False
        props = _kid(el, 'sdtPr')
        return props is not None and (_kid(props, 'dataBinding') is not None or _kid(props, 'text') is not None)

    def bound(self, p, real=None):
        """The stretches of p's text, as (start, end) in the text of `real` (p's atoms that have text), that lie in
        a content control bound to a document property: all of it when such a control holds p itself."""
        if real is None:
            real = [a for a in self.atoms(p) if a[1]]
        sdt = self.q('sdt')

        def tied(el):
            props = _kid(el, 'sdtPr') if el.tag == sdt else None
            return props is not None and _kid(props, 'dataBinding') is not None
        up = p.up
        while up is not None:
            if tied(up):
                return [(-1, sum(len(a[1]) for a in real) + 1)]     # every edit, at either end too, is inside
            up = up.up
        out, at = [], 0
        for _, text, wrappers in real:
            if any(tied(w) for w in wrappers):
                if out and out[-1][1] == at:
                    out[-1] = (out[-1][0], at + len(text))
                else:
                    out.append((at, at + len(text)))
            at += len(text)
        return out

    def units(self):
        """The blocks of the body in document order.  The lines of a table of contents are left out, and counted in
        left_out: Word writes them from the headings, and they are never offered, aligned or changed.  The body
        paragraphs between a unit and the next, when such lines are among them, are kept in tocs under that unit."""
        q, skip = self.q, self.no_blocks
        P, TBL, TR, TC = q('p'), q('tbl'), q('tr'), q('tc')
        out, gap, toc = [], [], False
        self.left_out, self.tocs = set(), {}

        def add(unit):
            nonlocal gap, toc
            if toc and out:
                self.tocs[out[-1]] = gap
            gap, toc = [], False
            out.append(unit)

        def block(el, cell):
            nonlocal toc
            for child in el:
                tag = child.tag
                if tag == P:
                    marker, kind, num, depth = self.marker(child)     # first, so that lists count as they did
                    if cell is not None:
                        cell.append(child)                          # a cell's paragraphs are all kept
                    else:
                        text = self.text(child)
                        if not text or text.isspace():
                            gap.append(child)
                            continue
                        style = self.generated(child)
                        if style:
                            self.left_out.add(style)
                            gap.append(child)
                            toc = True
                            continue
                        add(_Unit(text, p=child, marker=marker, kind=kind, num=num, depth=depth))
                elif tag == TBL:
                    table(child, child, cell)
                elif tag.__class__ is str and tag not in skip and len(child):
                    block(child, cell)

        def table(el, top, cell):
            for child in el:
                tag = child.tag
                if tag == TR:
                    cells = [] if cell is None else None
                    row(child, cells, cell)
                    if cells is not None:
                        texts = ['\n'.join([self.text(p) for p in paras]) for paras in cells]
                        if any(t and not t.isspace() for t in texts):
                            add(_Unit(' | '.join(texts), tr=child, table=top, cells=cells))
                elif tag.__class__ is str and tag not in skip and len(child):
                    table(child, top, cell)

        def row(el, cells, cell):
            for child in el:
                tag = child.tag
                if tag == TC:
                    if cells is None:
                        block(child, cell)          # a table inside a cell: its paragraphs belong to that cell
                    else:
                        paras = []
                        block(child, paras)
                        cells.append(paras)
                elif tag.__class__ is str and tag not in skip and len(child):
                    row(child, cells, cell)
        if self.body is not None:
            block(self.body, None)
        if toc and out:
            self.tocs[out[-1]] = gap
        return out

    def fields(self, p):
        """How many complex fields p opens, less how many it closes."""
        kinds, fld = self.kind, self.q('fldChar')

        def walk(el):
            n = 0
            for child in el:
                if child.tag == fld:
                    state = _value(child, 'fldCharType')
                    n += (state == 'begin') - (state == 'end')
                elif kinds.get(child.tag) != 0 and len(child):
                    n += walk(child)
            return n
        return walk(p)

    def heads(self, p, gap, others):
        """True when p is the title of the table of contents whose lines are in gap: it is styled "TOC Heading", or
        it is in the content control that holds them and none of the units beside p, in others (one around the
        whole body is not the table's)."""
        if p is None:
            return False
        if self.style_name(p) == 'toc heading':
            return True
        box = next(el for el in gap if self.generated(el)).up
        while box is not None and box.tag != self.q('sdt'):
            box = box.up

        def inside(el):
            while el is not None and el is not box:
                el = el.up
            return box is not None and el is box
        return inside(p) and not any(inside(u.p if u.p is not None else u.tr) for u in others)

    def toc_end(self, p, gap):
        """Of the paragraphs in gap, which follow p, the one a table of contents among them ends with: its last line,
        or the paragraph after it where its field ends.  p itself when the field ends further on, in a paragraph
        with text of its own: anything put before that paragraph would be in the field's result, which Word
        replaces when it updates the table."""
        last = max(k for k, el in enumerate(gap) if self.generated(el))
        depth = max(0, self.fields(p)) if p is not None else 0
        for k, el in enumerate(gap):
            depth += self.fields(el)
            if k >= last and depth <= 0:
                return el
        return p

    def outside(self, el, units):
        """el, or the outermost content control around it that holds none of these units (a table of contents is
        usually one, its title inside)."""
        sdt, content = self.q('sdt'), self.q('sdtContent')
        bounds = [u.p if u.p is not None else u.tr for u in units if u is not None]
        while el.up is not None and el.up.tag == content and el.up.up is not None and el.up.up.tag == sdt:
            box = el.up.up
            for bound in bounds:
                up = bound
                while up is not None and up is not box:
                    up = up.up
                if up is box:
                    return el
            el = box
        return el

    # ---- changing text -------------------------------------------------------------------------------------------
    def t(self, text):
        return _new(self.q('t'), text, {_SPACE: 'preserve'} if _keep_space(text) else None)

    def pieces(self, text):
        """New elements for a stretch of text: w:t for the words, w:br and w:tab for the characters that stand for
        them."""
        out = []
        for i, piece in enumerate(_SPECIAL.split(text)):
            if i % 2:
                out.append(_new(self.q('br' if piece == '\n' else 'tab')))
            elif piece:
                out.append(self.t(piece))
        return out

    def put(self, parent, index, nodes):
        _own(parent)
        for offset, node in enumerate(nodes):
            node.up = parent
            parent.insert(index + offset, node)

    @staticmethod
    def index(parent, child):
        for i, other in enumerate(parent):
            if other is child:
                return i
        raise ValueError(DAMAGED)

    def splice(self, t, start, end, new):
        """Replace characters start:end of a w:t.  Tabs and line breaks in the new text become elements after it."""
        if len(t):                                  # a comment inside the text: keep it, after the text
            t.text = _own_text(t)
            for child in t:
                child.tail = None
        old = t.text or ''
        parts = _SPECIAL.split(new) if ('\n' in new or '\t' in new) else None
        if parts is None:
            t.text = old[:start] + new + old[end:]
        else:
            t.text = old[:start] + parts[0]
            rest = self.pieces(new[len(parts[0]):])
            if old[end:]:
                if rest[-1].tag == t.tag:
                    rest[-1].text += old[end:]
                    if _keep_space(rest[-1].text):
                        rest[-1].set(_SPACE, 'preserve')
                else:
                    rest.append(self.t(old[end:]))
            self.put(t.up, self.index(t.up, t) + 1, rest)
        if _keep_space(t.text):
            t.set(_SPACE, 'preserve')
        t.st = 2
        _touch(t.up)

    def run_like(self, source, text):
        """A new run holding text, formatted like the run `source` (or like the paragraph mark, given a w:pPr)."""
        q = self.q
        run = _new(q('r'))
        props = None
        if source is not None:
            props = next((c for c in source if c.tag == q('rPr')), None)
        if props is not None and len(props):
            copy = self.copy(props)
            copy.up = run
            run.append(copy)
        for node in self.pieces(text):
            node.up = run
            run.append(node)
        return run

    def copy(self, el, leave=None):
        """A copy of a properties element, without what belongs to the one paragraph (or row) it came from."""
        leave = leave or self.not_copied
        twin = _new(el.tag, None, dict(el.attrib))
        twin.ns = el.ns
        for child in el:
            if child.tag.__class__ is str and child.tag not in leave:
                kid = self.copy(child, leave)
                kid.up = twin
                twin.append(kid)
        if el.text and not el.text.isspace():
            twin.text = el.text
        return twin

    def fields_with_text(self, atoms):
        simple = self.q('fldSimple')
        return set(w for _, text, wrappers in atoms if text for w in wrappers
                   if w.tag == simple or self.kind.get(w.tag) == _FIELD)

    def field_texts(self, atoms):
        """{field: the text it shows} for the fields a paragraph's text lies in."""
        simple, out = self.q('fldSimple'), {}
        for _, text, wrappers in atoms:
            for w in wrappers:
                if w.tag == simple or self.kind.get(w.tag) == _FIELD:
                    out[w] = out.get(w, '') + text
        return out

    def standing(self, real):
        """For each character of a paragraph's text, what it would lose by being typed again somewhere else:
        2 inside a link, a tracked insertion, a content control or a field, 1 in a run formatted differently from
        most of the paragraph, 0 otherwise.  None when the whole paragraph is 0."""
        q = self.q
        props, text_tag = q('rPr'), q('t')
        looks, weight, seen = [], Counter(), {}
        for el, text, wrappers in real:
            run = el.up
            look = seen.get(id(run))
            if look is None:
                look = seen[id(run)] = _signature(next((c for c in run if c.tag == props), None))
            looks.append(look)
            if not wrappers and el.tag == text_tag:
                weight[look] += len(text)
        usual = max(weight, key=weight.get) if weight else None
        out, special = [], False
        for (el, text, wrappers), look in zip(real, looks):
            rank = 2 if wrappers else 0 if look == usual else 1
            special = special or rank > 0
            out.extend([rank] * len(text))
        return out if special else None

    def beside(self, atoms, marks):
        """{mark: the word it belongs to, in lower case} for the marks atoms() found, '' where there is none."""
        starts, at = [], 0
        for atom in atoms:
            starts.append(at)
            at += len(atom[1])
        starts.append(at)
        text = ''.join([atom[1] for atom in atoms])
        out = {}
        for el, k in marks:
            at = starts[k]
            if self.anchored[el.tag] < 0:
                words = _WORD.findall(text, max(0, at - 200), at)
                out[el] = words[-1].lower() if words else ''
            else:
                word = _WORD.search(text, at, at + 200)
                out[el] = word.group().lower() if word else ''
        return out

    def accept(self, p):
        """Accept the tracked insertions, deletions and moves in a paragraph's text.  True when it had any.
        Properties (a tracked paragraph mark or format change) and text boxes are not gone into, and stay as they
        are.  A move whose ranges name it is accepted in the other paragraph too (see unmove)."""
        q, kinds = self.q, self.kind
        keep, drop = (q('ins'), q('moveTo')), (q('del'), q('moveFrom'))
        halves = (q('moveFrom'), q('moveTo'))
        moves = self.moves() if any(el.tag in halves for el in p.iter()) else {}
        accepted = set()

        def walk(el):
            kids, here, below = [], False, False
            for child in el:
                tag = child.tag
                if tag in drop:
                    here = True
                    accepted.update(moves.get(child, ()))
                elif tag in keep:
                    here = True
                    accepted.update(moves.get(child, ()))
                    walk(child)
                    kids.extend(child)
                else:
                    if kinds.get(tag) != 0 and len(child):
                        below = walk(child) or below
                    kids.append(child)
            if here:
                _own(el)
                for kid in kids:
                    kid.up = el
                el[:] = kids
            return here or below
        done = walk(p)
        if accepted:
            self.unmove(accepted, moves)
        return done

    def moves(self):
        """{each half of a tracked move in the document: the names of the move ranges it lies in}.  A move is two
        halves, the words where they were (moveFrom) and where they went (moveTo), each in a range of its own that
        bears the move's name."""
        q = self.q
        ends = {q('moveFromRangeStart'): q('moveFromRangeEnd'), q('moveToRangeStart'): q('moveToRangeEnd')}
        within = {q('moveFrom'): q('moveFromRangeEnd'), q('moveTo'): q('moveToRangeEnd')}
        open_, out = {}, {}
        for el in self.root.iter():
            tag = el.tag
            if tag in ends:
                open_[(ends[tag], el.get(q('id')))] = el.get(q('name'))
            elif tag in within:
                out[el] = frozenset(name for (end, _), name in open_.items() if end == within[tag] and name)
            elif tag in within.values():
                open_.pop((tag, el.get(q('id'))), None)
        return out

    def unmove(self, names, moves):
        """A move with a half accepted in a rewritten paragraph is accepted whole, and its range markers go.  Left
        as it was, Word still pairs the halves up, and its Reject All would put the new wording back where the words
        came from."""
        q = self.q
        starts = (q('moveFromRangeStart'), q('moveToRangeStart'))
        ids = {(el.tag, el.get(q('id'))) for el in self.root.iter() if el.tag in starts and el.get(q('name')) in names}
        marks = ids | {(q('moveFromRangeEnd') if tag == starts[0] else q('moveToRangeEnd'), at) for tag, at in ids}
        gone = [el for el in self.root.iter() if (el.tag, el.get(q('id'))) in marks]
        gone += [el for el, held in moves.items() if held & names and el.up is not None and el in list(el.up)]
        for el in gone:
            parent = el.up
            _own(parent)
            kids = list(el) if el.tag == q('moveTo') else []        # moveTo: its words stay; moveFrom: they go
            for kid in kids:
                kid.up = parent
            at = list(parent).index(el)
            parent[at:at + 1] = kids

    def write(self, p, new):
        """Give a paragraph new text, changing as little of it as the difference between old and new allows."""
        marks = []
        atoms = self.atoms(p, marks)
        words = self.beside(atoms, marks) if marks else None
        real = [a for a in atoms if a[1]]
        old = ''.join([a[1] for a in real])
        edits = _diff(old, new, self.standing(real) if len(real) > 1 else None)
        if not edits:
            return
        tied = self.bound(p, real) if real else ()
        if tied and any(start < hi and end > lo if end > start else lo < start < hi
                        for start, end, _ in edits for lo, hi in tied):
            self.notes.append(BOUND % _short(old.strip()))
            return
        if self.accept(p):
            # Then once more from the start: the atoms and their wrappers are not what they were.
            self.notes.append(TRACKED % _short(new if new.strip() else old))
            return self.write(p, new)
        if not real:
            holder = next((a[0] for a in atoms), None)
            if holder is not None:                  # a w:t that is there but empty
                self.splice(holder, 0, 0, new)
            else:
                runs = [c for c in p if c.tag == self.q('r')]
                self.put(p, len(p), [self.run_like(runs[-1] if runs else self.properties(p), new)])
            return
        starts, at = [], 0
        for atom in real:
            starts.append(at)
            at += len(atom[1])
        text_tag = self.q('t')
        for start, end, new_text in reversed(edits):
            if end > start:
                first = bisect_right(starts, start) - 1
                last = bisect_right(starts, end - 1) - 1
                for i in range(last, first - 1, -1):
                    el, text, _ = real[i]
                    lo, hi = max(start, starts[i]) - starts[i], min(end, starts[i] + len(text)) - starts[i]
                    piece = new_text if i == first else ''
                    if el.tag == text_tag:
                        self.splice(el, lo, hi, piece)
                    else:                           # a tab or a line break goes; what replaces it stands in its place
                        run = el.up
                        where = self.index(run, el)
                        _own(run)
                        run.remove(el)
                        self.put(run, where, self.pieces(piece))
                continue
            i = bisect_right(starts, start) - 1
            if starts[i] < start < starts[i] + len(real[i][1]):
                self.splice(real[i][0], start - starts[i], start - starts[i], new_text)
                continue
            # Between two pieces.  New text takes the formatting of the run before it (of the run after it at the
            # start of the paragraph), but it does not join a hyperlink, a field result, a tracked insertion or a
            # content control that merely ends there.
            if starts[i] == start:
                before, after = (real[i - 1] if i else None), real[i]
            else:
                before, after = real[i], (real[i + 1] if i + 1 < len(real) else None)
            pick, side = (before, 1) if before is not None else (after, 0)
            if before is not None and after is not None:
                inner, outer = before[2], after[2]
                if len(outer) < len(inner) and inner[:len(outer)] == outer:
                    pick, side = after, 0
            elif pick[2] and self.kind.get(pick[2][0].tag) != _FIELD and pick[2][0].up is p:
                # The paragraph begins or ends inside a wrapper: the new text gets a run of its own outside it.
                plain = [a for a in (real if side == 0 else reversed(real)) if not a[2] and a[0].tag == text_tag]
                source = plain[0][0].up if plain else None
                self.put(p, self.index(p, pick[2][0]) + side, [self.run_like(source, new_text)])
                continue
            el = pick[0]
            if el.tag == text_tag:
                where = len(pick[1]) if side else 0
                self.splice(el, where, where, new_text)
            else:
                self.put(el.up, self.index(el.up, el) + side, self.pieces(new_text))
        if not new:
            return
        marks = [] if words else None
        now = self.atoms(p, marks)
        where = _short(new)                 # the paragraph as it now reads, which is what can be found in Word
        if words and self.beside(now, marks) != words:
            self.notes.append(MARK_MOVED % where)
        emptied = self.fields_with_text(real) - self.fields_with_text(now)
        if emptied:
            # A field whose words are all gone is taken out, as a link is.  Left in, it shows its result again the
            # next time Word works it out (a date as soon as the file opens), run into the words on either side.
            spans = [self.field_runs(w) for w in emptied]
            gone = set(el for span in spans if span for el in span)
            for parent in set(el.up for el in gone):
                _own(parent)
                parent[:] = [child for child in parent if child not in gone]
            if gone:
                self.notes.append(FIELD_GONE % where)
            if None in spans:
                # One that cannot be taken out whole (it spans paragraphs, or it is a form field) is left, and
                # what it shows can only be taken out until Word next works the field out again.
                self.notes.append('The text of a field was taken out, but the field is still there and Word may '
                                  'fill it in again when it updates its fields: "%s".' % where)
        shown, shows = self.field_texts(real), self.field_texts(now)
        if any(shows.get(w, '').split() and shows[w].split() != text.split() for w, text in shown.items()):
            self.notes.append('Words were written where a field (a date, a cross-reference) shows its result, and '
                              'Word will replace them when it updates the field: "%s".' % where)
        link = self.q('hyperlink')
        had = set(w for _, text, wrappers in real if text for w in wrappers if w.tag == link)
        if had:
            lost = had - set(w for _, text, wrappers in now if text for w in wrappers if w.tag == link)
            hollow = set(el for el in lost if el.up is not None and self.hollow(el))
            for parent in set(el.up for el in hollow):      # each parent once, however many links it lost
                _own(parent)
                parent[:] = [child for child in parent if child not in hollow]
            if lost:
                self.notes.append('A link could not be kept, because the words it was on were all rewritten or '
                                  'moved: "%s".' % where)

    def hollow(self, el):
        """True for a link that holds nothing but runs with no content left in them."""
        q = self.q
        run, props, text = q('r'), q('rPr'), q('t')
        for child in el:
            if child.tag != run:
                return False
            for inner in child:
                if inner.tag != props and not (inner.tag == text and not inner.text and not len(inner)):
                    return False
        return True

    def field_runs(self, w):
        """What to take out for a field that shows no text any more: a w:fldSimple that holds only empty runs, or
        the runs of a complex field from its begin to its end (nested fields with it), when they stand side by side
        and hold nothing but the field.  None for any other field: one that spans paragraphs, or a form field,
        whose begin holds what was typed in it."""
        q = self.q
        if w.tag == q('fldSimple'):
            return [w] if w.up is not None and self.hollow(w) else None
        start, fld, text, run = w.up, w.tag, q('t'), q('r')
        parent = start.up if start is not None else None
        if parent is None or start.tag != run:
            return None
        only = (q('rPr'), q('instrText'), q('lastRenderedPageBreak'))
        out, depth = [], 0
        for child in parent[self.index(parent, start):]:
            if child.tag != run:
                if any(el.tag == fld for el in child.iter()):
                    return None
                continue                            # a bookmark or a proofing mark stays where it is
            for el in child:
                if el.tag not in only and not (el.tag in (fld, text) and not el.text and not len(el)):
                    return None
                if el.tag == fld:
                    state = _value(el, 'fldCharType')
                    depth += (state == 'begin') - (state == 'end')
            out.append(child)
            if depth <= 0:
                return out
        return None

    def set_text(self, p, new):
        """New text for a paragraph.  White space at the two ends of a paragraph belongs to the document, not to
        the rewrite: it is kept as it was.  Returns True when the paragraph changed."""
        old = self.text(p)
        core = old.strip()
        new = new.strip()
        if new == core:
            return False
        if new.split() != core.split() and self.unseen(p):
            self.notes.append(UNSEEN % _short(core))
            return False
        if new:
            lead = old[:len(old) - len(old.lstrip())]
            new = lead + new + old[len(lead) + len(core):]
        self.write(p, new)
        return True

    # ---- adding and removing paragraphs --------------------------------------------------------------------------
    def paragraph_like(self, model, text):
        """A new paragraph with the paragraph properties of `model` and, for its text, the character formatting most
        of the model's own text has.  Nothing else of the model is copied: no picture, field, note or bookmark."""
        q = self.q
        p = _new(q('p'))
        if model is not None:
            props = self.properties(model)
            if props is not None:
                copy = self.copy(props)
                if len(copy):
                    copy.up = p
                    p.append(copy)
            weight, best = Counter(), {}
            for el, text_, wrappers in self.atoms(model):
                if el.tag == q('t') and text_ and not wrappers:
                    key = _signature(next((c for c in el.up if c.tag == q('rPr')), None))
                    weight[key] += len(text_)
                    best.setdefault(key, el.up)
            source = best[max(weight, key=weight.get)] if weight else None
        else:
            source = None
        if text:
            run = self.run_like(source, text)
            run.up = p
            p.append(run)
        return p

    def row_like(self, unit, pieces):
        """A new table row modelled on the row of `unit`, with one piece of text per cell; None when that row is
        not plain enough to copy: a merged cell, a cell that holds anything but one paragraph, something wrapped
        round a cell.  Only the properties of the row, of its cells and of their paragraphs are copied."""
        q, leave = self.q, self.not_copied_row
        cell_tag, cell_props = q('tc'), q('tcPr')
        row_props = (q('trPr'), q('tblPrEx'))
        merges = (q('vMerge'), q('hMerge'))
        cells = [c for c in unit.tr if c.tag == cell_tag]
        if len(cells) != len(pieces) or len(cells) != len(unit.cells):
            return None
        row = _new(q('tr'))
        for child in unit.tr:
            if child.tag in row_props:
                twin = self.copy(child, leave)
                twin.up = row
                row.append(twin)
            elif child.tag.__class__ is str and child.tag != cell_tag:
                return None
        for cell, paras, piece in zip(cells, unit.cells, pieces):
            if len(paras) != 1:
                return None
            twin = _new(cell_tag)
            for child in cell:
                if child.tag == cell_props:
                    if any(c.tag in merges for c in child):
                        return None
                    props = self.copy(child, leave)
                    props.up = twin
                    twin.append(props)
                elif child.tag.__class__ is str and child is not paras[0]:
                    return None
            p = self.paragraph_like(paras[0], piece.strip())
            p.up = twin
            twin.append(p)
            twin.up = row
            row.append(twin)
        return row

    def removable(self, p):
        """True when the paragraph holds nothing but text and what means nothing without it: no picture, text box,
        note or comment reference, content control, tracked deletion or page break, no section break, and no half
        of a bookmark or of a field."""
        q, plain = self.q, self.plain
        props, section, skipped = q('pPr'), q('sectPr'), (q('rPr'), q('instrText'), q('smartTagPr'))
        start, stop, br, kind = q('bookmarkStart'), q('bookmarkEnd'), q('br'), q('type')
        field, simple = q('fldChar'), q('fldSimple')
        marks, open_fields = Counter(), [0]

        def plain_inside(el):
            for child in el:
                tag = child.tag
                if tag.__class__ is not str or tag in skipped:
                    continue
                if tag == props:
                    if any(c.tag == section for c in child):
                        return False
                elif tag == start:
                    marks[_value(child, 'id')] += 1
                elif tag == stop:
                    marks[_value(child, 'id')] -= 1
                elif tag == br:
                    if child.get(kind) in ('page', 'column'):
                        return False
                elif tag == field:
                    state = _value(child, 'fldCharType')
                    if state == 'begin':
                        open_fields[0] += 1
                    elif state == 'end':
                        open_fields[0] -= 1
                        if open_fields[0] < 0:
                            return False
                elif tag in plain or tag == simple:
                    if not plain_inside(child):
                        return False
                else:
                    return False
            return True
        return plain_inside(p) and not open_fields[0] and not any(marks.values())

    def clear(self, p):
        atoms = [a for a in self.atoms(p) if a[1]]
        if atoms:
            self.write(p, '')

    def merged_away(self, p):
        """True for a paragraph in a table cell that continues a vertical merge: Word shows nothing of that cell."""
        q = self.q
        cell = p.up
        props = next((c for c in cell if c.tag == q('tcPr')), None) if cell is not None and cell.tag == q('tc') else None
        merge = next((c for c in props if c.tag == q('vMerge')), None) if props is not None else None
        return merge is not None and merge.get(q('val'), 'continue') != 'restart'

    def serialise(self, pure=False):
        return _Writer(self.src, self.root, self.prefixes, pure).document()


def _signature(el):
    """An element's content as something that can be compared: used to tell one character formatting from another."""
    if el is None:
        return ()
    return (el.tag, tuple(sorted(el.attrib.items())), tuple(_signature(c) for c in el if c.tag.__class__ is str))


def _keep_space(text):
    return text[:1].isspace() or text[-1:].isspace() or '  ' in text


_SPECIAL = re.compile(r'([\n\t])')
_UNSAFE = re.compile(r'[^\t\n\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')


def _clean(text):
    """A block as it will be written: line ends as \\n, and nothing XML cannot hold."""
    if '\r' in text:
        text = text.replace('\r\n', '\n').replace('\r', '\n')
    return _UNSAFE.sub('', text)


# ====================================================================================================================
# Differences
# ====================================================================================================================
# A word, a run of spaces, or one other character.  Scripts written without spaces count character by character.
_TOKEN = re.compile(r'[^\W\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U0003ffff]+|[^\S\n\t]+|[\s\S]')
_SMALL = 90_000                                         # what difflib is given: lists of up to 300 x 300 items


def _gap(token):
    return token.isspace() and token != '\n' and token != '\t'


def _matches(a, b, patience=256, depth=0, rank=None):
    """The stretches two lists have in common, as (i, j, size) in order.

    The lists are first cut at the items that occur exactly once in each and come in the same order in both (a
    rare word is a better signpost than the longest run of common ones), and each piece between two such anchors
    is treated the same way.  A piece with no anchor goes to difflib if it is small.  A large one gets the shortest
    edit script when its two sides differ in at most `patience` places, and is otherwise given up as wholly
    different: nothing here can run away with a paragraph or a document of any length."""
    n, m = len(a), len(b)
    head, top = 0, min(n, m)
    while head < top and a[head] == b[head]:
        head += 1
    tail, top = 0, top - head
    while tail < top and a[n - 1 - tail] == b[m - 1 - tail]:
        tail += 1
    out = [(0, 0, head)] if head else []
    if head + tail < n and head + tail < m:
        a2, b2 = a[head:n - tail], b[head:m - tail]
        rank2 = rank[head:n - tail] if rank else None
        anchors = _anchors(a2, b2, rank2) if depth < 12 else []
        if anchors:
            found, i0, j0 = [], 0, 0
            for i, j in anchors + [(len(a2), len(b2))]:
                inner = _matches(a2[i0:i], b2[j0:j], patience, depth + 1, rank2[i0:i] if rank2 else None)
                found.extend((i0 + x, j0 + y, size) for x, y, size in inner)
                if i < len(a2):
                    found.append((i, j, 1))
                i0, j0 = i + 1, j + 1
        elif len(a2) * len(b2) <= _SMALL:
            found = [x for x in difflib.SequenceMatcher(None, a2, b2, autojunk=False).get_matching_blocks() if x[2]]
        else:
            found = _shortest_script(a2, b2, patience) or []
        out.extend((head + x, head + y, size) for x, y, size in found)
    if tail:
        out.append((n - tail, m - tail, tail))
    return out


def _shortest_script(a, b, limit):
    """Myers' greedy algorithm, given up (None) once the two lists turn out to differ in more than `limit` places
    or the search has cost more than it should."""
    n, m = len(a), len(b)
    budget = min(8 * (n + m) + limit * limit, 1500000)
    zero = limit + 1
    reach, trace = [0] * (2 * zero + 1), []        # reach[zero + k]: how far along diagonal k the search has got
    for d in range(limit + 1):
        trace.append(array('i', reach[zero - d:zero + d + 1]))
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and reach[zero + k - 1] < reach[zero + k + 1]):
                x = reach[zero + k + 1]
            else:
                x = reach[zero + k - 1] + 1
            y = x - k
            first = x
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            reach[zero + k] = x
            budget -= x - first + 1
            if x >= n and y >= m:
                out, x, y = [], n, m
                for back in range(d, 0, -1):        # walk the script backwards, collecting the equal stretches
                    before, k = trace[back], x - y
                    if k == -back or (k != back and before[back + k - 1] < before[back + k + 1]):
                        px = before[back + k + 1]
                        py, sx = px - k - 1, px
                    else:
                        px = before[back + k - 1]
                        py, sx = px - k + 1, px + 1
                    if x > sx:
                        out.append((sx, sx - k, x - sx))
                    x, y = px, py
                if x:
                    out.append((0, 0, x))
                out.reverse()
                return out
        if budget < 0:
            return None
    return None


def _anchors(a, b, rank=None):
    """Tokens that occur exactly once in each list, kept where they come in the same order in both.

    When the order differs (a clause moved to the front of its sentence) only one side of the move can stay where
    it is, and the other is typed again.  `rank` says, for each token of a, what it would lose by that: words that
    carry a link or a tracked change stay put before words with formatting of their own, and those before plain
    words, which lose nothing.  Without rank, or when no anchor has any, the longest run of anchors is kept."""
    count_a, count_b = Counter(a), Counter(b)
    where = {t: j for j, t in enumerate(b) if count_b[t] == 1 and count_a.get(t) == 1}
    pairs = [(i, where[t]) for i, t in enumerate(a) if t in where]
    if rank and any(rank[i] for i, _ in pairs):
        return _heaviest(pairs, rank)
    ends, chain, link = [], [], []             # the longest run of pairs whose places in b also go up
    for n, (_, j) in enumerate(pairs):
        at = bisect_left(ends, j)
        if at == len(ends):
            ends.append(j)
            chain.append(n)
        else:
            ends[at] = j
            chain[at] = n
        link.append(chain[at - 1] if at else -1)
    out, n = [], chain[-1] if chain else -1
    while n >= 0:
        out.append(pairs[n])
        n = link[n]
    out.reverse()
    return out


def _heaviest(pairs, rank):
    """Of the anchor pairs (i, j), in order of i, the chain with rising j that keeps the most words of the highest
    rank, then the most of the next, then the most plain ones.  A Fenwick tree over j: n log n steps."""
    size = len(pairs)
    scale = (1, size + 1, (size + 1) * (size + 1))
    order = sorted(range(size), key=lambda n: pairs[n][1])
    place = [0] * size
    for r, n in enumerate(order):
        place[n] = r + 1
    best, who = [0] * (size + 1), [-1] * (size + 1)
    total, link = [0] * size, [-1] * size
    for n, (i, _) in enumerate(pairs):
        r, most, prev = place[n] - 1, 0, -1
        while r > 0:
            if best[r] > most:
                most, prev = best[r], who[r]
            r -= r & -r
        total[n], link[n] = most + scale[rank[i]], prev
        r = place[n]
        while r <= size:
            if best[r] < total[n]:
                best[r], who[r] = total[n], n
            r += r & -r
    out, n = [], max(range(size), key=total.__getitem__)
    while n >= 0:
        out.append(pairs[n])
        n = link[n]
    out.reverse()
    return out


def _tokens(text):
    """A text as words, stretches of white space (tabs and line breaks included, each stretch one token) and
    single other characters."""
    out = []
    for token in _TOKEN.findall(text):
        if out and token.isspace() and out[-1].isspace():
            out[-1] += token
        else:
            out.append(token)
    return out


def _diff(old, new, standing=None):
    """The edits that turn old into new: (start, end, replacement) on old's characters, in order, never touching.
    `standing` (see _Document.standing) ranks old's characters by what they would lose if they were typed again."""
    if old == new:
        return []
    a, b = _tokens(old), _tokens(new)
    at, total = [], 0
    for token in a:
        at.append(total)
        total += len(token)
    at.append(total)
    rank = [standing[k] for k in at[:-1]] if standing else None
    # White space is compared as white space, whatever it is made of: where both texts have some between the same
    # two words, the document keeps its own (a tab, a line break, a no-break space, two spaces).  That is what a
    # text box or a language model flattens without meaning to; only between words that changed is it rewritten.
    plain_a = [' ' if token.isspace() else token for token in a]
    plain_b = [' ' if token.isspace() else token for token in b]
    edits, i, j = [], 0, 0
    for x, y, size in _matches(plain_a, plain_b, rank=rank) + [(len(a), len(b), 0)]:
        if i < x or j < y:
            _pair(a[i:x], b[j:y], at[i], edits)
        i, j = x + size, y + size
    merged = []
    for edit in edits:
        if merged and merged[-1][1] == edit[0]:
            merged[-1] = (merged[-1][0], edit[1], merged[-1][2] + edit[2])
        else:
            merged.append(edit)
    return merged


def _pair(old, new, at, edits):
    """A stretch of old tokens that became a stretch of new ones.  Word for word, so that each new word takes the
    formatting of the word in whose place it stands; the last pair takes whatever is left over."""
    old_words = [k for k, t in enumerate(old) if not _gap(t)]
    new_words = [k for k, t in enumerate(new) if not _gap(t)]
    pairs = min(len(old_words), len(new_words))
    cuts = [(0, 0)]
    for k in range(pairs):
        cuts.append((old_words[k], new_words[k]))
        if k + 1 < pairs:
            cuts.append((old_words[k] + 1, new_words[k] + 1))
    cuts.append((len(old), len(new)))
    for (i0, j0), (i1, j1) in zip(cuts, cuts[1:]):
        before, after = ''.join(old[i0:i1]), ''.join(new[j0:j1])
        if before != after:
            edits.append((at, at + len(before), after))
        at += len(before)


def _lead(text):
    """(kind, length) of the list or heading marker a block opens with: '-', '1', '#', or '' for none."""
    n, i = len(text), 0
    while i < n and i < 24 and text[i] == ' ':
        i += 1
    if text.startswith('- ', i):
        return '-', i + 2
    j = i
    while j < n and j - i < 9 and '0' <= text[j] <= '9':
        j += 1
    if j > i and text.startswith('. ', j):
        return '1', j + 2
    j = i
    while j < n and j - i < 6 and text[j] == '#':
        j += 1
    if j > i and i < 4 and text.startswith(' ', j):
        return '#', j + 1
    return '', 0


def _strip_marker(text, kind, marker=None):
    """The block without the marker that a paragraph of this kind is shown with.  A paragraph that is neither a
    heading nor a list item has none: there a leading '- ' or '2. ' is the paragraph's own text."""
    if not kind:
        return text
    if marker and text.startswith(marker):
        return text[len(marker):]
    found, length = _lead(text)
    if found == kind or (found in ('-', '1') and kind in ('-', '1')):
        return text[length:]
    return text


def _key(text):
    """A block as it is compared when old and new blocks are lined up: its words, whatever its marker and
    however they are spaced."""
    return ' '.join(text[_lead(text)[1]:].split())


def _align(old, new):
    """Line the new blocks up with the old: [(old index or None, new index or None)] in document order."""
    n, m = len(old), len(new)
    straight = [(i, i) for i in range(min(n, m))]
    a, b = [_key(s) for s in old], [_key(s) for s in new]
    if n == m:
        same = sum(1 for x, y in zip(a, b) if x == y)
        if same >= n - 1:
            return straight
    found = _matches(a, b, 4096)
    if n == m and sum(size for _, _, size in found) <= same:
        return straight                 # block for block, unless that would rewrite blocks that only moved
    steps, i, j = [], 0, 0
    for x, y, size in found + [(n, m, 0)]:
        steps.extend(_pair_blocks(a, b, i, x, j, y))
        steps.extend((x + k, y + k) for k in range(size))
        i, j = x + size, y + size
    return steps


def _pair_blocks(a, b, i0, i1, j0, j1):
    """Between two stretches of unchanged blocks: which old block became which new one.  The same number on both
    sides pairs off in order.  Otherwise blocks are paired, in order, so that the pairs share as many of their
    words as possible, and the rest are blocks that went (old only) or blocks that were added (new only)."""
    rows, cols = i1 - i0, j1 - j0
    if rows == cols:
        return [(i0 + k, j0 + k) for k in range(rows)]
    # Only pairings near the diagonal are weighed: a block is looked for within `band` places of where it was.
    band = abs(rows - cols) + 12
    if not rows or not cols or rows * (2 * band + 1) > 600000:
        paired = min(rows, cols)
        return ([(i0 + k, j0 + k) for k in range(paired)] + [(i0 + k, None) for k in range(paired, rows)]
                + [(None, j0 + k) for k in range(paired, cols)])
    old_words = [frozenset(a[i0 + r].split()) for r in range(rows)]
    new_words = [frozenset(b[j0 + c].split()) for c in range(cols)]
    lo = [max(0, r - band) for r in range(rows + 1)]
    hi = [min(cols, r + band) for r in range(rows + 1)]
    score = [None] * rows + [[0.0] * (hi[rows] - lo[rows] + 1)]     # score[r][c - lo[r]]: the best from (r, c) on
    alike = [None] * rows                                           # alike[r][c - lo[r]]: 0 to 1, by shared words
    for r in range(rows - 1, -1, -1):
        below, low, high, words = score[r + 1], lo[r + 1], hi[r + 1], old_words[r]
        row, like = [0.0] * (hi[r] - lo[r] + 1), [0.0] * (hi[r] - lo[r] + 1)
        for c in range(hi[r], lo[r] - 1, -1):
            best = below[c - low] if low <= c <= high else -1.0
            if c < hi[r] and row[c + 1 - lo[r]] > best:
                best = row[c + 1 - lo[r]]
            if c < cols and low <= c + 1 <= high:
                total = len(words) + len(new_words[c])
                share = like[c - lo[r]] = 2.0 * len(words & new_words[c]) / total if total else 0.0
                if share and below[c + 1 - low] + share > best:
                    best = below[c + 1 - low] + share
            row[c - lo[r]] = best
        score[r], alike[r] = row, like
    out, r, c = [], 0, 0
    while r < rows or c < cols:
        if r < rows:
            here, share, below, low, high = score[r][c - lo[r]], alike[r][c - lo[r]], score[r + 1], lo[r + 1], hi[r + 1]
            if c < cols and share and low <= c + 1 <= high and here == below[c + 1 - low] + share:
                out.append((i0 + r, j0 + c))
                r, c = r + 1, c + 1
                continue
            if low <= c <= high and here == below[c - low]:
                out.append((i0 + r, None))
                r += 1
                continue
        out.append((None, j0 + c))
        c += 1
    return out


# ====================================================================================================================
# The two functions
# ====================================================================================================================
def _short(text, limit=60):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '\u2026'


def blocks(data: bytes) -> list[str]:
    """One string per body paragraph that has text, in document order, as the app shows it for rewriting.

    A heading opens with '# ' to '###### ' (by the name of its paragraph style: "Title" and "heading 1" are both
    '# '), a list item with '- ' or 'N. ', two spaces deeper per level.  A table row is one block, its cells joined
    with ' | ' (a cell of several paragraphs has them on separate lines).  A tab is a tab and a line break is a
    newline.  Left out: tracked deletions (tracked insertions are kept), field codes, text boxes, the lines of a
    table of contents, and everything that is not the body: headers, footers, footnotes, endnotes and comments.
    """
    return read(data)[0]


def read(data: bytes) -> tuple[list[str], list[str]]:
    """(the blocks as blocks() gives them, notes for the user about what they leave out).

    The notes so far are TOC_NOTE and FIGURES_NOTE, when a table of contents or of figures was left out.
    """
    try:
        doc = _Document(data)
        found = [unit.marker + unit.text for unit in doc.units()]
    except RecursionError:
        raise ValueError(DAMAGED) from None
    toc = any(style.startswith('toc') for style in doc.left_out)
    return found, [TOC_NOTE] * toc + [FIGURES_NOTE] * ('table of figures' in doc.left_out)


def rewrite(data: bytes, new_blocks: list[str]) -> tuple[bytes, list[str]]:
    """A copy of the document with its text replaced by new_blocks, which line up with blocks(data).

    A block that did not change leaves its paragraph exactly as it was.  A block that changed keeps its runs: the
    words that survive keep their formatting, a word that replaces another takes that word's formatting, and added
    words take the formatting of the text before them.  A block that is gone has its paragraph removed, or emptied
    where removing it would damage the layout; a block that is new becomes a paragraph styled like the one before
    it.  Returns the new file and a list of sentences about anything that could not be done as asked.
    """
    if not isinstance(new_blocks, (list, tuple)) or not all(isinstance(b, str) for b in new_blocks):
        raise ValueError(BAD_BLOCKS)
    try:
        return _rewrite(data, list(new_blocks))
    except RecursionError:
        raise ValueError(DAMAGED) from None


def _shape(text):
    """(kind, depth) of the marker a new block opens with: '#' and the heading level, '-' or '1' and the list
    level (two spaces a level), ('', 0) for none."""
    kind, length = _lead(text)
    if kind == '#':
        return kind, text[:length].count('#')
    if kind:
        return kind, (len(text) - len(text.lstrip(' '))) // 2
    return '', 0


def _around(i, n, reach=2000):
    """Block numbers near i, nearest first: a few ahead (what follows a heading or a list is usually body text),
    then back, then further ahead.  Never more than `reach` either way: a new block is not styled from afar."""
    for k in range(i + 1, min(i + 9, n)):
        yield k
    for k in range(min(i, n - 1), max(i - reach, -1), -1):
        yield k
    for k in range(i + 9, min(i + reach, n)):
        yield k


def _model(units, i, text, skip=()):
    """The block a new block is styled like (None: a plain paragraph in the default style).  The new block stands
    after units[i]; i is -1 before the first.  No block in `skip` is ever the model.

    The rule is "styled like the one before it", and the marker a new block arrives with says whether that is what
    it is.  With the marker of the block before it (always so when both are ordinary text) it is styled exactly
    like that block.  Otherwise the marker is believed.  A block with no marker, placed after a heading or a list
    item, is ordinary text: it is styled like the nearest ordinary paragraph, not as one more heading.  A marked
    block that does not match the block before it is styled like the block after it if that one matches (a new
    first item of a list), else like the nearest heading of that level or list item of that depth (of the same
    list, when a neighbour is in one).  A number with no list on either side stays as typed: joining a list
    elsewhere in the document would give it that list's count."""
    kind, depth = _shape(text)
    n = len(units)
    prev = units[i] if 0 <= i < n and units[i].p is not None and units[i] not in skip else None
    nxt = units[i + 1] if i + 1 < n and units[i + 1].p is not None and units[i + 1] not in skip else None

    def nearest(test):
        for k in _around(i, n):
            if units[k].p is not None and units[k] not in skip and test(units[k]):
                return units[k]
        return None
    if kind:
        for near in (prev, nxt):
            if near is not None and near.kind == kind:
                if near.depth == depth:
                    return near
                return nearest(lambda u: u.kind == kind and u.depth == depth
                               and (kind == '#' or u.num == near.num)) or near
        if kind != '1':
            found = nearest(lambda u: u.kind == kind and u.depth == depth) or nearest(lambda u: u.kind == kind)
            if found is not None:
                return found
    if prev is not None and not prev.kind:
        return prev
    return nearest(lambda u: not u.kind)


def _few(notes):
    """The notes for the user, with no more than MAX_NOTES of any one kind spelled out."""
    kinds, out, more = {}, [], 0
    for note in notes:
        kind = note.split(':', 1)[0][:40]
        kinds[kind] = kinds.get(kind, 0) + 1
        if kinds[kind] <= MAX_NOTES:
            out.append(note)
        else:
            more += 1
    if more:
        out.append('%d more notes like these were left out.' % more)
    return out


def _rewrite(data, new):
    doc = _Document(data)
    units = doc.units()
    notes = doc.notes
    gone, added, last = [], [], -1

    def beside(k):
        """The units next to units[k]."""
        return [units[x] for x in (k - 1, k + 1) if 0 <= x < len(units)]

    def titles_toc(k):
        """True when units[k] is the title of a table of contents left out after it."""
        return units[k] in doc.tocs and doc.heads(units[k].p, doc.tocs[units[k]], beside(k))
    steps = _align([u.marker + u.text for u in units], new)
    # The title of a table of contents that went, with new blocks typed where it stood, was renamed: the first of
    # them is its new text, and only the rest go after the table.
    for k in range(len(steps) - 1, 0, -1):
        (i, j), (x, y) = steps[k - 1], steps[k]
        if j is None and x is None and new[y].strip() and _lead(new[y])[0] == units[i].kind and titles_toc(i):
            steps[k - 1:k + 1] = [(i, y)]
    for i, j in steps:
        if i is None:
            added.append((last, _clean(new[j])))
            continue
        last = i
        unit = units[i]
        if j is None:
            gone.append(unit)
        elif new[j] == unit.marker + unit.text:
            continue                    # the block as blocks() gave it: its paragraph is not looked at again
        elif not _strip_marker(_clean(new[j]), unit.kind, unit.marker).strip():
            gone.append(unit)           # a block that comes back empty was deleted, whatever the count says
        elif unit.p is not None:
            doc.set_text(unit.p, _strip_marker(_clean(new[j]), unit.kind, unit.marker))
        else:
            _rewrite_row(doc, unit, _clean(new[j]), notes)

    # New blocks: each goes after the block before it, as a paragraph; next to a table row it is a new row when it
    # has that row's number of cells.
    before, after, loose = {}, {}, []
    # The plain paragraph just above a table of contents is usually its title: no new block is styled like it.
    titles = set(unit for unit in doc.tocs if not unit.kind)
    for i, text in added:
        if not text.strip():
            continue
        prev = units[i] if i >= 0 else None
        nxt = units[i + 1] if i + 1 < len(units) else None
        row = prev if prev is not None and prev.p is None else nxt if prev is None and nxt is not None and nxt.p is None else None
        if row is not None:
            inside = prev is not None and nxt is not None and nxt.p is None and nxt.table is prev.table
            pieces = text.split(' | ')
            if len(pieces) != len(row.cells):
                pieces = text.split('|')
            made = None
            if len(pieces) == len(row.cells) and (len(pieces) > 1 or inside):
                made = doc.row_like(row, pieces)
            if made is not None:
                (after if prev is not None else before).setdefault(row.tr, []).append(made)
                if row.tr.tail and row.tr.tail.isspace():
                    made.tail = row.tr.tail
                continue
            if inside:
                notes.append('New text could not be added between two rows of a table: "%s".' % _short(text))
                continue
        # A new block typed just below the title of a table of contents goes after the end of the table, and one typed
        # above that title, first in the body, stays above it: never in its field or its content control.
        title = max(i, 0)
        gap = doc.tocs[units[title]] if units and titles_toc(title) else None
        model = _model(units, i, text, titles)
        body = _strip_marker(text, model.kind if model is not None else '').strip()
        if not body:
            continue
        p = doc.paragraph_like(model.p if model is not None else None, body)
        if prev is not None:
            anchor = prev.p if prev.p is not None else prev.table
            if gap:
                anchor = doc.outside(doc.toc_end(prev.p, gap), beside(title))
        elif units:
            anchor = units[0].p if units[0].p is not None else units[0].table
            if gap:
                anchor = doc.outside(anchor, beside(title))
        else:
            loose.append(p)
            continue
        # Never inside a content control that holds one paragraph (a document property, plain text): Word finds a
        # second paragraph there damaged.  The new one goes beside the control.
        while anchor.up is not None and anchor.up.tag == doc.q('sdtContent') and doc.single(anchor.up.up):
            anchor = anchor.up.up
        (after if prev is not None else before).setdefault(anchor, []).append(p)
        if anchor.tail and anchor.tail.isspace():
            p.tail = anchor.tail        # a file written with one element per line stays that way

    # Blocks that are gone.  A table row keeps its shape: its cells are emptied.
    doomed = {}
    again = set(_key(text) for _, text in added)
    for unit in gone:
        if unit.p is not None and doc.unseen(unit.p):
            notes.append(UNSEEN % _short(unit.text))
            continue
        if unit.p is not None and doc.bound(unit.p):
            notes.append(BOUND % _short(unit.text))
            continue
        if unit.p is not None:
            doomed[unit.p] = unit
            if _key(unit.marker + unit.text) in again and doc.standing([a for a in doc.atoms(unit.p) if a[1]]):
                notes.append('A paragraph that changed place was typed again where it now stands, without the links '
                             'and formatting it had: "%s".' % _short(unit.text))
            continue
        for paras in unit.cells:
            for p in paras:
                doc.clear(p)
        notes.append('A table row was emptied rather than removed, to keep the shape of its table: "%s".'
                     % _short(unit.text))

    q = doc.q
    table, section = q('tbl'), q('sectPr')
    blockish = frozenset([q('p'), table, q('sdt'), q('customXml'), q('altChunk'), '{%s}AlternateContent' % _MC])
    parents, seen = [], set()
    for el in list(before) + list(after) + list(doomed):
        if el.up is not None and id(el.up) not in seen:
            seen.add(id(el.up))
            parents.append(el.up)
    for parent in parents:
        seq = []
        for child in parent:
            seq.extend(before.get(child, ()))
            seq.append(child)
            seq.extend(after.get(child, ()))
        # One pass, so that taking out many paragraphs costs no more than taking out one.  following[k] is the
        # next block-level thing after seq[k]; `behind` is the last one kept so far.
        following, ahead = [None] * len(seq), None
        for k in range(len(seq) - 1, -1, -1):
            following[k] = ahead
            if seq[k].tag in blockish:
                ahead = seq[k]
        kept, behind = [], None
        for k, child in enumerate(seq):
            if child in doomed:
                # Not the only paragraph of its container, and not the paragraph that keeps a table apart from
                # the next table or from the end: Word joins two tables that touch, and wants a paragraph last.
                alone = behind is None and following[k] is None
                holds_tables_apart = behind is not None and behind.tag == table and (
                    following[k] is None or following[k].tag == table)
                if not alone and not holds_tables_apart and doc.removable(child):
                    continue
                doc.clear(child)
                notes.append('A paragraph was emptied rather than removed, because the layout or something in it '
                             'depends on it: "%s".' % _short(doomed[child].text))
            kept.append(child)
            if child.tag in blockish:
                behind = child
        _own(parent)
        for child in kept:
            child.up = parent
        parent[:] = kept
    if loose and doc.body is not None:  # a body with no text at all: the new paragraphs go before its page setup
        body = doc.body
        doc.put(body, next((k for k, c in enumerate(body) if c.tag == section), len(body)), loose)

    if not doc.root.st:
        return doc.package.data, _few(notes)    # nothing changed: the very same file
    return doc.package.with_document(doc.serialise()), _few(notes)


def _rewrite_row(doc, unit, text, notes):
    """A table row maps back to its cells when it comes back with one piece per cell and every cell is a single
    paragraph.  Otherwise it is left as it was."""
    cells = unit.cells
    if not text.strip():
        for paras in cells:
            for p in paras:
                doc.clear(p)
        return
    pieces = text.split(' | ')
    if len(pieces) != len(cells):
        loose = [piece.strip() for piece in text.split('|')]
        if len(loose) == len(cells):
            pieces = loose
    if len(pieces) != len(cells):
        why = 'the new text has %d cell(s) and the row has %d' % (len(pieces), len(cells))
    elif any(len(paras) != 1 for paras in cells):
        why = 'one of its cells holds more than one paragraph'
    else:
        for paras, piece in zip(cells, pieces):
            if piece.strip() and not doc.text(paras[0]).strip() and doc.merged_away(paras[0]):
                notes.append('Text for a merged table cell was not written, because that cell is hidden behind the '
                             'cell above it: "%s".' % _short(piece))
                continue
            doc.set_text(paras[0], piece)
        return
    if text.split() != unit.text.split():           # (a row that only came back spaced differently needs no note)
        notes.append('A table row was left as it was, because %s: "%s".' % (why, _short(unit.text)))
