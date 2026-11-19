"""The main window: job tables on the left, log viewer on the right, cluster status below."""
import os, time

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
                             QMainWindow, QMenu, QPlainTextEdit, QPushButton, QSplitter, QSystemTrayIcon, QTabBar,
                             QTabWidget, QVBoxLayout, QWidget)

from ..config import APP, S, cset, load_clusters, save_clusters, save_settings
from ..local import fmt_dt, history, local_exit_text, meminfo, running_jobs
from ..slurm import array_task_count, ClusterState, slurm_end_text
from ..ssh import close_all_masters, kill_active
from . import common
from .clusters import ClustersMixin
from .common import NO_SEL, RED, app_icon, ask, LogHighlighter, notify, taskbar_count, tray_icon, tree
from .dialogs import SettingsDialog
from .editor import EditorMixin
from .logview import LogViewMixin
from .status_panel import StatusPanelMixin
from .tables import TablesMixin


class Main(ClustersMixin, LogViewMixin, EditorMixin, StatusPanelMixin, TablesMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP)
        self.setWindowIcon(app_icon())
        self.resize(1400, 760)
        self.sel = None                       # payload of the selected job
        self.clusters = load_clusters()
        self.cstate = {c["name"]: ClusterState() for c in self.clusters}
        self.open_groups = set()              # (tab, group) expanded; everything starts collapsed
        self.open_arrays = set()              # (tab, key) of expanded Slurm arrays
        self.prev_ticks = {}                  # local job -> (CPU ticks, time) to compute CPU %
        mono = QFont("Monospace"); mono.setStyleHint(QFont.StyleHint.Monospace)

        self.status = QLabel()
        add = QPushButton("+"); add.setToolTip("Add a Slurm cluster (over SSH)"); add.setFixedWidth(36)
        add.clicked.connect(self.add_cluster)
        gear = QPushButton("⚙"); gear.setToolTip("Settings"); gear.setFixedWidth(36)
        gear.clicked.connect(self.open_settings)
        top = QHBoxLayout(); top.addWidget(self.status, 1); top.addWidget(add); top.addWidget(gear)

        self.tabs = QTabWidget()
        # "Running": the local table (with its column bar) on top, the clusters table (with its own) below.
        cols = ["Job", "Time", "RAM", "CPU", "GPU", "State", "Est. start", "Partition"]
        self.run_l, self.run_c = tree(cols), tree(cols)
        self.run_l.headerItem().setToolTip(2, "Memory (RSS) of the job and all its child processes")
        self.run_l.headerItem().setToolTip(3, f"CPU cores the job keeps busy (1.0 = one core at 100 %); "
                                              f"this machine has {os.cpu_count()}")
        self.run_l.headerItem().setToolTip(4, "GPU memory used by the job (nvidia-smi); — = it is not using the GPU")
        self.run_c.header().moveSection(7, 1)     # clusters: Job · Partition · Time · State · Est. start
        for t, hide in ((self.run_l, [6, 7]), (self.run_c, [2, 3, 4])):
            t.setFont(mono); t._color_col = 5
            for c in hide:
                t.setColumnHidden(c, True)
        run_top = QWidget(); rl0 = QVBoxLayout(run_top); rl0.setContentsMargins(0, 0, 0, 0); rl0.setSpacing(6)
        rl0.addWidget(self.run_l); rl0.addWidget(self.run_c, 1)
        # cluster status panel, always shown under the job tables: one tab per cluster, plus a last "+" tab that
        # offers the clusters to open (also: right-click a cluster > Show cluster status)
        self.cinfo = QTabWidget(); self.cinfo.setTabsClosable(True); self.cinfo.setDocumentMode(True)
        self.cinfo.tabCloseRequested.connect(self.close_cluster_info)
        self.cinfo_plus = QLabel("Click  <b>+</b>  to see the status of a cluster: free nodes, CPUs and GPUs per "
                                 "partition, the queue, and your scripts.")
        self.cinfo_plus.setAlignment(Qt.AlignmentFlag.AlignCenter); self.cinfo_plus.setWordWrap(True)
        self.cinfo_plus._cluster = None
        self.cinfo.addTab(self.cinfo_plus, " + ")
        self.cinfo.setTabToolTip(0, "Open the status of a cluster")
        self.cinfo.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        self.cinfo.tabBar().setTabButton(0, QTabBar.ButtonPosition.LeftSide, None)
        self.cinfo.tabBar().tabBarClicked.connect(self.cinfo_clicked)
        self.cinfo.currentChanged.connect(self.cinfo_changed)
        self.run_split = QSplitter(Qt.Orientation.Vertical); self.run_split.setChildrenCollapsible(False)
        self.run_split.addWidget(run_top); self.run_split.addWidget(self.cinfo)
        self.run_split.setStretchFactor(0, 3); self.run_split.setStretchFactor(1, 2)
        self.tabs.addTab(self.run_split, "Running")
        hist_w = QWidget(); hl = QVBoxLayout(hist_w); hl.setContentsMargins(0, 0, 0, 0)
        hcols = ["Job", "Partition", "Ended", "Duration", "Result"]
        self.hist_l, self.hist_c = tree(hcols), tree(hcols)
        self.hist_l.setColumnHidden(1, True)              # local jobs have no partition
        self.hist_l.setFont(mono); self.hist_c.setFont(mono)
        # ✓ / 🗑 buttons floating at the right end of the row (or group) under the mouse
        self.hover_box = None; self._hover = None
        for t in (self.hist_l, self.hist_c):
            t.viewport().installEventFilter(self)
            t.verticalScrollBar().valueChanged.connect(lambda _: self.unhover())
        clear = QPushButton("✓  Clear history"); clear.clicked.connect(lambda: self.clear_history(False))
        clear.setToolTip("Remove all finished jobs from the list.  Their logs are kept.")
        wipe = QPushButton("🗑  Delete history"); wipe.clicked.connect(lambda: self.clear_history(True))
        wipe.setToolTip("Remove all finished jobs from the list AND delete their logs: temporary local logs and the "
                        ".out/.err on the clusters.  Logs saved with -l are never deleted.")
        hbtns = QHBoxLayout(); hbtns.addWidget(clear); hbtns.addWidget(wipe)
        self.hsearch = QLineEdit(); self.hsearch.setPlaceholderText("Search the history (name, job id…)")
        self.hsearch.setClearButtonEnabled(True); self.hsearch.textChanged.connect(lambda _: self.refresh())
        self.hfilter = QComboBox(); self.hfilter.addItems(["All", "Errors only", "OK only"])
        self.hfilter.currentIndexChanged.connect(lambda _: self.refresh())
        sbar = QHBoxLayout(); sbar.addWidget(self.hsearch, 1); sbar.addWidget(self.hfilter)
        hl.addLayout(sbar); hl.addWidget(self.hist_l); hl.addWidget(self.hist_c, 1); hl.addLayout(hbtns)
        self.tabs.addTab(hist_w, "History")
        for t, name in ((self.run_l, "run"), (self.run_c, "run"), (self.hist_l, "hist"), (self.hist_c, "hist")):
            t.currentItemChanged.connect(lambda it, _, t=t: self.on_current(t, it))
            t.itemClicked.connect(lambda it, col: self.open_info(it.data(0, Qt.ItemDataRole.UserRole)))
            t.itemDoubleClicked.connect(lambda it, col: self.open_info(it.data(0, Qt.ItemDataRole.UserRole), new=True))
            t.customContextMenuRequested.connect(lambda pos, t=t: self.context_menu(t, pos))
            t.itemCollapsed.connect(lambda it, n=name: self.on_fold(n, it, True))
            t.itemExpanded.connect(lambda it, n=name: self.on_fold(n, it, False))
            t.itemClicked.connect(self.group_clicked)
            t.setExpandsOnDoubleClick(False)
            t.header().sectionClicked.connect(lambda col, t=t: self.sort_clicked(t, col))
            t.header().setToolTip("Click a column to sort by it (again: reverse; a third time: back to the usual order)")
        self.tabs.currentChanged.connect(lambda _: self.refresh())

        self.title = QLabel(NO_SEL); self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.out = QPlainTextEdit(); self.out.setReadOnly(True); self.out.setFont(mono)
        self.hl = LogHighlighter(self.out.document())
        # open logs as tabs, like a browser: click = show it in the current tab, double click = in a new tab
        self.logtabs = QTabBar(); self.logtabs.setTabsClosable(True); self.logtabs.setMovable(True)
        self.logtabs.setExpanding(False); self.logtabs.setDocumentMode(True); self.logtabs.setUsesScrollButtons(True)
        self.logtabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.logtabs.currentChanged.connect(self.tab_changed)
        self.logtabs.tabCloseRequested.connect(self.close_tab)
        self.tabdata, self._tab_seq, self._cur_tab, self._last_replace = {}, 0, None, None
        QShortcut(QKeySequence("Ctrl+W"), self, activated=lambda: self.close_tab(self.logtabs.currentIndex()))
        # log search (Ctrl+F): Enter next, Shift+Enter previous, Esc close
        self.findbar = QWidget(); fb = QHBoxLayout(self.findbar); fb.setContentsMargins(0, 0, 0, 0)
        self.find_edit = QLineEdit(); self.find_edit.setPlaceholderText("Search the log   (Enter: next · "
                                                                        "Shift+Enter: previous · Esc: close)")
        self.find_count = QLabel("")
        fb.addWidget(self.find_edit, 1); fb.addWidget(self.find_count)
        for txt, tip, fn in (("↑", "Previous (Shift+Enter)", lambda: self.find_step(-1)),
                             ("↓", "Next (Enter)", lambda: self.find_step(1)), ("✕", "Close (Esc)", self.close_find)):
            b = QPushButton(txt); b.setFixedWidth(32); b.setToolTip(tip); b.clicked.connect(fn); fb.addWidget(b)
        self.findbar.hide()
        self.matches, self.find_idx = [], -1
        self.find_edit.textChanged.connect(lambda _: self.update_find(jump=True))
        self.find_edit.returnPressed.connect(lambda: self.find_step(1))
        QShortcut(QKeySequence("Shift+Return"), self.find_edit, activated=lambda: self.find_step(-1),
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("Escape"), self.findbar, activated=self.close_find,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.open_find)

        self.stream, self._title_paths, self._title_eff = "out", None, ""
        self.out_btn = QPushButton("Output (.out)"); self.err_btn = QPushButton("Errors (.err)")
        grp = QButtonGroup(self); grp.setExclusive(True)
        for b, st in ((self.out_btn, "out"), (self.err_btn, "err")):
            b.setCheckable(True); b.setVisible(False); grp.addButton(b)
            b.clicked.connect(lambda _, st=st: self.set_stream(st))
        self.out_btn.setChecked(True)
        # batch scripts opened from a cluster's "Scripts" tab: editable, saved back over SSH
        self._loading = False
        self.save_btn = QPushButton("Save"); self.save_btn.setToolTip("Save it on the cluster (Ctrl+S)")
        self.save_btn.clicked.connect(lambda: self.save_script())
        self.run_btn = QPushButton("sbatch…")
        self.run_btn.clicked.connect(lambda: self.cur_script() and self.run_script(self.cur_script()["info"]))
        self._running_scripts = set()
        self.reload_btn = QPushButton("⟳"); self.reload_btn.setFixedWidth(32)
        self.reload_btn.setToolTip("Read it again from the cluster")
        self.reload_btn.clicked.connect(self.reload_script)
        for b in (self.save_btn, self.run_btn, self.reload_btn):
            b.setVisible(False)
        self.out.textChanged.connect(self.on_text_edited)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=lambda: self.cur_script() and self.save_script())
        head = QHBoxLayout(); head.addWidget(self.title, 1)
        for b in (self.reload_btn, self.save_btn, self.run_btn):
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.out_btn, 0, Qt.AlignmentFlag.AlignTop); head.addWidget(self.err_btn, 0, Qt.AlignmentFlag.AlignTop)
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.logtabs); rl.addLayout(head); rl.addWidget(self.findbar); rl.addWidget(self.out)

        split = QSplitter(); split.addWidget(self.tabs); split.addWidget(right); split.setSizes([700, 550])
        central = QWidget(); cl = QVBoxLayout(central); cl.addLayout(top); cl.addWidget(split, 1)
        self.setCentralWidget(central)

        self.tray, self._quitting, self._tray_n = None, False, None
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(S["local_every"] * 1000)
        self.prev_local, self.prev_cluster = None, {}    # to notify jobs that start / finish
        self.apply_settings()
        self.refresh()

    def refresh(self):
        for c in self.clusters:
            self.poll_cluster(c)
        jobs = running_jobs()
        self.check_finished(jobs)
        n_cl = sum(len(self.cstate[c["name"]].running) for c in self.clusters if c["name"] in self.cstate)
        self.update_tray(len(jobs), sum(1 for c in self.clusters if c["name"] in self.cstate
                                        for r in self.cstate[c["name"]].running if r["state"] != "PENDING"))

        # running
        rows, gstat = self.local_rows(jobs)
        mem = meminfo()
        self.status.setText(f"{time.strftime('%H:%M:%S')}    "
                            + (f"RAM {mem[0] / 2 ** 30:.1f}/{mem[1] / 2 ** 30:.1f} GB    " if mem else "")
                            + f"{gstat}    |    {len(jobs)} running locally"
                            + (f",  {n_cl} on clusters" if self.clusters else ""))
        if not rows and not self.clusters:
            rows.append((("none",), ["Nothing running.", "", "", "", "", "", "", ""], None, None))
        self.fill(self.run_l, "run", [("Local", f"Local   ·   {len(jobs)} running", rows)], flat=not self.clusters)
        groups = []
        for c in self.clusters:
            s = self.cstate[c["name"]]
            crow = self.cluster_run_rows(c, s)
            n_run = sum(1 for r in s.running if r["state"] != "PENDING")
            n_pd = sum(array_task_count(r["id"]) for r in s.running if r["state"] == "PENDING")
            groups.append((c["name"], self.cluster_head(c, f"{n_run} running, {n_pd} queued"), crow))
        self.run_c.setVisible(bool(self.clusters))
        if self.clusters:
            self.fill(self.run_c, "run", groups, flat=False)
        self.fit_local()

        # history
        rows = []
        for r in history():
            ex = r.get("exit", "?")
            if not self.hist_match(r.get("label", ""), r.get("cmd", ""), local_exit_text(ex), error=ex != 0):
                continue
            key = ("local", r.get("label"), r.get("end"))
            rows.append((key, [r.get("label", "?"), "", time.strftime("%d-%m %H:%M", time.localtime(r.get("end", 0))),
                               fmt_dt(r.get("end", 0) - r.get("start", 0)), local_exit_text(ex)],
                         dict(r, kind="local", key=key, sort={2: r.get("end", 0)}), RED if ex != 0 else None))
        if not rows and not self.clusters:
            rows.append((("none",), ["No finished jobs.", "", "", "", ""], None, None))
        self.update_cluster_info()
        self.fill(self.hist_l, "hist", [("Local", f"Local   ·   {len(rows)}", rows)], flat=not self.clusters)
        groups = []
        for c in self.clusters:
            s = self.cstate[c["name"]]
            crow = self.cluster_hist_rows(c, s)
            n = sum(len(r[4]) if len(r) > 4 else 1 for r in crow)
            groups.append((c["name"], self.cluster_head(c, f"{n} (last {cset(c, 'hist_days')} days)"), crow))
        self.hist_c.setVisible(bool(self.clusters))
        if self.clusters:
            self.fill(self.hist_c, "hist", groups, flat=False)
        self.fit_local()
        self.place_hover()
        self.refresh_tabs()
        self.show_log()

    # ── settings ──
    def apply_settings(self):
        self.timer.setInterval(S["local_every"] * 1000)
        self.run_c.setColumnHidden(4, not S["show_gpus"])        # clusters: GPU = node and GPU number(s)
        self.run_c.headerItem().setToolTip(4, "Node and GPU number(s) the job runs on (for queued jobs: how many "
                                              "GPUs it asks for)")
        self.setup_tray()
        f = self.out.font(); f.setPointSize(S["log_font"]); self.out.setFont(f)
        self.out.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if S["wrap_log"]
                                 else QPlainTextEdit.LineWrapMode.NoWrap)

    def open_settings(self):
        d = SettingsDialog(self, self.clusters)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        old = dict(S); S.update(d.values()); save_settings(); self.apply_settings()
        vals, changed, repoll = d.cluster_values(), False, set()
        for c in self.clusters:
            for k, v in vals.get(c["name"], {}).items():
                if c.get(k) != v:
                    if k == "hist_days" and cset(c, k) != v:
                        repoll.add(c["name"])
                    c[k] = v; changed = True
        if changed:
            save_clusters(self.clusters)
        if S["tail_kb"] != old["tail_kb"]:
            for st in self.cstate.values():
                st.logs.clear()
        for c in self.clusters:
            if c["name"] in repoll:
                self.poll_cluster(c, force=True)
        self.refresh()
        if self.sel:
            self.select(self.sel)

    # ── notifications ──
    def check_finished(self, jobs):
        now = {(j["label"], j["pid"]): j for j in jobs}
        if self.prev_local is not None and S["notify_local"]:
            gone = [j for k, j in self.prev_local.items() if k not in now]
            if gone:
                hist = history()
                for j in gone:
                    r = next((r for r in hist if r.get("label") == j["label"] and r.get("start") == j["start"]), None)
                    ex = r.get("exit") if r else None
                    if ex == 0 and not S["notify_ok"]:
                        continue
                    txt = local_exit_text(ex) if ex is not None else "finished"
                    notify(f"{j['label']}: {txt}", f"Local · {fmt_dt(time.time() - j['start'])}",
                           error=ex not in (0, None))
        self.prev_local = now
        for c in self.clusters:
            s = self.cstate.get(c["name"])
            if not s or not s.ok:
                continue
            prev = self.prev_cluster.get(c["name"])
            if prev is not None and prev[0] == s.t:
                continue          # no new query yet
            ids = {r["id"]: r for r in s.running}
            if prev is not None and S["notify_start"]:     # PD -> R (or an array task that just started)
                started = [r for jid, r in ids.items() if r["state"] == "RUNNING"
                           and (jid not in prev[1] or prev[1][jid]["state"] == "PENDING")]
                if 0 < len(started) <= 3:
                    for r in started:
                        notify(f"{r['name']}: started", f"{c['name']} · job {r['id']}"
                               + (f" · {r.get('part')}" if r.get("part") else ""))
                elif started:
                    names = ", ".join(dict.fromkeys(r["name"] for r in started))
                    notify(f"{c['name']}: {len(started)} jobs started", names[:200])
            waiting = prev[2] if prev else {}
            if prev is not None:
                for jid, r in prev[1].items():
                    if jid not in ids and r["state"] != "PENDING":
                        waiting[jid] = (r, 0)
            hist = {r["id"]: r for r in s.hist}
            for jid, (r, tries) in list(waiting.items()):
                h = hist.get(jid)
                if h or tries >= 3:
                    del waiting[jid]
                    txt = slurm_end_text(h["state"], h["exit"]) if h else "finished"
                    if S["notify_cluster"] and (txt != "ok" or S["notify_ok"]):
                        notify(f"{r['name']}: {txt}", f"{c['name']} · job {jid}", error=txt not in ("ok", "finished"))
                else:
                    waiting[jid] = (r, tries + 1)
            self.prev_cluster[c["name"]] = (s.t, ids, waiting)

    # ── system tray ──
    def setup_tray(self):
        want = S["tray"] and QSystemTrayIcon.isSystemTrayAvailable()
        if want and self.tray is None:
            self.tray = QSystemTrayIcon(tray_icon(0), self)
            m = QMenu(self)
            m.addAction("Show window").triggered.connect(self.show_window)
            m.addSeparator()
            m.addAction("Quit").triggered.connect(self.quit_app)
            self.tray.setContextMenu(m)
            self.tray.activated.connect(lambda r: self.toggle_window() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
            self.tray.setToolTip(APP); self.tray.show(); self._tray_n = None
        elif not want and self.tray is not None:
            self.tray.hide(); self.tray.deleteLater(); self.tray = None
        common.TRAY = self.tray        # for notify() without notify-send

    def update_tray(self, n_local, n_cluster):
        """Number of running jobs (queued cluster jobs not counted) on the tray icon and on the taskbar icon."""
        n = n_local + n_cluster
        if n != getattr(self, "_taskbar_n", None):
            taskbar_count(n); self._taskbar_n = n
        if self.tray is None:
            return
        if n != self._tray_n:
            self.tray.setIcon(tray_icon(n)); self._tray_n = n
        self.tray.setToolTip(f"{APP}: {n_local} running locally" + (f", {n_cluster} on clusters" if self.clusters else ""))

    def show_window(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def toggle_window(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_window()

    def quit_app(self):
        self._quitting = True
        self.close()

    # ── closing and history ──
    def closeEvent(self, ev):
        # With the tray: closing the window only hides it (still watching and notifying); "Quit" in its menu exits.
        if self.tray is not None and not self._quitting:
            self.hide(); ev.ignore()
            if S["tray_notice"]:
                self.tray.showMessage(f"{APP} keeps running in the background",
                                      "Still watching and notifying from the system tray.  To quit completely: "
                                      "right-click the icon > Quit.")
            return
        dirty = [d["info"]["label"] for d in self.tabdata.values() if self.script_dirty(d)]
        if dirty and not ask(self, "Quit", "These scripts have unsaved changes:\n\n  " + "\n  ".join(dirty)
                             + "\n\nQuit and lose them?"):
            self._quitting = False; ev.ignore(); return
        # When really quitting, with the option on: leave nothing behind (cut running queries and close the master
        # connections).  Without it, the connections stay open until the configured idle time has passed.
        if S["close_on_exit"]:
            kill_active()
            close_all_masters()
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(ev)
        QTimer.singleShot(0, QApplication.quit)
