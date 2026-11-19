"""Files of a cluster for the Scripts tab: finding them, #SBATCH options, columns."""
import os, re, shlex

from .config import FILE_TYPES, classify, type_exts
from .slurm import SBATCH_NOVAL_LONG, SBATCH_NOVAL_SHORT, _sections, split_submit_line


SBATCH_SHORT = {"p": "partition", "t": "time", "J": "job-name", "N": "nodes", "n": "ntasks", "c": "cpus-per-task",
                "G": "gpus", "a": "array", "A": "account", "q": "qos", "w": "nodelist", "o": "output", "e": "error"}


def sbatch_all(text):
    """#SBATCH lines of a script -> {long option: value} (short options translated)."""
    toks = []
    for l in text.splitlines():
        if l.startswith("#SBATCH"):
            try:
                toks += shlex.split(l[7:], comments=True)
            except ValueError:
                toks += l[7:].split("#")[0].split()
    opts, i = {}, 0
    while i < len(toks):
        t = toks[i]; i += 1
        if t.startswith("--"):
            k, eq, v = t[2:].partition("=")
            if not eq and k not in SBATCH_NOVAL_LONG and i < len(toks) and not toks[i].startswith("-"):
                v = toks[i]; i += 1
        elif t.startswith("-") and len(t) > 1:
            k, v = t[1], t[2:]
            if not v and k not in SBATCH_NOVAL_SHORT and i < len(toks):
                v = toks[i]; i += 1
            k = SBATCH_SHORT.get(k, k)
        else:
            continue
        opts[k] = v
    return opts


def script_resources(o):
    """{option: value} -> "2 nodes · 4 tasks · 8 cpus · gpu:a100:1" """
    parts = []
    for k, unit in (("nodes", "nodes"), ("ntasks", "tasks"), ("ntasks-per-node", "tasks/node"),
                    ("cpus-per-task", "cpus/task"), ("mem", "mem"), ("mem-per-cpu", "mem/cpu")):
        if o.get(k):
            parts.append(f"{o[k]} {unit}" if not k.startswith("mem") else f"{unit} {o[k]}")
    g = o.get("gres") or (f"gpu:{o['gpus']}" if o.get("gpus") else "") or \
        (f"gpu:{o['gpus-per-node']}/node" if o.get("gpus-per-node") else "")
    if g:
        parts.append(g)
    if o.get("array"):
        parts.append(f"array {o['array']}")
    return "  ·  ".join(parts)


def sbatch_minutes(t):
    """Slurm time limit ("60", "1:30:00", "2-12", "1-00:00:00") -> minutes, or None."""
    if not t:
        return None
    try:
        d, _, rest = t.rpartition("-")
        p = [int(x) for x in rest.split(":")]
        if d:                                   # days-hours[:minutes[:seconds]]
            p += [0] * (3 - len(p))
            return int(d) * 1440 + p[0] * 60 + p[1] + p[2] / 60
        if len(p) == 1:                         # minutes
            return p[0]
        if len(p) == 2:                         # minutes:seconds
            return p[0] + p[1] / 60
        return p[0] * 60 + p[1] + p[2] / 60
    except ValueError:
        return None


def sbatch_mem_mb(m):
    """"50G" / "16000" / "2T" -> MB, or None."""
    g = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", m or "", re.I)
    if not g:
        return None
    return float(g.group(1)) * {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024 ** 2}[g.group(2).upper()]


def sbatch_columns(o):
    """#SBATCH options -> ({column: text}, {column: sort key}) for the CPUs / Mem / GPUs / Array columns."""
    def num(k, dflt=None):
        try:
            return int(o.get(k, "").split("(")[0])
        except ValueError:
            return dflt
    nodes, tasks, cpt = num("nodes", 1), num("ntasks"), num("cpus-per-task")
    tpn = num("ntasks-per-node")
    ntask = tasks or ((tpn or 1) * (nodes or 1) if tpn else 1)
    cpus = ntask * (cpt or 1) if (tasks or tpn or cpt) else None
    txt = {"CPUs": (f"{cpus}" if cpus else "") + (f"  ({nodes} nodes)" if nodes and nodes > 1 else "")}
    mem = o.get("mem") or (o.get("mem-per-cpu") and f"{o['mem-per-cpu']}/cpu") or ""
    txt["Mem"] = mem
    mem_mb = sbatch_mem_mb(o.get("mem")) or ((sbatch_mem_mb(o.get("mem-per-cpu")) or 0) * (cpus or 1) or None)
    g, n, kind = o.get("gres", ""), None, ""
    m = re.search(r"gpu(?::([A-Za-z][\w.-]*))?(?::(\d+))?", g)
    if m:
        kind, n = m.group(1) or "", int(m.group(2) or 1)
    elif o.get("gpus") or o.get("gpus-per-node"):
        v = o.get("gpus") or o.get("gpus-per-node")
        kind, _, c = v.rpartition(":")
        n = int(c) if c.isdigit() else None
    txt["GPUs"] = (f"{n}" + (f" {kind}" if kind else "") + ("/node" if o.get("gpus-per-node") and not m else "")) \
        if n else ""
    txt["Array"] = o.get("array", "")
    key = {"CPUs": cpus, "Mem": mem_mb, "GPUs": (n or 0) * (nodes or 1) if n else None}
    return txt, key


def natural_key(s):
    """"run_10" after "run_9"."""
    return [(0, int(x), "") if x.isdigit() else (1, 0, x.lower()) for x in re.split(r"(\d+)", s or "") if x]


def remote_dir(d):
    """A folder typed by the user -> shell word ("~/x" is expanded on the cluster)."""
    d = d.strip()
    if d == "~":
        return '"$HOME"'
    if d.startswith("~/"):
        return '"$HOME"/' + shlex.quote(d[2:].strip("/") or ".")
    return shlex.quote(d)


def job_dirs_script(since):
    """Work folders and submitted scripts of your recent jobs (to find your batch scripts without configuring)."""
    return (f"sacct -X -n -P -S {shlex.quote(since)} -o WorkDir,SubmitLine 2>/dev/null || "
            f"sacct -X -n -P -S {shlex.quote(since)} -o WorkDir 2>/dev/null; true")


def parse_job_dirs(out, limit=40):
    """sacct WorkDir|SubmitLine lines -> folders (most recent last in sacct -> first here)."""
    dirs = []
    for l in reversed(out.splitlines()):
        wd, _, sub = l.partition("|")
        wd = wd.strip()
        if not wd.startswith("/"):
            continue
        cand = [wd]
        if sub:
            opts, scr = split_submit_line(sub)
            base = opts.get("chdir", wd) if opts.get("chdir", "").startswith("/") else wd
            if scr:
                p = os.path.normpath(scr if scr.startswith("/") else os.path.join(base, scr))
                cand.insert(0, os.path.dirname(p))
        for d in cand:
            if d not in dirs:
                dirs.append(d)
        if len(dirs) >= limit:
            break
    return dirs[:limit]


def scan_script(dirs):
    """dirs: [(folder as typed, depth)] -> remote script listing candidate files (mtime, path) and their #SBATCH
    lines.  Hidden folders are skipped; files over 1 MB too."""
    exts = list(dict.fromkeys(e for t in FILE_TYPES for e in type_exts(t))) or [".slurm"]
    names = " -o ".join(f"-iname '*{e}'" for e in exts)
    s = ""
    for d, depth in dirs:
        find = (f"find . -maxdepth {int(depth)} -path '*/.*' -prune -o -type f -size -1024k \\( {names} \\)")
        s += (f"echo @@DIR; ( cd {remote_dir(d)} 2>/dev/null && pwd && "
              f"{find} -printf 'F %T@ %P\\n' 2>/dev/null | head -1000 && "
              f"{find} -print0 2>/dev/null | xargs -0 -r grep -H -Z -m 40 '^#SBATCH' 2>/dev/null "
              f"| head -20000 | sed 's/^/H /' ); ")
    return s + "true"


def parse_scan(out):
    """-> [dict(path, dir, rel, mtime, opts)] (each file once, in the order of the folders)."""
    res, seen = [], set()
    for tag, _, lines in _sections(out):
        if tag != "DIR" or not lines or not lines[0].startswith("/"):
            continue
        root = lines[0].rstrip("/") or "/"
        heads, files = {}, []
        for l in lines[1:]:
            if l.startswith("F "):
                t, _, rel = l[2:].partition(" ")
                try:
                    files.append((rel, float(t)))
                except ValueError:
                    pass
            elif l.startswith("H ") and "\0" in l:
                f, _, h = l[2:].partition("\0")
                heads.setdefault(f[2:] if f.startswith("./") else f, []).append(h)
        for rel, t in files:
            path = root.rstrip("/") + "/" + rel
            if path in seen or "\n" in path:
                continue
            h = heads.get(rel)
            cat = classify(rel, bool(h))
            if not cat:
                continue
            seen.add(path)
            res.append(dict(path=path, dir=root, rel=rel, mtime=t, opts=sbatch_all("\n".join(h or [])),
                            cat=cat["name"], run=cat["run"]))
    return res
