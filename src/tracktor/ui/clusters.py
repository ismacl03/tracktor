"""Main window: background queries to the clusters, scancel, adding / editing clusters."""
import shlex, threading, time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog, QMessageBox

from ..config import CLUSTER_EVERY_DEFAULT, SSH, cset, save_clusters, tail_bytes
from ..local import clean_log
from ..slurm import ClusterState, eff_script, own_log, parse_eff, parse_poll, poll_script, resolve_logs
from ..ssh import connect_in_terminal, disconnect, ssh_opts, ssh_run
from .common import ask, notify, tell
from .dialogs import ClusterDialog


class ClustersMixin:
    """Background queries to the clusters, scancel, adding / editing clusters."""

    # ── clusters: background queries ──
    def cluster(self, name):
        return next((c for c in self.clusters if c["name"] == name), None)

    def poll_cluster(self, c, force=False):
        s = self.cstate.setdefault(c["name"], ClusterState())
        if s.busy:
            if force:
                s.again = True          # a query is under way: run another one as soon as it ends
            return
        if not force and time.time() - s.t < c.get("every", CLUSTER_EVERY_DEFAULT):
            return
        s.busy, s.again = True, False
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(max(time.time() - cset(c, "hist_days") * 86400,
                                                                       c.get("cleared", 0))))

        def run():
            while True:
                try:
                    ok, out, err = ssh_run(c["host"], poll_script(since), pre=c.get("pre", ""))
                    if ok:
                        s.running, s.hist, s.starts = parse_poll(out)
                    s.ok, s.err = ok, ("" if ok else err)
                except Exception as e:
                    s.ok, s.err = False, str(e)
                s.t, s.first = time.time(), False
                if not s.again:
                    break
                s.again = False
            s.busy = False
        threading.Thread(target=run, daemon=True).start()

    def fetch_cluster_log(self, name, jid, running, stream):
        c, s = self.cluster(name), self.cstate.get(name)
        k = (jid, stream)
        if not c or not s or k in s.log_busy:
            return
        old = s.logs.get(k)
        if old and (not running or time.time() - old[1] < cset(c, "cluster_log_every")):
            return
        s.log_busy.add(k)

        def run():
            try:
                paths = s.log_paths.get(jid)
                if not paths:
                    try:
                        paths = resolve_logs(c["host"], c.get("pre", ""), [jid], live=running, check=True).get(jid)
                    except OSError as e:
                        s.logs[k] = (f"(no connection to {name}: {e})", time.time()); return
                    if not paths:
                        s.logs[k] = ("(log not found: Slurm no longer remembers this job)", time.time())
                        return
                    s.log_paths[jid] = paths
                f = shlex.quote(paths[stream])
                ok, out, err = ssh_run(c["host"], f"if [ -e {f} ]; then tail -c {tail_bytes()} {f} 2>&1; "
                                       f"else echo @@NOLOG; fi", timeout=60, pre=c.get("pre", ""))
                if not ok:
                    text = f"(no connection to {name}: {err})"
                elif out.strip() == "@@NOLOG":
                    s.log_paths.pop(jid, None)     # look for it again next time (it may not have been created yet)
                    text = (f"(this log does not exist on {name}: it was deleted or moved, or the job has not "
                            f"written it yet)\n\n{paths[stream]}")
                else:
                    text = clean_log(out)
                s.logs[k] = (text, time.time())
            finally:
                s.log_busy.discard(k)
        threading.Thread(target=run, daemon=True).start()

    def fetch_eff(self, name, jid):
        """Efficiency (like seff) of a finished job, in the background and only once."""
        c, s = self.cluster(name), self.cstate.get(name)
        if not c or not s or jid in s.eff or jid in s.eff_busy:
            return
        s.eff_busy.add(jid)

        def run():
            try:
                ok, out, err = ssh_run(c["host"], eff_script(jid), timeout=30, pre=c.get("pre", ""))
                if ok:
                    s.eff[jid] = parse_eff(out) or "Efficiency: Slurm has no data for this job"
            finally:
                s.eff_busy.discard(jid)
        threading.Thread(target=run, daemon=True).start()

    def cluster_paths(self, info):
        s = self.cstate.get(info.get("cluster")) if info else None
        return s.log_paths.get(info["id"]) if s else None

    def delete_remote_logs(self, c, ids):
        """Delete those jobs' .out/.err on the cluster (in the background) and notify the result."""
        s = self.cstate.get(c["name"])
        ids = list(dict.fromkeys(ids))
        if not ids:
            return

        def run():
            paths, skipped, unknown = [], 0, 0
            for k in range(0, len(ids), 100):
                chunk = ids[k:k + 100]
                try:
                    info = resolve_logs(c["host"], c.get("pre", ""), chunk)
                except OSError:
                    info = {}
                for jid in chunk:
                    pp = info.get(jid) or (s.log_paths.get(jid) if s else None)
                    if not pp:
                        unknown += 1; continue
                    for path in dict.fromkeys([pp["out"], pp["err"]]):
                        if own_log(path, jid):
                            paths.append(path)
                        else:
                            skipped += 1
            n = 0
            for k in range(0, len(paths), 200):
                chunk = paths[k:k + 200]
                q = " ".join(shlex.quote(x) for x in chunk)
                ok, out, err = ssh_run(c["host"], f"for f in {q}; do [ -f \"$f\" ] && rm -f -- \"$f\" && echo @@DEL; done",
                                       timeout=120, pre=c.get("pre", ""))
                n += out.count("@@DEL") if ok else 0
            body = f"{n} .out/.err file(s) deleted"
            if skipped:
                body += f"; {skipped} kept because their name does not contain the job number (they might be shared)"
            if unknown:
                body += f"; for {unknown} job(s) Slurm no longer knows where the logs were"
            notify(f"{c['name']}: logs deleted", body)
            if s:
                for jid in ids:
                    s.log_paths.pop(jid, None)
                    s.logs.pop((jid, "out"), None); s.logs.pop((jid, "err"), None)
        threading.Thread(target=run, daemon=True).start()

    def remote(self, c, script, done, timeout=60):
        """Run a command on a cluster in the background; done(ok, out, err) is called in the window's thread."""
        res = {}

        def run():
            try:
                res["r"] = ssh_run(c["host"], script, timeout=timeout, pre=c.get("pre", ""))
            except Exception as e:
                res["r"] = (False, "", str(e))
        th = threading.Thread(target=run, daemon=True); th.start()

        def check():
            if th.is_alive():
                QTimer.singleShot(200, check); return
            done(*res["r"])
        QTimer.singleShot(200, check)

    def repoll(self, c):
        """Query a cluster's jobs now (after scancel / sbatch…) and once more a few seconds later, when Slurm has
        caught up (a cancelled job is still listed while it is being killed)."""
        self.poll_cluster(c, force=True)
        QTimer.singleShot(5000, lambda: self.poll_cluster(c, force=True))

    def scancel(self, p):
        c = self.cluster(p["cluster"])
        if not c or not ask(self, "Cancel", f"Cancel «{p['label']}» (job {p['id']}) on "
                                         f"{c['name']}?"):
            return
        res = {}

        def run():
            res["r"] = ssh_run(c["host"], f"scancel {shlex.quote(p['id'])} 2>&1", timeout=30, pre=c.get("pre", ""))
        th = threading.Thread(target=run, daemon=True); th.start()

        def done():
            if th.is_alive():
                QTimer.singleShot(300, done); return
            ok, out, err = res["r"]
            if ok and not out.strip():           # gone from "Running" right away; the re-poll confirms it
                s = self.cstate.get(c["name"])
                if s:
                    s.running = [r for r in s.running if r["id"] != p["id"] and not r["id"].startswith(p["id"] + "_")]
                self.refresh()
            self.repoll(c)
            if not ok or out.strip():
                tell(self, "Cancel", f"scancel: {(out.strip() or err)}")
        QTimer.singleShot(300, done)

    def disconnect_cluster(self, c):
        disconnect(c)
        st = self.cstate.get(c["name"])
        if st:
            st.ok, st.err = False, "connection closed"
        self.refresh()

    def connect(self, c):
        if not connect_in_terminal(c):
            tell(self, "Connect", "No terminal emulator found.  Open one and run:\n\n"
                 + " ".join(shlex.quote(x) for x in [SSH, *ssh_opts("yes", f"{cset(c, 'persist_hours')}h"), c["host"], "true"]),
                 QMessageBox.Icon.Information)
            return
        s = self.cstate.get(c["name"])
        # retry a few times while the user logs in
        for k in range(1, 13):
            QTimer.singleShot(k * 5000, lambda: self.poll_cluster(c, force=not (s and s.ok)))

    # ── add / edit / remove clusters ──
    def add_cluster(self):
        d = ClusterDialog(self, taken=[c["name"] for c in self.clusters])
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        c = d.value(); self.clusters.append(c); save_clusters(self.clusters)
        self.cstate[c["name"]] = ClusterState()
        self.refresh()

    def edit_cluster(self, c):
        d = ClusterDialog(self, existing=c, taken=[x["name"] for x in self.clusters if x is not c])
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        v = d.value()
        self.cstate.pop(c["name"], None)
        c.update(v); save_clusters(self.clusters)
        self.cstate[c["name"]] = ClusterState()
        self.refresh()

    def remove_cluster(self, c):
        if not ask(self, "Remove cluster", f"Remove «{c['name']}» from the window?\n(Nothing is changed "
                                f"on the cluster or in your ~/.ssh/config.)"):
            return
        self.clusters.remove(c); save_clusters(self.clusters); self.cstate.pop(c["name"], None)
        self.close_log_tabs(lambda i: i.get("cluster") == c["name"])
        self.refresh()
