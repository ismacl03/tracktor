"""Main window: the Running / History tables (groups, hover ✓ / 🗑, context menus, history)."""
import os, re, shlex, shutil, signal, subprocess, time

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QHBoxLayout, QMenu, QMessageBox, QToolButton, QTreeWidgetItem, QWidget

from ..config import S, save_clusters
from ..local import (CLK, alive, fmt_bytes, fmt_dt, gpu_info, job_dirs, proc_table, remove_history_entry, running_jobs,
                     stop_job, temp_logs, tree_of)
from ..slurm import SLURM_CODE, array_of, array_task_count, slurm_end_text, slurm_secs
from .common import GREY, ORANGE, RED, JobItem, ask, tell

DUR = re.compile(r"(\d+)([dhms])")
SIZE = re.compile(r"([\d.]+) (MB|GB)$")


def sort_key(head, text):
    """Key to sort a cell of the Running / History tables by its column, or None to sort it by its text (no
    number in it: it goes last when other cells of the column have one)."""
    if head in ("Time", "Duration"):
        if ":" in text:
            return slurm_secs(text)
        parts = DUR.findall(text)
        return sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u] for n, u in parts) if parts else None
    if head in ("RAM", "GPU"):
        m = SIZE.match(text)
        return float(m[1]) * (1024 if m[2] == "GB" else 1) if m else None
    if head == "CPU":
        try:
            return float(text.split()[0])
        except (ValueError, IndexError):
            return None
    return None


class TablesMixin:
    """The Running / History tables (groups, hover ✓ / 🗑, context menus, history)."""

    # ── building the trees ──
    # Groups open/close by clicking their header (▸/▾).  Everything starts collapsed.
    def group_clicked(self, it, col=0):
        p = it.data(0, Qt.ItemDataRole.UserRole)
        is_group = it.data(0, Qt.ItemDataRole.UserRole + 1) and not p
        if (is_group or (p and p.get("kind") == "array")) and col not in it.treeWidget()._xcols:
            it.setExpanded(not it.isExpanded())     # -> on_fold

    def on_current(self, t, it):
        """Selection: "Running" has two tables; selecting in one clears the selection of the other."""
        pairs = {self.run_l: self.run_c, self.run_c: self.run_l, self.hist_l: self.hist_c, self.hist_c: self.hist_l}
        if it is not None and t in pairs:
            other = pairs[t]
            other.blockSignals(True); other.setCurrentItem(None); other.clearSelection(); other.blockSignals(False)
        if it is not None:
            self.open_info(it.data(0, Qt.ItemDataRole.UserRole))

    def fit_local(self):
        self.fit_top(self.run_l); self.fit_top(self.hist_l)

    def fit_top(self, t):
        """The local table takes just the height of its rows (up to half the tab); the clusters table the rest."""
        if not self.clusters:
            t.setMinimumHeight(0); t.setMaximumHeight(16777215); return
        rows, stack = 0, [t.invisibleRootItem()]
        while stack:
            it = stack.pop()
            for i in range(it.childCount()):
                ch = it.child(i); rows += 1
                if ch.isExpanded():
                    stack.append(ch)
        rowh = t.sizeHintForRow(0) if t.topLevelItemCount() else t.fontMetrics().height() + 6
        rowh = max(rowh, t.fontMetrics().height() + 4)
        h = t.header().sizeHint().height() + max(rows, 1) * rowh + 2 * t.frameWidth() + 4
        h = min(h, max(120, int(self.tabs.height() * 0.5)))
        t.setMinimumHeight(h); t.setMaximumHeight(h)

    def on_fold(self, tab, it, folded):
        g = it.data(0, Qt.ItemDataRole.UserRole + 1)
        if g:                                       # every group opens and closes on its own
            (self.open_groups.discard if folded else self.open_groups.add)((tab, g))
            self.set_group_text(it)
            if it.treeWidget() in (self.run_l, self.hist_l):
                self.fit_local()
            self.place_hover()
            return
        k = it.data(0, Qt.ItemDataRole.UserRole + 3)
        if k is not None:
            (self.open_arrays.discard if folded else self.open_arrays.add)((tab, k))
            if it.treeWidget() in (self.run_l, self.hist_l):
                self.fit_local()
            self.place_hover()

    def sort_clicked(self, t, col):
        """Click a column header: ascending, descending, then back to the usual order.  Both tables of the tab (local
        and clusters) sort the same way; the cluster groups keep their place."""
        A, D = Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder
        cur = t._sort
        new = (col, A) if not cur or cur[0] != col else (col, D) if cur[1] == A else None
        pair = (self.run_l, self.run_c) if t in (self.run_l, self.run_c) else (self.hist_l, self.hist_c)
        for x in pair:
            x._sort = new
            x.header().setSortIndicatorShown(new is not None)
            if new:
                x.header().setSortIndicator(*new)
            x._layout = None                    # rebuilt: back in the usual order, or sorted
        self.refresh()

    def update_columns(self, t, tab):
        t.setHeaderHidden(False)

    @staticmethod
    def set_group_text(gi):
        head = gi.data(0, Qt.ItemDataRole.UserRole + 2) or ""
        txt = ("▾  " if gi.isExpanded() else "▸  ") + head
        if gi.text(0) != txt:
            gi.setText(0, txt)

    def fill(self, t, tabname, groups, flat=None):
        """groups = [(group, header, rows)], row = (key, columns, payload, color[, children]).  Without clusters,
        no groups.  Updated in place: only the rows that change are removed/added (the list is not rebuilt and does
        not jump).  It is only rebuilt when the groups change (a cluster is added/removed)."""
        KEY = Qt.ItemDataRole.UserRole + 3
        if flat is None:
            flat = len(groups) == 1 and groups[0][0] == "Local"
        layout = (flat, [g for g, _, _ in groups])
        cur = t.currentItem(); cur_key = cur.data(0, KEY) if cur else None
        t.blockSignals(True)
        if layout != t._layout:
            t.clear(); t._items, t._groups, t._hover = {}, {}, None
            t.setRootIsDecorated(False)     # no Qt arrows on groups: click the header (▸/▾ in the text)
            if not flat:
                for g, head, _ in groups:
                    gi = JobItem(t, [head]); gi.setData(0, Qt.ItemDataRole.UserRole + 1, g)
                    gi.setData(0, JobItem.RANK_ROLE, len(t._groups))
                    gi.setToolTip(0, "Click to expand or collapse")
                    f = gi.font(0); f.setBold(True); gi.setFont(0, f)
                    gi.setFirstColumnSpanned(True)
                    gi.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                    gi.setExpanded((tabname, g) in self.open_groups)
                    t._groups[g] = gi
            t._layout = layout
        for g, head, rows in groups:
            self._sync(t, tabname, t.invisibleRootItem() if flat else t._groups[g], rows)
        if cur_key is not None and cur_key in t._items and t.currentItem() is not t._items[cur_key]:
            t.setCurrentItem(t._items[cur_key])
        t.blockSignals(False)
        for g, head, rows in groups:
            if g in t._groups:
                t._groups[g].setData(0, Qt.ItemDataRole.UserRole + 2, head)
                self.set_group_text(t._groups[g])
            self._update(t, rows)
        if t._sort:
            t.sortItems(*t._sort)
        self.update_columns(t, tabname)

    def _forget(self, t, it):
        t._items.pop(it.data(0, Qt.ItemDataRole.UserRole + 3), None)
        if t._hover is it:
            t._hover = None
        for i in range(it.childCount()):
            self._forget(t, it.child(i))

    def _sync(self, t, tab, parent, rows):
        KEY = Qt.ItemDataRole.UserRole + 3
        want = {r[0] for r in rows}
        for i in range(parent.childCount() - 1, -1, -1):        # remove rows that are gone
            ch = parent.child(i)
            if ch.data(0, KEY) not in want:
                self._forget(t, ch); parent.takeChild(i)
        for i, r in enumerate(rows):                              # insert new rows in place
            key, cols = r[0], r[1]
            ch = parent.child(i) if i < parent.childCount() else None
            it = t._items.get(key)
            if ch is not None and ch.data(0, KEY) == key:
                it = ch
            elif t._sort and it is not None and (it.parent() or t.invisibleRootItem()) is parent:
                pass                                              # sorted by a column: fill() sorts it afterwards
            else:
                if it is not None:                                # it was somewhere else: move it
                    par = it.parent() or t.invisibleRootItem()
                    par.takeChild(par.indexOfChild(it))
                else:
                    it = JobItem(cols); it.setData(0, KEY, key); t._items[key] = it
                parent.insertChild(min(i, parent.childCount()), it)
            kids = r[4] if len(r) > 4 else None
            self._sync(t, tab, it, kids or [])
            if kids:
                it.setExpanded((tab, key) in self.open_arrays)

    def _update(self, t, rows):
        for r in rows:
            key, cols, payload, color = r[:4]
            it = t._items[key]
            for c, v in enumerate(cols):
                if c in t._xcols:
                    continue
                if it.text(c) != v:
                    it.setText(c, v)
                it.setData(c, Qt.ItemDataRole.ForegroundRole, color if (color and c == t._color_col) else None)
            it.setData(0, Qt.ItemDataRole.UserRole, payload)
            sk = (payload or {}).get("sort", {})
            for c, v in enumerate(cols):
                it.setData(c, JobItem.SORT_ROLE, sk[c] if c in sk else sort_key(t.headerItem().text(c), v))
            if payload and (payload.get("tip") or payload.get("cmd")):
                tip = payload.get("tip") or payload["cmd"]
                for c in range(len(cols)):
                    if c not in t._xcols:
                        it.setToolTip(c, tip)
            if len(r) > 4 and r[4]:
                self._update(t, r[4])

    # ── ✓ (remove from the history) and 🗑 (remove and delete its logs) on the row under the mouse ──
    def hover_target(self, t, it):
        """-> ("row", key, payload) for a job / array, ("group", name) for a Local / cluster header, or None."""
        if it is None:
            return None
        p = it.data(0, Qt.ItemDataRole.UserRole)
        if p:
            return ("row", it.data(0, Qt.ItemDataRole.UserRole + 3))
        g = it.data(0, Qt.ItemDataRole.UserRole + 1)
        return ("group", g) if g else None

    def hover_item(self, t, it):
        tgt = self.hover_target(t, it)
        if tgt is None:
            self.unhover(); return
        self._hover = (t, tgt)
        if self.hover_box is not None and self.hover_box.parent() is not t.viewport():
            self.hover_box.hide()
        self.hover_box = getattr(t, "_hover_box", None)
        if self.hover_box is None:
            box = QWidget(t.viewport()); t._hover_box = box; lay = QHBoxLayout(box); lay.setContentsMargins(0, 0, 4, 0); lay.setSpacing(2)
            box.btns = []
            for txt, color, delete in (("✓", "#2e9e48", False), ("🗑", "#d04040", True)):
                b = QToolButton(box); b.setText(txt); b.setAutoRaise(True)
                b.setStyleSheet(f"QToolButton {{ color: {color}; font-weight: bold; padding: 0 4px; }}")
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _, d=delete: self.hover_action(d))
                lay.addWidget(b); box.btns.append(b)
            self.hover_box = box
        group = tgt[0] == "group"
        what = (f"all the jobs of {tgt[1]}" if group else "this job")
        self.hover_box.btns[0].setToolTip(f"Remove {what} from the history (logs are kept)")
        self.hover_box.btns[1].setToolTip(f"Remove {what} from the history and delete {'their logs' if group else 'its log'} "
                                          f"(temporary local logs, or the .out/.err on the cluster; logs saved with -l "
                                          f"are never deleted)")
        self.place_hover()

    def hover_item_of(self, t, tgt):
        return t._items.get(tgt[1]) if tgt[0] == "row" else t._groups.get(tgt[1])

    def place_hover(self):
        """Put the ✓ / 🗑 buttons at the right end of the hovered row (it may have moved after a refresh)."""
        if not self._hover or self.hover_box is None:
            return
        t, tgt = self._hover
        it = self.hover_item_of(t, tgt)
        r = t.visualItemRect(it) if it is not None else None
        if r is None or not r.isValid() or r.height() <= 0:
            self.unhover(); return
        box = self.hover_box
        h = r.height(); [b.setFixedHeight(h) for b in box.btns]
        box.adjustSize()
        box.setGeometry(t.viewport().width() - box.width(), r.top(), box.width(), h)
        box.show(); box.raise_()

    def unhover(self):
        self._hover = None
        if self.hover_box is not None:
            self.hover_box.hide()

    def eventFilter(self, obj, ev):
        for t in (self.hist_l, self.hist_c):
            if obj is t.viewport():
                if ev.type() == ev.Type.Leave:
                    if self.hover_box is None or not self.hover_box.underMouse():
                        self.unhover()
                elif ev.type() == ev.Type.MouseMove:
                    it = t.itemAt(ev.position().toPoint())
                    tgt = self.hover_target(t, it)
                    if tgt is None:
                        self.unhover()
                    elif not self._hover or self._hover != (t, tgt):
                        self.hover_item(t, it)
                elif ev.type() == ev.Type.Resize:
                    self.place_hover()
        return False

    def hover_action(self, delete):
        if not self._hover:
            return
        t, tgt = self._hover
        it = self.hover_item_of(t, tgt)
        p = it.data(0, Qt.ItemDataRole.UserRole) if it is not None else None
        self.unhover()
        # Act once Qt has finished handling the click: refresh() rebuilds parts of the tree (crash otherwise).
        if tgt[0] == "group":
            QTimer.singleShot(0, lambda: self.clear_history(delete, only=tgt[1]))
        elif p:
            QTimer.singleShot(0, lambda: self.remove_from_history(p, delete))

    def remove_from_history(self, p, delete=False):
        """✓: only out of the list.  🗑: also delete its logs (temporary local log / .out and .err on the cluster)."""
        if p["kind"] == "local":
            remove_history_entry(p, delete_log=delete)
        else:
            c, s = self.cluster(p["cluster"]), self.cstate.get(p["cluster"])
            if c:
                known = {r["id"] for r in s.hist} if s and s.ok else None
                ids = p.get("ids") or [p["id"]]
                c["hidden"] = [i for i in c.get("hidden", []) if known is None or i in known] + ids
                save_clusters(self.clusters)
                if delete:
                    self.delete_remote_logs(c, ids)
        if delete:
            self.close_log_tabs(lambda i: i.get("key") == p.get("key") or
                                (p.get("ids") and i.get("cluster") == p.get("cluster") and i.get("id") in p["ids"]))
        self.refresh()

    def cluster_head(self, c, n):
        s = self.cstate.get(c["name"])
        if not s or s.first:
            return f"{c['name']}   ·   connecting…"
        if not s.ok:
            return f"{c['name']}   ·   not connected (right-click > Connect…)   ·   {s.err[:80]}"
        return f"{c['name']}   ·   {n}   ·   {time.strftime('%H:%M:%S', time.localtime(s.t))}"

    def hist_match(self, *texts, error=False):
        """History filter: search box text and 'All / Errors only / OK only'."""
        f = self.hfilter.currentIndex()
        if (f == 1 and not error) or (f == 2 and error):
            return False
        q = self.hsearch.text().strip().lower()
        return not q or any(q in str(x).lower() for x in texts)

    def local_rows(self, jobs):
        """Local rows of "Running" with the RAM, CPU and GPU of each job (summed over its child processes)."""
        table = proc_table(); kids = {}
        for pid, (pp, _, _) in table.items():
            kids.setdefault(pp, []).append(pid)
        gstat, gpid = gpu_info()
        now = time.time(); rows, seen = [], set()
        same = {}
        for j in jobs:
            same.setdefault((j["cmd"], j["cwd"]), []).append(j["label"])
        for j in jobs:
            key = ("local", j["label"], j["pid"]); seen.add(key)
            pids = tree_of(j["pid"], table, kids)
            rss = sum(table[p][1] for p in pids); ticks = sum(table[p][2] for p in pids)
            gpu = sum(gpid.get(p, 0) for p in pids)
            prev = self.prev_ticks.get(key)
            cpu = f"{max(0, ticks - prev[0]) / CLK / max(now - prev[1], 1e-3):.1f} cores" if prev else "…"
            self.prev_ticks[key] = (ticks, now)
            dups = [x for x in same[(j["cmd"], j["cwd"])] if x != j["label"]]
            tip = j["cmd"] + (f"\n\n⚠ DUPLICATE: the same command, in the same folder, is also running as: "
                              f"{', '.join(dups)}" if dups else "")
            rows.append((key, [f"● {j['label']}", fmt_dt(now - j["start"]), fmt_bytes(rss), cpu,
                               fmt_bytes(gpu) if gpu else "—", "⚠ duplicate" if dups else "running"],
                         dict(j, kind="local", key=key, tip=tip), ORANGE if dups else None))
        for k in [k for k in self.prev_ticks if k not in seen]:
            del self.prev_ticks[k]
        return rows, gstat

    def cluster_run_rows(self, c, s):
        """Rows of one cluster in "Running"; the tasks of a job array are grouped in one expandable row."""
        entries = [r for r in s.running if S["show_pending"] or r["state"] != "PENDING"]
        by_arr = {}
        for r in entries:
            if array_of(r["id"]):
                by_arr.setdefault(array_of(r["id"]), []).append(r)

        def task_row(r):
            key = (c["name"], r["id"])
            st = SLURM_CODE.get(r["state"], r["state"])
            if r["state"] == "PENDING" and r["reason"].strip("()") not in ("", "None"):
                st += f" ({r['reason'].strip('()')})"
            est = time.strftime("%d-%m %H:%M", time.localtime(s.starts[r["id"]])) if r["id"] in s.starts else ""
            return (key, [f"● {r['name']}  [{r['id']}]", r["used"] if r["state"] != "PENDING" else "", "", "",
                          r.get("gpu", ""), st, est,
                          r.get("part", "")],
                    dict(kind="cluster", cluster=c["name"], id=r["id"], label=r["name"], running=True, key=key,
                         sort={6: s.starts.get(r["id"])},
                         cmd=f"{r['nodes']} node(s), time limit {r['limit']}" + (f", {r['reason']}" if r["state"] == "RUNNING" else "")),
                    GREY if r["state"] == "PENDING" else None)
        rows, done_groups = [], set()
        for r in entries:
            arr = array_of(r["id"])
            if not arr or len(by_arr[arr]) < 2:
                rows.append(task_row(r)); continue
            if arr in done_groups:
                continue
            done_groups.add(arr)
            tasks = by_arr[arr]
            n_run = sum(1 for t in tasks if t["state"] != "PENDING")
            n_pd = sum(array_task_count(t["id"]) for t in tasks if t["state"] == "PENDING")
            n_done = sum(1 for h in s.hist if array_of(h["id"]) == arr)
            total = n_run + n_pd + n_done
            used = max((t["used"] for t in tasks if t["state"] != "PENDING"), key=lambda u: slurm_secs(u) or 0, default="")
            key = (c["name"], "array", arr)
            ests = [s.starts[t["id"]] for t in tasks if t["id"] in s.starts]
            est = time.strftime("%d-%m %H:%M", time.localtime(min(ests))) if ests else ""
            rows.append((key, [f"● {tasks[0]['name']}  [{arr}]  · array", used, "", "", "",
                               f"{n_done}/{total} done · {n_run} R · {n_pd} PD", est,
                               ", ".join(dict.fromkeys(t.get("part", "") for t in tasks if t.get("part")))],
                         dict(kind="array", cluster=c["name"], id=arr, label=tasks[0]["name"], running=True, key=key,
                              sort={6: min(ests) if ests else None},
                              ids=[t["id"] for t in tasks],
                              cmd=f"Array {arr}: {total} tasks ({n_done} finished, {n_run} running, {n_pd} queued)"),
                         GREY if not n_run else None, [task_row(t) for t in tasks]))
        return rows

    def cluster_hist_rows(self, c, s):
        """Rows of one cluster in "History" (filtered); job arrays are grouped in one expandable row."""
        items = []
        for r in sorted(s.hist, key=lambda r: r["end"], reverse=True):
            if r["end"] <= c.get("cleared", 0) or r["id"] in c.get("hidden", ()):
                continue
            txt = slurm_end_text(r["state"], r["exit"])
            if not self.hist_match(r["name"], r["id"], txt, r["state"], error=txt != "ok"):
                continue
            items.append((r, txt))

        def task_row(r, txt):
            key = (c["name"], r["id"])
            return (key, [f"{r['name']}  [{r['id']}]", r.get("part", ""), time.strftime("%d-%m %H:%M", time.localtime(r["end"])),
                          fmt_dt(r["elapsed"]) if r["elapsed"] is not None else "?", txt],
                    dict(kind="cluster", cluster=c["name"], id=r["id"], label=r["name"], running=False, key=key,
                         sort={2: r["end"]},
                         cmd=f"Slurm state: {r['state']}, ExitCode {r['exit']}"),
                    RED if txt != "ok" else None)
        by_arr = {}
        for r, txt in items:
            if array_of(r["id"]):
                by_arr.setdefault(array_of(r["id"]), []).append((r, txt))
        rows, done_groups = [], set()
        for r, txt in items:
            arr = array_of(r["id"])
            if not arr or len(by_arr[arr]) < 2:
                rows.append(task_row(r, txt)); continue
            if arr in done_groups:
                continue
            done_groups.add(arr)
            tasks = sorted(by_arr[arr], key=lambda x: int(x[0]["id"].split("_")[1]) if x[0]["id"].split("_")[1].isdigit() else 0)
            n_err = sum(1 for _, t in tasks if t != "ok")
            end = max(x["end"] for x, _ in tasks)
            dur = max((x["elapsed"] or 0) for x, _ in tasks)
            key = (c["name"], "array", arr)
            rows.append((key, [f"{tasks[0][0]['name']}  [{arr}]  · array of {len(tasks)}",
                               ", ".join(dict.fromkeys(x.get("part", "") for x, _ in tasks if x.get("part"))),
                               time.strftime("%d-%m %H:%M", time.localtime(end)), fmt_dt(dur),
                               "ok" if not n_err else f"{n_err} failed"],
                         dict(kind="array", cluster=c["name"], id=arr, label=tasks[0][0]["name"], running=False, key=key,
                              sort={2: end},
                              ids=[x["id"] for x, _ in tasks],
                              cmd=f"Array {arr}: {len(tasks)} tasks, {n_err} failed (duration = that of the longest task)"),
                         RED if n_err else None, [task_row(x, t) for x, t in tasks]))
        return rows

    # ── right-click menus ──
    def context_menu(self, t, pos):
        it = t.itemAt(pos)
        if not it:
            return
        g = it.data(0, Qt.ItemDataRole.UserRole + 1)
        p = it.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self); acts = {}
        if g and g != "Local":
            c = self.cluster(g)
            acts[menu.addAction("Refresh now")] = lambda: self.poll_cluster(c, force=True)
            if t is self.run_c:
                acts[menu.addAction("Show cluster status")] = lambda: self.show_cluster_info(c)
            st = self.cstate.get(c["name"])
            if st and st.ok:
                acts[menu.addAction("Disconnect")] = lambda: self.disconnect_cluster(c)
            else:
                acts[menu.addAction("Connect… (log in from a terminal)")] = lambda: self.connect(c)
            menu.addSeparator()
            acts[menu.addAction("Edit…")] = lambda: self.edit_cluster(c)
            acts[menu.addAction("Remove cluster")] = lambda: self.remove_cluster(c)
        elif p and t in (self.run_l, self.run_c) and p["kind"] == "local":
            acts[menu.addAction("Terminate (SIGTERM)")] = lambda: self.stop(p, signal.SIGTERM, "Terminate")
            acts[menu.addAction("Kill (SIGKILL)")] = lambda: self.stop(p, signal.SIGKILL, "Kill")
        elif p and t in (self.run_l, self.run_c) and p["kind"] == "cluster":
            acts[menu.addAction("Cancel (scancel)")] = lambda: self.scancel(p)
        elif p and t in (self.run_l, self.run_c) and p["kind"] == "array":
            acts[menu.addAction(f"Cancel the whole array {p['id']} (scancel)")] = lambda: self.scancel(p)
        elif p and t in (self.hist_l, self.hist_c) and p["kind"] == "local":
            acts[menu.addAction("Run again…")] = lambda: self.relaunch(p)
        if not acts:
            return
        a = menu.exec(t.viewport().mapToGlobal(pos))
        if a in acts:
            acts[a]()

    def stop(self, j, sig, verb):
        if not alive(j["pid"]):
            self.refresh(); return
        if not ask(self, verb, f"{verb} «{j['label']}»?\n\n{j['cmd']}"):
            return
        try:
            stop_job(j["pid"], sig)
        except PermissionError:
            tell(self, verb, "Not allowed to send a signal to that process (or it is not a job started with track).")
        QTimer.singleShot(500, self.refresh)

    def relaunch(self, r):
        """Run a local job from the history again with `track`, in its folder, registering it in the same list."""
        track = shutil.which("track") or os.path.expanduser("~/.local/bin/track")
        if not os.path.exists(track):
            tell(self, "Run again", "`track` not found (did you run install.sh?)."); return
        argv, cmd, d = r.get("argv", ""), r.get("cmd", ""), r["_dir"]
        cwd = r.get("cwd") or os.path.expanduser("~")
        log = r.get("log", "")
        temp = log.startswith(os.path.join(d, "logs") + os.sep)
        busy = {j["label"] for j in running_jobs()}
        base = label = r.get("label", "job"); k = 2
        while label in busy:
            label = f"{base}-{k}"; k += 1
        box = QMessageBox(self); box.setWindowTitle("Run again"); box.setIcon(QMessageBox.Icon.Question)
        text = f"Run «{base}» again?\n\nCommand (exactly as it will be run):\n{argv or cmd}\n\nFolder:  {cwd}"
        if label != base:
            text += f"\nLabel:  {label}  (one with the same name is already running)"
        if not argv:
            text += ("\n\nThis is an old job: the command is run again as it was saved (check it if it had arguments "
                     "with spaces or quotes).")
        if not temp and log:
            text += f"\n\nIts log was permanent: {log}"
        box.setText(text)
        keep = box.addButton("Run, overwriting that log", QMessageBox.ButtonRole.AcceptRole) if (log and not temp) else None
        go = box.addButton("Run with a new temporary log" if keep else "Run", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        b = box.clickedButton()
        if b is not go and (keep is None or b is not keep):
            return
        opts = f"-l {shlex.quote(log)} " if (keep is not None and b is keep) else ""
        line = f"exec {shlex.quote(track)} {opts}{shlex.quote(label)} {argv or cmd}"
        subprocess.Popen(["bash", "-c", line], cwd=cwd if os.path.isdir(cwd) else None,
                         env=dict(os.environ, TRACKTOR_DIR=d), start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.tabs.setCurrentIndex(0)
        QTimer.singleShot(800, self.refresh)

    def clear_history(self, delete=False, only=None):
        """Clear history (✓): finished jobs out of the list, logs kept.  Delete history (🗑): also delete their logs
        (temporary local logs and the .out/.err on the clusters; -l logs are never deleted).  only = "Local" or the
        name of one cluster (the ✓ / 🗑 of a group header); None = everything."""
        local = only in (None, "Local")
        clusters = [c for c in self.clusters if only in (None, c["name"]) and c["name"] in self.cstate]
        visible = {c["name"]: [r["id"] for r in self.cstate[c["name"]].hist
                               if r["end"] > c.get("cleared", 0) and r["id"] not in c.get("hidden", ())]
                   for c in clusters}
        tmp = temp_logs() if (delete and local) else []
        where = "" if only is None else f" of {only}"
        if delete:
            title = "Delete history" + where
            msg = f"Remove all finished jobs{where} from the history AND delete their logs?\n\n"
            if local:
                msg += (f"Local: {len(tmp)} temporary log(s) will be deleted (jobs launched without -l).  Logs saved "
                        f"with -l and those of running jobs are not touched.\n" if tmp else
                        "Local: there are no temporary logs to delete.\n")
            if clusters:
                n = sum(len(v) for v in visible.values())
                msg += (f"\nClusters: the .out/.err of {n} finished job(s) will be deleted "
                        f"({', '.join(f'{k}: {len(v)}' for k, v in visible.items())}); only files whose name "
                        f"contains the job number.\n")
            msg += "\nThis cannot be undone."
        else:
            title = "Clear history" + where
            msg = (f"Remove all finished jobs{where} from the history?\n\nNothing is deleted: their logs stay where "
                   f"they are (use 🗑 / «Delete history» to also delete them).")
        if not ask(self, title, msg):
            return
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass
        if local:
            for d in job_dirs():
                p = os.path.join(d, "history.jsonl")
                if os.path.exists(p):
                    open(p, "w").close()
        now = time.time()
        for c in clusters:
            if delete and visible.get(c["name"]):
                self.delete_remote_logs(c, visible[c["name"]])
            c["cleared"] = now; c["hidden"] = []
        if clusters:
            save_clusters(self.clusters)
        if delete:      # close the tabs of the logs that no longer exist
            names = {c["name"] for c in clusters}
            self.close_log_tabs(lambda i: (i["kind"] == "local" and i.get("log") in tmp) or
                                (i["kind"] != "local" and i.get("cluster") in names and not i.get("running")))
        self.refresh()
