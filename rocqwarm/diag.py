"""REPL messages rendered as `coqc`-format diagnostics.

`rocq repl -emacs` reports a location as `Toplevel input, characters A-B:`,
where A and B are byte offsets from an anchor computed by `Session._absorb`:
the start of the line on which Rocq resumed reading, which is neither the
sentence nor the line the error is on.  Adding the anchor gives the absolute
position; the rendering is then `coqc`'s:

    File "./Foo.v", line 9, characters 7-17:
    Error: The variable bogus_name was not found in the current environment.

Held to a live `coqc` by `tests/test_rocq_warm_diag.py`, including a span
crossing a line break and a parse error, which reports no range at all.
"""

import re

from . import protocol

LOC_RE = re.compile(rb'^Toplevel input, characters (\d+)-(\d+):$', re.M)


def line_bol(text, off):
    """Byte offset of the start of the line containing `off`."""
    return text.rfind(b"\n", 0, off) + 1


def line_number(text, off):
    return text.count(b"\n", 0, off) + 1


def skip_blanks(text, i):
    """First byte at or after `i` that is neither whitespace nor a comment.

    Where a sentence begins, which is how a parse error is located: it carries
    no range of its own.  Rocq's comments nest, and a string inside one can
    hide a `*)`.
    """
    n = len(text)
    while i < n:
        c = text[i:i + 1]
        if c in b" \t\r\n":
            i += 1
        elif text[i:i + 2] == b"(*":
            depth, i = 1, i + 2
            while i < n and depth:
                if text[i:i + 2] == b"(*":
                    depth, i = depth + 1, i + 2
                elif text[i:i + 2] == b"*)":
                    depth, i = depth - 1, i + 2
                elif text[i:i + 1] == b'"':
                    i += 1
                    while i < n and text[i:i + 1] != b'"':
                        i += 1
                    i += 1
                else:
                    i += 1
        else:
            return i
    return n


def message_anchor(text, prev_end):
    """Where Rocq measures the next sentence's message offsets from.

    Rocq skips the whitespace after a sentence's `.`.  Crossing a newline puts
    the anchor on the line it lands on; meeting anything else first -- a
    trailing comment, or another sentence on the same line -- leaves it on the
    line the previous sentence ended on.
    """
    i, n = prev_end, len(text)
    while i < n and text[i:i + 1] in b" \t\r":
        i += 1
    if i < n and text[i:i + 1] == b"\n":
        return i + 1
    return line_bol(text, prev_end)


def locate(raw, anchor):
    """(abs_start, abs_end) for one message blob, or None if it has no location.

    `anchor` comes from `Session._absorb`.
    """
    m = LOC_RE.search(raw)
    if m is None or anchor is None:
        return None
    return anchor + int(m.group(1)), anchor + int(m.group(2))


# `rocq repl` parses at the `vernac_toplevel` grammar entry, so its syntax
# errors name that entry where `coqc` names plain `vernac`.  Rewritten so a
# warm diagnostic is byte-identical to the batch one.
REPL_TOPLEVEL_ENTRY = b"illegal begin of toplevel:vernac_toplevel"
BATCH_ENTRY = b"illegal begin of vernac"


def strip_location(raw):
    """The message itself: no location header, no echoed source, no markup."""
    out = []
    for line in raw.split(b"\n"):
        if LOC_RE.match(line) or line.startswith(b"> "):
            continue
        out.append(line)
    txt = protocol.message_text(b"\n".join(out))
    return txt.replace(REPL_TOPLEVEL_ENTRY, BATCH_ENTRY).strip()


def line_col(text, span):
    """(line, first column, last column) for a byte span, as `coqc` counts.

    Resolved by whoever holds the text the span is into -- the daemon, against
    the bytes it checked.  Re-reading the file to resolve them would place the
    diagnostic against whatever the file says now.
    """
    if span is None:
        return None
    start, end = span
    bol = line_bol(text, start)
    return (line_number(text, start), start - bol, end - bol)


def render_at(display_path, where, body):
    """One diagnostic, byte-for-byte in `coqc`'s shape, from `line_col`."""
    if where is None:
        # The message carries its own `Error:`/`Warning:` prefix; only the
        # location is missing, and there is none to give.
        return body
    line, first, last = where
    return 'File "%s", line %d, characters %d-%d:\n%s' % (
        display_path, line, first, last, body)


def render(display_path, text, span, message):
    """One diagnostic, for a caller that holds the text the span is into."""
    return render_at(display_path, line_col(text, span),
                     message.decode("utf8", "replace"))
