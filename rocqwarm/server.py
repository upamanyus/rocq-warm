"""The rocq-warm supervisor: warm `rocq repl` sessions, one per `.v` file.

One process per workspace, on a Unix socket under `<workspace>/.rocq-warm/`.
It owns the `rocq repl` children, so a check from a fresh shell reuses the
session the previous check left parked at the edit.

A session must never answer for a library that no longer exists, so: it is
discarded when any `.vo` it loaded changes on disk (the watched set being what
Rocq reports loading, not a `rocq dep` guess); a check is refused rather than
answered when any `.vo` in the closure is one make would rebuild, unless this
daemon's own compile of it is still running, in which case that is waited for;
and a green check, which writes no `.vo`, says so.  Sessions over their RSS
ceiling and checks over their wall timeout are killed and reported; sessions
are evicted LRU under a memory budget, and idle ones time out.

`self.sessions` maps each file to a `Slot` holding the file's session or marked
borrowed by a running check, giving at most one `rocq repl` per file and one
check of it at a time.  A borrower takes the session that exists or starts the
one that does not; a second check of the same file is refused rather than
queued.  So a session needs no lock, and nothing here waits on another thread.
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


# Absolute caps rather than a share of RAM: a per-checkout daemon that takes
# half of memory is fine alone and ruinous ten-up, and the process the kernel
# kills to make room belongs to somebody else.
DEFAULT_BUDGET_CAP = 32e9       # this daemon's sessions, all together
DEFAULT_MIN_FREE = 4e9          # ... and left free for everyone else


def _total_bytes():
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return 0


def _available_bytes():
    """Allocatable memory, which unlike our own bookkeeping moves when other
    processes on this machine grow."""
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
    # costs several GB, and a ceiling that kills it is worse than none.
    return budget / 2.0


class Slot:
    """One `.v` file's entry in the session table.

    Holds that file's `Session`, or is marked `borrowed` by the check using
    it.  Only the borrower may start, stop or replace `sess`; a non-borrower
    may read it and no more, `_stop_all` excepted.

    The fields here outlive any single session: when the file was last
    checked, and who holds it now.  What describes one `rocq repl` -- its
    flags, toolchain and loaded `.vo` set -- lives on the `Session`, so
    discarding the process discards that with it.
    """

    def __init__(self, path):
        self.path = path
        self.sess = None            # the session, once there is one
        self.borrowed = False       # ... and whether somebody is using it
        self.last_used = time.time()
        self.since = None           # when the borrow started
        self.deadline = None        # ... and what it promised to finish by

    def ready(self, flags, cwd, toolchain, rocq, env, rss_limit, cold):
        """The session to check with, started and answering for THIS build.

        Discards and respawns when the flags or toolchain differ from the ones
        the session was spawned for, when a `.vo` it loaded has changed, or on
        `--cold`.  The caller owns the slot, so each of those is a field
        assignment rather than a table mutation.
        """
        if self.sess is not None and not self.sess.alive:
            self.discard()          # a dead child holds nothing worth keeping
        # Only when there IS a session: otherwise `loaded_changed()` would
        # stat the `.vo` set of a session that no longer exists.
        if self.sess is not None:
            if (flags, toolchain) != (self.sess.flags, self.sess.toolchain):
                self.discard()
            elif cold or self.sess.loaded_changed():
                self.discard()
        if self.sess is None:
            self.sess = session_mod.Session(
                self.path, flags, cwd=cwd, rocq=rocq, env=env,
                rss_limit=rss_limit, toolchain=toolchain)
            self.sess.start()
        return self.sess

    def discard(self):
        """Stop the child; its flags and loaded set go with it."""
        if self.sess is not None:
            self.sess.stop()
            self.sess = None


class Server:
    def __init__(self, root, idle_timeout=DEFAULT_IDLE_TIMEOUT,
                 max_sessions=DEFAULT_MAX_SESSIONS):
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, ".rocq-warm")
        self.sock_path = os.path.join(self.dir, "sock")
        self.pids_path = os.path.join(self.dir, "sessions")
        # path -> Slot, one per file checked, never removed: every live `rocq
        # repl` is reachable from here at all times.  A slot is a few hundred
        # bytes; the session in it is the gigabytes reclamation takes away.
        self.sessions = {}
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
        # MERGE, never replace: `env=` is the child's whole environment, and
        # one without HOME or TMPDIR misbehaves for non-Rocq reasons.
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

    def _borrow(self, path, deadline=None):
        """The slot for `path`, the caller's alone until `_give_back`.

        None if another check holds it: one check per file, refused rather than
        queued, so a borrowed session has one user and needs no lock.  A new
        slot is born unborrowed, so creating one cannot trip the refusal below
        it; that refusal must return rather than fall through, or two threads
        that both found nothing parked would both spawn a `rocq repl`.
        """
        path = os.path.abspath(path)
        with self.lock:
            slot = self.sessions.get(path)
            if slot is None:
                slot = self.sessions[path] = Slot(path)
            if slot.borrowed:
                return None
            slot.borrowed = True
            slot.since, slot.deadline = time.time(), deadline
            return slot

    def _give_back(self, slot):
        """Hand a slot back, with whatever session it now holds."""
        with self.lock:
            slot.borrowed = False
            slot.since = slot.deadline = None
            slot.last_used = time.time()

    def _live_sessions(self):
        """(slot, session) for every slot that has one.

        `slot.sess` is read once per slot, so that a borrower discarding it
        cannot make a test and a use of it disagree.  Reading is all a
        non-borrower may do.
        """
        with self.lock:
            slots = list(self.sessions.values())
        out = []
        for slot in slots:
            sess = slot.sess
            if sess is not None:
                out.append((slot, sess))
        return out

    # ------------------------------------------------------- stray children

    def _record_sessions(self):
        """Write down which `rocq` processes we own.

        `Session.stop` only runs if the daemon is alive to run it, so a
        `kill -9` leaves children blocked on a closed stdin holding several GB
        each, and this file is how the next daemon collects them.  Called
        wherever a pid appears or goes, which is after a session starts rather
        than when its slot is created: a slot has no pid yet.
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

        Our budget bounds one daemon and cannot bound ten, so eviction also
        yields when `MemAvailable` is low, the only signal that moves when the
        pressure is not ours; under it even the last session goes, a cold check
        being a cost we pay ourselves where an OOM kill is one somebody else
        pays.  A borrowed session is never a candidate, since killing the child
        under a running check discards the work being saved, but it still
        counts against the budget -- as does a borrowed slot with no session
        yet, a cold start in progress.
        """
        while True:
            live = self._live_sessions()
            free = sorted((s for s, _sess in live if not s.borrowed),
                          key=lambda s: s.last_used)
            with self.lock:
                starting = sum(1 for s in self.sessions.values()
                               if s.borrowed and s.sess is None)
            held = len(live) + starting
            if not free:
                return                  # everything we hold is mid-check
            used = sum(sess.rss_bytes() for _s, sess in live)
            avail = _available_bytes()
            pressure = avail is not None and avail < self.min_free
            if not (pressure or held > self.max_sessions
                    or used > self.budget):
                return
            if not pressure and held == 1:
                return                  # our own budget never costs the last one
            if not self._reclaim(free[0].path):
                continue                # somebody took it; try the next

    def _reclaim(self, path):
        """Stop `path`'s session, if no check holds it.

        Borrows the victim first: `discard()` waits on the child so it cannot
        run under `self.lock`, and the borrow keeps a check from starting on
        the file while its Rocq is being killed.  False if another caller
        holds it.
        """
        slot = self._borrow(path)
        if slot is None:
            return False
        try:
            slot.discard()
        finally:
            self._give_back(slot)
        self._record_sessions()
        return True

    def reap_idle(self):
        now = time.time()
        timed_out = [slot.path for slot, _sess in self._live_sessions()
                     if not slot.borrowed
                     and now - slot.last_used > self.idle_timeout]
        for path in timed_out:
            self._reclaim(path)

    def report_wedged(self):
        """Log any slot still borrowed well past its check's deadline.

        Every check carries a wall timeout, so a slot held long after it means
        the machinery is stuck and the file has been refusing checks since.
        Not reclaimed: the borrower may still be running.
        """
        now = time.time()
        with self.lock:
            wedged = [(s.path, now - (s.since or now))
                      for s in self.sessions.values()
                      if s.borrowed and s.deadline is not None
                      and now > s.deadline + 60]
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
        """Check a file; the reply carries what to print and what to exit with.

        Rendering is in `report`, on this side of the socket, where the checked
        text, the workspace root and the compile job are.  The exit code is
        policy -- 1 "the proof is wrong" against 2 "never checked" -- so it is
        decided once, here.
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

        # One check per file at a time.  Refused rather than queued: what a
        # waiter would wait on is a proof, so no deadline would be honest.
        slot = self._borrow(path, deadline=t0 + timeout)
        if slot is None:
            return self._busy_refusal(path)
        try:
            # Before spawning: this slot is borrowed so eviction cannot pick
            # it, and the session it will hold already counts.
            self._evict()
            # Stat'ed before the session loads anything: a `.vo` rebuilt mid
            # check must not be recorded as though that were what it loaded.
            parked = slot.sess
            watched = sorted(set(closure)
                             | set(parked.loaded if parked else ()))
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
                sess.loaded = {p: pre.get(p, post[p]) for p in post}
        finally:
            # A slot never given back refuses its file for the daemon's life.
            self._give_back(slot)
            self._record_sessions()
        # Checkable again from here: `--compile` runs a real `rocq compile`
        # for minutes and touches no session, so it is outside the borrow.
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
            "stale": stale_rows,            # only when allow_stale let it through
            "note": note,
            "vo": _describe_job(job) if job is not None else None,
            # Why make would rebuild THIS file's .vo now; after a green check
            # that is "its source is newer".
            "vo_stale": project.staleness(project.vo_of(path), graph.graph),
            # The bytes this verdict is about, for a caller that edits fast.
            "digest": digest,
            # Resolved against the text checked, so nothing re-reads the file.
            "diags": [{"kind": d.kind,
                       "at": diag.line_col(text, d.span(text)),
                       "message": d.message().decode("utf8", "replace")}
                      for d in result.diags],
        }

    def _busy_refusal(self, path):
        """A check refused because another one holds the file.

        Exit 2 at the client, like a stale dependency: not a verdict about the
        proof.  Carries how long the file has been busy, which is what makes a
        wedged slot diagnosable.
        """
        with self.lock:
            slot = self.sessions.get(path)
            since = slot.since if slot is not None and slot.borrowed else None
        row = {"path": path, "since": since,
               "seconds": None if since is None else time.time() - since}
        # Can be missing: the slot may have been given back since the borrow
        # failed.
        detail = ("" if row["seconds"] is None
                  else ", running for %.0fs" % row["seconds"])
        return {"ok": False, "busy": row,
                "error": "one check per file at a time, and this file is "
                         "already being checked%s" % detail}

    # ------------------------------------------------------------ staleness

    def _stale(self, closure, graph, deadline):
        """Stale members of `closure`, after waiting for our own compiles.

        A `.vo` still stale because its compile has not finished is a reason
        to wait, not to refuse.  Only this daemon's own jobs are waited for; a
        `make` in another terminal is invisible to us.
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
        """What the daemon holds, sessions being checked right now included.

        Reads sessions it does not own, which is safe because it only observes
        -- a pid, an RSS, a list's length -- in single reads that cannot raise.
        """
        now = time.time()
        out = []
        for slot, sess in sorted(self._live_sessions(),
                                 key=lambda pair: pair[0].path):
            since = slot.since if slot.borrowed else None
            out.append({
                "path": slot.path,
                "pid": sess.live_pid(),
                "sentences": len(sess.sentences),
                "complete": sess.complete,
                "rss": sess.rss_bytes(),
                # Time since the slot was given back, which is not idleness
                # for a session being checked -- hence `busy`.
                "idle": now - slot.last_used,
                "busy": slot.borrowed,
                "busy_for": None if since is None else now - since,
                "watched": len(sess.loaded),
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

        Two clients can race to spawn one.  Without the lock the loser unlinks
        the winner's socket and binds its own, orphaning a daemon that holds
        its sessions resident and unreachable until it times out.  The lock fd
        is held for the daemon's life.
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
                if self._live_sessions():
                    empty_since = None
                    continue
                empty_since = empty_since or time.time()
                if time.time() - empty_since > self.idle_timeout:
                    self.shutdown()
            except Exception:
                pass

    def _stop_all(self):
        """Stop every child, borrowed or not.  Never leave one behind.

        The one place a non-borrower changes a borrowed slot: the daemon is on
        its way out, a mid-check child has to die anyway, and waiting for a
        long check to give its slot back would hang.  Reaches everything, since
        a slot never leaves the table; a borrower whose child dies under it
        sees `SessionDead` and takes its usual discard path.
        """
        for slot, _sess in self._live_sessions():
            try:
                slot.discard()                      # borrowed ones included
            except Exception:                       # noqa: BLE001
                pass
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
