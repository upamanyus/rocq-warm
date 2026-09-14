# Concurrency bugs found in the session table

Working notes for the `fix-concurrency` branch. Everything below was
reproduced against Rocq 9.1.1 (`/root/.opam/rocq911`) by driving a `Server`
object in-process from two threads; line numbers are as of `f532f0d`.

The short version: the daemon serialises *use* of a session correctly, and
gets *ownership* of a session wrong. `Entry.lock` does its job. The bugs are
all in the moment an entry is replaced in `Server.sessions`, where the table
is the only thing holding a reference to a live `rocq repl` and the code
drops that reference without stopping the child.

## 1. A replaced entry that is mid-check is never stopped

`_entry` (`rocqwarm/server.py:177`), on finding a stale entry whose lock it
cannot take:

```python
del self.sessions[key]
if entry.lock.acquire(blocking=False):
    to_stop, entry = entry, None
else:
    entry = None                # <- the last reference, dropped
```

The comment above it says "a still-busy stale session is simply orphaned from
the table and stopped when its check ends." Nothing implements the second
half. `to_stop` stays `None`, the local goes out of scope, and the checking
thread has no way to find its way back: every stop path other than the
exception handlers inside `do_check` goes through `_drop(key)`, which looks
the entry up *by key* and therefore finds the replacement.

Measured on a one-lemma file with no imports — a real development's session is
the several GB the design keeps talking about:

```
A finished: ok=True passed=True note=None      (the clean path, not an error)
  t+5s  A's rocq pid 234488 alive=True
reachable from the sessions table: False
recorded in the pid file:          False
after reap_idle() + _evict():      alive = True
after dropping every table entry:  alive = True
RSS of the orphan: 304032 kB
```

An orphan is invisible to every reclamation mechanism, all of which iterate
the table: `reap_idle`, `_evict`, `shutdown`'s `for key in list(self.sessions)`
loop — under the docstring "Never leave a child behind" — and
`_record_sessions`, so its pid never reaches the pid file and the *next*
daemon's `reap_strays` cannot collect it either.

It is reclaimed only when the daemon process exits and the child takes the
EOF on stdin (measured: within 2s). Two reasons not to rest on that. The
reaper's idleness test is `if self.sessions:`, which orphans are not in, so a
daemon holding nothing but orphans looks empty and exits at the 2×
`idle_timeout` mark — accidental cleanup, an hour late. And the codebase does
not believe in that path anyway: `_record_sessions`' own docstring justifies
the pid file with "its children are left blocked on a closed stdin, holding
several GB each, with nothing that will ever reap them."

## 2. `_drop` is keyed, not identity-checked, so it kills the replacement

Once entry A has been replaced by entry B under the same key, every
`self._drop(path)` in `do_check` (`:405`, `:410`, `:415`, `:433`, `:437`)
resolves to **B**. So the `moved` and `unreliable` paths leak A *and* tear
down a healthy B; the `FeedTimeout` / `MemoryLimit` paths stop A correctly via
`entry.sess.stop()` (that one goes through the object) and then still tear
down B.

If B is mid-check when that happens, `Session.stop` SIGKILLs its process group
and sets `self.proc = None` under B's feed — which is precisely the
`NoneType has no attribute stdin` failure `_entry`'s docstring cites as the
reason the table exists.

## 3. `_stale_entry` calls a not-yet-started session "cold"

This is the one that makes bug 1 routine rather than exotic.

```python
if force_cold or not entry.sess.alive:
    return "cold"
```

`alive` is `self.proc is not None and self.proc.poll() is None`, and a
brand-new `Entry` is never alive: `_entry` deliberately spawns nothing
("`start()` does, later and under the entry's own lock"). So a second check
arriving while the first is still between `Entry(...)` and `Popen(...)` sees
the fresh entry, calls it stale, and replaces it.

Two concurrent checks of the same file, both cold, from a barrier:

```
A: ok=True passed=True mode=cold 12.4s
B: ok=True passed=True mode=cold 12.3s
_entry returned: [('B', 140128518779104), ('A', 140128517048592)]   <- different
rocq repls spawned: [236451, 236450, 236458, 236456]  (each cold check restarts once)
still alive: [236458, 236456]
in the sessions table: [236456]
LEAKED (alive but untracked): [236458]
```

Two sessions for one file, two full cold starts in parallel, double the
memory, and one leaked child — with **no** dependency rebuilt and **no**
`--cold` anywhere. Just two invocations at once.

The same two checks once the session is warm behave exactly as designed: one
`Entry` handed to both threads, serialised by `Entry.lock`, both returning
`mode=replay`, nothing leaked. So the table logic is only wrong during the
cold window.

The window runs from `Entry(...)` inside `self.lock` to `self.proc = Popen(...)`
inside `start()`, and it contains real work: `_entry`'s tail
(`_record_sessions`, `_evict`), then `pre = project.fingerprint(watched)` in
`do_check` stat'ing the whole `.vo` closure, then the `entry.lock` acquire and
the spawn. Both clients do identical work before it (`graph.refresh`,
`_stale`), so they arrive in lockstep — an editor-on-save racing a `make` hits
this for real.

## 4. The client re-reads the `.v` after the check, unguarded

Adjacent, not a session-table bug, but the same class.

The client sends only a path; the server reads the file in `do_check:350` and
that text is what gets checked. After the reply comes back the *client* reads
the file again, for one purpose: `diag.render` turning the server's byte-offset
spans into a line and column. Nothing ties the two reads together. Edit the
file during a check that can legitimately run for minutes and the offsets from
T0 are resolved against the text at T1 — no crash, just a diagnostic silently
reported at the wrong line.

Compare `Compiler._run` (`compile.py:280`), which re-digests the source and
skips the job rather than write a `.vo` of text nobody asked for, and the
`pre`/`post` fingerprints in `do_check` that produce the `note`. The `.v`'s own
text is the one input with no such guard.

## The fix

See **[CONCURRENCY-PLAN.md](CONCURRENCY-PLAN.md)**, which replaces the
four-part fix that used to be written out here.

The short version of why it is shorter. All four bugs above are ways of losing
a reference to a live `rocq repl`, or of resolving one to the wrong object, and
they exist because `Session` is constructed under the table lock
(`server.py:209`). That puts `flags` and the toolchain -- both fixed at
construction -- out of reach of any in-place update, so "throw this session
away" has to be spelled as a table mutation, and the table is the only thing
holding the process. Move the construction under the per-file lock and it
becomes a field assignment: the table never changes shape, and a reference that
never moves cannot be lost.

So the plan is mostly deletion. No orphan list, no cap on it, no draining it
from three places, no identity-checked `_drop`. It also fixes three further
findings of the same kind, written up there: the pid file never names the
session that is running, eviction removes an entry a check is about to use, and
the global table lock is held across a few hundred `stat` calls.

## Regression tests

The scenarios are cheap to pin, and both repros are already written in this
shape: drive a `Server` in-process, hold two threads on a `threading.Barrier`,
assert on entry identity, on how many `rocq repl`s were spawned, and on
survivors that are alive but not in `self.sessions`. A `do 80000000 idtac`
spin gives a ~10s check, which is a wide enough window to step into.

Note that `Session._check` restarts the session on the cold path
(`session.py:774`), so a pid sampled while a cold check is running is *not*
the pid the check ends with. Sample `entry.sess.proc.pid` after the check
returns, or a leak test will chase a process the session retired itself.
