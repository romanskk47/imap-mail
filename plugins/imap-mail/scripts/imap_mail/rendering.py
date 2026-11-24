"""Text/HTML presentation helpers for reading and composing mail.

Everything here is deterministic and dependency-free. Nothing contacts a mail
server. Source HTML is untrusted: quoting uses an allowlist and canonical
re-serialization, never the provider's markup verbatim.
"""

from __future__ import annotations

from email import policy as email_policy
import html
from html.parser import HTMLParser
import re


# ---------------------------------------------------------------------------
# MIME serialization
# ---------------------------------------------------------------------------

_WIDE_MIME_HEADERS = {"content-type", "content-disposition"}


class MimeSafePolicy(email_policy.EmailPolicy):
    """Fold structured MIME headers without RFC 2047 encoded words.

    Python 3.15 folds a long unbreakable Content-Type token such as
    ``application/vnd.openxmlformats-officedocument.wordprocessingml.document``
    into ``application/=?utf-8?q?vnd...?=``, which mail clients cannot parse.
    Content-Type/Content-Disposition use RFC 2231 parameters, not encoded words,
    so they are folded at the RFC 5322 hard limit (998). Every other header
    keeps normal 78-column folding and short RFC 2047 encoded words.
    """

    wide_mime_headers = True

    def _wide(self, name):
        return (self.wide_mime_headers and isinstance(name, str) and
                name.lower() in _WIDE_MIME_HEADERS and
                (self.max_line_length or 0) and self.max_line_length < 998)

    def fold(self, name, value):
        if self._wide(name):
            return self.clone(wide_mime_headers=False, max_line_length=998).fold(name, value)
        return super().fold(name, value)

    def fold_binary(self, name, value):
        if self._wide(name):
            return self.clone(wide_mime_headers=False, max_line_length=998).fold_binary(name, value)
        return super().fold_binary(name, value)


SMTP_POLICY = MimeSafePolicy(linesep="\r\n")


# ---------------------------------------------------------------------------
# HTML -> readable text
# ---------------------------------------------------------------------------

_SKIP_CONTENT = {"script", "style", "head", "title", "template", "noscript", "xml",
                 "iframe", "object", "embed", "svg", "math", "select", "textarea", "button"}
_PARAGRAPH = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "ul", "ol", "dl", "hr"}
_LINE = {"div", "section", "article", "header", "footer", "main", "nav", "aside", "address",
         "center", "form", "fieldset", "figure", "figcaption", "tr", "li", "dt", "dd",
         "tbody", "thead", "tfoot", "caption", "blockquote"}
_CELL = {"td", "th"}
_LINK_SCHEMES = ("http://", "https://", "mailto:")


def _link_target(value):
    value = (value or "").strip()
    return value if value.lower().startswith(_LINK_SCHEMES) else None


def _same_link(text, href):
    def norm(value):
        value = value.strip().lower()
        for prefix in ("mailto:", "https://", "http://", "www."):
            if value.startswith(prefix):
                value = value[len(prefix):]
        return value.rstrip("/")
    return norm(text) == norm(href)


class _TextRenderer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []          # (quote depth, text)
        self.current = []
        self.depth = 0
        self.line_depth = 0
        self.pre = 0
        self.skip = []
        self.lists = []
        self.links = []

    # Line management -------------------------------------------------------
    def _text(self):
        return "".join(self.current)

    def _flush(self, force=False):
        text = self._text()
        if not self.pre:
            text = text.rstrip(" ")
        if text or force:
            self.lines.append((self.line_depth, text))
        self.current = []
        self.line_depth = self.depth

    def _soft_break(self):
        if self._text().strip():
            self._flush()
        else:
            self.current = []
            self.line_depth = self.depth

    def _blank(self):
        self._soft_break()
        if self.lines and self.lines[-1][1] != "":
            self.lines.append((self.depth, ""))

    def _append(self, value):
        if not self.current:
            self.line_depth = self.depth
        self.current.append(value)

    # Parser events ---------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.append(tag)
            return
        if tag in _SKIP_CONTENT:
            self.skip.append(tag)
            return
        attrs = dict(attrs)
        if tag == "br":
            self._flush(force=True)
        elif tag in _PARAGRAPH:
            self._blank()
            if tag == "pre":
                self.pre += 1
            elif tag in ("ul", "ol"):
                self.lists.append([tag, 0])
            elif tag == "hr":
                self._append("----")
                self._flush()
        elif tag == "blockquote":
            self._soft_break()
            self.depth += 1
            self.line_depth = self.depth
        elif tag in _LINE:
            self._soft_break()
            if tag == "li":
                if self.lists and self.lists[-1][0] == "ol":
                    self.lists[-1][1] += 1
                    self._append(f"{self.lists[-1][1]}. ")
                else:
                    self._append("- ")
        elif tag in _CELL:
            if self._text().strip():
                self._append("  ")
        elif tag == "a":
            self.links.append((_link_target(attrs.get("href")), len(self._text())))
        elif tag == "img":
            alt = (attrs.get("alt") or "").strip()
            if alt:
                self._append(f"[image: {alt}]")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr", "img"):
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in _PARAGRAPH:
            if tag == "pre" and self.pre:
                self._flush()
                self.pre -= 1
            elif tag in ("ul", "ol") and self.lists:
                self.lists.pop()
            self._blank()
        elif tag == "blockquote":
            self._soft_break()
            self.depth = max(0, self.depth - 1)
            self.line_depth = self.depth
        elif tag in _LINE:
            self._soft_break()
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            label = self._text()[start:] if start <= len(self._text()) else ""
            if href and label.strip() and not _same_link(label, href):
                self._append(f" <{href[7:] if href.lower().startswith('mailto:') else href}>")

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            pieces = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for index, piece in enumerate(pieces):
                if index:
                    self._flush(force=True)
                if piece:
                    self._append(piece)
            return
        value = re.sub(r"[ \t\r\n\f\v ​]+", " ", data)
        if not self._text() or self._text().endswith(" "):
            value = value.lstrip(" ")
        if value:
            self._append(value)

    def result(self):
        self._soft_break()
        output = []
        for depth, text in self.lines:
            if text == "" and output and output[-1][1] == "":
                continue
            output.append((depth, text))
        while output and output[0][1] == "":
            output.pop(0)
        while output and output[-1][1] == "":
            output.pop()
        rendered = []
        for depth, text in output:
            prefix = "> " * depth
            rendered.append((prefix + text) if text else prefix.rstrip())
        return "\n".join(rendered)


def html_to_text(source):
    """Readable plain text that keeps line breaks, lists, links and <pre> code."""
    if not isinstance(source, str) or not source.strip():
        return ""
    parser = _TextRenderer()
    try:
        parser.feed(source)
        parser.close()
        return parser.result()
    except Exception:
        value = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", "", source)
        value = html.unescape(re.sub(r"<[^>]*>", " ", value))
        return re.sub(r"[ \t]+", " ", value).strip()


# ---------------------------------------------------------------------------
# Authored text -> HTML
# ---------------------------------------------------------------------------

def text_to_html(text):
    """One block per line, like native mail clients, without CSS line handling.

    Editors such as Apple Mail discard ``white-space: pre-wrap`` when a draft is
    reopened, which collapsed paragraphs into one line. Only lines whose spacing
    matters (indentation, tabs, repeated spaces) keep pre-wrap, so even then
    the line break itself survives.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    for line in lines:
        if not line.strip():
            blocks.append("<div><br></div>")
        elif line[:1] in (" ", "\t") or "  " in line or "\t" in line:
            blocks.append('<div style="white-space:pre-wrap">' + html.escape(line, quote=False) + "</div>")
        else:
            blocks.append("<div>" + html.escape(line, quote=False) + "</div>")
    return "".join(blocks)


# ---------------------------------------------------------------------------
# Untrusted source HTML -> quotable HTML
# ---------------------------------------------------------------------------

_QUOTE_TAGS = {"a", "abbr", "b", "blockquote", "br", "caption", "center", "cite", "code",
               "dd", "del", "div", "dl", "dt", "em", "font", "h1", "h2", "h3", "h4", "h5",
               "h6", "hr", "i", "ins", "kbd", "li", "ol", "p", "pre", "q", "s", "samp",
               "small", "span", "strike", "strong", "sub", "sup", "table", "tbody", "td",
               "tfoot", "th", "thead", "tr", "tt", "u", "ul"}
_QUOTE_VOID = {"br", "hr"}
_QUOTE_DROP = _SKIP_CONTENT | {"form", "audio", "video", "canvas", "picture", "source", "map"}
_QUOTE_ATTRS = {
    "a": {"href", "title"},
    "td": {"colspan", "rowspan", "align", "valign", "width"},
    "th": {"colspan", "rowspan", "align", "valign", "width"},
    "table": {"border", "cellpadding", "cellspacing", "width", "align"},
    "font": {"color", "face", "size"},
    "ol": {"start", "type"},
    "p": {"align"},
    "div": {"align"},
}
_GLOBAL_ATTRS = {"dir", "style"}
_STYLE_PROPERTIES = {"color", "background-color", "font-weight", "font-style",
                     "text-decoration", "text-align", "font-family", "font-size"}
_STYLE_VALUE = re.compile(r"[#\w\s,.%'\"-]{1,120}")
_ATTR_VALUE = re.compile(r"[\w\s#%.,:;'\"/()+-]{0,200}")
MAX_QUOTE_HTML = 1024 * 1024


def _style(value):
    kept = []
    for declaration in (value or "").split(";"):
        if ":" not in declaration:
            continue
        name, item = (part.strip() for part in declaration.split(":", 1))
        name = name.lower()
        lowered = item.lower()
        if (name in _STYLE_PROPERTIES and _STYLE_VALUE.fullmatch(item) and
                "expression" not in lowered and "url" not in lowered and "\\" not in item):
            kept.append(f"{name}: {item}")
    return "; ".join(kept)


class _QuoteSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.stack = []
        self.skip = []

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.append(tag)
            return
        if tag in _QUOTE_DROP:
            self.skip.append(tag)
            return
        if tag == "img":
            alt = next((value for name, value in attrs if name == "alt" and value), "")
            if alt.strip():
                self.output.append(html.escape(f"[image: {alt.strip()}]", quote=False))
            return
        if tag not in _QUOTE_TAGS:
            return  # html/body/o:p/meta/link and similar wrappers keep only their text.
        allowed = _QUOTE_ATTRS.get(tag, set()) | _GLOBAL_ATTRS
        rendered = []
        for name, value in attrs:
            if name not in allowed or value is None:
                continue
            if name == "href":
                value = _link_target(value)
                if not value or any(ord(c) < 32 for c in value):
                    continue
            elif name == "style":
                value = _style(value)
                if not value:
                    continue
            elif not _ATTR_VALUE.fullmatch(value):
                continue
            rendered.append(f' {name}="{html.escape(value, quote=True)}"')
        self.output.append(f"<{tag}{''.join(rendered)}>")
        if tag not in _QUOTE_VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in _QUOTE_TAGS and tag not in _QUOTE_VOID and self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self.output.append(f"</{tag}>")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag not in _QUOTE_TAGS or tag in _QUOTE_VOID or tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            self.output.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data):
        if not self.skip and data:
            self.output.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            self.output.append(f"</{self.stack.pop()}>")
        return "".join(self.output)


def sanitize_quote_html(source):
    """Return allowlisted canonical HTML, or None when it cannot be trusted."""
    if not isinstance(source, str) or not source.strip() or len(source) > 8 * MAX_QUOTE_HTML:
        return None
    try:
        parser = _QuoteSanitizer()
        parser.feed(source)
        parser.close()
        value = parser.result().strip()
    except Exception:
        return None
    if not value or len(value) > MAX_QUOTE_HTML or not html_to_text(value).strip():
        return None
    return value


QUOTE_STYLE = "margin:0 0 0 .8ex;border-left:1px solid #ccc;padding-left:1ex"


# ---------------------------------------------------------------------------
# Signature overlap
# ---------------------------------------------------------------------------

_CLOSING = re.compile(
    r"(?i)^\W*(pozdrawiam|pozdrowienia|z pozdrowieniami|serdecznie pozdrawiam|"
    r"dzi[eę]kuj[eę]|dzi[eę]ki|best|kind regards|regards|cheers|thanks|thank you|"
    r"viele gr[uü](ß|ss)e|beste gr[uü](ß|ss)e|mit freundlichen gr[uü](ß|ss)en|"
    r"liebe gr[uü](ß|ss)e|saludos|un saludo|cordialement)\b")


def _norm(value):
    return re.sub(r"[\W_]+", " ", value.casefold()).strip()


def signature_overlap(authored, signature):
    """Remove authored closing lines that the configured signature repeats.

    Returns ``(authored, removed_lines, warnings)``. Only trailing authored
    lines equal to the signature's leading lines are removed; the last one may
    be a whole-word prefix ("Piotr" for "Piotr Romański"). The signature's
    first line anchors every match and at most three lines are considered.
    """
    sig_lines = [line for line in signature.splitlines() if line.strip()]
    lines = authored.split("\n")
    if not sig_lines or not authored.strip():
        return authored, [], []
    block = []
    for line in reversed(lines):
        if not line.strip():
            break
        block.insert(0, line)
    for count in range(min(3, len(sig_lines), len(block)), 0, -1):
        tail = block[-count:]
        expected = sig_lines[:count]
        if any(_norm(tail[i]) != _norm(expected[i]) for i in range(count - 1)):
            continue
        last, wanted = _norm(tail[-1]), _norm(expected[-1])
        if not last:
            continue
        if last == wanted or (count > 1 and wanted.startswith(last + " ")):
            remaining = "\n".join(lines[:len(lines) - count]).rstrip()
            if not remaining.strip():
                break
            return remaining, tail, []
    warnings = []
    if _CLOSING.search(sig_lines[0]) and any(_CLOSING.search(line) for line in block[-3:]):
        warnings.append("SIGNATURE_CLOSING_DUPLICATE_POSSIBLE")
    return authored, [], warnings
