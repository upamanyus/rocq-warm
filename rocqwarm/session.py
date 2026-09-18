"""A warm `rocq repl` session parked inside one .v file.

Keeps a Rocq REPL alive with the file already executed, remembering where each
sentence starts and which STM state it ran from, so a check replays only from
the first sentence the edit could have touched.  `BackTo` restores the whole
system state, the parser after a `Notation` included, which is what makes the
prefix reusable.

A check reports what the sentences it executed printed, a `Show.` among them,
by no longer filtering that part of the stream.  The reused prefix's output is
not reported, since it printed nothing this time round; its errors and
warnings are, because `coqc` reports them for this version of the file.
"""

import errno
import os
import re
import signal
import subprocess
import threading
import time

from . import diag as diagmod
from . import project
from . import protocol

# Sets `Flags.quiet`, which gates the goal `coqloop` prints after every
# sentence that changed the proof, and the `if_verbose` messages ("foo is
# defined").  It does NOT gate what a sentence prints on request: `Show`,
# `Check` and the `Print` family go through `msg_notice`, which `Flags.quiet`
# does not touch on 9.0 or 9.2.  `Time`'s "Finished transaction" has no known
# emitter, so it is not relied on where the option bites.
#
# Load-bearing on 9.0/9.1, where the goal print costs more than the proof does
# (about 3x `coqc` without it).  Inert on 9.2, which ignores the option when it
# is set from the REPL and skips the goal print for `-emacs` clients anyway.
PROLOGUE = b"Set Silent.\n"

# A sentinel must parse (a parse error emits no Chars line), execute, succeed,
# and be unmistakable in the truncated `[...]` display.  `Locate` on an unknown
# name does all four; `Print`/`Check` on one fails instead.
SENTINEL_FMT = "Locate rocq_warm_snt_%d."

# How far past Rocq's reported parse position we will write.  Must exceed the
# largest single sentence (real files reach 13.5 KB) plus whatever Rocq's input
# channel buffers, or the feed deadlocks: Rocq cannot report a sentence it has
# not finished reading.  Only a starting guess, since `_write_loop` widens it
# when it proves too small, and small on purpose: on an error everything
# already in flight still executes.
DEFAULT_WRITE_AHEAD = 16384
MAX_WRITE_AHEAD = 1 << 21
STALL_GRACE = 1.0
DEFAULT_IDLE_KILL = 20.0        # seconds of zero CPU while input is owed

# How long one command must run, behind an error, before it is interrupted.
# Long enough that formatting a large goal is not mistaken for a stuck tactic:
# printing burns CPU without advancing the parse, and a SIGINT during printing
# is fatal rather than catchable.
INTERRUPT_STALL = 2.0


class SessionDead(Exception):
    pass


class FeedTimeout(Exception):
    pass


class Unterminated(Exception):
    """The text fed ended in the middle of a sentence."""


class MemoryLimit(Exception):
    """The session outgrew its RSS ceiling and was killed."""


class Abandoned(Exception):
    """Nobody is waiting for this check any more.

    Raised when the client that asked for it went away -- a Ctrl+C, a closed
    terminal -- so there is no verdict to report and no reason to keep
    proving.  The session is not the casualty: it is left parked at the
    sentence the interrupt stopped Rocq on, with everything that did execute
    recorded, so the next check of the file replays from there.  `items` and
    `base` carry that executed work back to the caller.
    """

    def __init__(self, why, items=(), base=0):
        Exception.__init__(self, why)
        self.items, self.base = items, base
        self.line = None            # where it stopped, once it is parked


class Session:
    def __init__(self, path, flags, cwd=None, env=None, rss_limit=None,
                 rocq="rocq", toolchain=None):
        self.path = os.path.abspath(path)
        self.flags = list(flags)
        self.cwd = cwd or os.path.dirname(self.path)
        self.write_ahead = DEFAULT_WRITE_AHEAD
        self.env = env
        # The flags and switch this session can answer for; it cannot be
        # reused for others, since both are fixed at spawn time.
        self.toolchain = toolchain
        # .vo path -> (mtime_ns, size) as each was when this session loaded it.
        # Refilled after every check from what Rocq says it has loaded.
        self.loaded = {}
        # The absolute `rocq` the client resolved, not whatever is on the
        # daemon's PATH: a daemon outlives the shell that started it, and the
        # next caller may be in a different opam switch.
        self.rocq = rocq
        self.rss_limit = rss_limit  # killed above this mid-check; None to disable
        self.proc = None
        self.buf = b""              # output from the last complete prompt on
        self.stream_written = 0     # bytes ever written to Rocq's stdin
        self.parsed_end = 0         # highest stream offset Rocq reports parsing
        self.sentences = []         # the file's sentences, in order
        self.text = b""             # the bytes of the file we have executed
        self._sentinel = 0
        self._stop_writing = threading.Event()
        self._write_done = threading.Event()
        self._cv = threading.Condition()
        self.complete = False
        self.text_being_fed = b""
        self._scan_pos = 0
        self._libmap = {}           # logical name -> .vo path, as Rocq reports it
        # True while this child has been started and fed nothing but the
        # prologue, so a cold check can use it as it stands instead of
        # starting another.  False from the moment the file itself is fed,
        # whatever becomes of it.
        self.fresh = False          # ... once there IS a child; see `start`

    # ---------------------------------------------------------------- process

    def start(self):
        # A previous child may be dead and still holding its pipes.  `stop` is
        # a no-op when there is none.
        self.stop()
        argv = [self.rocq, "repl", "-emacs", "-q", "-time",
                "-topfile", self.path] + self.flags
        self.proc = subprocess.Popen(
            argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, env=self.env,
            start_new_session=True)
        self.buf = b""
        self._scan_pos = 0
        self.stream_written = 0
        self.parsed_end = 0
        self.sentences = []
        self.text = b""
        self.complete = False
        self._libmap = {}
        self._stop_writing.clear()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._await(lambda: protocol.PROMPT_RE.search(self.buf) is not None,
                    timeout=120, what="banner")
        self._trim_to_last_prompt()
        self._feed_raw(PROLOGUE, timeout=120)
        self.fresh = True           # nothing but the prologue, until a check

    def stop(self):
        if self.proc is None:
            return
        self._stop_writing.set()
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            pass
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except Exception:
                pass
        self.proc = None

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def loaded_changed(self, known=None):
        """Has any .vo this session holds been rebuilt, removed or replaced?

        A session that answers true is discarded rather than left answering
        for a library that no longer matches its source.  A missing file
        fingerprints as `(None, None)`, so deletion and rebuild compare alike.

        `known` is a fingerprint the caller has already taken, which must
        cover every path in `loaded`; a check takes one for the closure and
        this set together, and reusing it saves stat'ing a few hundred
        libraries a second time.  A path missing from it is a bug, and raises
        rather than being read as unchanged.
        """
        if known is not None:
            return any(self.loaded[p] != known[p] for p in self.loaded)
        now = project.fingerprint(sorted(self.loaded))
        return any(self.loaded[p] != (m, sz) for p, m, sz in now)

    def live_pid(self):
        """The child's pid, or None if there is no live child.

        `proc` is read once, so that a concurrent `stop()` cannot land between
        an `alive` test and a `proc.pid` read.  Callers outside the owning
        thread guard against `OSError`, not the `AttributeError` that would be.
        """
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return None
        return proc.pid

    def rss_bytes(self):
        try:
            with open("/proc/%d/statm" % self.proc.pid) as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except Exception:
            return 0

    def _cpu_ticks(self):
        """utime+stime.  A Rocq blocked on stdin burns no CPU and a running
        tactic does, which is how the two are told apart."""
        try:
            with open("/proc/%d/stat" % self.proc.pid) as f:
                fields = f.read().rsplit(")", 1)[1].split()
            return int(fields[11]) + int(fields[12])
        except Exception:
            return None

    # ------------------------------------------------------------------- I/O

    def _read_loop(self):
        while True:
            try:
                chunk = self.proc.stdout.read(1 << 16)
            except Exception:
                chunk = b""
            with self._cv:
                if not chunk:
                    self._cv.notify_all()
                    return
                self.buf += chunk
                m = None
                for m in protocol.PROGRESS_RE.finditer(self.buf, self._scan_pos):
                    self._scan_pos = m.end()
                if m is not None:
                    self.parsed_end = max(self.parsed_end, int(m.group(2)))
                self._cv.notify_all()

    def _await(self, pred, timeout, what):
        deadline = time.time() + timeout
        with self._cv:
            while not pred():
                if not self.alive:
                    raise SessionDead(self._death_note("waiting for " + what))
                left = deadline - time.time()
                if left <= 0:
                    raise FeedTimeout(what)
                self._cv.wait(min(left, 0.5))

    def _death_note(self, when):
        """Why the child is gone.

        A negative return code is a signal: -9 the OOM killer or a `pkill`,
        -15 a deliberate terminate.  Neither is a bug in the proof, and they
        are indistinguishable in the transcript without this.
        """
        rc = self.proc.poll() if self.proc is not None else None
        if rc is not None and rc < 0:
            why = "killed by signal %d%s" % (
                -rc, " (out of memory, or somebody pattern-killed it)"
                if rc == -9 else "")
        else:
            why = "exited with status %s" % rc
        return "rocq %s %s:\n%s" % (why, when,
                                     self.buf[-4000:].decode("utf8", "replace"))

    def _trim_to_last_prompt(self):
        with self._cv:
            last = None
            for last in protocol.PROMPT_RE.finditer(self.buf):
                pass
            if last is not None:
                self.buf = self.buf[last.start():]
                self._scan_pos = 0

    def _raw_write(self, data):
        """Write bypassing the look-ahead window (recovery text only)."""
        proc = self.proc
        if proc is None:
            raise SessionDead("session was stopped mid-write")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            return
        with self._cv:
            self.stream_written += len(data)
            self._cv.notify_all()

    def _write_all(self, data):
        """Write with a bounded look-ahead, and mark the write finished.

        At most `write_ahead` bytes of unparsed input are in flight, so a
        failure near the top of a file cannot let Rocq re-prove the rest out
        of the pipe buffer.
        """
        self._write_done.clear()
        try:
            self._write_loop(data)
        finally:
            self._write_done.set()

    def _write_loop(self, data):
        pos = 0
        while pos < len(data):
            if self._stop_writing.is_set():
                return
            with self._cv:
                budget = self.parsed_end + self.write_ahead - self.stream_written
                blocked_since = time.time()
                while budget <= 0:
                    if not self.alive or self._stop_writing.is_set():
                        return
                    self._cv.wait(0.2)
                    if time.time() - blocked_since > STALL_GRACE:
                        # No progress and nothing asked for: the window is
                        # smaller than the sentence Rocq is reading.  Widen it
                        # for the rest of the session.
                        if self.write_ahead < MAX_WRITE_AHEAD:
                            self.write_ahead *= 2
                            blocked_since = time.time()
                    budget = self.parsed_end + self.write_ahead - self.stream_written
            n = min(len(data) - pos, max(budget, 512))
            proc = self.proc
            if proc is None:
                return                  # stopped under us; the waiter sees it
            try:
                proc.stdin.write(data[pos:pos + n])
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as e:
                if isinstance(e, OSError) and e.errno not in (errno.EPIPE,):
                    raise
                return
            pos += n
            with self._cv:
                self.stream_written += n
                self._cv.notify_all()

    # ------------------------------------------------------------------ feed

    def _feed_raw(self, data, timeout, stop_on_error=False, cancelled=None):
        """Feed `data`, wait for it to execute, return its sentences.

        A trailing sentinel makes "done" exact: a slow tactic and a finished
        feed are otherwise indistinguishable from outside.

        `cancelled` is an event the caller sets when nobody is waiting for the
        answer any more.  Feeding then stops, the sentence Rocq is on is
        interrupted, and `Abandoned` is raised carrying what did execute --
        from a session still at a prompt, because being left usable is the
        whole point of interrupting it rather than killing it.  The feeds that
        keep a session consistent (the prologue, `BackTo`, recovery) pass no
        event: cutting one of those short is what would lose the session.
        """
        self._sentinel += 1
        sentinel = (SENTINEL_FMT % self._sentinel)
        sentinel_pat = re.compile(
            rb'Chars \d+ - \d+ \[' + re.escape(sentinel.replace(" ", "~").encode()) + rb'\]')
        payload = data + b"\n" + sentinel.encode() + b"\n"
        # Sized from the largest sentence this file has shown, rather than
        # ratcheting: everything in the window still executes when a sentence
        # fails, so the smallest window that cannot deadlock is the right one.
        biggest = max((x.end - x.start for x in self.sentences), default=0)
        self.write_ahead = max(DEFAULT_WRITE_AHEAD, 2 * biggest + 8192)
        base = self.stream_written
        self._stop_writing.clear()
        # Cleared before the thread starts, or the waiter observes the
        # previous feed's flag and skips error detection.
        self._write_done.clear()
        writer = threading.Thread(target=self._write_all, args=(payload,), daemon=True)
        writer.start()
        try:
            try:
                stopped_early, gone = self._await_sentinel(
                    sentinel_pat, timeout, stop_on_error, cancelled)
            except Unterminated:
                # All written and Rocq still waiting: the text ended inside a
                # sentence, comment or string, swallowing the sentinel.  Close
                # it before reporting, or the next feed lands inside it too.
                self._stop_writing.set()
                writer.join(timeout=10)
                self._recover(data, base, timeout)
                self._trim_to_last_prompt()
                raise
            if stopped_early:
                # Stopped mid-chunk -- behind an error, or because the client
                # went away -- so Rocq waits for the rest of a sentence and
                # never reaches the queued sentinel.  Close what is lexically
                # open, terminate, and re-send it.
                self._stop_writing.set()
                writer.join(timeout=10)
                self._interrupt_stalled_work()
                sentinel = self._recover(data, base, timeout)
        finally:
            self._stop_writing.set()
            writer.join(timeout=10)
        segments, _ = protocol.split_prompts(self.buf)
        items = protocol.parse_segments(segments)
        # Anything from before this chunk began belongs to the previous feed,
        # and its offsets are measured from a different base.
        items = [it for it in items
                 if not isinstance(it, protocol.Sentence)
                 or it.stream_start >= base]
        # drop the sentinel and anything after it
        end = len(items)
        for i, it in enumerate(items):
            if (isinstance(it, protocol.Sentence)
                    and it.display == sentinel.replace(" ", "~").encode()):
                end = i
                break
        items = items[:end]
        self._trim_to_last_prompt()
        if gone:
            # At a prompt, with the executed sentences in hand: the caller
            # records them and parks the session on the last of them.
            raise Abandoned("the client went away", items, base)
        return items, base

    def _recover(self, data, base, timeout):
        """Close what is lexically open in `data`, terminate the sentence Rocq
        is waiting on, and return to a prompt.  Returns the new sentinel."""
        consumed = self.stream_written - base
        depth, in_string = self.lex_state(data[:consumed])
        recovery = (b'"' if in_string else b"") + b" *)" * depth + b" .\n"
        self._sentinel += 1
        sentinel = (SENTINEL_FMT % self._sentinel)
        sentinel_pat = re.compile(
            rb'Chars \d+ - \d+ \['
            + re.escape(sentinel.replace(" ", "~").encode()) + rb'\]')
        self._raw_write(recovery + sentinel.encode() + b"\n")
        self._await_sentinel(sentinel_pat, timeout, stop_on_error=False)
        return sentinel

    def _sigint(self):
        """Interrupt the command Rocq is running.

        Rocq protects itself from `Sys.Break` only while executing; a signal
        arriving while it reads input, prints a prompt, or formats a large
        goal kills it outright (`Fatal error: exception Stdlib.Sys.Break`).
        Formatting is the dangerous case, because it burns CPU and reports no
        new sentence, exactly like a stuck tactic.  So a caller must first
        have all three for `INTERRUPT_STALL` seconds: CPU burning, no new
        sentence, and no new output.
        """
        try:
            os.kill(self.proc.pid, signal.SIGINT)
        except (OSError, AttributeError):
            pass

    def _interrupt_stalled_work(self, limit=60.0):
        """SIGINT a single command running long behind an error.

        Once a sentence has failed, the input already in flight still executes,
        against a goal of the wrong shape: that is how a `vm_compute` ends up
        on a free variable and eats tens of GB.  Rocq turns SIGINT into
        `Error: User interrupt.` and carries on.

        Signals only on `_sigint`'s predicate, which holds when one command
        has run a long time and not between two fast ones.
        """
        deadline = time.time() + limit
        idle_since = None
        last_ticks = self._cpu_ticks()
        last_parsed, last_out = self.parsed_end, len(self.buf)
        stuck_since = time.time()
        while time.time() < deadline and self.alive:
            time.sleep(0.05)
            now = time.time()
            ticks = self._cpu_ticks()
            if ticks is None:
                return
            if self.parsed_end != last_parsed or len(self.buf) != last_out:
                last_parsed, last_out, stuck_since = (
                    self.parsed_end, len(self.buf), now)
            if ticks == last_ticks:
                idle_since = idle_since or now
                if now - idle_since > 0.4:
                    return          # drained: it is waiting for input again
                continue
            last_ticks, idle_since = ticks, None
            if now - stuck_since > INTERRUPT_STALL:
                self._sigint()
                stuck_since = now

    def _await_sentinel(self, pat, timeout, stop_on_error=False, cancelled=None):
        """Wait for the sentinel and the prompt that follows it.

        Both, because on the sentinel's `Chars` line alone the following prompt
        may not have arrived; `_trim_to_last_prompt` then leaves the sentinel
        in the buffer and the next feed parses it as one of its own sentences,
        with offsets from the previous chunk.  That corrupts the sentence map
        and makes the next replay resume mid-sentence.

        Returns `(stopped_early, gone)`: whether the feed was cut short
        mid-chunk, so the caller must close the sentence Rocq is waiting on,
        and whether it was cut short because the client went away.  The two
        are separate answers -- a cancelled feed whose text was already
        written in full has nothing left to close -- and `done()` is consulted
        before either, so a feed that finished on its own reports a verdict
        rather than an abandonment.
        """
        def done():
            m = pat.search(self.buf)
            return m is not None and protocol.PROMPT_RE.search(self.buf, m.end())

        def hit_error():
            """Has a sentence failed?  Spotted during the feed, so Rocq does
            not re-prove the rest of the file behind a known error.

            A standing state id is necessary but not sufficient: a bare
            `Show.`, which `coqloop` answers without putting anything in the
            document, leaves it standing too.  So Rocq's own `Error:` must
            appear in the same segment.  That pairing is sound even though
            `Error:` alone is not, because any sentence that printed at all
            advanced the state id.
            """
            for before, after, seg in protocol.split_prompts(self.buf)[0]:
                if before == after and protocol.ERROR_RE.search(seg):
                    return True
            return False

        deadline = time.time() + timeout
        last_ticks = self._cpu_ticks()
        last_move = time.time()
        last_parsed, last_out = self.parsed_end, len(self.buf)
        stuck_since = time.time()
        error_seen = False
        gone = False
        while True:
            with self._cv:
                if done():
                    return False, gone
                if not self.alive:
                    raise SessionDead(self._death_note("mid-feed"))
                if stop_on_error and not error_seen and hit_error():
                    error_seen = True
                    self._stop_writing.set()
                    if not self._write_done.is_set():
                        return True, gone   # caller closes the sentence and retries
                if not gone and cancelled is not None and cancelled.is_set():
                    # Nobody is waiting for this answer any more.  Stop
                    # feeding and interrupt below, exactly as behind an error:
                    # the remaining work is work no one asked for, and the
                    # session is worth more parked than finished.
                    gone = True
                    self._stop_writing.set()
                    if not self._write_done.is_set():
                        return True, True
                    # Everything, sentinel included, is already in Rocq's
                    # hands, so it will reach a prompt on its own and there is
                    # nothing to close.  Wait for it -- interrupting what is
                    # running -- rather than writing a terminator behind a
                    # sentinel that has yet to execute.
                self._cv.wait(0.25)
                if done():
                    return False, gone
            now = time.time()
            if now > deadline:
                raise FeedTimeout("feed exceeded %.0fs" % timeout)
            if self.rss_limit and self.rss_bytes() > self.rss_limit:
                raise MemoryLimit(
                    "rocq reached %.1f GB, over the %.1f GB ceiling"
                    % (self.rss_bytes() / 1e9, self.rss_limit / 1e9))
            if self.parsed_end != last_parsed or len(self.buf) != last_out:
                last_parsed, last_out, stuck_since = (
                    self.parsed_end, len(self.buf), now)
            ticks = self._cpu_ticks()
            if ticks is None or ticks != last_ticks:
                if (error_seen or gone) and now - stuck_since > INTERRUPT_STALL:
                    # One command running long behind a known error, on a goal
                    # of the wrong shape, printing nothing -- or one running
                    # for a client that has gone.  See `_sigint` for why the
                    # predicate must be that narrow either way: a signal
                    # delivered a moment too early kills the session outright,
                    # which is the one outcome an interrupt must not produce.
                    self._sigint()
                    stuck_since = now
                last_ticks, last_move = ticks, now
            elif (self._write_done.is_set()
                  and now - last_move > DEFAULT_IDLE_KILL):
                # No CPU and a sentinel still owed: Rocq is blocked reading
                # stdin, so the text ended mid-sentence and swallowed it.
                raise Unterminated(
                    "end of file inside an unterminated sentence")

    # --------------------------------------------------------------- mapping

    def _absorb(self, items, base, file_start, file_end):
        """Attach .v byte offsets, and the anchor each message is measured from.

        Rocq's Chars counter runs over everything ever written to its stdin, so
        the stream offset where this chunk began converts a Chars range into a
        file range.

        A message's own offsets are measured from neither the sentence nor the
        error line: Rocq consumes the whitespace after a sentence's `.` and
        anchors on the line it lands on.  So

          * whitespace then a newline -> just past that first newline, and
            further blank lines, indentation and comment blocks before the
            sentence count into the offset;
          * anything else first, a trailing comment or a second sentence on
            the same line -> the previous sentence's line.

        The second case shifts every column on the following sentence by the
        width of the comment.  A chunk's first sentence follows this module's
        sentinel, always newline-terminated, so its anchor is the chunk start.
        """
        text = self.text_being_fed
        for it in items:
            if isinstance(it, protocol.Sentence):
                it.start = it.stream_start - base + file_start
                it.end = it.stream_end - base + file_start
        prev_end = None
        for i, it in enumerate(items):
            if not isinstance(it, protocol.Sentence):
                # No Chars line, so no range of its own: a parse error or a
                # toplevel-only command such as a bare `Show.`.
                it.start = diagmod.skip_blanks(
                    text, file_start if prev_end is None else prev_end)
                it.end = file_end
                for later in items[i + 1:]:
                    if isinstance(later, protocol.Sentence):
                        it.end = later.start
                        break
            it.anchor = (file_start if prev_end is None
                         else diagmod.message_anchor(text, prev_end))
            prev_end = it.end
        return items

    # --------------------------------------------------------- edit analysis

    @staticmethod
    def common_prefix_len(a, b):
        n = min(len(a), len(b))
        if a[:n] == b[:n]:
            return n
        lo, hi = 0, n                       # binary search: these are big
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if a[:mid] == b[:mid]:
                lo = mid
            else:
                hi = mid - 1
        return lo

    @staticmethod
    def blank_or_comment(chunk):
        """True if `chunk` is only whitespace and balanced comments.

        Rocq comments nest and a string inside one hides a `*)`, so this
        follows the lexer.  Used only to prove an edit cannot have changed the
        sentence structure; a false negative costs a replay and nothing more.
        """
        i, n, depth = 0, len(chunk), 0
        while i < n:
            if chunk[i:i + 2] == b"(*":
                depth, i = depth + 1, i + 2
                continue
            if depth == 0:
                if chunk[i:i + 1] not in b" \t\r\n":
                    return False
                i += 1
                continue
            if chunk[i:i + 2] == b"*)":
                depth, i = depth - 1, i + 2
            elif chunk[i:i + 1] == b'"':
                i += 1
                while i < n and chunk[i:i + 1] != b'"':
                    i += 1
                i += 1
            else:
                i += 1
        return depth == 0

    @staticmethod
    def lex_state(chunk):
        """(comment_depth, in_string) at the end of `chunk`.

        Cutting a feed short leaves Rocq waiting for the rest of a sentence, so
        it must be handed a terminator, and a bare `.` is not one inside a
        comment or a string.  The exact lexical state says what to close first.
        """
        i, n, depth, in_string = 0, len(chunk), 0, False
        while i < n:
            if in_string:
                if chunk[i:i + 2] == b'""':
                    i += 2
                elif chunk[i:i + 1] == b'"':
                    in_string, i = False, i + 1
                else:
                    i += 1
            elif chunk[i:i + 2] == b"(*":
                depth, i = depth + 1, i + 2
            elif depth and chunk[i:i + 2] == b"*)":
                depth, i = depth - 1, i + 2
            elif chunk[i:i + 1] == b'"':
                in_string, i = True, i + 1
            else:
                i += 1
        return depth, in_string

    UNSAFE_HEAD = re.compile(
        rb'^\s*(?:From\s+\S+\s+)?(?:Require|Declare\s+ML\s+Module|Load)\b')

    def _unsafe_to_undo(self, index):
        """Is any sentence at or after `index` one not worth undoing?

        `BackTo` does undo a `Require` correctly, but unloading a library is
        the one place a warm session could diverge from a cold `coqc`, and only
        header edits reach it, where a cold start costs little.
        """
        for s in self.sentences[index:]:
            if self.UNSAFE_HEAD.match(self.text[s.start:s.end]):
                return True
        return False

    # ----------------------------------------------------------------- check

    def plan(self, text):
        """How to check `text`, without doing it.

        ('cold',)                    -- throw the session away and start over
        ('shift', delta, from_index) -- the edit was whitespace/comments only
        ('replay', offset, state)    -- BackTo `state`, feed from `offset`
        """
        if not self.alive or not self.sentences:
            return ("cold",)
        d = self.common_prefix_len(self.text, text)
        k = 0
        while k < len(self.sentences) and self.sentences[k].end <= d:
            k += 1
        if k == 0 or self._unsafe_to_undo(k):
            return ("cold",)
        prev = self.sentences[k - 1]
        if self.complete and k < len(self.sentences):
            gap_start = prev.end
            gap_end_old = self.sentences[k].start
            delta = len(text) - len(self.text)
            gap_end_new = gap_end_old + delta
            if (d >= gap_start and gap_end_new >= gap_start
                    and self.text[gap_end_old:] == text[gap_end_new:]
                    and self.blank_or_comment(self.text[gap_start:gap_end_old])
                    and self.blank_or_comment(text[gap_start:gap_end_new])):
                return ("shift", delta, k)
        return ("replay", prev.end, prev.state_after)

    def check(self, text, timeout=1800, _retry=True, cancelled=None):
        """Execute `text`, reusing as much of the warm prefix as is sound.

        A child that dies mid-check, from a neighbour's `pkill` or an interrupt
        landing badly, costs a cold run rather than an error.

        `cancelled` is an event the caller sets when the client that asked for
        this check has gone; `Abandoned` is then raised instead of a verdict,
        from a session parked at the line Rocq had reached.
        """
        try:
            return self._check(text, timeout, cancelled)
        except SessionDead:
            if not _retry:
                raise
            self.stop()
            if cancelled is not None and cancelled.is_set():
                # The retry is a COLD run of the whole file, which is the
                # work the interrupt asked us to stop.  There is nobody to
                # answer, so the session is left dead for the next check to
                # replace rather than re-proving a file for no one.
                raise Abandoned("rocq died as the client went away")
            self.start()
            return self.check(text, timeout=timeout, _retry=False)

    def _check(self, text, timeout, cancelled=None):
        t0 = time.time()
        plan = self.plan(text)
        mode = plan[0]

        if mode == "shift":
            # Every stored anchor from k on moves with the text, including
            # sentence k's, which sits in the gap that changed.  An anchor here
            # is the origin the cached message offsets were measured from, not
            # a semantic position, so it must travel with the text they point
            # into; recomputing it from the new gap leaves them short by the
            # gap's change in width.
            _, delta, k = plan
            for s in self.sentences[k:]:
                s.start += delta
                s.end += delta
                s.anchor += delta
            self.text = text
            return CheckResult(True, self._prefix_diags(), mode="shift",
                               replayed=0, seconds=time.time() - t0,
                               total=len(self.sentences))

        if mode == "cold":
            # A cold check needs a child that has run nothing, and one that
            # has JUST BEEN STARTED already is one, so starting a second pays
            # a `rocq repl` startup to arrive where we are.  That is what the
            # first check of every file used to do: the slot starts the
            # session, `plan` then says cold because the sentence map is
            # empty, and this restarted it -- two spawns, and the pid written
            # between them named the one that was killed.
            #
            # "Fed nothing but the prologue" rather than "the sentence map
            # is empty", deliberately.  The map is also empty after a check
            # that failed on its very first sentence, and that child is NOT
            # equivalent to a new one: a `Require` can fail with the library
            # loaded anyway, which a cold `coqc` would not have.  Reusing
            # only a child that has never seen the file needs no argument
            # about what Rocq kept.
            if not (self.alive and self.fresh):
                self.start()
            resume = 0
        else:
            _, resume, state = plan
            self._backtrack(state)
            keep = 0
            while keep < len(self.sentences) and self.sentences[keep].end <= resume:
                keep += 1
            del self.sentences[keep:]

        if cancelled is not None and cancelled.is_set():
            # Gone before a single sentence of this check ran.  What the
            # session holds is the prefix the replay kept, and it has to be
            # left saying so: `text` outrunning the sentence map is what makes
            # the next check resume from a state Rocq is not in.
            self._park_at(text[:resume])
            raise Abandoned("the client went away before the check started")

        # From here the child has seen the file, so it is no longer one a
        # cold check may take as it finds.  Cleared before the feed rather
        # than after: what disqualifies it is having been fed, not what the
        # feed then did.
        self.fresh = False
        try:
            items, base = self._feed_raw(text[resume:], timeout=timeout,
                                         stop_on_error=True,
                                         cancelled=cancelled)
        except Unterminated:
            self._park_at(text[:resume])
            return CheckResult(
                False,
                self._prefix_diags() + [Diag(
                    "error", resume,
                    b"Error: Syntax error: end of file inside an "
                    b"unterminated sentence")],
                mode=mode, replayed=0, seconds=time.time() - t0,
                total=len(self.sentences))
        except Abandoned as gone:
            # Interrupted part-way through.  What executed before the signal
            # is real work on a session that is still at a prompt, so it is
            # recorded exactly as a verdict's would be -- the result is simply
            # thrown away, because there is nobody it is for.
            self._conclude(text, gone.items, gone.base, resume, mode, t0)
            gone.line = self.text.count(b"\n") + 1
            raise
        return self._conclude(text, items, base, resume, mode, t0)

    def _conclude(self, text, items, base, resume, mode, t0):
        """Record what a feed executed, and say what it found.

        Called however the feed ended, with a verdict or with an interrupt:
        the session state left behind -- the sentence map, the parked prefix,
        the state Rocq sits in -- is the same either way, and is what makes
        the next check a replay from the line this one reached.
        """
        self.text_being_fed = text
        items = self._absorb(items, base, resume, len(text))
        first_bad = next((i for i, it in enumerate(items) if it.failed), None)
        # A `None` stop slices to the end, which is exactly what "nothing
        # failed, so keep all of it" means.
        good = items[:first_bad]
        # The reused prefix's warnings, which a warm run never re-executes:
        # without them a replay drops every warning above the edit and stops
        # matching `coqc`.
        diags = self._prefix_diags()
        # Then everything this check executed, in Rocq's order, output
        # included.  A toplevel-only item such as a bare `Show.` never reaches
        # the sentence map, so this list carries it instead.
        executed = items if first_bad is None else items[:first_bad + 1]
        for it in executed:
            diags += _diags_of(it, include_info=True)
        self.sentences.extend(s for s in good if isinstance(s, protocol.Sentence))
        if first_bad is None:
            self.text = text
            self.complete = True
        else:
            # Parked at the broken sentence, so the next edit, which is the fix
            # for it, replays from here and nothing before.
            self._park_at(text[:items[first_bad].start])
        return CheckResult(first_bad is None, diags, mode=mode,
                           replayed=len(items), seconds=time.time() - t0,
                           total=len(self.sentences))

    def _park_at(self, prefix):
        """Leave the session consistent, holding `prefix` and nothing more.

        Every way of stopping short ends here: a broken sentence, a file that
        ended mid-sentence, a client that went away.  `text` must never claim
        more than the sentence map covers -- `plan` reads the two together,
        and a longer `text` makes the next check resume from a state Rocq is
        not in -- and Rocq itself must be back at the last sentence the map
        ends on, whatever the failed one left behind.
        """
        self.text = prefix
        self.complete = False
        if self.sentences:
            self._backtrack(self.sentences[-1].state_after)

    def _prefix_diags(self):
        """The errors and warnings of the sentences being kept, not their
        output: the prefix printed nothing this time round, while `coqc`
        reports its diagnostics for this version of the file.
        """
        out = []
        for s in self.sentences:
            out += _diags_of(s)
        return out

    def _backtrack(self, state):
        self._feed_raw(("BackTo %d." % state).encode(), timeout=300)

    def _state_now(self):
        """The state id Rocq is parked at, from its most recent prompt."""
        with self._cv:
            last = None
            for last in protocol.PROMPT_RE.finditer(self.buf):
                pass
            if last is None:
                return None
            body = protocol.PROMPT_BODY_RE.match(last.group(1))
            return int(body.group(2)) if body else None

    LIB_NAME_RE = re.compile(rb'^[^\s"<>]+$')
    LOCATED_RE = re.compile(
        rb'(\S+) has been loaded from file\s+(.+?)\s*$', re.S)

    def loaded_libraries(self, timeout=300):
        """{logical name: .vo path} for every library Rocq has loaded.

        Asked of Rocq, via `Print Libraries.` for the names and `Locate
        Library` for the files, because `rocq dep` sees neither an installed
        library nor a `Require` added after the session started.  The queries
        are undone with `BackTo`, leaving the session parked where it was, and
        each name is located once per session.
        """
        if not self.alive:
            return {}
        parked = self._state_now()
        try:
            items, _ = self._feed_raw(b"Print Libraries.", timeout=timeout)
            names = []
            for it in items:
                if not isinstance(it, protocol.Sentence):
                    continue
                for line in protocol.message_text(it.messages).splitlines():
                    line = line.strip()
                    if line and self.LIB_NAME_RE.match(line):
                        names.append(line.decode("utf8", "replace"))
            unknown = [n for n in names if n not in self._libmap]
            if unknown:
                query = b"".join(b"Locate Library %s.\n" % n.encode()
                                 for n in unknown)
                items, _ = self._feed_raw(query, timeout=timeout)
                for it in items:
                    if not isinstance(it, protocol.Sentence):
                        continue
                    m = self.LOCATED_RE.search(protocol.message_text(it.messages))
                    if m is None:
                        continue
                    name = m.group(1).decode("utf8", "replace")
                    path = re.sub(rb'\s+', b'', m.group(2)).decode("utf8", "replace")
                    self._libmap[name] = os.path.normpath(
                        os.path.join(self.cwd, path))
        finally:
            if parked is not None and self.alive:
                self._backtrack(parked)
        return {n: self._libmap[n] for n in names if n in self._libmap}


def _diags_of(item, include_info=False):
    out = []
    for blob in _split_messages(item.messages):
        kind = protocol.classify(blob)
        if kind != "info" or include_info:
            out.append(Diag(kind, item.anchor, blob))
    return out


INFOMSG_RE = re.compile(rb'<infomsg>.*?</infomsg>', re.S)


def _split_messages(raw):
    """Split one sentence's output into individual message blobs.

    Two boundaries, not one: errors and warnings are delimited by their
    `Toplevel input, characters A-B:` line, and `-emacs` wraps each info
    message in `<infomsg>...</infomsg>`.  On the location line alone, an info
    message printed just after a warning rides along inside the warning's blob
    and is classified and rendered as part of it.
    """
    if not raw or not raw.strip():
        return []
    segments, last = [], 0
    for m in INFOMSG_RE.finditer(raw):
        segments.append((False, raw[last:m.start()]))
        segments.append((True, m.group(0)))
        last = m.end()
    segments.append((False, raw[last:]))
    out = []
    for is_info, seg in segments:
        if not seg.strip():
            continue
        if is_info:
            out.append(seg)
        else:
            out += [p for p in
                    re.split(rb'(?m)(?=^Toplevel input, characters )', seg)
                    if p.strip()]
    return out


class Diag:
    """One error or warning, still carrying Rocq's raw blob.

    The span is derived rather than stored: it is the anchor plus the offsets
    in the blob, which is what `locate` does.  Turning it into a line and
    column needs the file text, which only the caller holds.
    """

    __slots__ = ("kind", "anchor", "raw")

    def __init__(self, kind, anchor, raw):
        self.kind, self.anchor, self.raw = kind, anchor, raw

    def span(self):
        return diagmod.locate(self.raw, self.anchor)

    def message(self):
        return diagmod.strip_location(self.raw)

    def render(self, display_path, text):
        return diagmod.render(display_path, text, self.span(),
                              self.message())

    def __repr__(self):
        return "Diag(%s, @%s, %r)" % (self.kind, self.anchor,
                                      self.message()[:60])


class CheckResult:
    def __init__(self, ok, diags, mode, replayed, seconds, total):
        self.ok, self.diags, self.mode = ok, diags, mode
        self.replayed, self.seconds, self.total = replayed, seconds, total

    def __repr__(self):
        return "CheckResult(ok=%s, mode=%s, replayed=%d/%d, %.1fs, %d diags)" % (
            self.ok, self.mode, self.replayed, self.total, self.seconds,
            len(self.diags))
