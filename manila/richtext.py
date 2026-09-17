"""Cleaning rich text before it is stored.

A rich note is HTML, and HTML from a paste is nobody's friend: it arrives with
scripts, styles, event handlers and whatever else the source page had lying
about. Everything written into a note passes through here first, so what is on
disk is already a small, known, inert subset -- tags for structure and emphasis,
and nothing that can act.

The browser cleans a paste too, for the look of the thing. This is the copy
that matters: it is the one a note cannot get past, and the one that protects
whoever opens the note next. Manila has no login, and on a tailnet the reader
of a note is not always its author.
"""

import re
from html import escape
from html.parser import HTMLParser

# Structure and emphasis only. Nothing here can load, run, or position anything.
ALLOWED = {
    "p", "br", "hr",
    "strong", "em", "u", "s", "code", "pre",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4",
    "blockquote",
    "a",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
}

# Old or presentational spellings of things we already keep.
RENAME = {"b": "strong", "i": "em", "strike": "s", "del": "s", "ins": "u", "div": "p"}

# Tags whose *contents* go with them. Everything else keeps its text.
DROP_WHOLE = {"script", "style", "iframe", "object", "embed", "applet", "svg",
              "math", "form", "input", "button", "select", "textarea",
              "noscript", "link", "meta", "head", "title", "base"}

VOID = {"br", "hr"}

# Only these can be linked. javascript:, data: and the rest never get an href.
SAFE_HREF = re.compile(r"^(https?://|mailto:|tel:)", re.I)

MAX_BYTES = 512 * 1024        # a note, not a website


class Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open = []
        self.dropping = 0        # depth inside a tag whose contents go too

    # -- structure --

    def handle_starttag(self, tag, attrs):
        if self.dropping:
            if tag in DROP_WHOLE:
                self.dropping += 1
            return
        if tag in DROP_WHOLE:
            self.dropping = 1
            return
        tag = RENAME.get(tag, tag)
        if tag not in ALLOWED:
            return               # unwrap: the tag goes, its text stays
        if tag in VOID:
            self.out.append(f"<{tag}>")
            return
        if tag == "a":
            href = next((v for k, v in attrs if k.lower() == "href" and v), "")
            if SAFE_HREF.match(href.strip()):
                self.out.append(
                    f'<a href="{escape(href.strip(), quote=True)}"'
                    ' target="_blank" rel="noopener noreferrer">')
            else:
                self.out.append("<a>")   # kept as a tag so nesting stays sane
            self.open.append("a")
            return
        self.out.append(f"<{tag}>")
        self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if self.dropping:
            return
        tag = RENAME.get(tag, tag)
        if tag in VOID and tag in ALLOWED:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if self.dropping:
            if tag in DROP_WHOLE:
                self.dropping -= 1
            return
        tag = RENAME.get(tag, tag)
        if tag not in ALLOWED or tag in VOID:
            return
        if tag not in self.open:
            return               # a close with no open; ignore it
        # Close anything left hanging inside, so the output is always balanced.
        while self.open:
            here = self.open.pop()
            self.out.append(f"</{here}>")
            if here == tag:
                break

    def handle_data(self, data):
        if not self.dropping:
            self.out.append(escape(data, quote=False))

    def handle_comment(self, data):
        pass                     # Word hides half its markup in comments

    def result(self):
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


def clean(html):
    """The stored form of a rich note: balanced, inert, allow-listed HTML."""
    if not html:
        return ""
    if len(html) > MAX_BYTES:
        raise ValueError(f"note is larger than {MAX_BYTES // 1024} KB")
    cleaner = Cleaner()
    cleaner.feed(str(html))
    cleaner.close()
    return tidy(cleaner.result())


def tidy(html):
    """Drop the empty wrappers an editor leaves behind."""
    previous = None
    while previous != html:
        previous = html
        html = re.sub(r"<(p|h[1-4]|blockquote|li|ul|ol|strong|em|u|s|code)>\s*</\1>",
                      "", html)
    return html.strip()


# --- rich <-> plain ----------------------------------------------------------
#
# Switching a note between the two editors must not cost anything. Plain notes
# already use markdown-ish markers for lists, so the same conventions carry the
# rest: a link keeps its address, a heading keeps its level, emphasis survives.
# Anything with no plain spelling -- a table -- keeps its words and loses its
# grid, which is said plainly in the confirmation rather than discovered later.

MARK = {"strong": "**", "em": "*", "s": "~~", "code": "`", "u": ""}


class ToText(HTMLParser):
    """Rich note -> the markers a plain note uses."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.lists = []          # "ul"/"ol" for each open level
        self.counts = []
        self.href = None
        self.label = []

    def line(self, text=""):
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")
        if text:
            self.out.append(text)

    def emit(self, text):
        (self.label if self.href is not None else self.out).append(text)

    def handle_starttag(self, tag, attrs):
        if tag in ("ul", "ol"):
            self.lists.append(tag)
            self.counts.append(0)
        elif tag == "li":
            self.line()
            depth = max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1] == "ol":
                self.counts[-1] += 1
                marker = f"{self.counts[-1]}."
            else:
                marker = "-"
            self.out.append(f"{'  ' * depth}{marker} ")
        elif tag in ("p", "blockquote", "tr", "pre"):
            self.line()
            if tag == "blockquote":
                self.out.append("> ")
        elif tag in ("h1", "h2", "h3", "h4"):
            self.line()
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.line("---")
            self.line()
        elif tag == "a":
            self.href = next((v for k, v in attrs if k.lower() == "href"), "") or ""
            self.label = []
        elif tag in MARK:
            self.emit(MARK[tag])
        elif tag in ("td", "th") and self.out and not self.out[-1].endswith(("\n", " ")):
            self.out.append("  ")

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
                self.counts.pop()
            if not self.lists:
                self.line()
        elif tag in ("li", "p", "blockquote", "h1", "h2", "h3", "h4", "tr", "pre"):
            self.line()
        elif tag == "a":
            label = "".join(self.label).strip()
            self.href, held = None, self.href
            if label and held and SAFE_HREF.match(held):
                self.out.append(f"[{label}]({held})")
            else:
                self.out.append(label)
        elif tag in MARK:
            self.emit(MARK[tag])

    def handle_data(self, data):
        self.emit(data)

    def result(self):
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_text(html):
    """A rich note as plain text, keeping links, headings and emphasis."""
    if not html:
        return ""
    reader = ToText()
    reader.feed(str(html))
    reader.close()
    return reader.result()


INLINE = [
    (re.compile(r"\*\*([^*]+)\*\*"), "strong"),
    (re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])"), "em"),
    (re.compile(r"~~([^~]+)~~"), "s"),
    (re.compile(r"`([^`]+)`"), "code"),
]
LINK = re.compile(r"\[([^\]]+)\]\((\S+?)\)")


def _inline(text):
    out = escape(text)
    def anchor(match):
        label, href = match.group(1), match.group(2)
        if not SAFE_HREF.match(href):
            return match.group(0)
        return (f'<a href="{href}" target="_blank" rel="noopener noreferrer">'
                f"{label}</a>")
    out = LINK.sub(anchor, out)
    for pattern, tag in INLINE:
        out = pattern.sub(lambda m, t=tag: f"<{t}>{m.group(1)}</{t}>", out)
    return out


ITEM = re.compile(r"^(\s*)(?:([-*+\u2022])|(\d+)[.)])\s+(.*)$")
HEAD = re.compile(r"^(#{1,4})\s+(.*)$")
QUOTED = re.compile(r"^>\s?(.*)$")


def from_text(text):
    """Plain text as a rich note. Nothing is invented that was not written."""
    if not text or not text.strip():
        return ""
    out = []
    stack = []                    # open list tags, outermost first

    def close_to(depth):
        while len(stack) > depth:
            out.append(f"</li></{stack.pop()}>")

    def item(depth, want, body):
        # A nested list belongs *inside* the item above it, so the item it
        # hangs off stays open until the nesting closes.
        while len(stack) > depth + 1:
            out.append(f"</li></{stack.pop()}>")
        if len(stack) == depth + 1:
            out.append("</li>" if stack[depth] == want else f"</li></{stack.pop()}>")
        if len(stack) == depth:
            stack.append(want)
            out.append(f"<{want}>")
        out.append(f"<li>{_inline(body)}")

    for line in str(text).replace("\r\n", "\n").split("\n"):
        if not line.strip():
            close_to(0)
            continue
        if re.fullmatch(r"\s*-{3,}\s*", line):
            close_to(0)
            out.append("<hr>")
            continue
        found = ITEM.match(line)
        if found:
            indent, bullet, _number, body = found.groups()
            item(len(indent.replace("\t", "  ")) // 2, "ul" if bullet else "ol", body)
            continue
        close_to(0)
        heading = HEAD.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        quoted = QUOTED.match(line)
        if quoted:
            out.append(f"<blockquote>{_inline(quoted.group(1))}</blockquote>")
            continue
        out.append(f"<p>{_inline(line)}</p>")
    close_to(0)
    return "".join(out)
