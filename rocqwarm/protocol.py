"""Parser for the `rocq repl -emacs -time` output stream.

The stream is a strict alternation of prompts and sentence reports:

    <prompt>Rocq < 3 || 0 < </prompt>       state before sentence 3
    Chars 32 - 52 [Check~foo.] 0.1 secs     the sentence Rocq just ran
    Error: ...                              anything the sentence printed
    <prompt>Rocq < 4 || 0 < </prompt>       state after it

Three facts about this stream carry the whole design, and each is checked by
`tests/test_rocq_warm_protocol.py` against a live Rocq:

* `Chars A - B` are **byte** offsets into the stdin stream, and the counter runs
  continuously across separate writes.  That is what lets us hand Rocq's own
  parser the job of splitting sentences.
* every executed sentence gets exactly one prompt before it and one after,
  whatever the vernac (queries, `Set`, `Section`/`Module`, bullets, `Fail`).
* a sentence that FAILED does not advance the state id.  That is the verdict
  signal -- far more robust than grepping for `Error:` in output that a proof's
  own `idtac` may have written.

Two sentence kinds produce no `Chars` line at all, and they must not be
confused, because one is a failure and the other is the output the user asked
for:

* a *parse* error -- Rocq reports it, skips to the next `.`, and carries on;
* a **toplevel-only** command.  `rocq repl` parses at the `vernac_toplevel`
  grammar entry, and `coqloop` answers those itself instead of putting them
  in the document: `Drop`, `Quit`, `BackTo`, `Show Goal N at M`, `Show Proof
  Diffs`, and -- since Rocq 9.2 -- a bare `Show.`, `Show N.` and `Show Diffs
  id.`.  It prints the goal and hands back the state it was given, so there
  is no new state id and `-time` has nothing to report.  A batch `coqc`
  parses the same file at the plain `vernac` entry, where every one of these
  is an ordinary command, which is why `coqc` accepts a file the REPL would
  otherwise look like it had rejected.  On 9.0 and 9.1 a bare `Show.` went
  through the document and did get a range; 9.2 moving it into this grammar
  is what made the common case of this visible.

So for a Chars-less segment the verdict cannot come from the state id, which
is unchanged either way; it comes from whether Rocq printed an error.  That is
the one place this parser reads the message text to decide a verdict, and it is
sound here for the reason it is unsound in general: a proof's own `idtac` can
print "Error:" but cannot do it from a sentence that never reached the
document.
"""

import re

PROMPT_RE = re.compile(rb'<prompt>(.*?)</prompt>', re.S)
PROMPT_BODY_RE = re.compile(rb'^(.*) < (\d+) \|(.*)\| (\d+) < $', re.S)
# The bracketed display is the sentence text with spaces turned into `~`, and
# it is NOT escaped: Iris tactics are full of `]` (`iDestruct ... as "[H1 H2]"`),
# so this must be greedy and anchored on the trailing ` N secs (Nu,Ns)`, never
# non-greedy on the `]`.  Getting that wrong silently drops those sentences from
# the parse-progress signal and deadlocks the write-ahead window.
CHARS_RE = re.compile(
    rb'^Chars (\d+) - (\d+) \[(.*)\] ([0-9.]+) secs \(([0-9.]+)u,([0-9.]+)s\)$',
    re.M)
# Rocq writes the `Chars` line straight after `</prompt>`, on the SAME line, so
# a `^`-anchored pattern only matches once the stream has been cut into
# per-sentence segments.  Progress tracking works on the raw stream and must
# therefore not anchor.
PROGRESS_RE = re.compile(rb'Chars (\d+) - (\d+) \[')
# Rocq's own report that a sentence failed.  Only ever consulted for a segment
# with no `Chars` line, where the state id cannot tell a parse error apart from
# a toplevel-only command; see the note at the top of this file.
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

    Either a parse error or a toplevel-only command -- see the note at the top
    of this file.  Neither has a range of its own, so `Session` reconstructs
    one from the surrounding sentences, and neither advances the state id.
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

    Returns (segments, tail) where each segment is
    (state_before, state_after, output_bytes) -- everything Rocq printed
    between the prompt that preceded the sentence and the one that followed it
    -- and `tail` is the output after the last complete prompt, belonging to a
    sentence still running.  Anything before the first prompt (the banner) is
    dropped.

    N prompts delimit N-1 sentences, and the state on prompt i+1 is what tells
    us whether sentence i succeeded, so both ends of each pair matter.
    """
    marks = []
    for m in PROMPT_RE.finditer(buf):
        body = PROMPT_BODY_RE.match(m.group(1))
        if body is None:                    # not a prompt we understand
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
