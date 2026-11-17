"""Canonical form of agent-authored dashboard HTML (AGENT-07 hidden-markup fix).

The linter checks, and the /d/ content frame serves, the same bytes: the WHATWG
serialisation of the tree that a spec-conformant HTML5 parser (html5lib, which handles
foreign content and integration points the way browsers do) builds from the stored code.
Whatever that parser did not build as an element is not served as one, so markup that
one parser hides and another sees cannot reach the browser unchecked.

The serialisation must be a fixpoint: serialize(parse(c)) == c. A document whose
canonical form changes when re-parsed is refused by the linter and never served.

Pure and deterministic: no database, no network, no logging of code.
"""

import functools
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import html5lib
from html5lib.treebuilders import getTreeBuilder

HTML_NS = "http://www.w3.org/1999/xhtml"
SVG_NS = "http://www.w3.org/2000/svg"
MATHML_NS = "http://www.w3.org/1998/Math/MathML"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XMLNS_NS = "http://www.w3.org/2000/xmlns/"

VOID = frozenset("area base basefont bgsound br col embed frame hr img input keygen link "
                 "meta param source track wbr".split())
# Text children of these HTML elements serialise verbatim (scripting is enabled in the frame).
RAW_PARENTS = frozenset("style script xmp iframe noembed noframes plaintext noscript".split())

NBSP = chr(0xA0)
_TREE = getTreeBuilder("etree", ET, fullTree=True)


@dataclass
class Element:
    ns: str
    name: str                      # local name as built (SVG names keep their case)
    attrs: list[tuple[str, str]]   # (qualified name, decoded value) in document order
    line: int                      # line of the start tag in the canonical text
    ancestors: tuple[tuple[str, str], ...]
    text: str | None = None        # verbatim text of a raw-text HTML element


@dataclass
class Canonical:
    html: str                      # canonical serialisation (what /d/ serves)
    stable: bool                   # serialize(parse(html)) == html
    elements: list[Element] = field(default_factory=list)
    has_doctype: bool = False


def parse(text: str):
    """The full document tree (DOCUMENT_ROOT element) as html5lib builds it."""
    p = html5lib.HTMLParser(tree=_TREE, namespaceHTMLElements=True)
    return p.parse(text, scripting=True)


def _split(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _qname(key: str) -> str:
    ns, local = _split(key)
    if not ns:
        return local
    if ns == XML_NS:
        return "xml:" + local
    if ns == XMLNS_NS:
        return "xmlns" if local == "xmlns" else "xmlns:" + local
    if ns == XLINK_NS:
        return "xlink:" + local
    return local


def _esc_text(s: str) -> str:
    return (s.replace("&", "&amp;").replace(NBSP, "&nbsp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _esc_attr(s: str) -> str:
    return (s.replace("&", "&amp;").replace(NBSP, "&nbsp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def serialize(root) -> tuple[str, list[Element], bool]:
    """WHATWG HTML serialisation of a parse() tree: (text, elements, has_doctype)."""
    out: list[str] = []
    line = [1]
    els: list[Element] = []
    doctype = [False]

    def emit(s: str) -> None:
        if s:
            out.append(s)
            line[0] += s.count("\n")

    def walk(el, stack: tuple) -> None:
        tag = el.tag
        if tag is ET.Comment:
            emit("<!--" + (el.text or "") + "-->")
            return
        if tag == "<!DOCTYPE>":
            doctype[0] = True
            emit("<!DOCTYPE " + (el.text or "") + ">")
            return
        if not isinstance(tag, str):
            return
        ns, local = _split(tag)
        attrs = [(_qname(k), v if isinstance(v, str) else "") for k, v in el.attrib.items()]
        rec = Element(ns, local, attrs, line[0], stack)
        els.append(rec)
        emit("<" + local + "".join(f' {k}="{_esc_attr(v)}"' for k, v in attrs) + ">")
        if ns == HTML_NS and local in VOID:
            return
        raw = ns == HTML_NS and local in RAW_PARENTS
        chunks: list[str] = []

        def text(s):
            if s:
                chunks.append(s)
                emit(s if raw else _esc_text(s))

        text(el.text)
        inner = stack + ((ns, local),)
        for child in el:
            walk(child, inner)
            text(child.tail)
        if raw:
            rec.text = "".join(chunks)
        emit("</" + local + ">")

    for child in root:
        walk(child, ())
    return "".join(out), els, doctype[0]


def canonicalize(text: str) -> Canonical:
    """Parse, serialise, re-parse: lint the tree the browser builds from the served bytes."""
    if text.startswith(chr(0xFEFF)):  # a browser's decoder drops one leading BOM
        text = text[1:]
    first, _, _ = serialize(parse(text))
    html, els, doctype = serialize(parse(first))
    return Canonical(html=html, stable=html == first, elements=els, has_doctype=doctype)


@functools.lru_cache(maxsize=32)
def serve_form(code: str) -> str | None:
    """The bytes /d/ serves for stored `code`, or None when it has no stable canonical form."""
    try:
        c = canonicalize(code)
    except Exception:  # defensive: never serve what could not be canonicalised
        return None
    return c.html if c.stable else None
