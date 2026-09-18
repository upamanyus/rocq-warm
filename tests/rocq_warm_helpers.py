"""Shared scaffolding for the rocq-warm tests.

Every test here runs a real Rocq.  There is no mock: the whole point of the
tool is that its answers match `coqc`'s, and a mock would only prove that the
code agrees with our beliefs about Rocq rather than with Rocq.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

HAVE_ROCQ = shutil.which("rocq") is not None and shutil.which("coqc") is not None
requires_rocq = unittest.skipUnless(
    HAVE_ROCQ, "needs rocq and coqc on PATH (eval $(opam env))")

# Most of the suite compares against `coqc` and needs it.  The session-table
# tests do not: they run `rocq repl` and assert on what the daemon owns, with
# no oracle to consult.  Gating them on `coqc` as well would skip them on a
# switch that ships only the 9.x `rocq` binary.
HAVE_ROCQ_REPL = shutil.which("rocq") is not None
requires_rocq_repl = unittest.skipUnless(
    HAVE_ROCQ_REPL, "needs rocq on PATH (eval $(opam env))")


class Workspace:
    """A throwaway one-file Rocq project."""

    def __init__(self, logical="T"):
        self.dir = tempfile.mkdtemp(prefix="rocq-warm-test-")
        self.logical = logical
        with open(os.path.join(self.dir, "_CoqProject"), "w") as f:
            f.write("-R . %s\n" % logical)

    def write(self, name, text):
        if isinstance(text, str):
            text = text.encode()
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(text)
        return path

    @property
    def flags(self):
        return ["-R", ".", self.logical]

    def build(self, *names, **kw):
        """Compile these files for real, in order, the way `make` would."""
        for name in names:
            subprocess.run(["coqc", "-q"] + self.flags + [name], cwd=self.dir,
                           check=True, capture_output=True,
                           timeout=kw.get("timeout", 300))

    def path(self, name):
        return os.path.join(self.dir, name)

    def touch(self, name, text=None):
        """Make `name` newer than anything written so far.

        A plain `touch` can land in the same clock tick as the previous write
        on a fast machine, and equal mtimes are up to date to `make` and to
        us alike.  Bump it explicitly instead.
        """
        path = self.path(name)
        if text is not None:
            self.write(name, text)
        latest = max((os.stat(os.path.join(self.dir, f)).st_mtime_ns
                      for f in os.listdir(self.dir)), default=0)
        now = max(time.time_ns(), latest + 1_000_000)
        os.utime(path, ns=(now, now))
        return path

    def coqc(self, name, timeout=300):
        """(returncode, normalized diagnostics) from a real cold compile."""
        proc = subprocess.run(["coqc", "-q"] + self.flags + [name],
                              cwd=self.dir, capture_output=True, timeout=timeout)
        out = (proc.stdout + proc.stderr).decode("utf8", "replace")
        return proc.returncode, normalize(out, self.dir)

    def coqc_sentences(self, name, timeout=300):
        """[(start, end)] for every sentence, straight from Rocq's own parser.

        `-time` reports the byte range of each sentence it executes, which makes
        `coqc` the oracle for the sentence map rather than anything we wrote.
        """
        proc = subprocess.run(["coqc", "-q", "-time"] + self.flags + [name],
                              cwd=self.dir, capture_output=True, timeout=timeout)
        out = (proc.stdout + proc.stderr).decode("utf8", "replace")
        return [(int(a), int(b))
                for a, b in re.findall(r'^Chars (\d+) - (\d+) \[', out, re.M)]

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def alive(pid):
    """Is `pid` a live process -- as opposed to gone, or an unreaped zombie?

    A killed child whose parent has not waited on it still has a `/proc`
    entry, so `os.kill(pid, 0)` calls it alive; every caller here is waiting
    for a session to be *gone*, and a zombie is gone enough.
    """
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def cpu_seconds(pid):
    """utime+stime, in seconds, or None if `pid` is gone.

    What a test means by "Rocq is inside the tactic" and cannot say with a
    clock: a loaded machine takes longer in wall-clock time to reach the same
    point, but the work costs the same CPU either way.  `Session._cpu_ticks`
    reads the same two fields for the same reason.
    """
    try:
        with open("/proc/%d/stat" % pid) as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None


def wait_for(pred, timeout=60, step=0.1):
    """Poll `pred` until it holds; returns whether it did in time."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


_WS = re.compile(r'[ \t]+')


def normalize(text, root=None):
    """Compare diagnostics on their content, not their incidental layout.

    Absolute paths become basenames and runs of spaces collapse, so a test
    failure means Rocq and rocq-warm actually disagree.
    """
    out = []
    for line in text.splitlines():
        line = re.sub(r'File "[^"]*/([^"/]+)"', r'File "\1"', line)
        line = re.sub(r'File "([^"/]+)"', r'File "\1"', line)
        line = _WS.sub(" ", line).rstrip()
        if line:
            out.append(line)
    return "\n".join(out)


def render_all(result, display, text):
    """rocq-warm's DIAGNOSTICS, normalized the same way `coqc`'s are.

    Errors and warnings only.  A check also reports what the sentences it
    executed printed, and that is deliberately not `coqc`'s output: a REPL
    prints "foo is defined" for a definition where a batch compile says
    nothing, and a warm run does not re-print what its reused prefix printed.
    The property these tests exist for is the verdict and the diagnostics;
    the output has its own tests in `test_rocq_warm_daemon.py`.
    """
    return normalize("\n".join(d.render(display, text) for d in result.diags
                               if d.kind != "info"))
