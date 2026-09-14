"""Two `rocq-warm check` invocations at once, over one file.

PROVISIONAL -- these are the repros from `CONCURRENCY-BUGS.md` written up as
tests, and they are RED on purpose until that branch's fixes land.  They may
not survive in this shape.

Unlike `test_rocq_warm_daemon.py`, which drives the real CLI in a subprocess,
these drive a `Server` object in-process from two threads.  The races are all
in the moment an entry is replaced in `Server.sessions`, and reaching that
moment needs a second request to arrive inside a window a subprocess cannot
be aimed at.  Nothing here depends on how long a check takes: the tests wait
on `Entry.lock` and on the session table, never on a sleep.
"""

import os
import threading
import unittest

from rocq_warm_helpers import Workspace, requires_rocq, wait_for
from rocqwarm import server as server_mod
from rocqwarm import session as session_mod

# ~10s of checking on a 2026 laptop, and slow is harmless: every test below
# keys off a predicate, so a slower machine only widens the window it needs.
SPIN = b"Lemma spin : True.\nProof. do 80000000 idtac. exact I. Qed.\n"
QUICK = b"Lemma quick : True.\nProof. exact I. Qed.\n"


def alive(pid):
    """Is `pid` a live process -- as opposed to gone, or an unreaped zombie?"""
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class ServerCase(unittest.TestCase):
    """A `Server` on a throwaway workspace, with every child accounted for."""

    BODY = QUICK

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.path = self.ws.write("C.v", self.BODY)
        self.srv = server_mod.Server(self.ws.dir)
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

    def kill_spawned(self):
        """Leave no rocq behind, including the ones the daemon lost track of."""
        import signal
        for pid in self.spawned:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                pass

    def check(self, **kw):
        req = {"cmd": "check", "path": self.path}
        req.update(kw)
        return self.srv.do_check(req)

    def in_background(self, **kw):
        """Run a check in another thread; returns (thread, results-dict)."""
        out = {}
        t = threading.Thread(
            target=lambda: out.update(result=self.check(**kw)), daemon=True)
        t.start()
        return t, out

    @property
    def entry(self):
        return self.srv.sessions.get(os.path.abspath(self.path))

    def tracked_pids(self):
        return [e.sess.proc.pid for e in self.srv.sessions.values()
                if e.sess.proc is not None]

    def leaked(self):
        """Children that are alive but no longer reachable from the table."""
        tracked = self.tracked_pids()
        return [p for p in self.spawned if alive(p) and p not in tracked]


class EntryTableTests(ServerCase):
    """The table itself.  No rocq runs: `_entry` deliberately spawns nothing."""

    def test_a_fresh_entry_is_not_stale(self):
        """`_entry` twice over must hand back the same entry.

        The session is started later, by `do_check`, under the entry's own
        lock -- so a brand-new entry is never `alive`, and `_stale_entry`'s
        `not entry.sess.alive` test reads that as "cold" and replaces an entry
        somebody else is about to use.
        """
        first = self.srv._entry(self.path)
        self.assertFalse(first.sess.alive, "_entry should not have spawned yet")
        self.assertIsNone(self.srv._stale_entry(
            first, first.flags, first.toolchain, False),
            "a not-yet-started entry was called stale")
        self.assertIs(self.srv._entry(self.path), first)


@requires_rocq
class ConcurrentCheckTests(ServerCase):

    BODY = SPIN

    def test_two_cold_checks_of_one_file_share_a_session(self):
        """The realistic shape: an editor-on-save racing a `make`.

        Both clients do identical work before `_entry` (`graph.refresh` behind
        one lock, then `_stale`), so they arrive together without any help
        from the test.
        """
        entries, bar = [], threading.Barrier(2)

        real_entry = server_mod.Server._entry

        def traced_entry(srv, path, force_cold=False, toolchain=None):
            e = real_entry(srv, path, force_cold=force_cold,
                           toolchain=toolchain)
            entries.append(e)
            return e

        server_mod.Server._entry = traced_entry
        self.addCleanup(setattr, server_mod.Server, "_entry", real_entry)

        out = {}

        def go(name):
            bar.wait()
            out[name] = self.check()

        threads = [threading.Thread(target=go, args=(n,)) for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)

        for name, res in out.items():
            self.assertTrue(res.get("passed"), "%s: %r" % (name, res))
        self.assertEqual(len(entries), 2, "both threads should reach _entry")
        self.assertIs(entries[0], entries[1],
                      "one file, two concurrent checks, two sessions")
        self.assertEqual(len(self.tracked_pids()), 1)
        self.assertEqual(self.leaked(), [], "a rocq nobody can reach")

    def test_a_replaced_busy_session_is_stopped(self):
        """Replacing a mid-check entry must not strand its `rocq repl`.

        `_entry`'s comment promises the busy one is "stopped when its check
        ends"; nothing does that, and the table was the last reference.
        """
        thread, out = self.in_background()
        self.assertTrue(wait_for(lambda: self.entry is not None
                                 and self.entry.lock.locked()),
                        "the check never got going")
        busy = self.entry

        self.assertIsNot(self.srv._entry(self.path, force_cold=True), busy)
        self.assertIsNot(self.entry, busy, "the busy entry is still in the table")

        thread.join(timeout=300)
        self.assertTrue(out["result"].get("passed"), out["result"])

        # Sample the pid AFTER the check: `Session._check` restarts the session
        # on the cold path, so a pid taken mid-check is one the session itself
        # already retired.
        orphan = busy.sess.proc.pid
        self.assertTrue(wait_for(lambda: not alive(orphan), timeout=30),
                        "the replaced session outlived its check")

    def test_a_timing_out_check_does_not_drop_its_replacement(self):
        """`_drop(path)` resolves by key, so it reaches the wrong entry.

        Once the timing-out check's own entry has been replaced, its cleanup
        tears down the replacement -- which a third client may already be
        feeding.
        """
        thread, out = self.in_background(timeout=3)
        self.assertTrue(wait_for(lambda: self.entry is not None
                                 and self.entry.lock.locked()),
                        "the check never got going")

        replacement = self.srv._entry(self.path, force_cold=True)
        thread.join(timeout=300)
        self.assertIn("timed out", out["result"].get("error", ""), out["result"])

        self.assertIs(self.entry, replacement,
                      "the timed-out check dropped somebody else's entry")


if __name__ == "__main__":
    unittest.main()
