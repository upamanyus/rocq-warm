"""Two `rocq-warm check` invocations at once, over one file.

Unlike `test_rocq_warm_daemon.py`, which drives the real CLI in a subprocess,
these drive a `Server` object in-process from two threads.  Everything being
asserted here lives in the moment one thread wants a session another thread
has, and reaching that moment needs a second request to arrive inside a window
a subprocess cannot be aimed at.  Nothing here waits on a duration: the tests
key off the session table, so a slower machine only widens the window.

The property underneath all of them is in `tearDown`, so it is checked by
every test in the file and not just the one that mentions it: **no `rocq repl`
this daemon spawned is alive unless the daemon can still reach it.**  That is
what the session table is for, and every bug these tests were written for was
a way of losing one.
"""

import os
import threading
import time
import unittest

from rocq_warm_helpers import Workspace, alive, requires_rocq_repl, wait_for
from rocqwarm import server as server_mod
from rocqwarm import session as session_mod

# ~10s of checking on a 2026 laptop, and slow is harmless: every test below
# keys off a predicate, so a slower machine only widens the window it needs.
SPIN = b"Lemma spin : True.\nProof. do 80000000 idtac. exact I. Qed.\n"
QUICK = b"Lemma quick : True.\nProof. exact I. Qed.\n"


class TrackingLock:
    """A lock that remembers which thread holds it.

    Only so a test can assert that some piece of work is NOT done under the
    session table's lock.  `threading.Lock` will say it is held; it will not
    say by whom, and "held by somebody" is not the question.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.owner = None

    def acquire(self, *a, **kw):
        got = self._lock.acquire(*a, **kw)
        if got:
            self.owner = threading.get_ident()
        return got

    def release(self):
        self.owner = None
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()

    def held_by_me(self):
        return self.owner == threading.get_ident()


class ServerCase(unittest.TestCase):
    """A `Server` on a throwaway workspace, with every child accounted for."""

    BODY = QUICK
    MAX_SESSIONS = server_mod.DEFAULT_MAX_SESSIONS

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.path = self.ws.write("C.v", self.BODY)
        self.srv = server_mod.Server(self.ws.dir, max_sessions=self.MAX_SESSIONS)
        self.srv.lock = TrackingLock()
        os.makedirs(self.srv.dir, exist_ok=True)

        # Every `rocq repl` this daemon spawns, whether or not the session
        # table still knows about it -- which is the whole question here.
        self.spawned = []
        real_start = session_mod.Session.start

        def traced_start(sess):
            real_start(sess)
            self.spawned.append(sess.proc.pid)

        session_mod.Session.start = traced_start
        self.addCleanup(setattr, session_mod.Session, "start", real_start)
        self.addCleanup(self.kill_spawned)

    def tearDown(self):
        leaked = self.leaked()
        self.srv._stop_all()
        self.assertEqual(leaked, [], "a rocq repl nobody can reach any more")

    def kill_spawned(self):
        """Leave no rocq behind, including the ones the daemon lost track of."""
        import signal
        for pid in self.spawned:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                pass

    # ------------------------------------------------------------- driving

    def check(self, path=None, **kw):
        req = {"cmd": "check", "path": path or self.path}
        req.update(kw)
        return self.srv.do_check(req)

    def in_background(self, **kw):
        """Run a check in another thread; returns (thread, results-dict)."""
        out = {}
        t = threading.Thread(
            target=lambda: out.update(result=self.check(**kw)), daemon=True)
        t.start()
        return t, out

    def race(self, **paths):
        """Check several files at the same instant; returns {name: response}.

        A barrier rather than a sleep, so the collision is the test's doing
        and not the scheduler's.  A thread that raised leaves its name out of
        the result, which is caught here rather than surfacing later as a
        `KeyError` in the assertions.
        """
        bar, out = threading.Barrier(len(paths)), {}

        def go(name, path):
            bar.wait()
            out[name] = self.check(path=path)

        threads = [threading.Thread(target=go, args=(n, p))
                   for n, p in paths.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)
        self.assertEqual(sorted(out), sorted(paths),
                         "a racing check never returned: %r" % (out,))
        return out

    def wait_until_checking(self):
        """Block until a check of self.path has borrowed it and has a rocq."""
        self.assertTrue(
            wait_for(lambda: self.borrowed(self.path) and bool(self.spawned)),
            "the check never got going")

    # ------------------------------------------------------------ observing

    def slot(self, path=None):
        return self.srv.sessions.get(os.path.abspath(path or self.path))

    def borrowed(self, path=None):
        slot = self.slot(path)
        return slot is not None and slot.borrowed

    def parked(self, path=None):
        """The session sitting in the file's slot, if there is one."""
        slot = self.slot(path)
        return None if slot is None else slot.sess

    def tracked_pids(self):
        return [sess.live_pid() for _slot, sess in self.srv._live_sessions()]

    def leaked(self):
        """Children that are alive but no longer reachable from the table."""
        tracked = self.tracked_pids()
        return [p for p in self.spawned if alive(p) and p not in tracked]

    def recorded_pids(self):
        try:
            rows = open(self.srv.pids_path).read().splitlines()
        except OSError:
            return []
        return [int(r.split("\t")[0]) for r in rows if r]


class SlotTableTests(ServerCase):
    """The table itself.  No rocq runs: borrowing a slot spawns nothing."""

    def test_borrowing_twice_over_returns_the_same_slot(self):
        """A slot is not made stale by having no session yet.

        When it was, a second check arriving before the first had spawned
        anything replaced the first's slot and started a session of its own.
        The slot is also the file's record across sessions, so it has to be
        the same object the next time round.
        """
        first = self.srv._borrow(self.path)
        self.assertIsNone(first.sess, "a borrow should not have spawned yet")
        self.srv._give_back(first)

        second = self.srv._borrow(self.path)
        self.addCleanup(self.srv._give_back, second)
        self.assertIs(second, first)

    def test_a_borrowed_file_is_refused_and_gets_no_second_slot(self):
        """The refusal has to come from a `return`, not a fall-through.

        `_borrow` builds a `Slot` when it finds none.  If the `borrowed`
        test fell through to that instead of returning, a second check would
        get its own slot and its own `rocq repl` for a file somebody else is
        already checking -- which is the bug this shape removes, one missing
        `return` away.
        """
        built = []
        real_init = server_mod.Slot.__init__

        def counted_init(slot, path):
            real_init(slot, path)
            built.append(path)

        held = self.srv._borrow(self.path)
        self.addCleanup(self.srv._give_back, held)

        server_mod.Slot.__init__ = counted_init
        self.addCleanup(setattr, server_mod.Slot, "__init__", real_init)
        self.assertIsNone(self.srv._borrow(self.path))
        self.assertEqual(built, [], "a second slot was built for a busy file")

    def test_a_slot_held_past_its_deadline_is_reported(self):
        """The one failure this model can produce, and it must not be silent.

        A check that hangs for ever never gives its slot back, and its file is
        refused from then on.  Nothing can safely take the slot away -- the
        thread that owns it may still be running -- so the daemon says so
        instead, which is the difference between a diagnosable wedge and a
        file that mysteriously stopped being checkable.
        """
        slot = self.srv._borrow(self.path)
        self.addCleanup(self.srv._give_back, slot)

        slot.deadline = time.time() + 3600
        self.assertEqual(self.srv.report_wedged(), 0, "reported a live check")

        slot.deadline = time.time() - 120
        self.assertEqual(self.srv.report_wedged(), 1)
        self.assertTrue(self.borrowed(), "reported AND reclaimed")

    def test_a_slot_holds_a_session_or_says_it_is_borrowed(self):
        slot = self.srv._borrow(self.path)
        self.assertTrue(slot.borrowed)
        self.assertIsNone(slot.sess, "nothing is spawned by borrowing")
        self.srv._give_back(slot)
        self.assertFalse(slot.borrowed)


@requires_rocq_repl
class ConcurrentCheckTests(ServerCase):

    BODY = SPIN

    def test_a_concurrent_check_of_one_file_is_refused(self):
        """The realistic shape: an editor-on-save racing an agent.

        Both threads do identical work before the checkout (`graph.refresh`
        behind one lock, then `_stale`), so they arrive together without any
        help from the test.  Exactly one gets a verdict; the other is told the
        file is busy, promptly, and nothing is left running behind either.
        """
        out = self.race(A=self.path, B=self.path)
        verdicts = [r for r in out.values() if r.get("ok")]
        refusals = [r for r in out.values() if not r.get("ok")]
        self.assertEqual(len(verdicts), 1, out)
        self.assertEqual(len(refusals), 1, out)
        self.assertTrue(verdicts[0]["passed"], verdicts[0])

        # Refused for THIS reason.  Without the `busy` key a stale dependency
        # or a missing rocq would satisfy the assertion just as well.
        self.assertIn("busy", refusals[0], refusals[0])
        self.assertIn("one check per file", refusals[0]["error"])

        self.assertEqual(len(self.tracked_pids()), 1,
                         "one file, two checks, more than one session")

    def test_the_refusal_does_not_wait_for_the_check(self):
        """Fail fast is the point: the refusal must not be a disguised queue.

        A `--wait` reintroduced later would still pass every other assertion
        in this file, and would only show up as a test that takes as long as
        the proof does.
        """
        thread, out = self.in_background()
        self.wait_until_checking()

        t0 = time.time()
        refused = self.check()
        elapsed = time.time() - t0

        self.assertIn("busy", refused, refused)
        self.assertLess(elapsed, 2.0,
                        "the refusal waited for the running check")
        self.assertGreaterEqual(refused["busy"]["seconds"], 0.0)

        thread.join(timeout=300)
        self.assertTrue(out["result"].get("passed"), out["result"])

    def test_a_slow_check_does_not_block_another_file(self):
        """The claim the daemon exists for, and the reason one check per file
        is a refusal rather than a queue: DIFFERENT files check in parallel.

        Asserted structurally rather than by timing, which would be a race on
        a loaded machine.  A file's slot stays borrowed for as long as its
        proof runs, so a verdict for the quick file that arrives while the
        slow file is still checked out can only have been reached alongside
        it -- and two `rocq repl` were spawned to do it, which is what "in
        parallel" has to mean.

        This covers the checks themselves.  The pre-flight every check runs
        before it borrows -- `graph.refresh`, behind one lock per project --
        is shared and is deliberately not what this measures.
        """
        quick = self.ws.write("D.v", QUICK)
        thread, out = self.in_background()          # C.v, the slow proof
        self.wait_until_checking()

        verdict = self.check(path=quick)
        # Read before joining: afterwards the slow check is over either way.
        still_running = self.borrowed(self.path)

        self.assertTrue(verdict.get("passed"), verdict)
        self.assertTrue(still_running,
                        "the slow check finished before the quick one, so "
                        "nothing here was concurrent -- SPIN is too fast")
        # Cumulative, so eviction under memory pressure cannot mask it.
        self.assertEqual(len(self.spawned), 2, self.spawned)

        thread.join(timeout=300)
        self.assertTrue(out["result"].get("passed"), out["result"])

    def test_a_cold_check_cannot_take_a_session_mid_check(self):
        """`--cold` is refused like anything else, and takes nothing.

        It used to replace the entry in the table instead, which stranded the
        running check's `rocq repl`: the table was the only reference to it,
        and every way of reclaiming a session looked the table up by path and
        therefore found the replacement.  Once the file is free, `--cold` does
        what it says -- in place, in the same slot.
        """
        thread, out = self.in_background()
        self.wait_until_checking()
        running = self.slot()

        self.assertIn("busy", self.check(cold=True))

        thread.join(timeout=300)
        self.assertTrue(out["result"].get("passed"), out["result"])
        self.assertIs(self.slot(), running, "the running check lost its slot")

        # Now that it is free, --cold restarts in place: same slot, one child.
        before = running.sess.live_pid()
        self.assertTrue(self.check(cold=True).get("passed"))
        self.assertIs(self.slot(), running)
        self.assertEqual(len(self.tracked_pids()), 1)
        self.assertNotEqual(self.slot().sess.live_pid(), before,
                            "--cold did not actually restart the session")

    def test_a_timed_out_check_leaves_the_file_checkable(self):
        """A discarded session leaves an empty slot, and blocks nothing.

        The cleanup used to resolve `_drop(path)` by key, so once the entry
        had been replaced it tore down somebody else's session instead of its
        own.  There is nothing to resolve now: the check discards the session
        in the slot it borrowed.
        """
        result = self.check(timeout=3)
        self.assertIn("timed out", result.get("error", ""), result)

        self.assertIsNone(self.parked(), "the session survived its timeout")
        self.assertFalse(self.borrowed())
        self.assertEqual(self.tracked_pids(), [])

        again = self.check()
        self.assertNotIn("busy", again, "the timed-out check wedged the file")
        self.assertTrue(again.get("passed"), again)
        self.assertEqual(again["mode"], "cold")


@requires_rocq_repl
class SlotReturnTests(ServerCase):
    """The hazard checkout introduces: a slot that never comes back."""

    def test_a_slot_is_returned_even_when_the_check_raises(self):
        """Without the `finally` the file is refused for the daemon's life.

        That is worse than any of the bugs this replaces, and it is invisible
        until somebody checks the same file twice.
        """
        real_check = session_mod.Session.check

        def exploding_check(sess, text, timeout=1800):
            raise RuntimeError("boom")

        session_mod.Session.check = exploding_check
        try:
            with self.assertRaises(RuntimeError):
                self.check()
        finally:
            session_mod.Session.check = real_check

        self.assertFalse(self.borrowed(), "the slot was never given back")
        again = self.check()
        self.assertNotIn("busy", again, "the raising check never gave the slot back")
        self.assertTrue(again.get("passed"), again)

    def test_a_discarded_session_leaves_nothing_behind(self):
        """`loaded` describes a process, so it lives on the process.

        Anything that outlived a `rocq repl` while still describing it would
        hand the next check a `watched` set, and `status` a row, about a
        session that no longer exists.  Holding it on the `Session` makes that
        impossible rather than merely handled: there is nothing to clear,
        because the object with the fields on it is the thing being dropped.
        """
        self.assertTrue(self.check().get("passed"))
        was = self.parked()
        self.assertTrue(was.loaded, "the check recorded nothing as loaded")

        self.assertTrue(self.srv._reclaim(os.path.abspath(self.path)))
        self.assertIsNone(self.parked(), "the slot kept its dead session")
        self.assertIsNone(was.live_pid(), "the discarded child is still alive")
        self.assertEqual(self.tracked_pids(), [])


@requires_rocq_repl
class BookkeepingTests(ServerCase):

    def test_the_pid_file_names_the_session_that_is_running(self):
        """It is the only cover for a BUSY child of a killed daemon.

        The daemon holds the only writer on each child's stdin, so its death
        is an EOF and an idle Rocq exits on its own.  A child mid-`vm_compute`
        will not read stdin and outlives it -- and used to be absent from the
        pid file, because the file was written when a table entry was created,
        which is before the session it names exists.
        """
        self.assertTrue(self.check().get("passed"))
        self.assertEqual(self.recorded_pids(), self.tracked_pids())
        self.assertEqual(len(self.recorded_pids()), 1)

        other = self.ws.write("D.v", QUICK)
        self.assertTrue(self.check(path=other).get("passed"))
        self.assertEqual(sorted(self.recorded_pids()),
                         sorted(self.tracked_pids()))
        self.assertEqual(len(self.recorded_pids()), 2)

    def test_the_staleness_check_is_not_under_the_table_lock(self):
        """Deciding whether a session still matches its libraries reads disk.

        The check fingerprints the closure and the session's own `.vo` set
        together and `loaded_changed` compares against that, so the `stat`
        sweep happens once -- but either of them under the session table's
        lock would be every other file's check waiting behind a few hundred
        `stat` calls, which is the opposite of what a daemon serving several
        files at once is for.
        """
        seen = []
        real = session_mod.Session.loaded_changed

        def watched(sess, known=None):
            seen.append(self.srv.lock.held_by_me())
            return real(sess, known=known)

        self.assertTrue(self.check().get("passed"))     # populates `loaded`
        session_mod.Session.loaded_changed = watched
        self.addCleanup(setattr, session_mod.Session, "loaded_changed", real)
        self.assertTrue(self.check().get("passed"))     # ... and consults it

        self.assertTrue(seen, "loaded_changed was never consulted")
        self.assertNotIn(True, seen,
                         "loaded_changed ran under the session table's lock")


@requires_rocq_repl
class EvictionTests(ServerCase):
    """More files in flight than session slots."""

    MAX_SESSIONS = 1

    def test_eviction_never_strands_a_session(self):
        """Eviction used to remove an entry a check was about to use.

        `_entry` evicted on its way out, before its caller had taken the
        entry's lock, and the victim chosen was whichever slot was free --
        which, with the others busy, was the brand-new one being handed over.
        The check then started a session in an entry no reclamation path could
        reach.  It needed no staleness and no `--cold`: only more files at
        once than session slots.
        """
        other = self.ws.write("D.v", QUICK)
        out = self.race(C=self.path, D=other)
        for name, res in out.items():
            self.assertTrue(res.get("passed"), "%s: %r" % (name, res))
        # The budget is one, so at most one session survives -- and whatever
        # did not survive was stopped, not merely forgotten (tearDown).
        self.assertLessEqual(len(self.tracked_pids()), self.MAX_SESSIONS)
        self.assertEqual(sorted(self.recorded_pids()),
                         sorted(self.tracked_pids()))


if __name__ == "__main__":
    unittest.main()
