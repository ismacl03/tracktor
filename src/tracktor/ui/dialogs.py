"""Dialogs: add / edit a cluster, folders and file types of the Scripts tab, settings."""
import html, os, threading

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QSpinBox,
                             QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..config import (CLUSTER_EVERY_DEFAULT, CLUSTER_SETTINGS, CLUSTERS_FILE, DEFAULTS, FILE_TYPES, FILE_TYPES_DEFAULT,
                      RUN_ACTIONS, S, SCRIPT_DEPTH, SETTINGS_FILE, cset, type_exts)
from ..ssh import bad_host, ssh_config_hosts, ssh_run
from .common import INFO_COLS, QUEUE_COLS, english_buttons, tell


# ───────────────────────────── add / edit cluster dialog ─────────────────────────────
class ClusterDialog(QDialog):
    def __init__(self, parent, existing=None, taken=()):
        super().__init__(parent)
        self.setWindowTitle("Edit cluster" if existing else "Add cluster")
        self.taken = set(taken)
        lay = QVBoxLayout(self)
        form = QFormLayout(); lay.addLayout(form)
        self.name = QLineEdit(existing["name"] if existing else "")
        self.name.setPlaceholderText("e.g. Group cluster")
        self.host = QComboBox(); self.host.setEditable(True)
        self.host.addItems(ssh_config_hosts())
        self.host.setCurrentText(existing["host"] if existing else "")
        self.host.lineEdit().setPlaceholderText("alias from ~/.ssh/config or user@server")
        self.host.currentTextChanged.connect(self.host_changed)
        help_ = QLabel("Tracktor uses your normal <b>ssh</b>, so any <i>Host</i> from ~/.ssh/config (the list) or "
                       "<i>user@server</i> works.  Port, key, ProxyJump, etc. are set in ~/.ssh/config.  If the "
                       "cluster asks for a password, key passphrase or 2FA, right-click the cluster &gt; "
                       "<b>Connect…</b> afterwards.")
        help_.setWordWrap(True)
        self.result_lbl = QLabel(""); self.result_lbl.setWordWrap(True)
        test = QPushButton("Test connection"); test.clicked.connect(self.test)
        form.addRow("Name:", self.name)
        form.addRow("SSH host:", self.host)
        self.pre = QLineEdit(existing.get("pre", "") if existing else "")
        self.pre.setPlaceholderText("optional, e.g.  module load slurm   (only if squeue is not found)")
        form.addRow("Setup commands:", self.pre)
        self.every = QSpinBox(); self.every.setRange(10, 3600); self.every.setSuffix(" s")
        self.every.setValue(int(existing.get("every", CLUSTER_EVERY_DEFAULT)) if existing else CLUSTER_EVERY_DEFAULT)
        self.every.setToolTip("How often squeue/sacct are queried.  Querying very often loads the login node.")
        form.addRow("Poll every:", self.every)
        lay.addWidget(help_)
        row = QHBoxLayout(); row.addWidget(test, 0, Qt.AlignmentFlag.AlignTop); row.addWidget(self.result_lbl, 1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        english_buttons(bb); bb.accepted.connect(self.check_accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.setMinimumWidth(620)
        self._test_res = None
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll_test)

    def host_changed(self, h):
        if not self.name.text().strip() or self.name.text() == getattr(self, "_auto_name", None):
            self._auto_name = h.split("@")[-1].split(".")[0]
            self.name.setText(self._auto_name)

    def test(self):
        host, pre = self.host.currentText().strip(), self.pre.text()
        if not host:
            return
        self.result_lbl.setText("Testing…"); self._test_res = None

        def run():
            ok, out, err = ssh_run(host, "squeue --version 2>&1 || echo NO_SLURM", timeout=30, pre=pre)
            self._test_res = (ok, out.strip(), err)
        threading.Thread(target=run, daemon=True).start()
        self.timer.start(300)

    def poll_test(self):
        if self._test_res is None:
            return
        self.timer.stop()
        ok, out, err = self._test_res
        QTimer.singleShot(0, self.adjustSize)
        if not ok:
            self.result_lbl.setText(f"<span style='color:#d04040'>Cannot connect without interaction: {err}</span><br>"
                                    "That is normal if the cluster asks for a password, key passphrase or 2FA: press "
                                    "<b>OK</b> and then right-click the cluster &gt; <b>Connect…</b>.")
        elif "NO_SLURM" in out or "not found" in out:
            self.result_lbl.setText("<span style='color:#d04040'>Connected, but Slurm (squeue) was not found.</span>")
        else:
            self.result_lbl.setText(f"<span style='color:#2a9d3a'>Connected. {out.splitlines()[-1] if out else ''}</span>")

    def check_accept(self):
        n, h = self.name.text().strip(), self.host.currentText().strip()
        if not n or not h:
            tell(self, "Missing data", "Please enter a name and an SSH host."); return
        if bad_host(h):
            tell(self, "SSH host", "The SSH host cannot start with «-» or contain spaces."); return
        if n == "Local" or n in self.taken:
            tell(self, "Name in use", f"There is already a group called «{n}»."); return
        self.accept()

    def value(self):
        return dict(name=self.name.text().strip(), host=self.host.currentText().strip(), pre=self.pre.text().strip(),
                    every=self.every.value())


class ScriptFoldersDialog(QDialog):
    """Where the "Scripts" tab of a cluster looks for batch scripts."""

    def __init__(self, parent, c):
        super().__init__(parent)
        self.setWindowTitle(f"Folders and file types · {c['name']}"); self.resize(640, 560)
        lay = QVBoxLayout(self)
        lab = QLabel(f"<b>Folders</b> on {html.escape(c['name'])} where to look, one per line (<tt>~/</tt> = your "
                     f"home there).  They are searched {SCRIPT_DEPTH} levels deep, skipping hidden folders.")
        lab.setWordWrap(True); lay.addWidget(lab)
        self.edit = QPlainTextEdit("\n".join(c.get("script_dirs", [])))
        self.edit.setPlaceholderText("~/project/scripts\n/scratch/me/runs")
        lay.addWidget(self.edit, 1)
        self.auto = QCheckBox(f"Also look in the folders of my jobs of the last {cset(c, 'hist_days')} days "
                              f"(only that folder, not below)")
        self.auto.setChecked(c.get("script_auto", True)); lay.addWidget(self.auto)
        lab = QLabel("<b>File types</b> (for all clusters): each category is a group of the list.  Extensions "
                     "separated by spaces.  Any listed file with <tt>#SBATCH</tt> lines goes to the sbatch "
                     "category.")
        lab.setWordWrap(True); lay.addWidget(lab)
        self.types = QTableWidget(0, 3); self.types.setHorizontalHeaderLabels(["Category", "Extensions", "Action"])
        self.types.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.types.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.types.verticalHeader().setVisible(False)
        for t in FILE_TYPES:
            self.add_type(t)
        lay.addWidget(self.types, 1)
        add = QPushButton("+ Add category"); add.clicked.connect(lambda: self.add_type(dict(name="", exts="", run="")))
        rm = QPushButton("− Remove selected"); rm.clicked.connect(lambda: self.types.removeRow(self.types.currentRow()))
        dflt = QPushButton("Defaults"); dflt.clicked.connect(self.default_types)
        row = QHBoxLayout(); row.addWidget(add); row.addWidget(rm); row.addStretch(1); row.addWidget(dflt)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        english_buttons(bb); bb.accepted.connect(self.accept); bb.rejected.connect(self.reject); lay.addWidget(bb)

    def add_type(self, t):
        r = self.types.rowCount(); self.types.insertRow(r)
        self.types.setItem(r, 0, QTableWidgetItem(t["name"])); self.types.setItem(r, 1, QTableWidgetItem(t["exts"]))
        cb = QComboBox()
        for k, v in RUN_ACTIONS.items():
            cb.addItem(v, k)
        cb.setCurrentIndex(list(RUN_ACTIONS).index(t.get("run", "")))
        self.types.setCellWidget(r, 2, cb)
        if not t["name"]:
            self.types.setCurrentCell(r, 0); self.types.editItem(self.types.item(r, 0))

    def default_types(self):
        self.types.setRowCount(0)
        for t in FILE_TYPES_DEFAULT:
            self.add_type(t)

    def value(self):
        dirs = [l.strip() for l in self.edit.toPlainText().splitlines() if l.strip()]
        return list(dict.fromkeys(dirs)), self.auto.isChecked()

    def type_values(self):
        out = []
        for r in range(self.types.rowCount()):
            n, e = self.types.item(r, 0), self.types.item(r, 1)
            t = dict(name=(n.text().strip() if n else ""), exts=(e.text().strip() if e else ""),
                     run=self.types.cellWidget(r, 2).currentData())
            if t["name"] and type_exts(t) and t["name"] not in [x["name"] for x in out]:
                out.append(t)
        return out


# ───────────────────────────── settings ─────────────────────────────
SECTIONS = [   # sections of the settings dialog (left sidebar)
    ("General", ["tray", "tray_notice", "log_font", "wrap_log", "tail_kb"]),
    ("Local", ["local_every"]),
    ("Clusters", ["show_pending", "show_gpus", "close_on_exit"]),
    ("Notifications", ["notify_local", "notify_cluster", "notify_ok", "notify_start"]),
]


class SettingsDialog(QDialog):
    def __init__(self, parent, clusters=()):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        body = QHBoxLayout(); lay.addLayout(body, 1)
        self.side = QListWidget(); self.side.setFixedWidth(150)
        self.pages = QStackedWidget()
        body.addWidget(self.side); body.addWidget(self.pages, 1)
        self.w, self.cl_vals = {}, {}
        for title, keys in SECTIONS:
            page = QWidget(); pl = QVBoxLayout(page); form = QFormLayout(); pl.addLayout(form)
            form.addRow(QLabel(f"<b>{title}</b>"))
            if title == "Clusters":
                if clusters:
                    # one cluster at a time: pick it in the drop-down, its own settings appear below
                    self.cl_vals = {c["name"]: dict({k: cset(c, k) for k in CLUSTER_SETTINGS},
                                                    queue_hide=list(c.get("queue_hide", [])),
                                                    parts_hide=list(c.get("parts_hide", []))) for c in clusters}
                    self.cl_combo = QComboBox(); self.cl_combo.addItems(list(self.cl_vals))
                    self.cl_combo.setMaximumWidth(260)
                    form.addRow("Cluster:", self.cl_combo)
                    self.cl_spins = {}
                    for k, (dv, label, lo, hi, unit, tip) in CLUSTER_SETTINGS.items():
                        w = QSpinBox(); w.setRange(lo, hi); w.setSuffix(f" {unit}"); w.setMaximumWidth(140)
                        w.setToolTip(tip); form.addRow(f"      {label}:", w); self.cl_spins[k] = w
                    # which columns its status panel shows (the first one of each table always stays)
                    self.cl_cols = {}
                    for key, title_, cols in (("queue_hide", "Queue columns", QUEUE_COLS[1:]),
                                              ("parts_hide", "Partition columns", INFO_COLS[1:])):
                        box = QWidget(); grid = QGridLayout(box); grid.setContentsMargins(0, 0, 0, 0)
                        grid.setHorizontalSpacing(14); grid.setVerticalSpacing(2)
                        for n, col in enumerate(cols):
                            cb = QCheckBox(col); grid.addWidget(cb, n // 4, n % 4)
                            self.cl_cols[(key, col)] = cb
                        form.addRow(f"      {title_}:", box)
                    self._cl_cur = None
                    self.cl_combo.currentTextChanged.connect(self.cluster_picked)
                    self.cluster_picked(self.cl_combo.currentText())
                    form.addRow(QLabel(""))
                    form.addRow(QLabel("<b>All clusters</b>"))
                else:
                    form.addRow(QLabel("<i>No clusters yet (add them with the + button).</i>"))
            for k in keys:
                d = DEFAULTS[k]
                if isinstance(d[0], bool):
                    w = QCheckBox(d[1]); w.setChecked(S[k]); form.addRow(w)
                else:
                    w = QSpinBox(); w.setRange(d[2], d[3]); w.setValue(S[k]); w.setSuffix(f" {d[4]}")
                    w.setMaximumWidth(140); form.addRow(d[1] + ":", w)
                self.w[k] = w
            pl.addStretch(1)
            self.pages.addWidget(page); self.side.addItem(title)
        self.side.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.side.setCurrentRow(0)
        self.w["tray_notice"].setEnabled(S["tray"])
        self.w["tray"].toggled.connect(self.w["tray_notice"].setEnabled)
        self.w["notify_ok"].setEnabled(S["notify_local"] or S["notify_cluster"])
        for k in ("notify_local", "notify_cluster"):
            self.w[k].toggled.connect(lambda _: self.w["notify_ok"].setEnabled(
                self.w["notify_local"].isChecked() or self.w["notify_cluster"].isChecked()))
        note = QLabel(f"<small>Saved in {SETTINGS_FILE.replace(os.path.expanduser('~'), '~')} "
                      f"(per-cluster settings in {CLUSTERS_FILE.replace(os.path.expanduser('~'), '~')}).</small>")
        note.setWordWrap(True); lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                              | QDialogButtonBox.StandardButton.RestoreDefaults)
        english_buttons(bb); bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.defaults)
        lay.addWidget(bb)

    def defaults(self):
        for k, d in DEFAULTS.items():
            (self.w[k].setChecked if isinstance(d[0], bool) else self.w[k].setValue)(d[0])
        for v in self.cl_vals.values():
            v.update({k: d[0] for k, d in CLUSTER_SETTINGS.items()}, queue_hide=[], parts_hide=[])
        if self.cl_vals:
            self._cl_cur = None; self.cluster_picked(self.cl_combo.currentText())

    def cluster_picked(self, name):
        self.save_cluster_fields()
        self._cl_cur = name
        if name in self.cl_vals:
            for k, w in self.cl_spins.items():
                w.setValue(self.cl_vals[name][k])
            for (key, col), cb in self.cl_cols.items():
                cb.setChecked(col not in self.cl_vals[name][key])

    def save_cluster_fields(self):
        if self._cl_cur in self.cl_vals:
            v = self.cl_vals[self._cl_cur]
            v.update({k: w.value() for k, w in self.cl_spins.items()})
            for key in ("queue_hide", "parts_hide"):
                v[key] = [col for (k, col), cb in self.cl_cols.items() if k == key and not cb.isChecked()]

    def values(self):
        return {k: (w.isChecked() if isinstance(w, QCheckBox) else w.value()) for k, w in self.w.items()}

    def cluster_values(self):
        """-> {cluster name: {setting: value}} (see CLUSTER_SETTINGS)"""
        if self.cl_vals:
            self.save_cluster_fields()
        return self.cl_vals
