"""`rocq-warm` -- the CLI in front of the warm-session daemon.

Deliberately thin.  It does four things the daemon cannot do for itself, and
then gets out of the way:

* find the workspace the daemon for this file lives in;
* resolve which `rocq` this shell means, and the environment that resolved it
  -- the whole point, since a daemon outlives the shell that started it and
  the next caller may be in another opam switch;
* start the daemon if nobody is serving that tree yet;
* write what comes back to stdout and stderr, and exit with the code it says.

Everything about how a check READS is the daemon's: diagnostics in `coqc`'s
exact format so `grep Error` keeps working, the warnings, the verdict line,
and which of the exit codes it is.  That side has the bytes it checked, the
workspace root and the compile job, and deciding any of it in both places is
how the two copies drift.

Exit codes: 0 the file checks, 1 it does not, 2 it could not be checked at
all -- a dependency whose `.vo` is older than its source, the file is already
being checked, no daemon, no rocq -- and 3 for the one thing that must never
happen, a green verdict that a real `rocq compile` then rejects.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time

from . import project, server


# The environment variables that decide WHICH Rocq runs and where it looks for
# libraries.  The daemon spawns its sessions with exactly these, taken from the
# client, so a warm session behaves like the shell you invoked from -- not like
# the shell that happened to start the daemon an hour ago.
ROCQ_ENV = ("PATH", "OCAMLPATH", "CAML_LD_LIBRARY_PATH", "OCAMLLIB",
            "COQPATH", "ROCQPATH", "COQLIB", "ROCQLIB", "COQCORELIB")


def rocq_environment():
    """(absolute rocq, the env that resolved it) for this invocation."""
    return (shutil.which("rocq"),
            {k: os.environ[k] for k in ROCQ_ENV if k in os.environ})


def workspace_for(path):
    """Where the daemon for `path` lives.

    The git checkout, when there is one, so that `status` and `stop` find the
    same daemon from anywhere in the tree -- a project can have several
    `_CoqProject` files and one daemon serves them all.
    """
    start = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    try:
        top = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                             capture_output=True, timeout=30)
        if top.returncode == 0:
            return top.stdout.decode().strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    proj = project.find_project(path)
    if proj:
        return os.path.dirname(proj)
    return start


def connect(root, spawn=True, timeout=30.0):
    sock_path = os.path.join(root, ".rocq-warm", "sock")
    deadline = time.time() + timeout
    spawned = False
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(sock_path)
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            s.close()
            if not spawn:
                return None
            if not spawned:
                spawn_daemon(root)
                spawned = True
            if time.time() > deadline:
                raise SystemExit("rocq-warm: daemon did not come up at %s" % sock_path)
            time.sleep(0.1)


def spawn_daemon(root):
    """Start the daemon, detached, with an explicit path to our own package.

    Not by cwd: `python -m` finding the package because of where it happens to
    be run from is exactly the kind of thing that breaks when someone moves the
    checkout or symlinks the entry point.
    """
    os.makedirs(os.path.join(root, ".rocq-warm"), exist_ok=True)
    package_parent = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = (package_parent + os.pathsep + env["PYTHONPATH"]
                         if env.get("PYTHONPATH") else package_parent)
    with open(os.path.join(root, ".rocq-warm", "log"), "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "rocqwarm.server", root],
            cwd=package_parent, env=env,
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True)


def request(root, msg, spawn=True):
    sock = connect(root, spawn=spawn)
    if sock is None:
        return None
    try:
        server.send_msg(sock, msg)
        return server.recv_msg(sock)
    finally:
        sock.close()


def cmd_check(args):
    """Ask the daemon, print what it says, exit with the code it gives.

    Everything about how a check READS -- the diagnostics in `coqc`'s shape,
    the warnings, the verdict line, and which of 0/1/2/3 it is -- is decided
    by the daemon, which is the side that has the text it checked, the
    workspace root and the compile job.  Deciding any of it twice is how the
    two copies drift.
    """
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        raise SystemExit("rocq-warm: no such file: %s" % path)
    rocq, env = rocq_environment()
    resp = request(workspace_for(path),
                   {"cmd": "check", "path": path, "cold": args.cold,
                    "timeout": args.timeout,
                    "rocq": rocq, "env": env,
                    "allow_stale": args.allow_stale,
                    "rebuild": args.rebuild,
                    "wait_vo": args.compile})
    if resp is None:
        sys.stderr.write("rocq-warm: no response\n")
        return 2
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        sys.stdout.write(resp.get("out", ""))
    # The fallbacks are for a daemon that failed before it could render --
    # an unhandled exception in `handle`, which answers with an error and
    # nothing else.
    sys.stderr.write(resp.get("log")
                     or "rocq-warm: %s\n" % resp.get("error", "no response"))
    return resp.get("exit", 2)


def cmd_status(args):
    root = os.path.abspath(args.root or workspace_for(os.getcwd()))
    # The one line the daemon cannot render, because there isn't one.
    resp = request(root, {"cmd": "status"}, spawn=False) or {
        "ok": False, "error": "no daemon running", "root": root,
        "out": "rocq-warm: no daemon running for %s\n" % root}
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        sys.stdout.write(resp.get("out", ""))
    return 0


def cmd_stop(args):
    root = os.path.abspath(args.root or workspace_for(os.getcwd()))
    resp = request(root, {"cmd": "stop"}, spawn=False)
    print("rocq-warm: %s" % ("stopped" if resp else "no daemon running"))
    return 0


def main(argv=None):
    if shutil.which("rocq") is None:
        raise SystemExit("rocq-warm: no `rocq` on PATH -- "
                         "eval $(opam env)")
    ap = argparse.ArgumentParser(prog="rocq-warm")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="check a .v file, reusing a warm session")
    c.add_argument("file")
    c.add_argument("--cold", action="store_true",
                   help="discard any warm session first")
    c.add_argument("--timeout", type=float, default=1800.0)
    c.add_argument("--json", action="store_true")
    c.add_argument("--compile", action="store_true",
                   help="on success, also run a real rocq compile (writes the "
                        ".vo; exit 3 if it disagrees)")
    c.add_argument("--rebuild", action="store_true",
                   help="compile stale dependencies (and what depends on "
                        "them) before checking, instead of refusing")
    c.add_argument("--allow-stale", action="store_true",
                   help="check even if a dependency's .vo is older than its "
                        "source (the verdict is then about the OLD library)")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("status", help="what the daemon is holding")
    s.add_argument("--root")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    k = sub.add_parser("stop", help="stop the daemon and free its sessions")
    k.add_argument("--root")
    k.set_defaults(func=cmd_stop)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
