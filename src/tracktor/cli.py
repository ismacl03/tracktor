"""`tracktor --list`: the jobs as text, for a terminal (e.g. over SSH from a phone).  Never imports Qt."""
import os, sys, threading, time

from .config import S, load_clusters, load_settings
from .local import fmt_dt, history, local_exit_text, running_jobs
from .slurm import SLURM_CODE, array_of, parse_poll, poll_script, slurm_end_text
from .ssh import ssh_run

MAX_FIN = 15               # finished jobs listed per group (most recent first)
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def col(txt, c):
    codes = {"red": "31", "green": "32", "yellow": "33", "grey": "90", "bold": "1"}
    return f"\033[{codes[c]}m{txt}\033[0m" if USE_COLOR and txt else txt


def table(rows, indent="  "):
    """Rows of cells (a cell may be (text, colour)) -> aligned lines."""
    plain = [[c[0] if isinstance(c, tuple) else c for c in r] for r in rows]
    w = [max(len(r[i]) for r in plain) for i in range(len(plain[0]))] if plain else []
    out = []
    for r, p in zip(rows, plain):
        cells = []
        for i, (c, t) in enumerate(zip(r, p)):
            pad = t if i == len(r) - 1 else t.ljust(w[i])
            cells.append(col(pad, c[1]) if isinstance(c, tuple) else pad)
        out.append((indent + "  ".join(cells)).rstrip())
    return out


def poll_all(clusters, since):
    """Query every cluster at once (reusing Tracktor's SSH connections).  -> {name: (running, hist) or error}."""
    res = {}

    def one(c):
        try:
            ok, out, err = ssh_run(c["host"], poll_script(since), timeout=40, pre=c.get("pre", ""))
            res[c["name"]] = parse_poll(out)[:2] if ok else (err or "no connection")
        except Exception as e:
            res[c["name"]] = str(e) or "no connection"
    th = [threading.Thread(target=one, args=(c,), daemon=True) for c in clusters]
    for t in th:
        t.start()
    for t in th:
        t.join(60)
    return res


def group_finished(hist):
    """Tasks of the same array with the same result -> one line with a count."""
    out, seen = [], {}
    for r in hist:
        base = array_of(r["id"])
        k = (base, r["state"].split()[0], r.get("exit"))
        if base and k in seen:
            seen[k]["n"] += 1; seen[k]["end"] = max(seen[k]["end"], r["end"]); continue
        d = dict(r, n=1); out.append(d)
        if base:
            seen[k] = d
    return out


def list_main(argv):
    """tracktor --list [--local | --clusters] [--hours N]"""
    hours, only = 24, None
    i = 0
    while i < len(argv):
        a = argv[i]; i += 1
        if a in ("--local", "--clusters"):
            only = a[2:]
        elif a in ("--hours", "-H") and i < len(argv) and argv[i].isdigit():
            hours = int(argv[i]); i += 1
        else:
            print(f"tracktor --list: unknown option {a}\n"
                  "usage: tracktor --list [--local | --clusters] [--hours N]   (finished jobs of the last N hours, "
                  "default 24)", file=sys.stderr)
            return 2
    load_settings()
    now, since_t = time.time(), time.time() - hours * 3600
    lines = []
    if only != "clusters":
        jobs = running_jobs()
        lines.append(col(f"Local  ·  {len(jobs)} running", "bold"))
        rows = [[j["label"], (fmt_dt(now - j["start"]), "green"), j.get("cwd", ""), (j.get("log", ""), "grey")]
                for j in sorted(jobs, key=lambda j: j["start"])]
        lines += table(rows) if rows else ["  (nothing running)"]
        allfin = [r for r in history() if r.get("end", 0) >= since_t]
        fin = allfin[:MAX_FIN]
        if fin:
            lines.append(f"  finished in the last {hours} h:")
            lines += table([[r.get("label", "?"), (local_exit_text(r.get("exit", "?")),
                                                    "red" if r.get("exit") != 0 else "green"),
                             fmt_dt(r.get("end", 0) - r.get("start", 0)),
                             time.strftime("%d-%m %H:%M", time.localtime(r.get("end", 0)))] for r in fin], "    ")
            if len(allfin) > len(fin):
                lines.append(f"    … and {len(allfin) - len(fin)} more")
        lines.append("")
    clusters = load_clusters() if only != "local" else []
    if clusters:
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since_t))
        res = poll_all(clusters, since)
        for c in clusters:
            r = res.get(c["name"], "no answer")
            if isinstance(r, str):
                lines.append(col(c["name"], "bold") + "  ·  " + col(f"not connected: {r.splitlines()[-1][:120] if r else ''}",
                                                                   "red"))
                lines.append(f"  (log in once with:  ssh {c['host']}   — or «Connect…» in the window)")
                lines.append(""); continue
            running, hist = r
            if not S.get("show_pending", True):
                running = [x for x in running if x["state"] != "PENDING"]
            n_r = sum(1 for x in running if x["state"] != "PENDING")
            lines.append(col(c["name"], "bold") + f"  ·  {n_r} running, {len(running) - n_r} queued")
            rows = []
            for x in running:
                st = SLURM_CODE.get(x["state"], x["state"])
                where = x.get("gpu") or x["reason"]
                rows.append([x["id"], x["name"], (st, "green" if st == "R" else "yellow"), x["part"],
                             f"{x['used']} / {x['limit']}" if x["state"] != "PENDING" else f"limit {x['limit']}",
                             (where, "grey")])
            lines += table(rows) if rows else ["  (nothing running or queued)"]
            allfin = group_finished(sorted((h for h in hist if h["end"] and h["end"] >= since_t),
                                           key=lambda h: -h["end"]))
            fin = allfin[:MAX_FIN]
            if fin:
                lines.append(f"  finished in the last {hours} h:")
                rows = []
                for h in fin:
                    txt = slurm_end_text(h["state"], h["exit"])
                    rows.append([h["id"] if h["n"] == 1 else f"{array_of(h['id'])}_* ×{h['n']}", h["name"],
                                 (txt, "green" if txt in ("ok", "finished") else "red"), fmt_dt(h["elapsed"] or 0),
                                 time.strftime("%d-%m %H:%M", time.localtime(h["end"]))])
                lines += table(rows, "    ")
                if len(allfin) > len(fin):
                    lines.append(f"    … and {len(allfin) - len(fin)} more")
            lines.append("")
    print("\n".join(lines).rstrip())
    return 0
