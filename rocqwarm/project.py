"""A .v file's build flags, its dependency graph, and what is stale.

The nearest `_CoqProject` gives the flags, so the session gets the load path
the build uses.  The `.vo` graph is kept current, so a `Require` added
mid-session does not leave a dependency unwatched.  `make`'s rule decides
what is stale: a `.vo` older than its `.v`, or than a `.vo` it requires, no
longer matches its source, and a proof checked against it gets verdicts about
a program that does not exist.
"""

import os
import subprocess
import threading


PROJECT_NAMES = ("_CoqProject", "_RocqProject")


def find_project(start):
    """Nearest _CoqProject at or above `start`'s directory."""
    d = os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start))
    while True:
        for name in PROJECT_NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def parse_project(path):
    """Flags from a _CoqProject, in the order `rocq` wants them.

    Only load-path and `-arg` directives; the listed `.v` files are the
    build's business.
    """
    flags = []
    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            i = 0
            while i < len(parts):
                tok = parts[i]
                if tok in ("-R", "-Q") and i + 2 < len(parts):
                    flags += [tok, parts[i + 1], parts[i + 2]]
                    i += 3
                elif tok in ("-I",) and i + 1 < len(parts):
                    flags += [tok, parts[i + 1]]
                    i += 2
                elif tok == "-arg" and i + 1 < len(parts):
                    flags.append(parts[i + 1])
                    i += 2
                elif tok.startswith("-"):
                    flags.append(tok)
                    i += 1
                else:
                    i += 1              # a source file, not a flag
    return flags


def flags_for(path, project=None):
    """(flags, cwd) for one .v file."""
    project = project or find_project(path)
    if project is None:
        return [], os.path.dirname(os.path.abspath(path))
    return parse_project(project), os.path.dirname(os.path.abspath(project))


def load_roots(flags, cwd):
    """The physical directories the -R/-Q flags bind, absolute."""
    roots = []
    i = 0
    while i < len(flags):
        if flags[i] in ("-R", "-Q") and i + 2 < len(flags):
            roots.append(os.path.normpath(os.path.join(cwd, flags[i + 1])))
            i += 3
        else:
            i += 1
    return roots


def project_sources(project_path, cwd, flags=None):
    """The .v files a _CoqProject lists, plus every .v under its -R/-Q roots.

    Both, not either: `-R ../model Riscv` makes another tree loadable, and
    unwalked its files appear in the graph as edges but never as nodes, so
    nothing beneath them is watched.  Listed files first, then the rest
    sorted, all relative to `cwd`.
    """
    files = []
    seen = set()
    if project_path:
        with open(project_path) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                for tok in line.split():
                    if tok.endswith(".v"):
                        key = os.path.normpath(os.path.join(cwd, tok))
                        if key not in seen:
                            seen.add(key)
                            files.append(tok)
    roots = load_roots(flags, cwd) if flags is not None else []
    if not files and not roots:
        roots = [cwd]
    walked = []
    for root in roots:
        for d, dirs, names in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for n in names:
                if n.endswith(".v"):
                    key = os.path.normpath(os.path.join(d, n))
                    if key not in seen:
                        seen.add(key)
                        walked.append(os.path.relpath(key, cwd))
    return files + sorted(walked)


def _run_dep(flags, cwd, sources, timeout, rocq, env):
    """{target.vo: [dep.vo]} from one `rocq dep` run, or None if it could not
    be run.

    `rocq dep` prints NOTHING AT ALL if any file it is handed is missing, so
    callers filter first: a `_CoqProject` routinely lists generated files.
    """
    if not sources:
        return {}
    try:
        out = subprocess.run([rocq, "dep", "-noglob"] + flags + list(sources),
                             cwd=cwd, capture_output=True, timeout=timeout,
                             env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    graph = {}
    for line in out.stdout.decode("utf8", "replace").splitlines():
        if ":" not in line:
            continue
        lhs, rhs = line.split(":", 1)
        targets = [t for t in lhs.split() if t.endswith(".vo")]
        deps = [os.path.normpath(os.path.join(cwd, t))
                for t in rhs.split() if t.endswith(".vo")]
        for t in targets:
            graph[os.path.normpath(os.path.join(cwd, t))] = deps
    if not graph and out.returncode != 0:
        return None
    return graph


def vo_of(v_path):
    return v_path[:-2] + ".vo"


def v_of(vo_path):
    return vo_path[:-3] + ".v"


class DepGraph:
    """A project's `.vo` dependency graph, kept current between checks.

    A full `rocq dep` over a large tree costs a second or two, too slow per
    edit, so refreshes are incremental: every source is stat'ed and only those
    whose mtime moved are re-run.  The file being checked is always among
    them, so editing its `Require` lines moves the closure.
    """

    def __init__(self, flags, cwd, project_path=None, rocq="rocq", env=None,
                 timeout=300):
        self.flags = list(flags)
        self.cwd = cwd
        self.project_path = project_path
        self.rocq, self.env, self.timeout = rocq, env, timeout
        self.graph = {}         # vo -> [vo]
        self.stamps = {}        # abs .v -> mtime_ns
        self.refreshes = 0      # how many `rocq dep` runs, for the tests
        # One DepGraph is shared by every session in a project, and clients
        # can check different files in it at the same instant, so `refresh`'s
        # mutation and `closure`'s reads are locked.
        self._lock = threading.Lock()

    def refresh(self, extra=()):
        """Bring the graph up to date; returns how many files were re-scanned.

        `extra` names .v files no root or listing covers.  The file being
        checked is always one, so a file outside the project still gets its
        dependencies looked up.
        """
        with self._lock:
            return self._refresh_locked(extra)

    def _refresh_locked(self, extra=()):
        rel = {}
        for s in project_sources(self.project_path, self.cwd, self.flags):
            rel[os.path.normpath(os.path.join(self.cwd, s))] = s
        for e in extra:
            e = os.path.abspath(e)
            if e not in rel:
                rel[e] = os.path.relpath(e, self.cwd)
        changed = []
        for v_abs, r in rel.items():
            try:
                st = os.stat(v_abs).st_mtime_ns
            except OSError:
                st = None
            if v_abs not in self.stamps or self.stamps[v_abs] != st:
                changed.append((v_abs, r, st))
        for v_abs in [v for v in self.stamps if v not in rel]:
            del self.stamps[v_abs]
            self.graph.pop(vo_of(v_abs), None)
        present = [(v, r, st) for v, r, st in changed if st is not None]
        for v_abs, _r, _st in changed:
            if _st is None:
                self.stamps[v_abs] = None
                self.graph.pop(vo_of(v_abs), None)
        if present:
            patch = _run_dep(self.flags, self.cwd, [r for _v, r, _s in present],
                             self.timeout, self.rocq, self.env)
            self.refreshes += 1
            if patch is None:
                return 0                # could not run it; try again next time
            for v_abs, _r, st in present:
                self.stamps[v_abs] = st
                vo = vo_of(v_abs)
                if vo in patch:
                    self.graph[vo] = patch[vo]
                else:
                    self.graph.pop(vo, None)
        return len(present)

    def closure(self, path):
        """Every .vo `path` transitively loads, absolute and sorted."""
        seed = vo_of(os.path.abspath(path))
        with self._lock:
            direct = self.graph.get(seed)
            if direct is not None:
                return self._walk_locked(direct)
        # Not in the graph, so a file outside the project.  `rocq dep` is a
        # subprocess and runs without the lock; the walk retakes it.
        direct = _direct_deps(path, self.flags, self.cwd,
                              rocq=self.rocq, env=self.env) or []
        with self._lock:
            return self._walk_locked(direct)

    def _walk_locked(self, direct):
        """`direct` closed over the graph.  Caller holds the lock, which every
        read of self.graph is under."""
        seen, queue = set(), list(direct)
        while queue:
            d = queue.pop()
            if d in seen:
                continue
            seen.add(d)
            queue.extend(self.graph.get(d, ()))
        return sorted(seen)


def _direct_deps(path, flags, cwd, timeout=120, rocq="rocq", env=None):
    rel = os.path.relpath(os.path.abspath(path), cwd)
    try:
        out = subprocess.run([rocq, "dep", "-noglob"] + flags + [rel],
                             cwd=cwd, capture_output=True, timeout=timeout,
                             env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    deps = []
    for line in out.stdout.decode("utf8", "replace").splitlines():
        if ":" in line:
            deps += [os.path.normpath(os.path.join(cwd, t))
                     for t in line.split(":", 1)[1].split() if t.endswith(".vo")]
    return sorted(set(deps))


def fingerprint(deps):
    """(path, mtime_ns, size) per dependency; missing files get None fields.

    Compared verbatim next check: any difference means a rebuild, so the
    session holds a stale library.
    """
    out = []
    for d in deps or ():
        try:
            st = os.stat(d)
            out.append((d, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((d, None, None))
    return out


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def staleness(vo, graph):
    """Why `make` would rebuild `vo`, or None if it would not.

    Make's rule verbatim: rebuilt when missing or older than a prerequisite,
    a `.vo`'s being its `.v` and the `.vo` files it requires.  Equal mtimes
    count as current, as for make.
    """
    vo_m = _mtime(vo)
    v = v_of(vo)
    v_m = _mtime(v)
    if vo_m is None:
        if v_m is None:
            return "%s does not exist (nor does %s)" % (vo, v)
        return "%s has not been built" % vo
    if v_m is not None and v_m > vo_m:
        return "%s is newer than %s" % (v, vo)
    for d in graph.get(vo, ()):
        d_m = _mtime(d)
        if d_m is not None and d_m > vo_m:
            return "%s is older than %s, which it requires" % (vo, d)
    return None


def stale_deps(closure, graph):
    """[(vo, why)] for every member of `closure` that make would rebuild."""
    out = []
    for vo in closure:
        why = staleness(vo, graph)
        if why:
            out.append((vo, why))
    return out


def rebuild_plan(closure, stale, graph):
    """The .vo files to rebuild, in dependency order.

    Everything stale, plus every closure member depending on something stale:
    rebuilding `Base.vo` leaves `Mid.vo` older than it.  Each entry is
    (vo, [vo it must wait for]).
    """
    members = set(closure)
    dependents = {}
    for vo in members:
        for d in graph.get(vo, ()):
            if d in members:
                dependents.setdefault(d, []).append(vo)
    todo = set(vo for vo, _why in stale)
    queue = list(todo)
    while queue:
        vo = queue.pop()
        for up in dependents.get(vo, ()):
            if up not in todo:
                todo.add(up)
                queue.append(up)
    order, done = [], set()

    def visit(vo, trail):
        if vo in done:
            return
        if vo in trail:
            return                              # a cycle; rocq will complain
        for d in graph.get(vo, ()):
            if d in todo:
                visit(d, trail | {vo})
        done.add(vo)
        order.append((vo, [d for d in graph.get(vo, ()) if d in todo]))

    for vo in sorted(todo):
        visit(vo, frozenset())
    return order
