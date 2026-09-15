"""Ctrl+C: what happens to the work, and to the session that was doing it.

A client that dies is the ordinary case, not the exotic one -- an agent that
gives up, an editor that is closed, a person who has seen the error they were
looking for scroll past.  What must not happen is what used to: the daemon
kept proving for nobody until the check's half-hour timeout, and because a
file stays checked out for as long as its check runs, every other check of it
was refused for that same half hour.

So an interrupted check has to end three ways at once, and each is asserted
below:

* the work stops, promptly, rather than being run to completion for nobody;
* the `rocq repl` SURVIVES it.  Killing the child would be easy, and would
  throw away the minutes of state that are the whole point of the tool;
* the session goes back into the table, parked at the line the interrupt
  stopped it on, so the very next check of the file replays from there.

The socket half needs no Rocq: an EOF on the connection is the only notice the
daemon gets, and that is plain sockets.  The session half needs a real one,
because "stopped where it stood and still usable" is a claim about Rocq's
reaction to a signal, which no double can make on its behalf.
"""

import json
import os
import signal
import socket
import subprocess
import threading
import time
import unittest

from rocq_warm_helpers import (TOOLS, Workspace, alive, normalize,
                               requires_rocq, requires_rocq_repl, wait_for)
from rocqwarm import diag as diag_mod
from rocqwarm import server as server_mod
from rocqwarm import session as session_mod

# The `Server` fixture, and the property every test using it carries: no `rocq
# repl` is left alive that the daemon cannot still reach.  Imported rather
# than rebuilt, because an interrupt is one more way to lose a child and it
# should be watched by the same tearDown as every other way.
from test_rocq_warm_concurrency import ServerCase

CLI = os.path.join(TOOLS, "rocq-warm")

# Around 20 seconds of tactic on a 2026 laptop, which is the same iteration
# count `test_rocq_warm_concurrency.py` uses and for the same reason: long
# enough that an interrupt lands inside the tactic on any machine, and slow is
# harmless because every test here keys off the session table rather than a
# duration.  Not longer, either -- `do N` costs memory in N, and a session
# that crosses its RSS ceiling is killed as a memory hog, which looks exactly
# like an interrupt that went wrong.
SPIN = b"""Lemma spin : True.
Proof.
do 80000000 idtac.
exact I.
Qed.
"""
# The same file with the spin edited out, which is what a person does next.
FIXED = SPIN.replace(b"do 80000000 idtac.\n", b"idtac.\n")


class PeerWatchTests(unittest.TestCase):
    """`_watch_peer`: is anybody still waiting for this reply?

    No Rocq and no daemon -- a socketpair is the whole world a watcher sees.
    """

    def setUp(self):
        self.us, self.them = socket.socketpair()
        # The daemon's end of the request, and the pipe it says "done" down.
        self.stop_r, self.stop_w = socket.socketpair()
        for sock in (self.us, self.them, self.stop_r, self.stop_w):
            self.addCleanup(sock.close)      # a test may have closed it already
        self.gone = threading.Event()

    def watch(self):
        t = threading.Thread(target=server_mod._watch_peer,
                             args=(self.us, self.gone, self.stop_r),
                             daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(self.stop_w.close)
        return t

    def test_a_client_that_is_still_waiting_is_left_alone(self):
        """The false positive that would matter most: cancelling live checks.

        A client sends its request and then does nothing at all for as long
        as the proof takes, which to anything but the socket itself is
        indistinguishable from being dead.  Nothing here has to be timed --
        the watcher blocks -- so the wait is only to catch a spontaneous one.
        """
        t = self.watch()
        time.sleep(0.2)
        self.assertFalse(self.gone.is_set(), "cancelled a client that is there")
        self.assertTrue(t.is_alive(), "the watcher gave up on a live client")

    def test_a_client_that_goes_away_is_noticed(self):
        t = self.watch()
        self.them.close()
        self.assertTrue(wait_for(self.gone.is_set, timeout=5),
                        "the EOF was never noticed")
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "the watcher kept polling after the EOF")

    def test_a_client_that_talks_out_of_turn_is_treated_as_gone(self):
        """Anything arriving at all ends the request, not just an EOF.

        The client has already sent everything the protocol gives it to send,
        so bytes behind that are a client this daemon cannot account for.
        Ending the request is both the honest answer and the cheap one, since
        an abandoned check keeps its session; carrying on instead would leave
        a second message on this connection to be swallowed in silence, and
        would spin on a core for as long as a talkative client kept writing.
        """
        t = self.watch()
        self.them.sendall(b"hello?")
        self.assertTrue(wait_for(self.gone.is_set, timeout=5),
                        "bytes after the request were passed over")
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "the watcher kept polling")

    def test_the_watcher_stops_when_the_request_is_over(self):
        """It must be finished before the connection is closed.

        A `select` still blocked on a closed fd wakes on whatever the next
        thread opens in its place, and would then read a stranger's
        connection.  Since the watcher blocks, the thing that ends it has to
        be a wake-up and not a flag -- so closing this end is the test.
        """
        t = self.watch()
        self.stop_w.close()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "the watcher outlived its request")
        self.assertFalse(self.gone.is_set(), "a finished request was cancelled")

    def test_a_client_leaving_as_the_request_ends_is_not_reported(self):
        """Both ready at once, and the request being over wins.

        Whichever order the two land in, there is no check left to cancel and
        `gone` would be a departure reported against nobody.
        """
        t = self.watch()
        self.them.close()
        self.stop_w.close()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "the watcher outlived its request")


class ServedRequestTests(unittest.TestCase):
    """`_serve_one`, over a real socket.  Nothing here starts a Rocq."""

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.srv = server_mod.Server(self.ws.dir)
        os.makedirs(self.srv.dir, exist_ok=True)

    def serve(self, req):
        """Hand one request to the daemon the way the socket does."""
        us, them = socket.socketpair()
        self.addCleanup(them.close)
        t = threading.Thread(target=self.srv._serve_one, args=(them,),
                             daemon=True)
        t.start()
        server_mod.send_msg(us, req)
        return us, t

    def test_a_request_is_answered_and_its_watcher_reaped(self):
        """The watcher is an implementation detail and must stay one.

        Every request now starts a thread, so every request has to end one:
        a daemon that leaks one per check is a daemon that runs out.
        """
        before = threading.active_count()
        us, t = self.serve({"cmd": "ping"})
        self.addCleanup(us.close)

        resp = server_mod.recv_msg(us)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["pid"], os.getpid())

        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "the request never finished")
        self.assertTrue(wait_for(lambda: threading.active_count() <= before,
                                 timeout=5),
                        "a watcher thread outlived its request")

    def test_a_request_with_bytes_behind_it_is_still_answered(self):
        """The watcher reads, so when it starts is load-bearing.

        It is started only after the request has been parsed.  Started any
        earlier it would race `recv_msg` for the request's own bytes, take
        some of them, and -- now that stray bytes end the request -- abandon a
        check over the bytes that were asking for it.  The client would wait
        for a reply that was never coming.

        Trailing bytes are what that race looks like from outside, so they are
        sent deliberately: whichever thread gets them, the request in front of
        them must still be answered.  A `ping` is the probe because it reads
        no cancellation, which isolates the question to the framing.
        """
        us, t = self.serve({"cmd": "ping"})
        self.addCleanup(us.close)
        us.sendall(b"a second thing nobody asked for")

        resp = server_mod.recv_msg(us)
        self.assertIsNotNone(resp, "the request went unanswered")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["pid"], os.getpid())
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "the request never finished")

    def test_a_client_gone_before_the_check_starts_spawns_nothing(self):
        """The cheapest interrupt to honour, and the easiest to miss.

        A check that is already abandoned when it arrives must not start a
        `rocq repl` to find that out.  Driven through `_check` with the event
        pre-set, because this path must not need a rocq on PATH -- it is the
        path that refuses to go looking for one.
        """
        path = self.ws.write("A.v", b"Definition a := 1.\n")
        spawned = []
        real_start = session_mod.Session.start
        session_mod.Session.start = lambda sess: spawned.append(sess)
        self.addCleanup(setattr, session_mod.Session, "start", real_start)

        gone = threading.Event()
        gone.set()
        resp = self.srv._check(path, {"cmd": "check", "path": path}, gone)

        self.assertTrue(resp.get("abandoned"), resp)
        self.assertFalse(resp.get("ok"), resp)
        self.assertEqual(spawned, [],
                         "spawned a rocq for a client that had gone")
        self.assertEqual(self.srv.sessions, {}, "borrowed a slot for nobody")

    def test_an_abandoned_check_is_not_a_verdict(self):
        """Exit 2, and never 1: there is no answer, which is not "no".

        Nobody normally reads this reply -- the socket it would go to is
        closed -- but a caller that only half-closed and kept reading must not
        be told its proof is broken.
        """
        path = self.ws.write("A.v", b"Definition a := 1.\n")
        gone = threading.Event()
        gone.set()
        resp = self.srv.do_check({"cmd": "check", "path": path}, cancelled=gone)

        self.assertEqual(resp["exit"], 2, resp)
        self.assertIn("abandoned", resp["log"])


class InterruptCase(ServerCase):
    """Driving a real check to the middle of a tactic and taking its client."""

    BODY = SPIN
    checking_pid = None         # the child that was interrupted

    def inside_a_tactic(self):
        """Block until Rocq is inside a sentence rather than between two.

        `parsed_end` moves as Rocq reports each sentence it finishes, so a
        `parsed_end` that has stopped moving while the child still burns CPU
        is Rocq in the middle of one -- which is where the interrupt has to
        land for this file to be testing anything.  The 0.3s is a sampling
        interval and not a guess at a window: on a slower machine the
        predicate holds more plainly rather than less.
        """
        def stuck():
            sess = self.parked()
            if sess is None or not sess.parsed_end:
                return False
            was, ticks = sess.parsed_end, sess._cpu_ticks()
            time.sleep(0.3)
            return (sess.parsed_end == was
                    and ticks is not None and sess._cpu_ticks() != ticks)

        self.assertTrue(wait_for(stuck, timeout=120),
                        "rocq never got into the spinning tactic")

    def abandon(self):
        """Check the spinning file, take the client away, return the reply."""
        gone, out = threading.Event(), {}
        t = threading.Thread(target=lambda: out.update(
            result=self.srv.do_check({"cmd": "check", "path": self.path},
                                     cancelled=gone)), daemon=True)
        t.start()
        self.wait_until_checking()
        self.inside_a_tactic()
        self.checking_pid = self.parked().live_pid()
        gone.set()
        t.join(timeout=300)
        self.assertIn("result", out, "the abandoned check never returned")
        return out["result"]

    def assertParkedAndUsable(self, resp):
        """Interrupted, kept, and good for the next check -- whatever the file.

        The three things an interrupt owes, asserted the same way for a file
        that was written in full before it landed and for one that was not.
        """
        self.assertTrue(resp.get("abandoned"), resp)
        sess = self.parked()
        self.assertIsNotNone(sess, "the interrupt lost the session")
        self.assertEqual(sess.live_pid(), self.checking_pid,
                         "the session was replaced rather than interrupted")
        self.assertFalse(sess.complete,
                         "the check ran to the end -- SPIN is too fast here, "
                         "so nothing was interrupted")
        self.assertTrue(sess.sentences, "it kept none of what it executed")
        self.assertFalse(self.borrowed(), "the slot was never given back")

        # Usable, not merely alive: a session left waiting for the rest of a
        # sentence answers nothing ever again.
        self.ws.write("C.v", FIXED)
        again = self.check()
        self.assertTrue(again.get("passed"), again)
        self.assertEqual(again["mode"], "replay", again)
        self.assertEqual(self.parked().live_pid(), self.checking_pid,
                         "the next check ran in a different rocq")


@requires_rocq_repl
class InterruptedCheckTests(InterruptCase):
    """A real `rocq repl`, really interrupted, in the middle of a tactic."""

    def test_the_work_stops_and_the_session_survives_where_it_stopped(self):
        """All three properties of an interrupt, on the one session.

        `complete` is what says the work was actually cut short: a check that
        reached the end of the file would have set it whatever the client then
        did.  So a failure there means SPIN is too short for the machine, not
        that the interrupt was too weak.
        """
        resp = self.abandon()
        self.assertTrue(resp.get("abandoned"), resp)

        sess = self.parked()
        self.assertIsNotNone(sess, "the interrupt lost the session")
        self.assertIsNotNone(sess.live_pid(), "the interrupt killed rocq")
        # The same child, still: an interrupt that killed it and started
        # another would satisfy everything else here and would have thrown
        # away exactly what the session is kept for.
        self.assertEqual(sess.live_pid(), self.checking_pid,
                         "the session was replaced rather than interrupted")

        self.assertFalse(sess.complete,
                         "the check ran to the end -- SPIN is too fast here, "
                         "so nothing was interrupted")
        self.assertTrue(sess.sentences, "it kept none of what it executed")
        self.assertTrue(SPIN.startswith(sess.text),
                        "parked holding text the file does not begin with")
        self.assertLess(len(sess.text), len(SPIN))
        self.assertFalse(self.borrowed(), "the slot was never given back")

    def test_the_file_is_checkable_again_at_once_and_replays(self):
        """The bug a user actually meets, and the payoff for keeping the child.

        Before this, the file stayed checked out until the abandoned check
        timed out half an hour later, and every check of it in between was
        refused as busy.  Now the next check is served immediately -- and from
        the parked prefix, which is what makes an interrupt cheap rather than
        merely survivable.
        """
        self.abandon()
        parked = self.parked()
        self.assertIsNotNone(parked)

        self.ws.write("C.v", FIXED)
        again = self.check()

        self.assertNotIn("busy", again,
                         "the interrupted check left the file checked out")
        self.assertTrue(again.get("passed"), again)
        self.assertEqual(again["mode"], "replay",
                         "it did not resume from where the interrupt stopped")
        self.assertIs(self.parked(), parked,
                      "the replay ran in a different session")
        self.assertEqual(self.parked().live_pid(), self.checking_pid,
                         "the replay ran in a different rocq")

    @requires_rocq
    def test_a_session_that_was_interrupted_still_answers_like_coqc(self):
        """The property the whole tool is judged on, after an interrupt.

        Everything above would pass on a session that came back subtly wrong:
        it is alive, it is parked, it replays, it says OK.  What says the
        state is really sound is the same oracle as everywhere else -- put an
        error past the interrupted line and diff the whole verdict against a
        cold `coqc`.

        The interrupt reaches that state by signal where the rest of the suite
        reaches it by a failed sentence, and `User interrupt.` arrives in the
        middle of a tactic rather than at a sentence boundary, so it is worth
        asking Rocq rather than assuming.
        """
        self.abandon()
        broken = FIXED.replace(b"exact I.", b"exact 0.")
        self.ws.write("C.v", broken)

        resp = self.check()
        self.assertEqual(resp["mode"], "replay", resp)
        self.assertFalse(resp["passed"], resp)
        self.assertEqual(resp["exit"], 1)

        got = normalize("\n".join(
            diag_mod.render_at("C.v", d["at"], d["message"])
            for d in resp["diags"] if d["kind"] != "info"))
        rc, expected = self.ws.coqc("C.v")
        self.assertNotEqual(rc, 0, "coqc accepted the broken file")
        self.assertEqual(got, expected)


@requires_rocq_repl
class InterruptedMidWriteTests(InterruptCase):
    """The same interrupt, with the file still being written when it lands.

    A check writes with a bounded look-ahead, so on a file larger than that
    window the writer is still going when the interrupt arrives, and stopping
    it leaves Rocq waiting for the rest of a sentence it will never be sent.
    Closing that -- with the right terminator for whatever the cut landed
    inside -- is a different branch from the small-file case, where everything
    has been written and only the running tactic needs interrupting.  A
    session left waiting mid-sentence would look perfectly alive and would
    never answer again, so "usable afterwards" is the assertion that matters.
    """

    # Comfortably past the 16 KB the look-ahead starts at, in sentences small
    # enough that none of them is what widens it.
    BODY = SPIN + b"".join(b"Lemma filler_%d : True. Proof. exact I. Qed.\n" % i
                           for i in range(2000))

    def test_an_interrupt_mid_file_leaves_a_session_that_still_works(self):
        self.assertGreater(len(self.BODY), 4 * session_mod.DEFAULT_WRITE_AHEAD,
                           "the file is too small to stop the writer")
        self.assertParkedAndUsable(self.abandon())


@requires_rocq_repl
class InterruptedCliTests(unittest.TestCase):
    """The whole path, from the signal a terminal really sends.

    The tests above set the event by hand; this one sends SIGINT to a
    `rocq-warm check` subprocess, which is the only way to test that the EOF
    is noticed at all.

    The daemon is in a session of its own -- `spawn_daemon` detaches it -- so
    a Ctrl+C in the terminal reaches the client and nothing else.  That is
    what makes this safe to do to a daemon holding other files' sessions: the
    signal stays on the client's side of the socket, and the daemon's side is
    driven entirely by the EOF.
    """

    def setUp(self):
        self.ws = Workspace()
        # LIFO: the daemon must be stopped before its socket is deleted.
        self.addCleanup(self.ws.cleanup)
        self.addCleanup(self.stop_daemon)
        self.path = self.ws.write("C.v", SPIN)

    def stop_daemon(self):
        subprocess.run([CLI, "stop", "--root", self.ws.dir],
                       capture_output=True, timeout=60)

    def status(self):
        out = subprocess.run([CLI, "status", "--root", self.ws.dir, "--json"],
                             capture_output=True, cwd=self.ws.dir, timeout=60)
        try:
            return json.loads(out.stdout)
        except ValueError:
            return {"sessions": []}       # no daemon yet

    def session_row(self):
        for row in self.status().get("sessions") or ():
            if row["path"] == os.path.abspath(self.path):
                return row
        return {}

    def test_ctrl_c_stops_the_check_and_leaves_the_session_warm(self):
        client = subprocess.Popen([CLI, "check", "C.v"], cwd=self.ws.dir,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.addCleanup(client.kill)
        # A couple of seconds into the check, which for this file is inside
        # the spinning tactic: everything before it -- the cold start, two
        # trivial sentences -- is milliseconds, and the tactic itself is tens
        # of seconds.  `sentences` is asserted below, so an interrupt that
        # landed too early fails loudly rather than passing vacuously.
        self.assertTrue(
            wait_for(lambda: (self.session_row().get("busy_for") or 0) > 2.0,
                     timeout=180),
            "the check never got going")
        pid = self.session_row()["pid"]

        client.send_signal(signal.SIGINT)
        rc = client.wait(timeout=120)
        _out, err = client.communicate()

        self.assertEqual(rc, 130, err)
        self.assertIn(b"interrupted", err)

        # The daemon gives the slot back once it has parked the session, which
        # is the moment the file becomes checkable again.
        def parked_again():
            row = self.session_row()
            return bool(row) and row.get("busy_for") is None

        self.assertTrue(wait_for(parked_again, timeout=180),
                        "the file stayed checked out after its client had gone")
        row = self.session_row()
        self.assertEqual(row["pid"], pid, "the session was killed, not parked")
        self.assertTrue(alive(pid), "the rocq repl did not survive the Ctrl+C")
        self.assertFalse(row["complete"],
                         "the check ran to the end -- SPIN is too fast here")
        self.assertTrue(row["sentences"], "it kept none of what it executed")

        # And the payoff: the next check is answered at once, from there.
        self.ws.write("C.v", FIXED)
        again = subprocess.run([CLI, "check", "C.v"], cwd=self.ws.dir,
                               capture_output=True, timeout=300)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(b"replay", again.stderr,
                      "the interrupted session was not reused")


if __name__ == "__main__":
    unittest.main()
