# A simpler concurrency plan

The fix for the bugs in [CONCURRENCY-BUGS.md](CONCURRENCY-BUGS.md), replacing
the four-part one written up at the bottom of that file. It is smaller, it is
mostly deletion, and the correctness argument fits in a paragraph.

Line numbers are as of `fe3a9e6`.

## The root cause the bug notes stop one step short of

The notes diagnose it as "the daemon serialises *use* of a session correctly
and gets *ownership* wrong". True, but the reason is mechanical and it is one
line of code:

> **`Session` is constructed under the table lock** (`_entry`, `server.py:209`).

Everything follows from that. Because the constructor call lives inside `with
self.lock`, the two arguments that are fixed at construction -- `flags` and the
toolchain's `rocq`/`env` -- can only be changed by building a new `Entry` and
swapping it into `self.sessions`. So "throw this session away" had to be
spelled as a **table mutation**, and the table is the only thing holding a
reference to a live `rocq repl`. Every bug in the notes is a different way of
losing that reference, or of resolving it to the wrong object afterwards.

Move the construction inside the caller's own ownership of the file and "throw
the session away" becomes a field assignment on an object nobody else can see.
The table stops being the place where sessions are created and destroyed, and
becomes what it should have been: a record of who owns what.

## The one rule

A session is **checked out, used, and returned**. There is no lock on a
session, because a checked-out session is not shared with anybody.

> A file's **`Slot`** is in exactly one of two tables: `self.idle`, where a
> slot may be taken from, and `self.busy`, where a slot may only be *looked
> at*. `_checkout` moves it one way and `_return` moves it back, both under
> `self.lock`. **A thread may use the session inside a slot only between its
> own checkout and its own return** -- during which the slot is in `busy`, and
> the checking thread holds the only reference anyone is allowed to use.

Checking out a file that is already checked out does not wait and does not
create a second slot. It fails, and the caller is told the file is busy.

That is the entire concurrency design for sessions. One lock, guarding two
dicts and nothing else, held only for the moves between them; and no thread
ever waits on another thread, so there is no lock ordering to establish and no
hang to reason about. (`DepGraph` and `Compiler` keep their own locks over
their own state, as they do today.)

The two supporting rules are about that lock:

* Nothing blocking happens under `self.lock` -- no `Popen`, no `stat`, no
  `stop()`. Moving a dict entry is all it ever does, and it is never held while
  another lock is taken.
* A slot in `busy` may be **read** by the reclamation paths, never used. What
  "read" means is pinned down in [Looking at a busy slot](#looking-at-a-busy-slot)
  below; it is a short list, and everything on it is already crash-safe.

### Why checkout rather than a per-slot lock

An earlier draft gave each slot a `threading.Lock` and left it in one table.
Checkout is better for a reason that is not about either mechanism's cost:

**A lock is a rule; a checkout is a fact.** "Only touch `slot.sess` under
`slot.lock`" is a sentence in a docstring, and the next person to add a feature
can look a slot up in the table and use it without ever seeing that sentence.
Under checkout there is nothing to look up: an in-use slot is not in the table
you take slots from, and the only reference to it was handed to one thread.

It also closes a window the lock version still had. Between `_slot(path)`
returning and the caller taking the lock, the slot sat in the table unlocked,
so `_evict` could reach in and discard the warm session the caller was about to
use. Checkout hands the slot over inside the same critical section that makes
it unavailable to everyone else, so that window does not exist.

And it deletes `_idle_victim`. "The least-recently-used session that is not
mid-check" stops being a question to answer with a non-blocking lock probe:
every slot in `idle` is by definition not mid-check.

This only works because a busy file is refused rather than queued. If callers
waited, checkout would need a condition variable to wait on and would be the
worse design. Fail-fast is what makes it the simpler one.

## Why that is enough

Each bug's absence is a one-line consequence of "in exactly one table, moved
only under the lock".

**Every live `rocq repl` is reachable.** A slot is in `idle` or in `busy` at
every instant, including every instant during the move, because the move
happens under the lock. A process lives in `slot.sess` and is created and
destroyed only by the thread that holds the slot. So there is no state in which
a live child is not reachable from one of the two tables -- there is no
operation that drops a reference to a slot at all, only one that moves it.
*Bugs 1, 5 and 6.*

**A key names one slot**, so "drop the session for this path" and "drop the
session I am using" are the same thing, and `_drop`'s lookup-by-key has nothing
left to get wrong. It becomes `slot.discard()` on the object already in hand.
*Bug 2.*

**Nothing is ever replaced.** A second check of a file does not get a slot at
all, so it cannot misread the first's state, and liveness stops being a
staleness criterion: "there is no process yet" is a step of `ready()`, not a
reason to touch a table. *Bug 3.*

**Eviction cannot take a session out from under a check**, and not because it
checks: a checked-out slot is not in `idle`, and `idle` is the only place
eviction looks. The dangerous case is gone rather than guarded against.

**A second session for one file cannot be created**, because the only thing
that creates one is a checkout, and a checkout of a busy file fails. This is
the property the current code violates by accident, and it is now the same
fact as "the slot is in `busy`".

Compare with the plan it replaces: no `self.orphans` list, no cap on it, no
draining it from `_evict`, `_reaper` and `shutdown`, no identity-checked
`_drop`, no orphan rows in the pid file. None of it gets built.

### Measured, with one clause deleted

Most of the win arrives before any of the restructuring above. Deleting `or not
entry.sess.alive` from `_stale_entry` (`:173`) and re-running bug 3's repro --
two concurrent cold checks of one file, from a barrier, on Rocq 9.1.1:

```
wall 10.1s
  A: passed=True mode=replay  0.0s
  B: passed=True mode=cold   10.0s
_entry returned the SAME entry to both: True
rocq repls spawned:        [241425, 241428]
in the session table:      [241428]
LEAKED (alive, untracked): []
sessions pid file:         ''
```

Against the notes' before: two entries, two parallel cold starts, one leaked
child. Now one session, one cold start, the other check replaying warm behind
it, and nothing left over. (Two pids spawned and one alive is not a leak: the
session retires its own process on the cold path, `session.py:774`.) That one
clause is bug 3, and bug 3 is what made bugs 1 and 2 routine.

`sessions pid file: ''` in that same run is finding 5 below, reproduced a
second time by accident: a live session, and nothing written down.

The rest of the plan is what makes it *provable* rather than merely observed,
and what fixes the three paths -- flags change, toolchain change, eviction --
that this clause does not touch.

## Three more findings, in the same family

All three were reproduced against Rocq 9.1.1 (`/root/.opam/rocq911`) while
writing this. They are the same root cause, and the plan above fixes all three
without a line aimed at any of them.

### 5. The pid file never names the session that is actually running

`_record_sessions` is called from `_entry` only `if created` -- which is
*before* `do_check` starts the session -- and from `_drop`. A session started
by `do_check` is therefore never recorded. Three checks of one file in a
one-file workspace:

```
check 1: passed=True mode=cold    live session pid=240591
   .rocq-warm/sessions contains: ''
check 2: passed=True mode=replay  live session pid=240591
   .rocq-warm/sessions contains: ''
check 3: passed=True mode=replay  live session pid=240591
   .rocq-warm/sessions contains: ''

after checking a second file, sessions file: '240591\t/tmp/probe/C.v\n'
   table: C.v pid=240591 alive=True
   table: D.v pid=240610 alive=True
```

The file is empty for as long as the daemon holds one session, and after that
it is always one session behind. So `reap_strays` -- the mechanism README
promises collects the *busy* child a killed daemon leaves behind, the one case
the stdin-EOF story does not cover -- has nothing to collect in the single
session case, and never knows about the newest session in any case.

There is a second defect in the same eight lines: the row builder tests
`e.sess.alive` and then reads `e.sess.proc.pid`, two reads of a field another
thread may null in between, and the `AttributeError` that follows is not an
`OSError`, so the surrounding `except OSError` does not catch it. It would
surface as a failed check. `do_status` (`:535`) has the same two-step.

### 6. Eviction removes an entry a check is about to use

`_entry` calls `_evict()` on the way out, while the caller has not yet reached
`with entry.lock`. `_idle_victim` skips every slot whose lock it cannot take --
so with the other slots busy, the free one it picks is the brand-new entry the
caller is about to start a session in. Two files, `max_sessions=1`, no
`--cold`, no rebuilt `.vo`:

```
B's entry created:            True
B still in the session table: False
table now holds:              ['A.v']
```

`do_check` then runs `with eb.lock:` and `eb.sess.start()` on an entry no
reclamation path can reach. This is bug 1 through a different door, and it
needs no staleness at all -- only more files in flight than session slots,
which is the ordinary shape of several agents working in one checkout.

### 7. The global table lock is held across a few hundred `stat` calls

`_entry` calls `_stale_entry` under `self.lock`, which calls
`entry.loaded_changed()`, which calls `project.fingerprint(sorted(entry.loaded))`
over every `.vo` the session has loaded. DESIGN puts that set at "a few hundred
libraries" for a real development. Every concurrent check of **every other
file** in the workspace waits behind it, because they all want the same lock.

Under this plan that call moves into `ready()`, inside the caller's own
checkout, where `self.lock` is long since released and the only file it can
delay is its own.

## What the code becomes

`Entry` becomes `Slot`, and `Server.sessions` becomes `Server.idle` and
`Server.busy`. Both renames are worth it: the ownership rule changes, and every
site that used to reach into the table should be looked at once rather than
kept working by accident.

```python
class Slot:
    """One file's session.  Whoever checked it out owns it outright: no lock,
    because nothing else may touch it until it is returned."""

    def __init__(self, path):
        self.path = path
        self.sess = None            # the rocq repl, or None
        self.flags = self.cwd = self.toolchain = None
        self.loaded = {}            # .vo -> (mtime_ns, size), as loaded
        self.libraries = {}
        self.last_used = time.time()
        self.busy_since = None      # set by _checkout, cleared by _return

    def ready(self, flags, cwd, toolchain, rocq, env, cold)
    def discard(self)               # stop the session, keep the slot
```

The whole of the concurrency lives in two Server methods:

```python
def _checkout(self, path):
    """The slot for `path`, owned by this thread until _return.  None if
    somebody else has it."""
    with self.lock:
        if path in self.busy:
            return None                     # one check per file, no waiting
        slot = self.idle.pop(path, None)
        if slot is None:
            slot = Slot(path)
        slot.busy_since = time.time()
        self.busy[path] = slot
        return slot

def _return(self, slot):
    with self.lock:
        slot.busy_since = None
        slot.last_used = time.time()
        self.idle[slot.path] = self.busy.pop(slot.path)
```

Two notes on `_checkout`, because they are the only places it can go wrong.
The `busy` test must come first and must return rather than fall through to
`Slot(path)`: building a second slot for a checked-out file is precisely the
bug being fixed, and it is one missing `return` away. And `_return` must be
unmissable -- a `finally`, or better a context manager, since a slot that is
never returned leaves its file permanently refused.

`ready()` is where every "throw the session away" reason now lands, and it is a
straight line with no table in it:

```python
def ready(self, flags, cwd, toolchain, rocq, env, cold):
    if (flags, toolchain) != (self.flags, self.toolchain):
        self.discard()
        self.flags, self.cwd, self.toolchain = flags, cwd, toolchain
    elif cold or self.loaded_changed():
        self.discard()
    if self.sess is None:
        self.sess = session_mod.Session(self.path, self.flags, cwd=self.cwd,
                                        rocq=rocq, env=env, rss_limit=...)
    if not self.sess.alive:
        self.sess.start()
    return self.sess
```

The flags/toolchain case is no longer special. That is the whole trick: it was
special only because the constructor was on the wrong side of the wrong lock.

`do_check` loses its five `self._drop(path)` calls and gains a `finally`:

```python
slot = self._checkout(path)
if slot is None:
    return {"ok": False, "busy": self._busy_row(path)}  # exit 2, not a verdict
try:
    watched = sorted(set(closure) | set(slot.loaded))
    pre = fingerprint(watched)                # measured while we own it
    sess = slot.ready(flags, cwd, toolchain, rocq, env, cold=req.get("cold"))
    self._record_sessions()                   # a pid exists exactly now
    try:
        result = sess.check(text, timeout=timeout)
    except (FeedTimeout, MemoryLimit, SessionDead) as e:
        slot.discard()
        return {...}
    ...
    if moved or unreliable:
        slot.discard()
finally:
    self._return(slot)                        # even if something above raised
# The file is checkable again from here.  `--compile` can take minutes and
# does not touch the session, so it must not be inside the checkout.
if result.ok and not moved and req.get("wait_vo"):
    ...
self._evict()
```

Returning the slot before the `--compile` wait is a small behaviour change
worth taking deliberately: today a `--compile` holds the entry lock only for
the check, but under a checkout it would hold the *file* for the length of a
real `rocq compile` unless the boundary is drawn here.

Deleted outright: the replacement block in `_entry` (`:202`-`:206`) with its
`del`, its try-lock and its `to_stop`; `_stale_entry`'s `not entry.sess.alive`
clause; `_drop`'s lookup by key; and `_idle_victim` entirely.

The reclamation paths only ever look at `idle`:

* `_evict` and `reap_idle` pick the LRU slot **in `idle`** and `discard()` it.
  No lock probing, no "is this one mid-check": a busy slot is not there to be
  picked. They may take the last idle session under machine-wide pressure,
  exactly as today.
* `shutdown` is the one place that touches `busy`, and the reason belongs in
  the docstring: it is about to `os._exit`, a mid-check child has to die
  anyway, and a shutdown that waits for a thirty-minute check is the hang a
  shutdown path must not have. It still reaches everything, because everything
  is in one of the two tables.
* `_record_sessions` moves to where a pid changes -- after `ready()` starts a
  session, and inside `discard()` -- iterates both tables, and reads `proc`
  once through a new `Session.live_pid()`, which `do_status` uses too.
* "Is the daemon empty?" becomes "no slot in either table holds a session",
  rather than `if self.sessions:`.

### Looking at a busy slot

Three things outside the owning thread need to know about a checked-out
session, and pretending otherwise would be the one dishonest part of "no
concurrency to think about". The list is closed, and short:

| who | reads | why it is safe |
|---|---|---|
| `_evict` | `sess.rss_bytes()` | the budget has to count busy sessions or it over-admits; the call is one `/proc` read that already returns 0 on any exception |
| `_record_sessions`, `do_status` | `sess.live_pid()` | a single read of `proc`, which is what `live_pid` exists to make atomic |
| `shutdown` | `sess.stop()` | the documented exception above |

Everything else -- `check`, `start`, `ready`, `discard`, the sentence map, the
loaded set -- is the owner's alone. The rule to keep is that a non-owner may
**observe** a busy slot and may never **change** it, with `shutdown` as the one
stated exception, on its way out of the process.

## Is the resulting interface good enough for agents?

The behaviour change a caller can observe is that **a second check of the same
file is refused, where today it races.** It does not queue and it does not
wait. That is the only thing this plan takes away, and it is worth arguing
rather than asserting.

**The axis agents actually use is untouched.** An agent loop is sequential per
file -- write, check, read the error, write again -- and parallel across files:
several agents in a checkout, or one fanning out over a directory. Different
files are different slots, no lock is held across a check, and parallelism
there is bounded only by the memory budget. Finding 7 above means this plan
makes that axis *faster*, not slower.

**A second session for one file buys nothing, so refusing it costs nothing.**
Two `rocq repl`s for one file is twice several gigabytes spent answering one
question, and the loser pays a second full cold start -- minutes on a real
development. The measurement above, on the same repro and the same machine:

| | wall | sessions | cold starts | leaked | what the 2nd check got |
|---|---|---|---|---|---|
| racing, as today | 12.4s | 2 | 2 | 1 child | `cold`, 12.3s |
| one session | 10.1s | 1 | 1 | none | `replay`, 0.0s, 0 sentences |

The last column is the whole argument. The second check's work is worthless:
once the first has finished, the same question is answered by a warm replay
that executes nothing and takes no measurable time. A caller told "busy, try
again" therefore loses one round trip and nothing else -- and a caller made to
wait would have been handed that same 0.0s replay at the end of the wait.

**Refusing beats waiting**, and not mainly because it is less code:

* A wait has no honest length. What is being waited on is a proof, which may
  legitimately run until the check timeout, so every default is either too
  short to help or long enough to be indistinguishable from a hang.
* A refusal is information the caller can act on; a wait is that same
  information withheld. An agent told the file is busy can go check another
  file, which is what it should be doing anyway.
* It keeps the "nothing ever blocks on another thread" rule, which is what
  leaves the design with no lock-ordering argument to make and no hang to
  reason about. A `--wait` would have put one back.
* A caller that genuinely wants to wait can, in the one line of shell or Python
  it would have written anyway, with a backoff of its own choosing. The daemon
  should not be picking that policy on its behalf.

So the interface needs one addition, and it is not a flag:

**1. A `busy` refusal.** When `_checkout` returns `None`, the reply is
`{"ok": false, "busy": {...}}` and the client exits **2** -- explicitly not a verdict about
the proof, the same category as a stale dependency -- with one line naming what
the session is doing and since when:

```
rocq-warm: proofs/Big.v NOT CHECKED -- a check of it has been running for 47s
rocq-warm: this file is already being checked; check another file, or retry
```

No request field, no CLI flag, no default to argue about. The client already
has a refusal path with the right exit code and the right shape
(`report_refusal`), and this is one more case in it.

**2. `status --json`, with `busy` and `busy_since` per slot.** Optional, and
worth it: a refused agent may want to know whether the file has been busy for
two seconds or twenty minutes before deciding what to do. `status` already
returns everything else and has no machine-readable form; its `idle` field is
time since `last_used`, which does not distinguish "idle 3s, finished" from
"running, 3s in".

And one correctness fix that matters more for an agent than for a human, which
is bug 4:

**3. The server renders diagnostic locations and echoes the digest.** It
already has the exact bytes it checked and already computes
`digest_of(text=text)`. Returning `line`/`col_start`/`col_end` alongside the
span removes the client's second `open(path, "rb").read()` -- the unguarded
re-read that silently reports a diagnostic at the wrong line when the file
changed during a check. Returning the digest lets an agent confirm the verdict
is about the bytes it wrote. An agent edits fast enough for both to matter.

### Deliberately not built

* **No waiting, no queue, no `--wait`.** An earlier draft of this plan took a
  per-slot lock with a deadline. It was strictly worse: it needed a request
  field, a CLI flag and a default nobody can pick correctly, it reintroduced
  blocking between threads, and it bought the caller a 0.0s replay it could
  have had by retrying. Fail fast, say why, and let the caller decide. Not
  waiting is also what leaves checkout as the simplest mechanism that works;
  a queue would need something to wait on, and the lock would come back.
* **No preemption, no supersede-by-digest.** Tempting, and the compiler
  already works this way for `.vo` jobs: a `Job` carries the digest of the text
  it was asked to compile, and `Server._cancel_other_text` (`:525`) cancels a
  running job whose text has moved on. It is the natural next step if agents
  turn out to collide on one file in practice. But it needs an abort flag
  polled inside the feed loop, and the feed loop is the one place where DESIGN
  says cutting short needs lexical recovery -- so it should be paid for by a
  measurement, not by anticipation. Nothing here forecloses it: an abort flag
  is *set* by another thread but *acted on* only by the owning thread, so it
  changes no ownership and the rule above still holds verbatim.
* **No second session per file.** That is what the current code does by
  accident, and it is the bug.

### What it costs, stated plainly

Nothing blocks, so the cost is not latency: it is that **a check of a file
already being checked does not happen at all**, and the caller has to notice.
Two consequences worth naming rather than discovering:

* An editor-on-save hook racing a manual check now reports a refusal where it
  used to report a verdict. That is the right answer -- the verdict it used to
  give came from a second session that should never have existed -- but it is
  a visible change, and the message has to be clear enough that nobody reads
  exit 2 as "the proof is broken". The stale-dependency refusal already sets
  that precedent and shares the exit code.
* A caller that wants a verdict rather than a refusal has to retry. The
  retry is cheap by construction: the check it is queued behind is the one
  warming the session it will replay against, measured at 0.0s and 0 sentences
  above. A loop with a few seconds of backoff is the whole of it.

What already bounds how long a file stays busy: the SIGINT heuristic bounds a
*failing* proof's burn (tens of minutes to 5.2s on the file in DESIGN), the RSS
ceiling bounds a runaway's memory, and the wall timeout bounds everything.

## Tests

The four in `tests/test_rocq_warm_concurrency.py` are provisional, and three of
them lose their premise, which is the point -- they assert on a replacement
that will no longer happen, or on a second check that will no longer run.

| test | under this plan |
|---|---|
| `test_a_fresh_entry_is_not_stale` | passes once the `alive` clause goes. Restate it for checkout: check out, return, check out again, and assert the *same* slot comes back -- a not-yet-started session must not cost anyone a new one |
| `test_two_cold_checks_of_one_file_share_a_session` | premise gone: they do not share, the second is refused. Rewrite as `test_a_concurrent_check_of_one_file_is_refused` -- two threads off a barrier, exactly one verdict and one `busy` refusal, one `rocq repl`, nothing leaked |
| `test_a_replaced_busy_session_is_stopped` | premise gone. Rewrite: a `--cold` check arriving mid-check is refused and the running check is unharmed; the *next* `--cold` check restarts in place, one slot, one live pid |
| `test_a_timing_out_check_does_not_drop_its_replacement` | premise gone. Rewrite: a timed-out check discards its session and releases a usable slot; the next check cold-starts in the same slot |

The rewritten second test is the one to be careful with. "Refused" has to mean
refused *for this reason*: assert on the `busy` key, not merely on `ok` being
false, or a stale-dependency refusal or a missing `rocq` would satisfy it just
as well. And assert the refusal is prompt -- it is the one thing a reader will
want proof of, and it is what a re-introduced wait would silently break.

Three to add, one per new finding:

* the pid file names the live session after a single check of a single file,
  and names both after two;
* `max_sessions=N` with `N+1` files in flight leaks nothing;
* `loaded_changed()` does not run under `Server.lock` -- assert it by holding
  the table lock in one thread and requiring another thread's `_checkout` to
  return promptly, or simply by construction once the call has moved.

And two for the hazards checkout introduces, which are the price of it:

* **a slot is always returned.** Make `do_check` raise from inside the
  checkout (patch `Session.check` to throw something unexpected), then assert
  the next check of that file is not refused. Without the `finally` the file
  is dead for the daemon's life, which is a worse failure than any bug in the
  notes, and it is invisible until somebody checks that file twice.
* **a busy file never gets a second slot.** Check one out by hand, call
  `_checkout` again, assert it returns `None` *and* that no new `Slot` was
  constructed -- the missing `return` that falls through to `Slot(path)` is
  the one-character version of bug 3, and an assertion on the return value
  alone would not catch a slot built and thrown away.

And one worth more than any of them: make the invariant a `tearDown`
assertion in `ServerCase`, so **every** test in the file checks it --

> no pid in `spawned` is alive unless it is the live pid of a slot in `idle`
> or in `busy`

-- which is `leaked() == []` with `tracked_pids` widened to both tables. It is
already written in the harness and currently asserted in one test out of four.

## Order

Independently shippable, in this order:

1. `Slot`, `_checkout`, `_return`, `ready`, `discard`; delete the replacement
   path and `_idle_victim`. Fixes bugs 1, 2, 3 and findings 6 and 7. The only
   step that touches the session table, and the only one that is not small.
2. `_record_sessions` at start/stop, plus `Session.live_pid()`. Fixes finding 5.
3. The `busy` refusal on `check`, and its message; `status --json` with the
   busy bit if it is wanted.
4. Server-side rendering and the digest in the reply. Fixes bug 4.
