"""Main window: the cluster status panel (+ tab, partitions, queue, Scripts tab)."""
import html, threading, time

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton, QTabWidget,
                             QTreeWidget, QVBoxLayout, QWidget)

from ..config import CLUSTER_INFO_EVERY_DEFAULT, FILE_TYPES, S, SCRIPT_DEPTH, cset, save_clusters, save_file_types
from ..scripts import job_dirs_script, parse_job_dirs, parse_scan, sbatch_columns, sbatch_minutes, scan_script
from ..slurm import SLURM_CODE, ClusterState, info_script, parse_info, slurm_secs
from ..ssh import ssh_run
from .common import GREY, INFO_COLS, QUEUE_COLS, SortItem, apply_sort, fmt_mb, sortable
from .dialogs import ScriptFoldersDialog


class StatusPanelMixin:
    """The cluster status panel (+ tab, partitions, queue, Scripts tab)."""

    # ── cluster status panel ──
    INFO_COLS, QUEUE_COLS = INFO_COLS, QUEUE_COLS

    def apply_columns(self, w, c):
        """Hide the columns this cluster does not want (Settings > Clusters); GPUs also with "Show … GPU(s)" off."""
        hide = set(c.get("queue_hide", [])) | (set() if S["show_gpus"] else {"GPUs"})
        for k, name in enumerate(self.QUEUE_COLS):
            w.queue.setColumnHidden(k, name in hide)
        hide = set(c.get("parts_hide", []))
        for k, name in enumerate(self.INFO_COLS):
            w.table.setColumnHidden(k, name in hide)

    def cinfo_index(self, name):
        return next((i for i in range(self.cinfo.count()) if self.cinfo.widget(i)._cluster == name), -1)

    def cinfo_clicked(self, i):
        """The "+" tab: choose a cluster to open."""
        if self.cinfo.widget(i) is not self.cinfo_plus:
            return
        menu = QMenu(self); acts = {}
        for c in self.clusters:
            a = menu.addAction(c["name"] + ("   (open)" if self.cinfo_index(c["name"]) >= 0 else ""))
            acts[a] = lambda c=c: self.show_cluster_info(c)
        if not self.clusters:
            menu.addAction("No clusters yet").setEnabled(False)
        menu.addSeparator()
        acts[menu.addAction("Add a cluster…")] = self.add_cluster
        bar = self.cinfo.tabBar()
        a = menu.exec(bar.mapToGlobal(bar.tabRect(i).bottomLeft()))
        if a in acts:
            acts[a]()

    def cinfo_changed(self, i):
        """The "+" tab only shows its hint when no cluster is open; otherwise stay on a cluster tab."""
        w = self.cinfo.widget(i)
        if w is not None and w is not self.cinfo_plus:
            self._cinfo_prev = w
        elif w is self.cinfo_plus and self.cinfo.count() > 1:
            def back():
                if self.cinfo.currentWidget() is self.cinfo_plus and self.cinfo.count() > 1:
                    prev = getattr(self, "_cinfo_prev", None)
                    j = self.cinfo.indexOf(prev) if prev is not None else -1
                    self.cinfo.setCurrentIndex(j if j >= 0 else self.cinfo.indexOf(self.cinfo_plus) - 1)
            QTimer.singleShot(0, back)

    def show_cluster_info(self, c):
        i = self.cinfo_index(c["name"])
        if i < 0:
            w = QWidget(); w._cluster, w._shown = c["name"], None
            lay = QVBoxLayout(w); lay.setContentsMargins(6, 6, 6, 6)
            w.summary = QLabel("Reading the cluster status…"); w.summary.setWordWrap(True)
            w.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            w.table = QTreeWidget(); w.table.setHeaderLabels(self.INFO_COLS); w.table.setRootIsDecorated(False)
            w.table.setUniformRowHeights(True); w.table.setFont(self.run_c.font())
            w.table.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            sortable(w.table, lambda w=w: self.redraw_cluster_info(w))
            tips = ["", "Maximum run time of a job", "Nodes with nothing running / all nodes",
                    "Nodes down, drained, in maintenance… (their CPUs and GPUs are not counted as free)",
                    "Idle CPU cores on usable nodes / all cores", "GPUs not allocated on usable nodes / all GPUs",
                    "Memory not allocated on usable nodes",
                    "Jobs of all users (— when the cluster only lets you see your own jobs)", "Your jobs"]
            for k, tip in enumerate(tips):
                w.table.headerItem().setToolTip(k, tip)
            w.queue = QTreeWidget(); w.queue.setHeaderLabels(self.QUEUE_COLS); w.queue.setRootIsDecorated(False)
            w.queue.setUniformRowHeights(True); w.queue.setFont(self.run_c.font())
            w.queue.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            sortable(w.queue, lambda w=w: self.redraw_cluster_info(w))
            w.inner = QTabWidget(); w.inner.addTab(w.table, "Partitions"); w.inner.addTab(w.queue, "Queue")
            w.inner.addTab(self.scripts_tab(w), "Scripts")
            lay.addWidget(w.summary); lay.addWidget(w.inner, 1)
            i = self.cinfo.insertTab(self.cinfo.indexOf(self.cinfo_plus), w, c["name"])
            h = self.run_split.height()
            if self.cinfo.height() < h * 0.35:          # the empty panel is short: make room for the status
                self.run_split.setSizes([int(h * 0.55), int(h * 0.45)])
        self.cinfo.setCurrentIndex(i)
        self.tabs.setCurrentIndex(0)
        self.fetch_cluster_info(c, force=True)
        if self.cstate[c["name"]].scripts is None:
            self.fetch_scripts(c)
        self.apply_columns(self.cinfo.widget(i), c)
        self.render_cluster_info(self.cinfo.widget(i))
        self.render_scripts(self.cinfo.widget(i))

    def close_cluster_info(self, i):
        w = self.cinfo.widget(i)
        if w is None or w is self.cinfo_plus:
            return
        if getattr(self, "_cinfo_prev", None) is w:
            self._cinfo_prev = None
        self.cinfo.removeTab(i)
        w.deleteLater()

    def fetch_cluster_info(self, c, force=False):
        s = self.cstate.setdefault(c["name"], ClusterState())
        if s.info_busy or (not force and time.time() - s.info_t < c.get("info_every", CLUSTER_INFO_EVERY_DEFAULT)):
            return
        s.info_busy = True

        def run():
            try:
                ok, out, err = ssh_run(c["host"], info_script() + "true", timeout=120, pre=c.get("pre", ""))
                if ok:
                    s.info, s.info_err = parse_info(out), ""
                else:
                    s.info_err = err or "no connection"
            except Exception as e:
                s.info_err = str(e)
            s.info_t, s.info_busy = time.time(), False
        threading.Thread(target=run, daemon=True).start()

    def update_cluster_info(self):
        """Called by refresh(): re-query the open panels when due (only while the window is shown; when it is shown
        again they are brought up to date at once), close those of removed clusters, redraw."""
        for i in reversed(range(self.cinfo.count())):
            w = self.cinfo.widget(i)
            if w is self.cinfo_plus:
                continue
            c = self.cluster(w._cluster)
            if not c:
                self.close_cluster_info(i); continue
            if self.isVisible() and not self.isMinimized():   # paused while the window is hidden / minimized
                self.fetch_cluster_info(c)
            self.apply_columns(w, c)
            self.render_cluster_info(w)
            self.render_scripts(w)

    @staticmethod
    def fmt_gpus(free, tot):
        if not tot:
            return "—"
        f, t = sum(free.values()), sum(tot.values())
        types = [k for k in tot if k]
        if not types:
            return f"{f} / {t}"
        if len(types) == 1:
            return f"{f} / {t}  {types[0]}"
        return f"{f} / {t}  (" + ", ".join(f"{k} {free.get(k, 0)}/{tot[k]}" for k in tot) + ")"

    def redraw_cluster_info(self, w):
        w._shown = None; self.render_cluster_info(w)

    def render_cluster_info(self, w):
        name = w._cluster; c = self.cluster(name); s = self.cstate.get(name)
        if not c or not s:
            return
        stamp = (s.info_t, s.info_err, s.info is None)
        if w._shown == stamp:
            return
        w._shown = stamp
        every = c.get("info_every", CLUSTER_INFO_EVERY_DEFAULT)
        when = time.strftime("%H:%M:%S", time.localtime(s.info_t)) if s.info_t else ""
        if s.info is None:
            w.summary.setText(f"<b>{name}</b>  ·  " + (f"<span style='color:#d04040'>could not read the status: "
                                                        f"{s.info_err[:200]}</span>" if s.info_err else "reading…"))
            return
        i, T = s.info, s.info["total"]
        dash = "—"

        def jobs(d, mine=False):
            r, p = (d["my_run"], d["my_pend"]) if mine else (d["run"], d["pend"])
            return dash if r is None else f"{r} / {p}"
        mem = lambda mb: f"{mb / 1024 ** 2:.1f} TB" if mb >= 1024 ** 2 else f"{mb / 1024:.0f} GB"
        lines = [f"<b>{name}</b>  ·  updated {when} (every {every} s)"
                 + (f"  ·  <span style='color:#d04040'>last update failed: {s.info_err[:120]}</span>" if s.info_err else "")]
        if i["have_nodes"]:
            lines.append(f"Nodes: <b>{T['nodes']}</b> ({T['idle']} idle, {T['down']} unavailable)  ·  "
                         f"CPUs free: <b>{T['cpu_free']}</b> / {T['cpu_tot']}  ·  "
                         f"GPUs free: <b>{self.fmt_gpus(T['gpu_free'], T['gpu_tot'])}</b>  ·  "
                         f"free memory: {mem(T['mem_free'])}")
        else:
            lines.append("Nodes: — (this cluster does not show them)")
        lines.append("Jobs of everyone: " + (f"<b>{T['run']}</b> running, <b>{T['pend']}</b> queued" if T["run"] is not None
                                             else "— (this cluster only lets you see your own jobs)"))
        mine = f"Your jobs: <b>{T['my_run']}</b> running, <b>{T['my_pend']}</b> queued"
        if i["share"]:
            mine += ("  ·  fairshare: " + ", ".join(f"{a} {float(v):.3f}" if v.replace('.', '', 1).isdigit()
                                                     else f"{a} {v}" for a, v in i["share"])
                     + "  <small>(0–1; the higher, the sooner your jobs start)</small>")
        lines.append(mine)
        w.summary.setText("<br>".join(lines))
        vbar = w.table.verticalScrollBar().value()
        w.table.clear()
        for d in i["parts"]:
            it = SortItem([d["name"] + ("  (default)" if d["name"] == i["default"] else ""),
                                  d.get("limit") or dash,
                                  f"{d['idle']} / {d['nodes']}" if d["nodes"] else dash,
                                  str(d["down"]) if d["nodes"] else dash,
                                  f"{d['cpu_free']} / {d['cpu_tot']}" if d["nodes"] else dash,
                                  self.fmt_gpus(d["gpu_free"], d["gpu_tot"]),
                                  mem(d["mem_free"]) if d["nodes"] else dash,
                                  jobs(d), jobs(d, True)])
            if d.get("avail") and d["avail"] != "up":
                for k in range(len(self.INFO_COLS)):
                    it.setForeground(k, GREY)
                it.setToolTip(0, f"Partition {d['avail']}")
            for k, v in (("Time limit", slurm_secs(d.get("limit") or "")), ("Down", d["down"] if d["nodes"] else None),
                         ("Free mem", d["mem_free"] if d["nodes"] else None)):
                if v is not None:
                    it.setData(self.INFO_COLS.index(k), SortItem.SORT_ROLE, float(v))
            w.table.addTopLevelItem(it)
        apply_sort(w.table)
        w.table.verticalScrollBar().setValue(vbar)
        # queue of everyone (only when the cluster lets us see other users' jobs)
        qi = w.inner.indexOf(w.queue)
        w.inner.setTabVisible(qi, not i["private"])
        if i["private"]:
            return
        n_r = sum(1 for q in i["queue"] if q["state"] == "RUNNING")
        n_pd = sum(1 for q in i["queue"] if q["state"] == "PENDING")
        w.inner.setTabText(qi, f"Queue of everyone ({n_r} R, {n_pd} PD)")
        vbar = w.queue.verticalScrollBar().value()
        w.queue.clear()
        bold = w.queue.font(); bold.setBold(True)
        for q in i["queue"][:5000]:
            g = q.get("gpu", "")
            if g.startswith(q["reason"] + " · ") and ", " not in g:     # the node is already in the last column
                g = g[len(q["reason"]) + 3:]
            pend = q["state"] == "PENDING"
            it = SortItem([q["id"], q["user"], q["part"], q["name"], SLURM_CODE.get(q["state"], q["state"]),
                           q["used"] if not pend else "", q["limit"], q["nodes"],
                           str(q["cpus"]) if q.get("cpus") else "", fmt_mb(q.get("mem")), g, q["reason"].strip("()")])
            for c, v in (("CPUs", q.get("cpus")), ("Mem", q.get("mem")), ("Time", slurm_secs(q["used"]) if not pend
                                                                          else None), ("Limit", slurm_secs(q["limit"]))):
                if v is not None:
                    it.setData(self.QUEUE_COLS.index(c), SortItem.SORT_ROLE, float(v))
            if q["mine"]:
                for k in range(len(self.QUEUE_COLS)):
                    it.setFont(k, bold)
            if pend:
                for k in range(len(self.QUEUE_COLS)):
                    it.setForeground(k, GREY)
            k = self.QUEUE_COLS.index("Mem")
            it.setToolTip(k, "Memory given to the job" if not pend else "Memory the job asks for")
            w.queue.addTopLevelItem(it)
        apply_sort(w.queue)        # usual order: that of squeue (running first)
        w.queue.verticalScrollBar().setValue(vbar)

    # ── files of a cluster ("Scripts" tab of its status panel, grouped by category; they open in the log viewer,
    # editable; Slurm scripts can be submitted and bash scripts run) ──
    SCRIPT_COLS = ["File", "Job name", "Partition", "Time", "CPUs", "Mem", "GPUs", "Array", "Modified", "Folder"]

    def scripts_tab(self, w):
        box = QWidget(); v = QVBoxLayout(box); v.setContentsMargins(0, 4, 0, 0)
        w.scripts_lbl = QLabel(""); w.scripts_lbl.setWordWrap(True)
        dirs = QPushButton("Folders and file types…")
        dirs.setToolTip("Choose where to look and which kinds of files to list")
        dirs.clicked.connect(lambda: self.script_folders(self.cluster(w._cluster)))
        again = QPushButton("Rescan"); again.setToolTip("Look for the files again")
        again.clicked.connect(lambda: self.fetch_scripts(self.cluster(w._cluster)))
        bar = QHBoxLayout(); bar.addWidget(w.scripts_lbl, 1); bar.addWidget(dirs); bar.addWidget(again)
        t = w.scripts = QTreeWidget(); t.setHeaderLabels(self.SCRIPT_COLS)
        t.setUniformRowHeights(True); t.setFont(self.run_c.font())
        t.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos: self.script_menu(w, pos))
        t.setExpandsOnDoubleClick(False)
        sortable(t, lambda w=w: self.redraw_scripts(w), (self.SCRIPT_COLS.index("Modified"), Qt.SortOrder.DescendingOrder))
        for k, tip in (("Time", "Time limit (#SBATCH --time)"), ("CPUs", "Tasks × CPUs per task"),
                       ("Mem", "Memory asked for (--mem, or --mem-per-cpu)"), ("GPUs", "GPUs asked for (--gres / --gpus)")):
            t.headerItem().setToolTip(self.SCRIPT_COLS.index(k), tip)

        def clicked(it, new=False):
            p = it.data(0, Qt.ItemDataRole.UserRole)
            if p:
                self.open_info(p, new=new)
            elif not new:
                it.setExpanded(not it.isExpanded())
        t.itemClicked.connect(lambda it, _: clicked(it))
        t.itemDoubleClicked.connect(lambda it, _: clicked(it, True))
        t.itemExpanded.connect(lambda it: w._folded.discard(it.data(0, Qt.ItemDataRole.UserRole + 1)))
        t.itemCollapsed.connect(lambda it: w._folded.add(it.data(0, Qt.ItemDataRole.UserRole + 1)))
        t.setToolTip("Click: open in the viewer (editable; Ctrl+S saves it on the cluster) · double click: in a new "
                     "tab · right-click: submit / run it")
        w._scripts_shown, w._folded, w._seen_cats = None, set(), set()
        v.addLayout(bar); v.addWidget(t, 1)
        return box

    def script_folders(self, c):
        if not c:
            return
        d = ScriptFoldersDialog(self, c)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        c["script_dirs"], c["script_auto"] = d.value()
        save_clusters(self.clusters)
        types = d.type_values()
        if types != FILE_TYPES:
            save_file_types(types)
            for x in self.clusters:               # the other lists already read: with the new types too
                st = self.cstate.get(x["name"])
                if x is not c and st and st.scripts is not None:
                    self.fetch_scripts(x)
        self.fetch_scripts(c)

    def fetch_scripts(self, c):
        s = self.cstate.get(c["name"]) if c else None
        if not s or s.scripts_busy:
            return
        s.scripts_busy = True

        def run():
            try:
                dirs = [(d, SCRIPT_DEPTH) for d in c.get("script_dirs", [])]
                if c.get("script_auto", True):
                    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - cset(c, "hist_days") * 86400))
                    ok, out, err = ssh_run(c["host"], job_dirs_script(since), timeout=60, pre=c.get("pre", ""))
                    if ok:
                        dirs += [(d, 1) for d in parse_job_dirs(out)]
                    elif not dirs:
                        raise OSError(err)
                if dirs:
                    ok, out, err = ssh_run(c["host"], scan_script(dirs), timeout=120, pre=c.get("pre", ""))
                    if not ok:
                        raise OSError(err)
                    s.scripts = parse_scan(out)
                else:
                    s.scripts = []
                s.scripts_err, s.scripts_ndirs = "", len(dirs)
            except Exception as e:
                s.scripts_err = str(e) or "no connection"
            finally:
                s.scripts_t, s.scripts_busy = time.time(), False
        threading.Thread(target=run, daemon=True).start()

    def redraw_scripts(self, w):
        w._scripts_shown = None; self.render_scripts(w)

    def render_scripts(self, w):
        name = w._cluster; c = self.cluster(name); s = self.cstate.get(name)
        if not c or not s:
            return
        stamp = (s.scripts_t, s.scripts_busy, s.scripts_err)
        if w._scripts_shown == stamp:
            return
        w._scripts_shown = stamp
        lst = s.scripts or []
        msg = ""
        if s.scripts_busy:
            msg = "Looking for files…"
        elif s.scripts_err:
            msg = f"<span style='color:#d04040'>could not look for files: {html.escape(s.scripts_err[:200])}</span>"
        elif s.scripts is not None and not lst:
            msg = ("Nothing found.  Use <b>Folders and file types…</b> to say where your scripts are."
                   if not c.get("script_dirs") else "Nothing found in those folders.")
        elif s.scripts is not None:
            when = time.strftime("%H:%M", time.localtime(s.scripts_t))
            msg = (f"{len(lst)} files in {getattr(s, 'scripts_ndirs', 0)} folders  ·  searched at {when}  ·  "
                   f"click one to open it")
        w.scripts_lbl.setText(msg)
        if s.scripts is None:
            return
        t = w.scripts; vbar = t.verticalScrollBar().value()
        t.blockSignals(True); t.clear()
        bold = t.font(); bold.setBold(True)
        order = [x["name"] for x in FILE_TYPES]
        cols = {c: k for k, c in enumerate(self.SCRIPT_COLS)}
        for rank, cat in enumerate(order + sorted({d["cat"] for d in lst} - set(order))):
            rows = [d for d in lst if d["cat"] == cat]
            if not rows:
                continue
            g = SortItem([f"{cat}   ·   {len(rows)}"] + [""] * (len(self.SCRIPT_COLS) - 1))
            g.setFont(0, bold); g.setData(0, Qt.ItemDataRole.UserRole + 1, cat); g.setData(0, SortItem.RANK_ROLE, rank)
            t.addTopLevelItem(g)
            if cat not in w._seen_cats:              # first time: long groups start folded
                w._seen_cats.add(cat)
                if len(rows) > 25:
                    w._folded.add(cat)
            for d in rows:
                o = d["opts"]
                txt, key = sbatch_columns(o)
                it = SortItem([d["rel"], o.get("job-name", ""), o.get("partition", ""), o.get("time", ""), txt["CPUs"],
                               txt["Mem"], txt["GPUs"], txt["Array"],
                               time.strftime("%Y-%m-%d %H:%M", time.localtime(d["mtime"])), d["dir"]])
                key["Time"], key["Modified"] = sbatch_minutes(o.get("time")), d["mtime"]
                for c, v in key.items():
                    if v is not None:
                        it.setData(cols[c], SortItem.SORT_ROLE, float(v))
                it.setToolTip(0, d["path"])
                it.setData(0, Qt.ItemDataRole.UserRole, self.script_info(name, d["path"], d["run"]))
                g.addChild(it)
            g.setExpanded(cat not in w._folded)
        apply_sort(t)                                 # by the column the user chose, or newest first
        t.blockSignals(False)
        t.verticalScrollBar().setValue(vbar)

    def script_menu(self, w, pos):
        it = w.scripts.itemAt(pos)
        p = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        if not p:
            return
        menu = QMenu(self)
        acts = {menu.addAction("Open"): lambda: self.open_info(p),
                menu.addAction("Open in a new tab"): lambda: self.open_info(p, new=True)}
        menu.addSeparator()
        if p.get("run") in self.RUN_LABEL:
            a = menu.addAction(self.RUN_LABEL[p["run"]]); acts[a] = lambda: self.run_script(p)
            a.setEnabled(p["key"] not in self._running_scripts)
        acts[menu.addAction("Copy path")] = lambda: QApplication.clipboard().setText(p["path"])
        a = menu.exec(w.scripts.viewport().mapToGlobal(pos))
        if a in acts:
            acts[a]()
