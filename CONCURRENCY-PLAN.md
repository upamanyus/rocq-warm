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

Move the construction one lock inwards and "throw the session away" becomes a
field assignment. The table then never has to change shape at all, and a
reference that never moves cannot be lost.

## The one rule

> `Server.sessions` maps a file to a **`Slot`**. A slot is created once and
> **never removed and never replaced**. The `rocq repl` lives in `slot.sess`,
> and **only the thread holding `slot.lock` may read, start or stop it.**

The slot is the lock, the bookkeeping, and the identity of "the session for
this file". It costs a few hundred bytes. The `rocq repl` is what costs
gigabytes, and it is the only thing any reclamation path takes away.

Two supporting rules, so that deadlock is not a question either:

* `self.lock` protects `self.sessions` and `self.graphs`, and nothing blocking
  happens under it -- no `Popen`, no `stat`, no `stop()`, no acquiring a slot
  lock.
* The two locks are never nested, in either order.

## Why that is enough

Each bug's absence is a one-line consequence.

**Every live `rocq repl` is reachable.** A process is created only by
`Slot.start` and destroyed only by `Slot.stop`, both under the slot lock, both
assigning `slot.sess` before releasing it; the slot is in the table from before
the process exists until after the daemon is gone. There is no operation that
drops the last reference, because there is no operation that drops a reference
at all. *Bugs 1, 5 and 6.*

**A key names one object for the daemon's life**, so "drop the session for this
path" and "drop the session I am using" are the same thing, and `_drop`'s
lookup-by-key has nothing left to get wrong. It becomes `slot.discard()`, called
on the object already in hand. *Bug 2.*

**Nothing is ever replaced**, so there is no window in which a second check can
misread the first's state, and liveness stops being a staleness criterion:
"there is no process yet" is a step of `ready()`, not a reason to touch the
table. *Bug 3.*

**Eviction cannot take a session out from under a check**, because it needs the
slot lock and the checker holds it; and when it does win the lock, the worst it
can do is leave `slot.sess is None` for a check that has not started yet, which
`ready()` then handles by starting one. A slot that loses its session is not a
slot that leaks one.

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

Under this plan that call moves into `ready()`, under the slot lock, where it
blocks only the file it is about.

## What the code becomes

`Entry` becomes `Slot` -- the rename is worth it, because the object's lifetime
rule changes and every call site should be looked at once.

```python
class Slot:
    """One file's session, for the daemon's life.  Only the lock holder may
    touch `sess`."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.sess = None            # the rocq repl, or None
        self.flags = self.cwd = self.toolchain = None
        self.loaded = {}            # .vo -> (mtime_ns, size), as loaded
        self.libraries = {}
        self.last_used = time.time()
        self.busy_since = None

    def acquire(self, deadline):    # the only way in
    def release(self)
    def ready(self, flags, cwd, toolchain, rocq, env, cold)
    def discard(self)               # stop the session, keep the slot
    def busy_row(self)              # what is running, for a refusal and status
```

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
slot = self._slot(path)                       # get-or-create, never replace
if not slot.acquire(deadline=t0 + wait):
    return {"ok": False, "busy": slot.busy_row()}      # exit 2, not a verdict
try:
    watched = sorted(set(closure) | set(slot.loaded))
    pre = fingerprint(watched)                # now measured while we own it
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
    slot.release()
self._evict()
```

Deleted outright: the replacement block in `_entry` (`:202`-`:206`) with its
`del`, its try-lock and its `to_stop`; `_stale_entry`'s `not entry.sess.alive`
clause; `_drop`'s lookup by key.

The reclamation paths stop removing anything:

* `_evict` and `reap_idle` take the LRU slot's lock non-blockingly and
  `discard()` it. The slot stays. "Is the daemon empty?" becomes
  `any(s.sess is not None for s in slots)` rather than `if self.sessions:`.
* `shutdown` is the one place allowed to stop a session without the lock, and
  the reason should be in the docstring: it is about to `os._exit`, a mid-check
  child has to die anyway, and blocking on a thirty-minute check to release a
  lock is exactly the hang a shutdown path must not have. It still reaches
  everything, because the table still holds everything.
* `_record_sessions` moves to where a pid changes -- after `ready()` starts a
  session, and inside `discard()` -- and reads `proc` once through a new
  `Session.live_pid()`, which `do_status` uses too.

## Is the resulting interface good enough for agents?

The behaviour change a caller can observe is that **a second check of the same
file waits instead of racing.** That is worth arguing about rather than
asserting, because it is the only thing this plan takes away.

**The axis agents actually use is untouched.** An agent loop is sequential per
file -- write, check, read the error, write again -- and parallel across files:
several agents in a checkout, or one fanning out over a directory. Different
files are different slots, no lock is held across a check, and parallelism
there is bounded only by the memory budget. Finding 7 above means this plan
makes that axis *faster*, not slower.

**Serialising one file is the right resource policy, not a concession.** Two
`rocq repl`s for one file is twice several gigabytes to answer one question,
and the loser pays a second full cold start -- minutes on a real development.
Run one after the other and the second replays warm in seconds instead. The
current behaviour is not parallelism with a cost; it is two cold starts, double
the memory and a leaked child.

The measurement above says it plainly, on the same repro and the same machine:

| | wall | sessions | cold starts | leaked |
|---|---|---|---|---|
| racing, as today | 12.4s | 2 | 2 | 1 child |
| serialised | 10.1s | 1 | 1 | none |

Serialising is *faster*, because the thing being serialised is two copies of
one expensive job contending for one machine. There is no throughput being
traded away here to buy correctness.

So the interface is good enough as it stands, with two small additions that
turn blocking from a hang into an answer:

**1. `check` gains a `wait`** (CLI `--wait SECONDS`, default: the check
timeout). If the slot cannot be acquired in time, the reply is `{"ok": false,
"busy": {...}}` and the client exits **2** -- not a verdict about the proof,
the same category as a stale dependency -- with a line naming what is running
and for how long. `--wait 0` fails fast. One request field, one reply field,
one flag, and it is the whole difference between "the tool hung" and "that file
is busy, go work on another one".

**2. `status --json`, with `busy` and `busy_since` per slot.** An agent that
gets a busy refusal needs exactly one call to decide whether to wait or move
on. `status` already returns everything else and has no machine-readable form;
its `idle` field is time since `last_used`, which does not distinguish "idle
3s, finished" from "running, 3s in".

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

* **No per-slot request queue.** A lock with a deadline is a one-place queue
  with no bookkeeping, and an agent that loses the race wants a fast honest
  answer more than a place in line. Python locks are not FIFO, so a waiter can
  in principle be passed over; the deadline bounds that, and what it gets on
  expiry is true.
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

A long check blocks later checks of the same file for up to `--wait`. What
already bounds that: the SIGINT heuristic bounds a *failing* proof's burn (tens
of minutes to 5.2s on the file in DESIGN), the RSS ceiling bounds a runaway's
memory, and the wall timeout bounds everything. What an agent can do about it:
`--wait 0`, and check another file.

## Tests

The four in `tests/test_rocq_warm_concurrency.py` are provisional and two of
them lose their premise, which is the point -- they assert on a replacement
that will no longer happen.

| test | under this plan |
|---|---|
| `test_a_fresh_entry_is_not_stale` | passes once the `alive` clause goes; assert `_slot(p) is _slot(p)` holds for the daemon's life |
| `test_two_cold_checks_of_one_file_share_a_session` | passes; strengthen it to assert the second check's `mode` is not `cold` |
| `test_a_replaced_busy_session_is_stopped` | premise gone. Rewrite: a `--cold` check arriving mid-check waits, then restarts in place; one slot, one live pid, nothing leaked |
| `test_a_timing_out_check_does_not_drop_its_replacement` | premise gone. Rewrite: a timed-out check discards its session and leaves the slot usable; the next check cold-starts in the same slot |

Three to add, one per new finding:

* the pid file names the live session after a single check of a single file,
  and names both after two;
* `max_sessions=N` with `N+1` files in flight leaks nothing;
* `loaded_changed()` does not run under `Server.lock` -- assert it by holding
  the table lock in one thread and requiring another thread's `_slot` to
  return promptly, or simply by construction once the call has moved.

And one worth more than any of them: make the invariant a `tearDown`
assertion in `ServerCase`, so **every** test in the file checks it --

> no pid in `spawned` is alive unless it is some slot's `sess.proc.pid`

-- which is `leaked() == []`, already written in the harness and currently
asserted in one test out of four.

## Order

Independently shippable, in this order:

1. `Slot`, `_slot`, `ready`, `discard`; delete the replacement path. Fixes bugs
   1, 2, 3 and findings 6 and 7. The only step that touches the session table.
2. `_record_sessions` at start/stop, plus `Session.live_pid()`. Fixes finding 5.
3. `wait`/`busy` on `check`; `status --json` with the busy bit.
4. Server-side rendering and the digest in the reply. Fixes bug 4.
