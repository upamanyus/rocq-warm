"""Parser for the `rocq repl -emacs -time` output stream.

The stream is a strict alternation of prompts and sentence reports:

    <prompt>Rocq < 3 || 0 < </prompt>       state before sentence 3
    Chars 32 - 52 [Check~foo.] 0.1 secs     the sentence Rocq just ran
    Error: ...                              anything the sentence printed
    <prompt>Rocq < 4 || 0 < </prompt>       state after it

Three properties of the stream, each held to a live Rocq by
`tests/test_rocq_warm_protocol.py`:

* `Chars A - B` are **byte** offsets into the stdin stream, counted
  continuously across separate writes.  Rocq's own parser therefore does the
  sentence splitting.
* every executed sentence gets exactly one prompt before it and one after,
  whatever the vernac (queries, `Set`, `Section`/`Module`, bullets, `Fail`).
* a sentence that failed does not advance the state id.  That, not the
  presence of `Error:`, is the verdict: a proof's own `idtac` can print
  anything.

Two sentence kinds report no `Chars` line, one a failure and one the output
the user asked for:

* a *parse* error -- Rocq reports it, skips to the next `.`, and carries on;
* a **toplevel-only** command.  `rocq repl` parses at the `vernac_toplevel`
  grammar entry and `coqloop` answers these itself rather than adding them to
  the document: `Drop`, `Quit`, `BackTo`, `Show Goal N at M`, `Show Proof
  Diffs`, and since Rocq 9.2 a bare `Show.`, `Show N.` and `Show Diffs id.`
  (on 9.0 and 9.1 those go through the document and do get a range).  The
  state it was given is handed back, so there is no new state id and `-time`
  reports nothing.  A batch `coqc` parses at the plain `vernac` entry, where
  all of these are ordinary commands.

The state id is unchanged either way, so a `Chars`-less segment's verdict
comes from whether Rocq printed an error.  Reading message text for a verdict
is unsound in general and sound here: an `idtac` cannot print from a sentence
that never reached the document.
"""

import re

PROMPT_RE = re.compile(rb'<prompt>(.*?)</prompt>', re.S)
PROMPT_BODY_RE = re.compile(rb'^(.*) < (\d+) \|(.*)\| (\d+) < $', re.S)
# The bracketed display is the sentence text with spaces as `~`, and it is NOT
# escaped: proof scripts contain `]` (`iDestruct ... as "[H1 H2]"`).  So this
# is greedy and anchored on the trailing ` N secs (Nu,Ns)`.  Non-greedy on the
# `]` drops those sentences from the progress signal and deadlocks the feed.
CHARS_RE = re.compile(
    rb'^Chars (\d+) - (\d+) \[(.*)\] ([0-9.]+) secs \(([0-9.]+)u,([0-9.]+)s\)$',
    re.M)
# Unanchored, because Rocq writes the `Chars` line straight after `</prompt>`
# on the same line: `^` matches only after the stream is cut into segments, and
# progress tracking reads the raw stream.
PROGRESS_RE = re.compile(rb'Chars (\d+) - (\d+) \[')
# Consulted only for a segment with no `Chars` line, where the state id cannot
# tell a parse error from a toplevel-only command.
ERROR_RE = re.compile(rb'(?m)^Error:')


class Sentence:
    """One vernac Rocq executed, with everything it printed."""

    __slots__ = ("stream_start", "stream_end", "display", "secs",
                 "state_before", "state_after", "messages", "start", "end",
                 "anchor")

    def __init__(self, stream_start, stream_end, display, secs,
                 state_before, state_after, messages):
        self.stream_start = stream_start
        self.stream_end = stream_end
        self.display = display
        self.secs = secs
        self.state_before = state_before
        self.state_after = state_after
        self.messages = messages
        self.start = None       # byte offset in the .v file, filled in by Session
        self.end = None
        self.anchor = None      # what Rocq's message offsets are relative to

    @property
    def failed(self):
        return self.state_after == self.state_before

    def __repr__(self):
        return "Sentence(%s-%s, %r, %s%s)" % (
            self.start, self.end, self.display[:40],
            self.state_before, " FAILED" if self.failed else "")


class Untimed:
    """A sentence Rocq executed and reported no `Chars` line for.

    A parse error or a toplevel-only command.  Neither has a range of its own,
    so `Session` reconstructs one from the surrounding sentences, and neither
    advances the state id.
    """

    __slots__ = ("state_before", "messages", "failed", "start", "end", "anchor")

    def __init__(self, state_before, messages, failed):
        self.state_before = state_before
        self.messages = messages
        self.failed = failed
        self.start = None
        self.end = None
        self.anchor = None

    state_after = property(lambda self: self.state_before)
    display = property(
        lambda self: b"<parse error>" if self.failed else b"<toplevel command>")

    def __repr__(self):
        return "Untimed(%s-%s%s)" % (self.start, self.end,
                                     " FAILED" if self.failed else "")


def split_prompts(buf):
    """Cut a stream into one segment per sentence.

    Returns (segments, tail).  Each segment is (state_before, state_after,
    output_bytes): everything printed between the prompt before the sentence
    and the one after it.  `tail` is the output past the last complete prompt,
    belonging to a sentence still running; the banner before the first prompt
    is dropped.

    N prompts delimit N-1 sentences, and prompt i+1's state is what says
    whether sentence i succeeded, so both ends of each pair are needed.
    """
    marks = []
    for m in PROMPT_RE.finditer(buf):
        body = PROMPT_BODY_RE.match(m.group(1))
        if body is None:                    # not a recognised prompt
            continue
        marks.append((int(body.group(2)), m.start(), m.end()))
    segments = []
    for i in range(len(marks) - 1):
        state, _, end = marks[i]
        segments.append((state, marks[i + 1][0], buf[end:marks[i + 1][1]]))
    tail = buf[marks[-1][2]:] if marks else buf
    return segments, tail


def parse_segments(segments):
    """Turn segments into Sentence / Untimed objects."""
    out = []
    for state_before, state_after, seg in segments:
        m = CHARS_RE.search(seg)
        if m is None:
            out.append(Untimed(state_before, seg,
                               failed=ERROR_RE.search(seg) is not None))
            continue
        messages = (seg[:m.start()] + seg[m.end():])
        out.append(Sentence(int(m.group(1)), int(m.group(2)), m.group(3),
                            float(m.group(4)), state_before, state_after,
                            messages))
    return out


def message_text(raw):
    """Strip the markup `-emacs` mode wraps around messages."""
    txt = raw.replace(b"<infomsg>", b"").replace(b"</infomsg>", b"")
    txt = txt.replace(b"<warning>", b"").replace(b"</warning>", b"")
    return txt.strip()


def classify(raw):
    """'error', 'warning' or 'info' for one message blob."""
    if ERROR_RE.search(raw):
        return "error"
    if b"<warning>" in raw or re.search(rb'(?m)^Warning:', raw):
        return "warning"
    return "info"
