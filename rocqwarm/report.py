"""What a check prints, and what it exits with.

On the daemon's side of the socket, where the evidence is: the bytes checked,
the workspace root, and the compile job.  The client writes these two strings
and exits with this code.

The exit code is decided here, once, because callers act on the difference
between 1 -- the proof is wrong -- and 2, which is no verdict about the proof
at all.  `client.CHECK_EPILOG` spells the codes out, and is what `check
--help` prints.
"""

import os

from . import diag


def relative(text, root):
    """Paths in a message as the user would type them, not absolute."""
    return text.replace(root + os.sep, "")


def check(resp, path, root, req):
    """(stdout, stderr, exit code) for a check, however it turned out."""
    display = os.path.relpath(path, root)
    if resp.get("ok"):
        return _verdict(resp, display, root, req)
    return _refusal(resp, display, root)


def _verdict(resp, display, root, req):
    """A check that happened.  0 if the proof compiles, 1 if it does not."""
    out = "".join(diag.render_at(display, d["at"], d["message"]) + "\n"
                  for d in resp["diags"])
    log = ""
    for row in resp.get("stale") or ():
        log += ("rocq-warm: warning: checking against a stale dependency: "
                "%s\n" % relative(row["why"], root))
    if resp.get("note"):
        log += "rocq-warm: warning: %s\n" % resp["note"]
    vo = resp.get("vo")
    tail = "; wrote %s.vo" % display[:-2] if vo and vo["state"] == "ok" else ""
    log += ("rocq-warm: %s %s [%s, %d/%d sentences, %.1fs, %.1f GB]%s\n"
            % (display, "OK" if resp["passed"] else "FAILED", resp["mode"],
               resp["replayed"], resp["sentences"], resp["seconds"],
               resp["rss"] / 1e9, tail))
    if resp["passed"] and not req.get("wait_vo") and resp.get("vo_stale"):
        # The thing that bites: the file is green, and everything that
        # requires it is still reading the .vo from before the edit.
        log += ("rocq-warm: warning: %s.vo was NOT regenerated (%s); anything "
                "that requires it is refused until it is rebuilt -- run make, "
                "or `rocq-warm check %s --compile`\n"
                % (display[:-2], relative(resp["vo_stale"], root), display))
    if not resp["passed"]:
        return out, log, 1
    if not req.get("wait_vo") or (vo is not None and vo["state"] == "ok"):
        return out, log, 0
    out += vo["output"] if vo else ""
    if vo is not None and vo["state"] == "failed":
        # Green here and rejected by a real compile: the one disagreement
        # that must never happen.
        log += ("rocq-warm: rocq compile DISAGREED (exit %s) -- this is a bug "
                "in rocq-warm, please report it\n" % vo["rc"])
        return out, log, 3
    log += ("rocq-warm: the .vo was not written (%s)\n"
            % (vo["state"] if vo else "no compile was run"))
    return out, log, 2


def _refusal(resp, display, root):
    """A check that did not happen, and why.  Exit 2, never 1: none of these
    is a verdict about the proof."""
    busy = resp.get("busy")
    if busy:
        secs = busy.get("seconds")
        return "", (
            "rocq-warm: %s NOT CHECKED -- it is already being checked%s\n"
            "rocq-warm: one check per file at a time -- retry when it "
            "finishes, or check a different file\n"
            % (display, "" if secs is None else " (%.0fs so far)" % secs)), 2

    stale = resp.get("stale")
    if stale:
        one = len(stale) == 1
        it = "it" if one else "them"
        log = ("rocq-warm: %s NOT CHECKED -- %d dependenc%s stale (make would "
               "rebuild %s):\n"
               % (display, len(stale), "y is" if one else "ies are", it))
        out = ""
        for row in stale:
            log += "  %s\n" % relative(row["why"], root)
            if row.get("compile_output"):
                out += row["compile_output"]
        log += ("rocq-warm: rebuild %s first, or pass --rebuild to have "
                "rocq-warm compile %s, or --allow-stale to check against %s "
                "anyway\n" % (it, it, it))
        return out, log, 2

    out, log = "", ""
    for job in resp.get("compile_failed") or ():
        out += job["output"]
        log += ("rocq-warm: rebuilding %s FAILED (%s)\n"
                % (relative(job["path"], root), job["why"]))
    log += "rocq-warm: %s\n" % resp.get("error", "no response")
    return out, log, 2


def status(resp):
    """`rocq-warm status`, as the client should print it."""
    avail = resp.get("available")
    out = ("daemon pid %d, up %.0fs, budget %.1f GB%s\n"
           % (resp["pid"], resp["uptime"], resp["budget"] / 1e9,
              "" if avail is None else
              "; machine has %.1f GB free, yields below %.1f"
              % (avail / 1e9, resp.get("min_free", 0) / 1e9)))
    for s in resp["sessions"]:
        # "idle 4s" would read as the opposite of the truth for a session
        # being checked, so a busy one says so instead.
        when = ("busy %4.0fs" % s["busy_for"] if s["busy_for"] is not None
                else "idle %4.0fs" % s["idle"])
        out += ("  %-60s %s %4d sentences  %5.1f GB  %s  %4d .vo watched  "
                "pid %s\n"
                % (s["path"], "complete" if s["complete"] else "  parked",
                   s["sentences"], s["rss"] / 1e9, when, s["watched"],
                   s["pid"]))
    for j in resp.get("compiles") or ():
        out += ("  compile %-52s %-9s %5.0fs%s\n"
                % (j["path"], j["state"], j["seconds"],
                   "  " + j["why"] if j.get("why") else ""))
    return out
