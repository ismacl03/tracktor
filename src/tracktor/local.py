"""Local jobs started with `track`: running ones, history, logs, CPU / RAM / GPU usage."""
import json, os, subprocess, time

from .config import CONF_DIR, STATE_DIR, tail_bytes


# ───────────────────────────── local jobs ─────────────────────────────
def job_dirs():
    dirs = [STATE_DIR]
    try:
        dirs += [l.strip() for l in open(os.path.join(CONF_DIR, "dirs")) if l.strip()]
    except OSError:
        pass
    return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]


def alive(pid):
    try:
        os.kill(pid, 0); return True
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError):
        return True


def descendants(pid):
    """PIDs of all descendants of pid (reading /proc), deepest first."""
    children = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(d))
    out, stack = [], [pid]
    while stack:
        for c in children.get(stack.pop(), []):
            out.append(c); stack.append(c)
    return out[::-1]


def is_track(pid):
    """Is pid really a `track` wrapper (a bash running track / track.sh)?  A job file could be stale or forged."""
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        return False
    return any(os.path.basename(a.decode(errors="replace")) in ("track", "track.sh") for a in argv[:3])


def stop_job(pid, sig):
    """Send sig to the program running under track (pid = track's bash) and its children.  Nothing is sent to the
    bash itself: when the program dies, track writes the history entry with its exit code (143 = terminated,
    137 = killed)."""
    if not is_track(pid):
        raise PermissionError(f"process {pid} is not a track job")
    targets = descendants(pid) or [pid]
    for p in targets:
        try:
            os.kill(p, sig)
        except ProcessLookupError:
            pass
    return targets


def read_tail(path, nbytes=None):
    nbytes = nbytes or tail_bytes()
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - nbytes))
            txt = f.read().decode("utf-8", "replace")
    except OSError:
        return "(cannot read the log: %s)" % path
    return clean_log(txt)


def clean_log(txt):
    # progress bars rewrite the line with \r: keep the last version of each line
    return "\n".join(l.split("\r")[-1] for l in txt.replace("\x00", "").split("\n"))


def fmt_dt(sec):
    sec = int(max(sec, 0)); d, sec = divmod(sec, 86400); h, r = divmod(sec, 3600); m, s = divmod(r, 60)
    if d:
        return f"{d}d{h:02d}h"
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def running_jobs():
    out = []
    for d in job_dirs():
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".job"):
                continue
            try:
                kv = dict(l.split("=", 1) for l in open(os.path.join(d, fn)).read().splitlines() if "=" in l)
            except OSError:
                continue
            pid = int(kv.get("pid", "0") or 0)
            if not alive(pid):
                continue
            log = kv.get("log", "")
            if log and not os.path.isabs(log):
                log = os.path.join(kv.get("cwd", os.path.dirname(d)), log)
            out.append(dict(label=fn[:-4], pid=pid, start=int(kv.get("start", time.time())), log=log,
                            cmd=kv.get("cmd", ""), cwd=kv.get("cwd", ""), argv=kv.get("argv", "")))
    return out


def history():
    out = []
    for d in job_dirs():
        p = os.path.join(d, "history.jsonl")
        if os.path.exists(p):
            for l in open(p):
                try:
                    out.append(dict(json.loads(l), _dir=d, _line=l.strip()))
                except json.JSONDecodeError:
                    pass
    return sorted(out, key=lambda r: r.get("end", 0), reverse=True)


def temp_logs():
    """Temporary logs (<tracktor dir>/logs/*.log) not used by a running job: the ones "Clear history" deletes."""
    busy = {os.path.realpath(j["log"]) for j in running_jobs() if j["log"]}
    out = []
    for d in job_dirs():
        ld = os.path.join(d, "logs")
        if os.path.isdir(ld):
            out += [p for p in (os.path.join(ld, fn) for fn in sorted(os.listdir(ld)) if fn.endswith(".log"))
                    if os.path.isfile(p) and os.path.realpath(p) not in busy]
    return out


def remove_history_entry(r, delete_log=False):
    """Remove one line from its folder's history.jsonl; with delete_log, also delete its log if it is temporary (logs
    saved with -l are never deleted)."""
    p = os.path.join(r["_dir"], "history.jsonl")
    try:
        lines = open(p).read().splitlines()
    except OSError:
        return
    if r["_line"] in (l.strip() for l in lines):
        i = next(k for k, l in enumerate(lines) if l.strip() == r["_line"])
        del lines[i]
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write("".join(l + "\n" for l in lines))
        os.replace(tmp, p)
    log = r.get("log", "")
    if delete_log and log and log in temp_logs():
        try:
            os.remove(log)
        except OSError:
            pass


def gpu_info():
    """-> (GPU status text, {pid: bytes of GPU memory it uses})."""
    status, per_pid = "GPU: no nvidia-smi", {}
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
        u, m, t = [x.strip() for x in o.strip().splitlines()[0].split(",")]
        status = f"GPU {u}%  {int(m) / 1024:.1f}/{int(t) / 1024:.1f} GB"
        o = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=3).stdout
        for l in o.strip().splitlines():
            pid, mem = [x.strip() for x in l.split(",")]
            per_pid[int(pid)] = per_pid.get(int(pid), 0) + int(mem) * 2 ** 20
    except Exception:
        pass
    return status, per_pid


PAGE, CLK = os.sysconf("SC_PAGE_SIZE"), os.sysconf("SC_CLK_TCK")


def proc_table():
    """One pass over /proc: {pid: (ppid, resident memory in bytes, CPU ticks used)}."""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                st = f.read().rsplit(")", 1)[1].split()
            out[int(d)] = (int(st[1]), int(st[21]) * PAGE, int(st[11]) + int(st[12]))
        except (OSError, IndexError, ValueError):
            continue
    return out


def tree_of(pid, table, kids):
    out, stack = [pid], [pid]
    while stack:
        for c in kids.get(stack.pop(), []):
            out.append(c); stack.append(c)
    return [p for p in out if p in table]


def meminfo():
    """-> (bytes of RAM in use, total bytes) or None."""
    try:
        kv = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo")}
        return kv["MemTotal"] - kv["MemAvailable"], kv["MemTotal"]
    except (OSError, KeyError, ValueError, IndexError):
        return None


def fmt_bytes(b):
    if b is None:
        return ""
    return f"{b / 2 ** 30:.1f} GB" if b >= 2 ** 30 else f"{b / 2 ** 20:.0f} MB"


def local_exit_text(ex):
    return "ok" if ex == 0 else {143: "terminated", 137: "killed", 130: "interrupted"}.get(ex, f"error {ex}")
