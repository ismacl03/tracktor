"""Slurm over SSH: queries and their parsing (jobs, history, efficiency, log paths, cluster status)."""
import re, shlex, time

from .config import S
from .local import fmt_bytes, fmt_dt
from .ssh import ssh_run


def slurm_secs(s):
    """'1-02:03:04' / '02:03:04' / '03:04' -> seconds."""
    try:
        d, _, rest = s.rpartition("-")
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return (int(d) if d else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


def slurm_time(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None
SLURM_CODE = {"RUNNING": "R", "PENDING": "PD", "CONFIGURING": "CF", "COMPLETING": "CG", "SUSPENDED": "S",
              "REQUEUED": "RQ", "RESIZING": "RS", "STOPPED": "ST"}
SLURM_RUN = {"RUNNING", "PENDING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED", "RESIZING"}
SLURM_END = {"COMPLETED": "ok", "CANCELLED": "cancelled", "TIMEOUT": "timeout", "OUT_OF_MEMORY": "out of memory",
             "NODE_FAIL": "node failure", "PREEMPTED": "preempted", "BOOT_FAIL": "boot failure",
             "DEADLINE": "deadline"}


def slurm_end_text(state, exitcode):
    st = state.split()[0] if state else "?"
    if st == "FAILED":
        return f"error {exitcode.split(':')[0]}" if exitcode else "error"
    return SLURM_END.get(st, st.lower())


def gpu_script(only_mine):
    """For each job: on which node(s) and GPU number(s) it runs (scontrol show job -d), or the GPUs it asks for."""
    mine = "grep -F \"UserId=$u(\" | " if only_mine else ""
    return (f"scontrol show job -d -o 2>/dev/null | {mine}grep -oE '^JobId=[^ ]*|ArrayJobId=[^ ]*|ArrayTaskId=[^ ]*|"
            f"JobState=[^ ]*|Nodes=[^ ]+ CPU_IDs=[^ ]+ Mem=[^ ]+ GRES(_IDX)?=[^ ]*|TresPerNode=[^ ]*'; ")


def parse_gpus(lines):
    """Output of gpu_script -> {job id as squeue shows it: "g010 · GPU 4" / "wants 2 a100" / ""}."""
    recs, r = [], None
    for l in lines:
        k, _, v = l.strip().partition("=")
        if k == "JobId":
            r = dict(id=v, nodes=[]); recs.append(r)
        elif r is None:
            continue
        elif k in ("ArrayJobId", "ArrayTaskId", "JobState", "TresPerNode"):
            r[k] = v
        elif k == "Nodes":
            node = v.split()[0]
            g = re.search(r"GRES(?:_IDX)?=(\S*)", l)
            r["nodes"].append((node, g.group(1) if g else ""))
    out = {}
    for r in recs:
        a, t = r.get("ArrayJobId"), r.get("ArrayTaskId")
        jid = r["id"] if not (a and t) else (f"{a}_{t}" if t.isdigit() else f"{a}_[{t}]")
        if r.get("JobState") == "RUNNING" or r["nodes"]:
            parts = []
            for node, gres in r["nodes"]:
                for m in re.finditer(r"gpu(?::([^:(,]+))?(?::\d+)?\(IDX:([^)]*)\)", gres):
                    if m.group(2) not in ("", "N/A"):
                        parts.append(f"{node} · GPU {m.group(2)}")
            txt = ", ".join(dict.fromkeys(parts))
        else:
            m = re.search(r"gpu(?::([^:,]+))?:(\d+)", r.get("TresPerNode", ""))
            txt = f"wants {m.group(2)}" + (f" {m.group(1)}" if m.group(1) else "") if m else ""
        out[jid] = txt
        if r["id"] != jid:
            out.setdefault(r["id"], txt)
    return out


def poll_script(since):
    return (f"u=${{USER:-$(whoami)}}; echo '@@Q'; squeue -u \"$u\" -h -o '%i|%T|%M|%l|%D|%R|%P|%j' 2>&1; "
            f"echo '@@S'; squeue -u \"$u\" -h -t PD --start -o '%i|%S' 2>/dev/null; "
            + (f"echo '@@G'; {gpu_script(True)}" if S["show_gpus"] else "") +
            f"echo '@@A'; sacct -u \"$u\" -S {since} -X -n -P -o JobID,State,Start,End,Elapsed,ExitCode,Partition,JobName 2>&1")


def parse_poll(out):
    """-> (queued/running, finished, {id: estimated start time of queued jobs})."""
    sec, cur = {"@@Q": [], "@@S": [], "@@A": [], "@@G": []}, None
    for l in out.splitlines():
        if l.strip() in sec:
            cur = l.strip(); continue
        if cur:
            sec[cur].append(l)
    q, a = "\n".join(sec["@@Q"]), "\n".join(sec["@@A"])
    starts = {}
    for l in sec["@@S"]:
        jid, _, st = l.partition("|")
        t = slurm_time(st.strip())
        if t:
            starts[jid.strip()] = t
    running, hist, warn = [], [], []
    gpus = parse_gpus(sec["@@G"])
    for l in q.strip().splitlines():
        p = l.split("|", 7)
        if len(p) < 8:
            warn.append(l.strip()); continue
        jid, st, used, lim, nodes, reason, part, name = p
        running.append(dict(id=jid, state=st, used=used, limit=lim, nodes=nodes, reason=reason, part=part, name=name,
                            gpu=gpus.get(jid, "")))
    run_ids = {r["id"] for r in running}
    for l in a.strip().splitlines():
        p = l.split("|", 7)
        if len(p) < 8:
            if l.strip():
                warn.append(l.strip())
            continue
        jid, st, start, end, el, ex, part, name = p
        endt = slurm_time(end)
        if jid in run_ids or endt is None or st.split()[0] in SLURM_RUN:
            continue
        hist.append(dict(id=jid, state=st, start=slurm_time(start), end=endt, elapsed=slurm_secs(el), exit=ex,
                         part=part, name=name))
    if warn and not running and not hist and "not found" in " ".join(warn):
        raise RuntimeError("does not look like a Slurm cluster: " + warn[0])
    return running, hist, starts


def array_of(jid):
    """'123_4' / '123_[5-9]' -> '123';  not an array -> None."""
    return jid.split("_", 1)[0] if "_" in jid else None


def array_task_count(jid):
    """'123_4' -> 1;  '123_[0-9,12%4]' -> 11 (tasks represented by one squeue row)."""
    m = re.search(r"_\[([^\]]*)\]", jid)
    if not m:
        return 1
    n = 0
    for part in m.group(1).split("%")[0].split(","):
        a, _, b = part.partition("-")
        try:
            step = 1
            if ":" in b:
                b, stp = b.split(":"); step = int(stp)
            n += (int(b) - int(a)) // step + 1 if b else 1
        except ValueError:
            n += 1
    return n


def eff_script(jid):
    return f"sacct -j {shlex.quote(jid)} -n -P -o JobID,ReqMem,MaxRSS,TotalCPU,AllocCPUS,Elapsed,Timelimit 2>&1"


def parse_size(txt, default_unit=""):
    """'16G' / '4000M' / '123K' / '4000Mc' -> (bytes, 'c'|'n'|'')."""
    m = re.match(r"^([\d.]+)([KMGTP]?)([cn]?)$", txt.strip())
    if not m:
        return None, ""
    unit = m.group(2) or default_unit
    return float(m.group(1)) * {"": 1, "K": 2 ** 10, "M": 2 ** 20, "G": 2 ** 30, "T": 2 ** 40, "P": 2 ** 50}[unit], m.group(3)


def cpu_secs(txt):
    """sacct TotalCPU: '1-02:03:04' / '02:03:04' / '03:04.567' -> seconds."""
    try:
        d, _, rest = txt.strip().rpartition("-")
        parts = [float(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return (int(d) if d else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


def parse_eff(out):
    """seff-like summary: peak memory vs requested, CPU efficiency and time used vs the limit."""
    rows = [l.split("|") for l in out.strip().splitlines() if l.count("|") >= 6]
    if not rows:
        return ""
    alloc = next((r for r in rows if "." not in r[0]), rows[0])
    _, reqmem, _, totcpu, ncpu, elapsed, limit = alloc[:7]
    parts = []
    rss = [parse_size(r[2])[0] for r in rows if r[2].strip()]
    rss = max([x for x in rss if x is not None], default=None)
    req, per = parse_size(reqmem, "M")
    try:
        ncpu = int(ncpu)
    except ValueError:
        ncpu = 0
    if req and per == "c" and ncpu:
        req *= ncpu
    if rss:
        parts.append(f"peak memory {fmt_bytes(rss)}" + (f" of {fmt_bytes(req)} requested ({100 * rss / req:.0f} %)" if req else ""))
    el = slurm_secs(elapsed)
    tc = cpu_secs(totcpu)
    if not tc:
        tc = sum(cpu_secs(r[3]) or 0 for r in rows if "." in r[0]) or None
    if tc is not None and el and ncpu:
        parts.append(f"CPU {100 * tc / (el * ncpu):.0f} % of {ncpu} core(s)")
    lim = slurm_secs(limit) if limit and limit[0].isdigit() else None
    if el is not None:
        parts.append(f"time {fmt_dt(el)}" + (f" of {fmt_dt(lim)} ({100 * el / lim:.0f} %)" if lim else ""))
    return "Efficiency: " + "  ·  ".join(parts) if parts else ""


def expand_slurm_path(pat, jid, name, user, workdir):
    arr, _, task = jid.partition("_")

    def rep(m):
        k = m.group(2)
        v = {"j": jid, "x": name, "u": user, "A": arr, "a": task or "4294967294", "%": "%"}.get(k)
        if v is None:
            return m.group(0)
        return v.zfill(int(m.group(1))) if m.group(1) and k in "jAa" else v
    if not pat:
        pat = f"slurm-{arr}_{task}.out" if task else f"slurm-{jid}.out"
    p = re.sub(r"%(\d*)([jxuAa%])", rep, pat)
    return p if p.startswith("/") or not workdir else workdir.rstrip("/") + "/" + p


SBATCH_NOVAL_LONG = {"hold", "parsable", "exclusive", "requeue", "no-requeue", "test-only", "wait", "quiet",
                     "verbose", "spread-job", "use-min-nodes", "overcommit", "contiguous", "oversubscribe",
                     "ignore-pbs", "get-user-env", "no-kill", "help", "usage", "version", "reboot"}
SBATCH_NOVAL_SHORT = set("HhIkOQsvVW")
SBATCH_KEYS = {"o": "output", "output": "output", "e": "error", "error": "error",
               "D": "chdir", "chdir": "chdir", "workdir": "chdir", "J": "name", "job-name": "name"}


def parse_sbatch_args(toks, find_script=False):
    """sbatch options -> ({"output"/"error"/"chdir"/"name": value}, batch script or None)."""
    opts, i = {}, 0
    while i < len(toks):
        t = toks[i]; i += 1
        if t.startswith("--"):
            k, eq, v = t[2:].partition("=")
            if not eq and k not in SBATCH_NOVAL_LONG and i < len(toks):
                v = toks[i]; i += 1
            if k == "wrap":
                find_script = False
        elif t.startswith("-") and len(t) > 1:
            k, v = t[1], t[2:]
            if not v and k not in SBATCH_NOVAL_SHORT and i < len(toks):
                v = toks[i]; i += 1
        elif find_script:
            return opts, t
        else:
            continue
        if k in SBATCH_KEYS:
            opts[SBATCH_KEYS[k]] = v
    return opts, None


def split_submit_line(line):
    """'sbatch -o x run.slurm' -> (options after sbatch, script)."""
    try:
        toks = shlex.split(line)
    except ValueError:
        toks = line.split()
    for n, t in enumerate(toks):
        if t.rsplit("/", 1)[-1] == "sbatch":
            return parse_sbatch_args(toks[n + 1:], find_script=True)
    return {}, None


def sbatch_header(text):
    """#SBATCH lines of a batch script -> options."""
    toks = []
    for l in text.splitlines():
        if l.startswith("#SBATCH"):
            try:
                toks += shlex.split(l[7:], comments=True)
            except ValueError:
                toks += l[7:].split("#")[0].split()
    return parse_sbatch_args(toks)[0]


def _sections(out):
    """'@@TAG arg' marker lines split the output of a remote script -> [(tag, arg, [lines])]."""
    res = []
    for l in out.splitlines():
        if l.startswith("@@"):
            tag, _, arg = l[2:].partition(" ")
            res.append((tag, arg.strip(), []))
        elif res:
            res[-1][2].append(l)
    return res


def resolve_logs(host, pre, ids, live=False, check=False):
    """Where are the .out/.err of these Slurm jobs?  -> {jid: {"out": path, "err": path}}.
    Tries, in order: scontrol (live jobs), sacct StdOut/StdErr (recent Slurm), the -o/-e of the sbatch command line
    (sacct SubmitLine) or the #SBATCH lines of the batch script, and Slurm's default slurm-<id>.out.  With
    check=True, paths that do not exist are replaced by files in the work folder whose name contains the job id."""
    q = shlex.quote(",".join(ids))
    sc = "".join(f"echo '@@SC {j}'; scontrol show job -o {shlex.quote(j)} 2>/dev/null | tr ' ' '\\n' | "
                 f"grep -E '^(StdOut|StdErr|WorkDir|JobName)='; " for j in ids) if live else ""
    script = (f"echo \"@@U ${{USER:-$(whoami)}}\"; {sc}"
              f"if o=$(sacct -j {q} -X -n -P -o JobID,WorkDir,JobName,StdOut,StdErr 2>/dev/null); then "
              f"echo @@ACCT; printf '%s\\n' \"$o\"; else echo @@ACCT; sacct -j {q} -X -n -P -o JobID,WorkDir,JobName; fi; "
              f"echo @@SUB; sacct -j {q} -X -n -P -o JobID,SubmitLine 2>/dev/null; true")
    ok, out, err = ssh_run(host, script, timeout=120, pre=pre)
    if not ok:
        raise OSError(err)
    user, info = "", {}           # jid -> dict(wd, name, out, err, sub)
    for tag, arg, lines in _sections(out):
        if tag == "U":
            user = arg
        elif tag == "SC":
            kv = dict(l.split("=", 1) for l in lines if "=" in l)
            if kv.get("WorkDir"):
                info.setdefault(arg, {}).update(wd=kv.get("WorkDir", ""), name=kv.get("JobName", ""),
                                                out=kv.get("StdOut", ""), err=kv.get("StdErr", ""), live=True)
        elif tag in ("ACCT", "SUB"):
            for l in lines:
                f = l.split("|")
                if f[0] not in ids or len(f) < 2:
                    continue
                d = info.setdefault(f[0], {})
                if tag == "SUB":
                    d["sub"] = "|".join(f[1:])
                elif not d.get("live"):
                    d.update(wd=f[1], name=f[2] if len(f) > 2 else "")
                    if len(f) >= 5:
                        d.update(out=f[3], err=f[4])
    # jobs whose log paths Slurm did not keep: read the options they were submitted with
    want = {}
    for jid, d in info.items():
        if d.get("out"):
            continue
        opts, scr = split_submit_line(d.get("sub", ""))
        d["opts"] = opts
        if scr and not ("output" in opts and "error" in opts):
            want[jid] = scr
    if want:
        s = "".join(f"echo '@@H {j}'; (cd {shlex.quote(info[j]['wd'] or '.')} && grep -E '^#SBATCH' "
                    f"{shlex.quote(scr)}) 2>/dev/null; " for j, scr in want.items())
        ok, out, err = ssh_run(host, s + "true", timeout=60, pre=pre)
        for tag, arg, lines in _sections(out if ok else ""):
            if tag == "H" and arg in info:
                info[arg]["opts"] = {**sbatch_header("\n".join(lines)), **info[arg]["opts"]}
    res = {}
    for jid, d in info.items():
        opts, wd = d.get("opts", {}), d.get("wd", "")
        if opts.get("chdir"):
            wd = opts["chdir"] if opts["chdir"].startswith("/") else wd.rstrip("/") + "/" + opts["chdir"]
        po = d.get("out") or opts.get("output", "")
        pe = d.get("err") or opts.get("error", "") or po
        if not po and not wd:
            continue
        name = d.get("name") or opts.get("name", "")
        res[jid] = {"out": expand_slurm_path(po, jid, name, user, wd),
                    "err": expand_slurm_path(pe, jid, name, user, wd), "wd": wd}
    if check and res:
        s = ""
        for jid, p in res.items():
            s += (f"echo '@@C {jid}'; for f in {shlex.quote(p['out'])} {shlex.quote(p['err'])}; do "
                  f"[ -e \"$f\" ] && echo \"OK $f\"; done; ")
            if p["wd"]:
                s += (f"find {shlex.quote(p['wd'])} -maxdepth 3 -type f -name {shlex.quote('*' + jid + '*')} "
                      f"2>/dev/null | head -20 | sed 's/^/F /'; ")
        ok, out, err = ssh_run(host, s + "true", timeout=60, pre=pre)
        for tag, jid, lines in _sections(out if ok else ""):
            if tag != "C" or jid not in res:
                continue
            okp = {l[3:] for l in lines if l.startswith("OK ")}
            found = [l[2:] for l in lines if l.startswith("F ")]
            p = res[jid]
            if p["out"] in okp or not found:
                continue
            errs = [f for f in found if f.endswith(".err")]
            outs = [f for f in found if not f.endswith(".err")]
            p["out"] = (outs or errs)[0]
            p["err"] = p["err"] if p["err"] in okp else (errs or outs)[0]
    for p in res.values():
        p.pop("wd", None)
    return res


def own_log(path, jid):
    """A log is only deleted if it is an absolute path and its file name contains the job number (otherwise it
    might be a shared file)."""
    if not path.startswith("/") or "/../" in path + "/" or "\n" in path:
        return False
    base = path.rsplit("/", 1)[-1]
    return bool(re.search(r"(?<!\d)" + re.escape(jid.partition("_")[0]) + r"(?!\d)", base))


class ClusterState:
    """State of one cluster, updated from a worker thread; the window only reads it."""

    def __init__(self):
        self.running, self.hist, self.again = [], [], False
        self.ok, self.err, self.t, self.busy, self.first = None, "", 0.0, False, True
        self.logs = {}           # (jid, "out"|"err") -> (text, t)
        self.log_paths = {}      # jid -> {"out": remote path, "err": remote path}
        self.log_busy = set()
        self.starts = {}         # queued id -> estimated start time
        self.eff = {}            # finished id -> efficiency text
        self.eff_busy = set()
        self.info, self.info_t, self.info_busy, self.info_err = None, 0.0, False, ""   # cluster status panel
        self.scripts, self.scripts_t, self.scripts_busy, self.scripts_err = None, 0.0, False, ""   # its "Scripts"
        self.script_text = {}    # path -> (text, mtime, error) read from the cluster
        self.script_busy = set()


# ───────────────────────────── cluster status (nodes, partitions, free CPUs/GPUs, queue) ─────────────────────────────
def info_script():
    return INFO_SCRIPT + (f"echo @@G; u=${{USER:-$(whoami)}}; {gpu_script(False)}" if S["show_gpus"] else "")


INFO_SCRIPT = (
    "echo @@PRIV; scontrol show config 2>/dev/null | grep -i '^PrivateData'; "
    "echo @@NODES; sinfo -h -N -O 'NodeHost:80,Partition:60,StateCompact:30,CPUsState:40,Gres:200,GresUsed:200,"
    "Memory:20,AllocMem:20' 2>/dev/null | tr -s ' '; "
    "echo @@PART; sinfo -h -o '%P|%a|%l' 2>/dev/null; "
    "echo @@Q; squeue -h -o '%P|%T|%u|%i|%M|%l|%D|%R|%j' 2>/dev/null; "
    "echo @@T; squeue -h -O 'JobArrayID:60,tres-alloc:300' 2>/dev/null | tr -s ' '; "
    "echo \"@@ME ${USER:-$(whoami)}\"; "
    "echo @@SHARE; sshare -h -P -U -o Account,FairShare 2>/dev/null; ")
NODE_OK = {"idle", "mix", "mixed", "alloc", "allocated", "comp", "completing"}


def tres(s):
    """'cpu=16,mem=500000M,node=1,gres/gpu=1' -> {"cpu": 16, "mem": MB, "node": 1}"""
    out = {}
    for kv in s.split(","):
        k, _, v = kv.partition("=")
        if k == "mem":
            m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTP]?)", v)
            if m:
                out["mem"] = float(m.group(1)) * {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024 ** 2,
                                                  "P": 1024 ** 3}[m.group(2)]
        elif k in ("cpu", "node") and v.isdigit():
            out[k] = int(v)
    return out


def gres_gpus(g):
    """'gpu:a100:2(S:0-1),gpu:t4:1' -> {"a100": 2, "t4": 1}; 'gpu:8' -> {"": 8}; '(null)' -> {}"""
    out = {}
    for part in re.split(r",(?![^()]*\))", g or ""):
        m = re.match(r"gpu(?::([^:(]+))?:(\d+)", part.strip())
        if m:
            out[m.group(1) or ""] = out.get(m.group(1) or "", 0) + int(m.group(2))
    return out


def parse_info(out):
    """Output of INFO_SCRIPT -> dict with the cluster totals and one row per partition (None = unknown)."""
    sec, cur = {}, None
    for l in out.splitlines():
        if l.startswith("@@"):
            tag, _, arg = l[2:].partition(" ")
            cur = tag; sec.setdefault(tag, []); sec[tag + "_arg"] = arg.strip(); continue
        if cur:
            sec[cur].append(l)
    me = sec.get("ME_arg", "")
    private = any("job" in l.lower() for l in sec.get("PRIV", []))
    parts, order, default, nodes = {}, [], None, {}
    for l in sec.get("PART", []):
        f = l.split("|")
        if len(f) >= 3:
            name = f[0].rstrip("*")
            if f[0].endswith("*"):
                default = name
            if name not in parts:
                order.append(name)
                parts[name] = dict(name=name, avail=f[1], limit=f[2], nodes=0, idle=0, down=0, cpu_free=0, cpu_tot=0,
                                   gpu_free={}, gpu_tot={}, mem_free=0, run=0, pend=0, my_run=0, my_pend=0)

    def add(d, n):
        d["nodes"] += 1
        if n["state"] == "idle":
            d["idle"] += 1
        d["cpu_tot"] += n["cpu"][3]
        for k, v in n["gpu"].items():
            d["gpu_tot"][k] = d["gpu_tot"].get(k, 0) + v
        if n["ok"]:
            d["cpu_free"] += n["cpu"][1]
            for k, v in n["gpu"].items():
                d["gpu_free"][k] = d["gpu_free"].get(k, 0) + max(0, v - n["gpu_used"].get(k, 0))
            d["mem_free"] += max(0, n["mem"] - n["amem"])
        else:
            d["down"] += 1
    for l in sec.get("NODES", []):
        f = l.split()
        if len(f) < 8:
            continue
        try:
            cpu = [int(x) for x in f[3].split("/")]
            mem, amem = int(f[6]), int(f[7])
        except ValueError:
            continue
        st = f[2].rstrip("*~#!%$@+^-").lower()
        n = dict(state=st, ok=st in NODE_OK, cpu=cpu, gpu=gres_gpus(f[4]), gpu_used=gres_gpus(f[5]), mem=mem, amem=amem)
        nodes.setdefault(f[0], n)
        p = f[1].rstrip("*")
        if p not in parts:
            order.append(p)
            parts[p] = dict(name=p, avail="", limit="", nodes=0, idle=0, down=0, cpu_free=0, cpu_tot=0, gpu_free={},
                            gpu_tot={}, mem_free=0, run=0, pend=0, my_run=0, my_pend=0)
        add(parts[p], n)
    total = dict(name="(all)", nodes=0, idle=0, down=0, cpu_free=0, cpu_tot=0, gpu_free={}, gpu_tot={}, mem_free=0,
                 run=0, pend=0, my_run=0, my_pend=0)
    for n in nodes.values():
        add(total, n)
    queue = []
    for l in sec.get("Q", []):
        f = l.split("|")
        if len(f) < 3:
            continue
        p = parts.get(f[0].split(",")[0])
        r, pd = f[1] == "RUNNING", f[1] == "PENDING"
        mine = f[2] == me
        if len(f) >= 9:
            queue.append(dict(part=f[0], state=f[1], user=f[2], id=f[3], used=f[4], limit=f[5], nodes=f[6],
                              reason=f[7], name="|".join(f[8:]), mine=mine))
        for d in [x for x in (p, total) if x]:
            d["run"] += r; d["pend"] += pd
            if mine:
                d["my_run"] += r; d["my_pend"] += pd
    if private:          # the cluster only shows our own jobs: the totals of all users are unknown
        for d in list(parts.values()) + [total]:
            d["run"] = d["pend"] = None
    share = [l.split("|") for l in sec.get("SHARE", []) if l.count("|") >= 1]
    rank = {"RUNNING": 0, "COMPLETING": 1, "CONFIGURING": 1, "PENDING": 2}
    queue.sort(key=lambda q: (rank.get(q["state"], 3), q["part"]))
    gpus = parse_gpus(sec.get("G", []))
    res = {}
    for l in sec.get("T", []):
        f = l.split()
        if len(f) >= 2:
            res[f[0]] = tres(f[1])
    for q in queue:
        q["gpu"] = gpus.get(q["id"], "")
        r = res.get(q["id"], {})
        q["cpus"], q["mem"] = r.get("cpu"), r.get("mem")
    return dict(parts=[parts[p] for p in order], total=total, default=default, private=private, queue=queue,
                have_nodes=bool(nodes), share=[(a, fs) for a, fs in share if a], user=me)
