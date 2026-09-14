"""The rocq-warm supervisor: warm sessions, kept alive between `rocq-warm` runs.

One process per workspace, listening on a Unix socket under
`<workspace>/.rocq-warm/`.  It owns the `rocq repl` children, so a check from a
fresh shell reuses the session the previous check left parked at the edit.

Every guard here exists because a warm session that lies is worse than no
session at all:

* a rebuilt dependency `.vo` throws the session away rather than being checked
  against the copy Rocq loaded into memory an hour ago -- and the set of
  `.vo` files watched is the one Rocq itself reports having loaded, not a
  guess from `rocq dep`;
* a dependency whose `.vo` is older than its `.v` (or than a `.vo` it
  requires) is refused, not checked against: the verdict would be about a
  library that no longer exists.  Make's own rule decides what "older" means;
* a green check does NOT write a `.vo` -- that would double the cost of
  every passing check -- so it says so, and a dependent checked next is
  refused until the `.vo` is rebuilt (by `make`, by `--compile`, or by
  `--rebuild`).  A compile the daemon is running itself is waited for;
* a session that exceeds its RSS ceiling or a check that exceeds its wall
  timeout is killed and reported, not left resident -- a large development has a history
  of a single `vm_compute` reaching 31 GB;
* sessions are evicted LRU under a global memory budget, and idle ones time out.

One check per file at a time, and a second one is REFUSED rather than queued.
A session is checked out of `self.idle` by the thread that wants it, owned
outright while it is used, and returned afterwards; being absent from the table
you take slots from is what excludes everyone else, so a session needs no lock
of its own.  Two `rocq repl` for one file would be twice several GB spent
answering one question, and the loser would pay a second full cold start --
where run one after the other, the second replays warm in no measurable time.
So the caller that loses is told the file is busy and for how long, and a retry
costs it nothing.  Nothing in here ever waits on another thread.
"""

import fcntl
import json
import os
import signal
import socket
import struct
import sys
import threading
import time

from . import (compile as compile_mod, diag, project, report,
               session as session_mod)

DEFAULT_IDLE_TIMEOUT = 1800.0
DEFAULT_MAX_SESSIONS = 4
DEFAULT_CHECK_TIMEOUT = 1800.0


# A daemon is per-checkout, and build machines run many checkouts at once.
# Every one of these defaults is therefore about NOT assuming the machine is
# ours: a daemon that helps itself to half of RAM is fine alone and ruinous
# ten-up, and the process it gets killed to make room for is somebody else's.
DEFAULT_BUDGET_CAP = 32e9       # this daemon's sessions, all together
DEFAULT_MIN_FREE = 4e9          # ... and leave at least this much for others


def _total_bytes():
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return 0


def _available_bytes():
    """What the kernel thinks can still be allocated without swapping.

    The number that matters on a shared machine: it moves when OTHER people's
    work grows, which per-daemon bookkeeping cannot see.
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _budget_bytes():
    env = os.environ.get("ROCQ_WARM_MAX_RSS_GB")
    if env:
        return float(env) * 1e9
    total = _total_bytes()
    if not total:
        return DEFAULT_BUDGET_CAP
    return min(total * 0.5, DEFAULT_BUDGET_CAP)


def _min_free_bytes():
    env = os.environ.get("ROCQ_WARM_MIN_FREE_GB")
    if env:
        return float(env) * 1e9
    return max(DEFAULT_MIN_FREE, _total_bytes() * 0.05)


def _session_ceiling(budget, max_sessions):
    env = os.environ.get("ROCQ_WARM_MAX_SESSION_GB")
    if env:
        return float(env) * 1e9
    # Half the budget, not budget/max_sessions: one big proof legitimately
    # costs several GB, and a ceiling tight enough to kill it is worse than no
    # ceiling at all.  This still catches a runaway an order of magnitude out.
    return budget / 2.0


class Slot:
    """One file's session, owned outright by whoever checked it out.

    There is no lock here, because a checked-out session is not shared: a slot
    is handed to exactly one thread at a time by `Server._checkout`, and until
    that thread gives it back nobody else holds a reference they may use.  A
    lock would be a rule in a docstring; being absent from the table you take
    slots from is a fact.

    Nothing on a slot outlives its `rocq repl` -- `loaded` and `libraries`
    describe what THAT process had loaded, `last_used` only orders live
    sessions for the LRU, and the flags and toolchain are recomputed from the
    project on every check.  So a slot with no session is the same thing as no
    slot, and `Server._return` drops it rather than parking it.
    """

    def __init__(self, path):
        self.path = path
        self.sess = None            # the rocq repl, or None
        self.flags = None
        self.cwd = None
        self.toolchain = None
        # .vo path -> (mtime_ns, size) as each was when the session loaded it.
        # Filled in after every check from what Rocq says it has loaded.
        self.loaded = {}
        self.libraries = {}         # logical name -> .vo path
        self.last_used = time.time()
        self.busy_since = None      # set by _checkout, cleared by _return
        self.deadline = None        # ... and what the check promised to finish by

    def loaded_changed(self):
        """Has any .vo the session holds been rebuilt, removed or replaced?"""
        now = project.fingerprint(sorted(self.loaded))
        return any(self.loaded[p] != (m, sz) for p, m, sz in now)

    def ready(self, flags, cwd, toolchain, rocq, env, rss_limit, cold):
        """The session to check with: started, and answering for THIS build.

        Every reason to throw a session away lands here, where it is a field
        assignment on an object nobody else can see.  That is the whole trick:
        `flags` and the toolchain are fixed when a `Session` is constructed, so
        as long as the construction happened under the session table's lock,
        changing either of them had to be spelled as a table mutation -- and
        the table was the only thing holding the child.
        """
        if self.sess is not None and not self.sess.alive:
            self.discard()          # a dead child holds nothing worth keeping
        # Guarded on there BEING a session: a slot with none has nothing to
        # invalidate, and `loaded_changed()` would stat every .vo that a
        # session which no longer exists used to hold.
        if self.sess is not None:
            if (flags, toolchain) != (self.flags, self.toolchain):
                self.discard()
            elif cold or self.loaded_changed():
                self.discard()
        if self.sess is None:
            self.flags, self.cwd, self.toolchain = flags, cwd, toolchain
            self.sess = session_mod.Session(
                self.path, flags, cwd=cwd, rocq=rocq, env=env,
                rss_limit=rss_limit)
            self.sess.start()
        return self.sess

    def discard(self):
        """Stop the child, and forget everything that described it."""
        if self.sess is not None:
            self.sess.stop()
            self.sess = None
        # Not bookkeeping: a slot that outlived its session and kept these
        # would hand the next check a `watched` set, and `status` a row, about
        # a session that does not exist.
        self.loaded = {}
        self.libraries = {}


class Server:
    def __init__(self, root, idle_timeout=DEFAULT_IDLE_TIMEOUT,
                 max_sessions=DEFAULT_MAX_SESSIONS):
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, ".rocq-warm")
        self.sock_path = os.path.join(self.dir, "sock")
        self.pids_path = os.path.join(self.dir, "sessions")
        # A session is checked out of `idle`, used, and returned.  A slot in
        # `busy` is owned by one thread and may be looked at by nobody else
        # (see `_stop_all` for the single stated exception).  A file with no
        # session is in neither, so "in neither table" and "has no session"
        # are the same statement.
        self.idle = {}
        self.busy = {}
        self.lock = threading.Lock()
        self.idle_timeout = idle_timeout
        self.max_sessions = max_sessions
        self._lock_fd = None
        self.budget = _budget_bytes()
        self.min_free = _min_free_bytes()
        self.started = time.time()
        self.graphs = {}
        # Only ever compiles on request: `--compile` and `--rebuild`.
        self.compiler = compile_mod.Compiler(
            available=_available_bytes, min_free=self.min_free)

    # -------------------------------------------------------------- sessions

    @staticmethod
    def _toolchain_env(toolchain):
        rocq, env_items = (toolchain or (None, ()))
        # MERGE, never replace: subprocess `env=` is the child's whole
        # environment, and a child without HOME or TMPDIR misbehaves in
        # ways that have nothing to do with Rocq.
        env = dict(os.environ, **dict(env_items)) if env_items else None
        return rocq or "rocq", env

    def _graph(self, flags, cwd, toolchain):
        """The dependency graph for one project, shared by its sessions."""
        key = (cwd, tuple(flags), toolchain)
        with self.lock:
            g = self.graphs.get(key)
        if g is None:
            rocq, env = self._toolchain_env(toolchain)
            g = project.DepGraph(flags, cwd, project.find_project(cwd),
                                 rocq=rocq, env=env)
            with self.lock:
                self.graphs[key] = g
        return g

    def _checkout(self, path):
        """The slot for `path`, owned by the caller until `_return`.

        None if somebody else has it.  Taking the slot OUT of `idle` is the
        exclusion: there is no lock on a session, because a checked-out session
        is not shared, and a caller that cannot have one is refused rather than
        queued.  One check per file at a time.

        The `busy` test has to come first and has to return rather than fall
        through: building a second slot for a file somebody is already checking
        is exactly the bug this replaces, and it is one missing `return` away.
        """
        path = os.path.abspath(path)
        with self.lock:
            if path in self.busy:
                return None
            slot = self.idle.pop(path, None)
            if slot is None:
                slot = Slot(path)
            slot.busy_since = time.time()
            self.busy[path] = slot
            return slot

    def _return(self, slot):
        """Give a slot back.  One with no session is dropped, not parked.

        Nothing on an empty slot describes anything that still exists, so
        keeping it would grow a table entry per file the daemon has ever been
        asked about and buy nothing.
        """
        with self.lock:
            self.busy.pop(slot.path, None)
            slot.busy_since = None
            slot.deadline = None
            if slot.sess is not None:
                slot.last_used = time.time()
                self.idle[slot.path] = slot

    def _slots(self):
        """Every slot, owned or not -- for the paths that only observe."""
        with self.lock:
            return list(self.idle.values()) + list(self.busy.values())

    def _live_sessions(self):
        """(slot, session) for every slot that has one.

        `slot.sess` is read ONCE per slot: an owner may be discarding it, and
        the whole point of reading it into a local is that a test and a use
        cannot then disagree.
        """
        out = []
        for slot in self._slots():
            sess = slot.sess
            if sess is not None:
                out.append((slot, sess))
        return out

    # ------------------------------------------------------- stray children

    def _record_sessions(self):
        """Write down which `rocq` processes we own.

        `Session.stop` only runs if the daemon is alive to run it.  Kill the
        daemon itself -- `kill -9`, a lost terminal, an OOM -- and its children
        are left blocked on a closed stdin, holding several GB each, with
        nothing that will ever reap them.  A pid file lets the NEXT daemon
        clean up after the last one.

        Call this wherever a pid appears or goes away, which means after a
        session STARTS and not merely when a slot is created.  Recording at
        creation writes down an empty row -- the session starts afterwards --
        so a daemon holding one session used to record nothing at all, and the
        reaper had nothing to collect.
        """
        try:
            rows = ["%d\t%s" % (pid, slot.path)
                    for slot, pid in ((s, sess.live_pid())
                                      for s, sess in self._live_sessions())
                    if pid is not None]
            tmp = self.pids_path + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(rows) + ("\n" if rows else ""))
            os.replace(tmp, self.pids_path)
        except OSError:
            pass

    def reap_strays(self):
        """Kill sessions a previous daemon left behind.

        By pid AND cmdline: a bare pid may have been recycled by an unrelated
        process, and on a shared machine a pattern kill would take out other
        checkouts' sessions as well as ours.
        """
        killed = 0
        try:
            rows = open(self.pids_path).read().splitlines()
        except OSError:
            return 0
        for row in rows:
            pid_s, _, path = row.partition("\t")
            if not pid_s.isdigit() or not path:
                continue
            try:
                with open("/proc/%s/cmdline" % pid_s, "rb") as f:
                    args = f.read().replace(b"\0", b" ").decode("utf8", "replace")
            except OSError:
                continue
            if "repl" in args and path in args:
                try:
                    os.kill(int(pid_s), signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
        self._record_sessions()
        return killed

    def _evict(self):
        """Keep the session set inside our own budget AND the machine's.

        Our own budget bounds one daemon.  It cannot bound ten, and it cannot
        see the compile somebody else just started, so eviction also yields
        when the machine as a whole is running out -- which is the only signal
        that works when the pressure is not ours.  Under real pressure we will
        give up our last session too: degrading to a cold check is a cost we
        pay ourselves, where an OOM kill is a cost somebody else pays.

        Only `idle` is ever taken from, so "do not evict a session that is
        mid-check" needs no test: a slot somebody is using is not there.  The
        budget still COUNTS the busy ones, or it would admit sessions against
        memory that is already spoken for.
        """
        while True:
            with self.lock:
                idle = sorted(self.idle.items(), key=lambda kv: kv[1].last_used)
                held = len(self.idle) + len(self.busy)
            if not idle:
                return                  # everything we hold is mid-check
            used = sum(sess.rss_bytes() for _s, sess in self._live_sessions())
            avail = _available_bytes()
            pressure = avail is not None and avail < self.min_free
            if not (pressure or held > self.max_sessions
                    or used > self.budget):
                return
            if not pressure and held == 1:
                return                  # our own budget never costs the last one
            if not self._reclaim(idle[0][0]):
                continue                # it went busy under us; try the next

    def _reclaim(self, path):
        """Stop `path`'s session and drop its slot, if nobody is using it.

        Check the victim out like any other caller and simply do not give it
        back.  That is not ceremony: `discard()` waits on the child, so it
        cannot run under `self.lock`, and a version that popped the slot from
        `idle` first would leave it in NEITHER table with its `rocq repl` still
        alive -- the unreachable child this design exists to make impossible.
        Held in `busy` for the length of the stop, it stays reachable
        throughout, and `_return` then drops it because its session is gone.
        """
        slot = self._checkout(path)
        if slot is None:
            return False
        try:
            slot.discard()
        finally:
            self._return(slot)
        self._record_sessions()
        return True

    def reap_idle(self):
        now = time.time()
        with self.lock:
            timed_out = [k for k, s in self.idle.items()
                         if now - s.last_used > self.idle_timeout]
        for key in timed_out:
            self._reclaim(key)

    def report_wedged(self):
        """Say so when a slot has been checked out past its own deadline.

        Every check carries a wall timeout, so no slot can legitimately still
        be held long after it: one that is means the check machinery itself is
        stuck, and its file has been refusing checks ever since.  That is the
        one failure this ownership model can produce, and it is silent unless
        somebody says it out loud.

        It deliberately does NOT reclaim the slot.  The thread that owns it may
        still be running, and taking a session away from a thread that is using
        it is exactly the class of bug the model removes.
        """
        now = time.time()
        with self.lock:
            wedged = [(s.path, now - (s.busy_since or now))
                      for s in self.busy.values()
                      if s.deadline is not None and now > s.deadline + 60]
        for path, held in wedged:
            compile_mod.log(
                "WEDGED: %s has been checked out for %.0fs, past the timeout "
                "of the check holding it; every check of it is being refused. "
                "Not reclaiming it -- the thread that owns it may still be "
                "running.", path, held)
        return len(wedged)

    # -------------------------------------------------------------- requests

    def handle(self, req):
        cmd = req.get("cmd")
        if cmd == "check":
            return self.do_check(req)
        if cmd == "status":
            return self.do_status()
        if cmd == "stop":
            self.compiler.stop()
            self._stop_all()
            return {"ok": True, "stopped": True}     # _serve_one then exits
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid()}
        return {"ok": False, "error": "unknown command %r" % cmd}

    def do_check(self, req):
        """Check a file, and say exactly what to print and what to exit with.

        The rendering is in `report`, on this side of the socket.  The daemon
        has the bytes it checked, the workspace root and the compile job; a
        client that re-derives any of that is a second implementation of the
        same policy -- and the exit code IS policy, since "could not be
        checked" (2) and "the proof is wrong" (1) are the distinction the
        whole tool turns on.  So `rocq-warm check` writes two strings and
        exits.
        """
        path = os.path.abspath(req["path"])
        resp = self._check(path, req)
        resp["out"], resp["log"], resp["exit"] = report.check(
            resp, path, self.root, req)
        return resp

    def _check(self, path, req):
        try:
            text = open(path, "rb").read()
        except OSError as e:
            return {"ok": False, "error": str(e)}
        t0 = time.time()
        timeout = float(req.get("timeout") or DEFAULT_CHECK_TIMEOUT)
        # `Set Silent` is decided when the session starts, so asking for the
        # proof's own output means starting over.
        toolchain = (req.get("rocq"),
                     tuple(sorted((req.get("env") or {}).items())))
        rocq, env = self._toolchain_env(toolchain)
        flags, cwd = project.flags_for(path)

        # What this text loads, from its Require lines as they are NOW.
        graph = self._graph(flags, cwd, toolchain)
        graph.refresh(extra=[path])
        closure = graph.closure(path)

        # A compile of this file from an older text is describing a file that
        # no longer exists; a compile of the same text is left to finish.
        digest = compile_mod.digest_of(text=text)
        compile_mod.log("check: %s (digest %s%s%s)", path, digest[:8],
                        ", --compile" if req.get("wait_vo") else "",
                        ", --rebuild" if req.get("rebuild") else "")
        self._cancel_other_text(path, digest)

        # Refuse to check against a library that no longer matches its
        # source.  A compile we started ourselves is waited for instead.
        stale = self._stale(closure, graph, deadline=t0 + timeout)
        if stale and req.get("rebuild"):
            failure = self._rebuild(closure, stale, graph, rocq, env,
                                    deadline=t0 + timeout)
            if failure is not None:
                return failure
            stale = self._stale(closure, graph, deadline=t0 + timeout)
        stale_rows = [self._stale_row(vo, why) for vo, why in stale]
        if stale and not req.get("allow_stale"):
            return {"ok": False, "stale": stale_rows,
                    "error": "%d stale dependenc%s" % (
                        len(stale), "y" if len(stale) == 1 else "ies")}

        # One check per file at a time.  A second one is refused outright
        # rather than queued: what it would be waiting on is a proof, so no
        # deadline is honest, and once the running check finishes the same
        # question is answered by a warm replay that executes nothing.
        slot = self._checkout(path)
        if slot is None:
            return self._busy_refusal(path)
        try:
            slot.deadline = t0 + timeout
            # Make room BEFORE spawning, not after: this slot is already in
            # `busy`, so it is counted (right -- we are about to fill it) and
            # cannot itself be chosen (right -- it is being used).
            self._evict()
            # Everything we know the session holds or is about to load, stat'ed
            # BEFORE it loads anything.  A .vo rebuilt while the check runs must
            # not be recorded with its new mtime as if that were what was loaded.
            watched = sorted(set(closure) | set(slot.loaded))
            pre = {p: (m, sz) for p, m, sz in project.fingerprint(watched)}
            sess = slot.ready(
                flags, cwd, toolchain, rocq, env,
                _session_ceiling(self.budget, self.max_sessions),
                cold=bool(req.get("cold")))
            self._record_sessions()     # there is a pid now, and not before
            try:
                result = sess.check(text, timeout=timeout)
            except session_mod.FeedTimeout as e:
                slot.discard()
                return {"ok": False, "error": "timed out after %.0fs (%s); "
                                              "session discarded" % (timeout, e)}
            except session_mod.MemoryLimit as e:
                slot.discard()
                return {"ok": False,
                        "error": "%s; session discarded (raise the ceiling "
                                 "with ROCQ_WARM_MAX_SESSION_GB)" % e}
            except session_mod.SessionDead as e:
                slot.discard()
                return {"ok": False, "error": "rocq died: %s" % e}
            unreliable = None
            try:
                libraries = sess.loaded_libraries()
            except Exception as e:                      # noqa: BLE001
                libraries, unreliable = {}, "%s: %s" % (type(e).__name__, e)
            rss = sess.rss_bytes()
            post = {p: (m, sz) for p, m, sz in project.fingerprint(
                sorted(set(watched) | set(libraries.values())))}
            moved = [p for p in watched if pre[p] != post[p]]
            note = None
            if moved:
                note = ("%s changed during the check; the verdict may be about "
                        "either version, and the session was discarded"
                        % ", ".join(os.path.relpath(p, self.root) for p in moved))
                slot.discard()
            elif unreliable:
                note = ("could not ask rocq what it loaded (%s); the session "
                        "was discarded" % unreliable)
                slot.discard()
            else:
                slot.loaded = {p: pre.get(p, post[p]) for p in post}
                slot.libraries = libraries
        finally:
            # Unmissable: a slot that is never given back leaves its file
            # refused for the daemon's life.
            self._return(slot)
            self._record_sessions()
        # The file is checkable again from here.  A `--compile` is a real
        # `rocq compile` that can run for minutes and does not touch the
        # session, so it must not be inside the checkout.
        compile_mod.log("check: %s %s [%s, %d sentences, %.1fs]%s", path,
                        "OK" if result.ok else "FAILED", result.mode,
                        result.replayed, result.seconds,
                        "; " + note if note else "")
        job = None
        if result.ok and not moved and req.get("wait_vo"):
            job = self.compiler.submit(path, flags, cwd, rocq=rocq, env=env,
                                       digest=digest)
            self.compiler.wait(path, timeout=max(0.0, t0 + timeout - time.time()))
            compile_mod.log("check: %s waited for job %d: %s", path, job.seq,
                            job.state)
        self._evict()
        return {
            "ok": True,
            "passed": result.ok,
            "mode": result.mode,
            "replayed": result.replayed,
            "sentences": result.total,
            "seconds": result.seconds,
            "rss": rss,
            "libraries": len(libraries),
            "stale": stale_rows,            # only when allow_stale let it through
            "note": note,
            "vo": _describe_job(job) if job is not None else None,
            # Why make would rebuild THIS file's .vo now -- after a green
            # check that is "its source is newer", which the user is told.
            "vo_stale": project.staleness(project.vo_of(path), graph.graph),
            # The digest of the bytes this verdict is about, so a caller that
            # edits fast can tell whether it is about what it wrote.
            "digest": digest,
            # Resolved HERE, against the text we actually checked.  The client
            # used to re-read the .v to turn these offsets into a line, with
            # nothing tying the two reads together.
            "diags": [{"kind": d.kind,
                       "span": d.span(text),
                       "at": diag.line_col(text, d.span(text)),
                       "message": d.message().decode("utf8", "replace")}
                      for d in result.diags],
        }

    def _busy_refusal(self, path):
        """A check that did not happen because the file is being checked.

        Exit 2 at the client, alongside a stale dependency: not a verdict about
        the proof.  It carries how long the file has been busy, which is what
        makes a session wedged by a hung check diagnosable rather than a
        mystery -- a file that has been busy for three days says so.
        """
        with self.lock:
            slot = self.busy.get(path)
            since = slot.busy_since if slot is not None else None
        row = {"path": path, "since": since,
               "seconds": None if since is None else time.time() - since}
        # The elapsed time is the part that can be missing -- the slot may have
        # been given back between the failed checkout and this lookup -- so the
        # reason is phrased the same either way, and only the detail varies.
        detail = ("" if row["seconds"] is None
                  else ", running for %.0fs" % row["seconds"])
        return {"ok": False, "busy": row,
                "error": "one check per file at a time, and this file is "
                         "already being checked%s" % detail}

    # ------------------------------------------------------------ staleness

    def _stale(self, closure, graph, deadline):
        """Stale members of `closure`, after waiting for our own compiles.

        A `.vo` that is stale because its compile has not finished yet is
        not a reason to refuse; it is a reason to wait.  Only the daemon's
        own jobs are waited for -- somebody's `make` in another terminal is
        invisible, and guessing at it would be guessing.
        """
        while True:
            stale = project.stale_deps(closure, graph.graph)
            pending = [j for j in (self.compiler.pending(vo) for vo, _w in stale)
                       if j is not None]
            if not pending or time.time() >= deadline:
                return stale
            for j in pending:
                self.compiler.wait(j.v, timeout=max(0.0, deadline - time.time()))

    def _stale_row(self, vo, why):
        row = {"vo": vo, "why": why}
        job = self.compiler.jobs.get(project.v_of(vo))
        if job is not None and job.state == "failed":
            row["compile_output"] = job.output.decode("utf8", "replace")
            row["why"] += " (rocq-warm's own compile of it failed)"
        return row

    def _rebuild(self, closure, stale, graph, rocq, env, deadline):
        """Compile what is stale, and what that makes stale, in order.

        Returns a response describing the failure, or None on success.
        """
        plan = project.rebuild_plan(closure, stale, graph.graph)
        jobs = []
        for vo, after in plan:
            v = project.v_of(vo)
            if not os.path.isfile(v):
                return {"ok": False, "error": "cannot rebuild %s: %s does not "
                                              "exist" % (vo, v)}
            flags, cwd = project.flags_for(v)
            jobs.append(self.compiler.submit(v, flags, cwd, rocq=rocq, env=env,
                                             after=after))
        self.compiler.wait_all(jobs, timeout=max(0.0, deadline - time.time()))
        failed = [j for j in jobs if j.done and not j.succeeded]
        if failed:
            return {"ok": False,
                    "error": "rebuilding %s failed" % ", ".join(
                        os.path.relpath(j.v, self.root) for j in failed),
                    "compile_failed": [_describe_job(j) for j in failed]}
        if not all(j.done for j in jobs):
            return {"ok": False, "error": "timed out rebuilding %d stale "
                                          "dependencies" % len(jobs)}
        return None

    def _cancel_other_text(self, path, digest):
        job = self.compiler.jobs.get(path)
        if job is not None and not job.done and job.digest != digest:
            self.compiler.cancel(path)

    def do_status(self):
        """What the daemon is holding, including what is being checked now.

        This reads sessions it does not own, which is allowed because it only
        OBSERVES: a pid, an RSS, and the length of a list the owner may be
        appending to.  Each is a single read that cannot raise, and the worst
        it can report is a number from the middle of a running check -- which
        is the number `status` is being asked for.
        """
        now = time.time()
        out = []
        for slot, sess in sorted(self._live_sessions(),
                                 key=lambda pair: pair[0].path):
            busy_since = slot.busy_since
            out.append({
                "path": slot.path,
                "pid": sess.live_pid(),
                "alive": sess.live_pid() is not None,
                "sentences": len(sess.sentences),
                "complete": sess.complete,
                "rss": sess.rss_bytes(),
                # `idle` is time since it was last GIVEN BACK, which for a
                # session being checked right now is not idleness at all --
                # hence `busy`, and how long it has been that way.
                "idle": now - slot.last_used,
                "busy": busy_since is not None,
                "busy_for": None if busy_since is None else now - busy_since,
                "libraries": len(slot.libraries),
                "watched": len(slot.loaded),
            })
        resp = {"ok": True, "pid": os.getpid(),
                "uptime": time.time() - self.started,
                "budget": self.budget, "min_free": self.min_free,
                "available": _available_bytes(), "sessions": out,
                "compiles": self.compiler.status()}
        resp["out"] = report.status(resp)
        return resp

    # ----------------------------------------------------------------- serve

    def serve(self):
        """Serve this workspace, if nobody else already is.

        Two clients can race to spawn a daemon.  Without the lock the loser
        unlinks the winner's socket and binds its own, which orphans a daemon
        that keeps its sessions resident and unreachable until it times out --
        exactly the memory nobody can account for.  The lock fd is held for the
        daemon's life and released when it exits.
        """
        os.makedirs(self.dir, exist_ok=True)
        self._lock_fd = os.open(os.path.join(self.dir, "lock"),
                                os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return                      # somebody else is serving this tree
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(16)
        strays = self.reap_strays()
        if strays:
            sys.stderr.write("reaped %d session(s) left by a previous daemon\n"
                             % strays)
        threading.Thread(target=self._reaper, daemon=True).start()
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._serve_one, args=(conn,),
                             daemon=True).start()

    def _reaper(self):
        """Drop idle sessions, and eventually the daemon itself.

        A daemon whose workspace has gone away -- a temp tree, a deleted
        worktree -- would otherwise sit on several GB of Rocq for ever.
        """
        empty_since = None
        while True:
            time.sleep(30)
            try:
                if not os.path.isdir(self.root):
                    self.shutdown()             # the workspace itself is gone
                self.reap_idle()
                self._evict()
                self.report_wedged()
                if self.idle or self.busy:
                    empty_since = None
                    continue
                empty_since = empty_since or time.time()
                if time.time() - empty_since > self.idle_timeout:
                    self.shutdown()
            except Exception:
                pass

    def _stop_all(self):
        """Stop every child, owned or not.  Never leave one behind.

        The single place a non-owner touches a busy slot's session, and it is
        deliberate: the daemon is on its way out, a mid-check child has to die
        anyway, and a shutdown that waits for a thirty-minute check to hand its
        slot back is exactly the hang a shutdown path must not have.  It
        reaches everything because everything with a session is in one of the
        two tables.
        """
        for _slot, sess in self._live_sessions():
            try:
                sess.stop()
            except Exception:                       # noqa: BLE001
                pass
        with self.lock:
            # `idle` is ours to drop; `busy` is NOT, even here.  Clearing it
            # would let a second checkout of a path somebody still owns
            # succeed, which is the one thing these tables exist to prevent.
            # An owner returning a slot whose child we just killed parks a
            # dead session, and the next `ready()` rebuilds it -- which is
            # what it does for any session that died on its own.
            self.idle.clear()
        self._record_sessions()

    def shutdown(self):
        """Stop every session, then the daemon.  Never leave a child behind."""
        self.compiler.stop()
        self._stop_all()
        os._exit(0)

    def _serve_one(self, conn):
        try:
            req = recv_msg(conn)
            if req is None:
                return
            try:
                resp = self.handle(req)
            except Exception as e:                      # never take the daemon
                resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            send_msg(conn, resp)
            if req.get("cmd") == "stop":
                os._exit(0)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _describe_job(job):
    if job is None:
        return None
    d = job.describe()
    d["output"] = job.output.decode("utf8", "replace")
    return d


def send_msg(sock, obj):
    payload = json.dumps(obj).encode()
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def recv_msg(sock):
    head = _recv_exactly(sock, 4)
    if head is None:
        return None
    body = _recv_exactly(sock, struct.unpack("!I", head)[0])
    return None if body is None else json.loads(body)


def _recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def main(argv):
    root = argv[1] if len(argv) > 1 else os.getcwd()
    Server(root).serve()


if __name__ == "__main__":
    main(sys.argv)
