"""Main window: the log viewer on the right (tabs like a browser, Ctrl+F search)."""
import html, re, time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import QTextEdit

from ..local import read_tail
from .common import NO_SEL, ask


class LogViewMixin:
    """The log viewer on the right (tabs like a browser, Ctrl+F search)."""

    def set_stream_buttons(self, info):
        """Output/errors buttons: only for cluster jobs; errors disabled if both go to the same file."""
        is_cl = bool(info) and info["kind"] == "cluster"
        self.out_btn.setVisible(is_cl); self.err_btn.setVisible(is_cl)
        if not is_cl:
            return
        paths = self.cluster_paths(info)
        same = bool(paths) and paths["out"] == paths["err"]
        self.err_btn.setEnabled(not same)
        self.err_btn.setToolTip("This job writes its errors to the same file as its output (Slurm's default when "
                                "--error is not used)" if same else (paths["err"] if paths else ""))
        self.out_btn.setToolTip(paths["out"] if paths else "")
        stream = "out" if same else self.stream
        (self.out_btn if stream == "out" else self.err_btn).setChecked(True)

    def set_stream(self, stream):
        if stream != self.stream:
            self.stream = stream
            if self._cur_tab in self.tabdata:
                self.tabdata[self._cur_tab]["stream"] = stream
            if self.sel:
                self.select(self.sel)

    # ── selection and log ──
    def select(self, info):
        if not info:
            return
        self.sel = info
        extra = ""
        script = info["kind"] == "script"
        self.hl.setDocument(None if script else self.out.document())   # no error/warning colours in scripts
        if not script:
            self.out.setReadOnly(True)
        if script:
            self._title_paths, self._title_eff = None, ""
            self.title.setText(f"<b>{html.escape(info['label'])}</b>  —  {html.escape(info['cluster'])}  ·  "
                               f"{html.escape(info['path'])}<br><small>Edit it here; Ctrl+S saves it on the cluster"
                               + {"sbatch": "; «sbatch…» submits it", "bash": "; «Run…» runs it with bash"}.get(
                                   info.get("run"), "") + ".</small>")
            self.set_stream_buttons(None)
            self.show_script(force=True)
            return
        self.update_script_buttons()
        if info["kind"] == "local":
            where = info["log"]
        elif info["kind"] == "array":
            where = f"{info['cluster']}  ·  array {info['id']}"
        else:
            paths = self.cluster_paths(info)
            where = f"{info['cluster']}  ·  job {info['id']}" + (f"  ·  {paths[self.eff_stream()]}" if paths else "")
            if not info.get("running"):
                self.fetch_eff(info["cluster"], info["id"])
                st = self.cstate.get(info["cluster"])
                extra = (st.eff.get(info["id"]) if st else "") or ""
        self._title_paths = self.cluster_paths(info) if info["kind"] == "cluster" else None
        self._title_eff = extra
        self.title.setText(f"<b>{html.escape(info['label'])}</b>  —  {html.escape(where)}<br>"
                           f"<small>{html.escape(info.get('cmd', ''))}</small>"
                           + (f"<br><small>{extra}</small>" if extra else ""))
        self.set_stream_buttons(info)
        self.show_log(force=True)

    def eff_stream(self):
        paths = self.cluster_paths(self.sel)
        return "out" if (paths and paths["out"] == paths["err"]) else self.stream

    def current_log_text(self):
        i = self.sel
        if i["kind"] == "local":
            return read_tail(i["log"])
        if i["kind"] == "array":
            return (f"Array {i['id']} ({len(i.get('ids', []))} tasks in this list).\n"
                    "Expand it and click a task to see its log.")
        st = self.eff_stream()
        self.fetch_cluster_log(i["cluster"], i["id"], i.get("running", False), st)
        s = self.cstate.get(i["cluster"])
        got = s.logs.get((i["id"], st)) if s else None
        return got[0] if got else "(reading the log from the cluster…)"

    def show_log(self, force=False):
        if not self.sel:
            return
        if self.sel["kind"] == "script":
            return self.show_script(force)
        sb = self.out.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 3
        txt = self.current_log_text()
        if force or txt != self.out.toPlainText():
            pos = sb.value()
            self.out.setPlainText(txt)
            if self.searching():                 # while searching: do not jump to the end, stay where we were
                if not force:
                    sb.setValue(pos)
                self.update_find(jump=force)
            elif force or at_bottom:
                self.out.moveCursor(QTextCursor.MoveOperation.End)
            else:
                sb.setValue(pos)
        if self.sel["kind"] == "cluster":
            st = self.cstate.get(self.sel["cluster"])
            eff = (st.eff.get(self.sel["id"]) if st and not self.sel.get("running") else "") or ""
            if self.cluster_paths(self.sel) != self._title_paths or eff != self._title_eff:
                self.select(self.sel)   # paths or efficiency are now known: update title and buttons

    # ── log search ──
    def searching(self):
        return self.findbar.isVisible() and bool(self.find_edit.text())

    def open_find(self):
        self.findbar.show(); self.find_edit.setFocus(); self.find_edit.selectAll()
        self.update_find(jump=True)

    def close_find(self):
        self.findbar.hide(); self.matches, self.find_idx = [], -1
        self.out.setExtraSelections([]); self.find_count.setText("")

    def update_find(self, jump=False):
        q = self.find_edit.text()
        if not q or not self.findbar.isVisible():
            self.matches = []; self.out.setExtraSelections([]); self.find_count.setText(""); return
        doc = self.out.toPlainText().lower()
        self.matches = [m.start() for m in re.finditer(re.escape(q.lower()), doc)][:5000]
        if not self.matches:
            self.find_idx = -1; self.out.setExtraSelections([])
            self.find_count.setText("no matches"); return
        if jump or self.find_idx < 0:
            top = self.out.cursorForPosition(self.out.viewport().rect().topLeft()).position()
            self.find_idx = next((i for i, p in enumerate(self.matches) if p >= top), 0)
            self.go_match(self.find_idx)
        else:
            self.find_idx = min(self.find_idx, len(self.matches) - 1)
            self.paint_matches()

    def find_step(self, d):
        if not self.matches:
            self.update_find(jump=True); return
        self.go_match((self.find_idx + d) % len(self.matches))

    def go_match(self, i):
        self.find_idx = i
        n = len(self.find_edit.text()); cur = QTextCursor(self.out.document())
        cur.setPosition(self.matches[i]); cur.setPosition(self.matches[i] + n, QTextCursor.MoveMode.KeepAnchor)
        self.out.setTextCursor(cur); self.out.ensureCursorVisible()
        self.paint_matches()

    def paint_matches(self):
        n = len(self.find_edit.text()); sels = []
        for i, p in enumerate(self.matches[:2000]):
            sel = QTextEdit.ExtraSelection(); cur = QTextCursor(self.out.document())
            cur.setPosition(p); cur.setPosition(p + n, QTextCursor.MoveMode.KeepAnchor); sel.cursor = cur
            sel.format.setBackground(QColor("#ff9632" if i == self.find_idx else "#f5e663"))
            sel.format.setForeground(QColor("black"))
            sels.append(sel)
        self.out.setExtraSelections(sels)
        self.find_count.setText(f"{self.find_idx + 1}/{len(self.matches)}")

    # ── log tabs (like a browser): click = open in the current tab, double click = open in a new tab; a job that
    # is already open in a tab just brings that tab to the front ──
    @staticmethod
    def tab_title(info):
        if info["kind"] == "script":
            return f"📝 {info['label']}"
        return info["label"] if info["kind"] == "local" else f"{info['label']}  [{info['id']}]"

    @staticmethod
    def tab_tip(info):
        if info["kind"] == "script":
            return f"{info['cluster']}  ·  {info['path']}"
        if info["kind"] == "local":
            return f"{info['label']}\n{info.get('log', '')}"
        return f"{info['label']}\n{info['cluster']}  ·  {'array' if info['kind'] == 'array' else 'job'} {info['id']}"

    def tab_index(self, tid):
        return next((i for i in range(self.logtabs.count()) if self.logtabs.tabData(i) == tid), -1)

    def find_tab(self, key):
        return next((i for i in range(self.logtabs.count())
                     if self.tabdata[self.logtabs.tabData(i)]["info"].get("key") == key), -1)

    def set_tab_info(self, i, info):
        d = self.tabdata.get(self.logtabs.tabData(i))
        dirty = bool(d) and d["info"] is info and self.script_dirty(d)
        self.logtabs.setTabText(i, ("● " if dirty else "") + self.tab_title(info))
        self.logtabs.setTabToolTip(i, self.tab_tip(info) + ("\n(unsaved changes)" if dirty else ""))

    def open_info(self, info, new=False):
        if not info:
            return
        key, cur = info.get("key"), self.logtabs.currentIndex()
        i = self.find_tab(key)
        lr = self._last_replace
        if (new and lr and i == cur >= 0 and lr[0] == self.logtabs.tabData(cur) and lr[2] == key
                and time.time() - lr[3] < 2):
            # the first click of this double click put the job in the current tab: give that tab its log back
            self.tabdata[lr[0]].update(lr[1]); self.set_tab_info(cur, lr[1]["info"])
            self._last_replace, i = None, -1
        elif i >= 0:
            if i != cur:
                self.logtabs.setCurrentIndex(i)          # -> tab_changed
            return
        if cur >= 0 and self.script_dirty(self.tabdata[self.logtabs.tabData(cur)]):
            new = True                                   # never replace a script with unsaved changes
        if new or cur < 0:
            self._tab_seq += 1; tid = self._tab_seq
            self.tabdata[tid] = dict(info=info, stream="out", scroll=None)
            self._last_replace = None
            self.logtabs.blockSignals(True)
            i = self.logtabs.addTab(""); self.logtabs.setTabData(i, tid); self.set_tab_info(i, info)
            self.logtabs.blockSignals(False)
        else:
            self.save_scroll()
            i, tid = cur, self.logtabs.tabData(cur)
            self._last_replace = (tid, dict(self.tabdata[tid]), key, time.time())
            self.tabdata[tid] = dict(info=info, stream="out", scroll=None)
            self.set_tab_info(i, info)
        if i != self.logtabs.currentIndex():
            self.logtabs.blockSignals(True); self.logtabs.setCurrentIndex(i); self.logtabs.blockSignals(False)
            self.save_scroll()
        self._cur_tab = None
        self.activate(i)

    def save_scroll(self):
        d = self.tabdata.get(self._cur_tab)
        if d and d["info"].get("kind") == "script" and d.get("orig") is not None:
            d["cursor"] = self.out.textCursor().position()
        if d:
            sb = self.out.verticalScrollBar()
            d["scroll"] = None if sb.value() >= sb.maximum() - 3 else sb.value()

    def tab_changed(self, i):
        tid = self.logtabs.tabData(i) if i >= 0 else None
        if tid is not None and tid == self._cur_tab:
            return                                       # same tab, only its position changed
        self.save_scroll()
        self.activate(i)

    def activate(self, i):
        if i < 0 or not self.logtabs.count():
            self._cur_tab = None; self.clear_selection(); return
        tid = self.logtabs.tabData(i); d = self.tabdata[tid]
        self._cur_tab = tid; self.stream = d["stream"]
        self.select(d["info"])
        if d["scroll"] is not None:
            self.out.verticalScrollBar().setValue(d["scroll"])

    def close_tab(self, i):
        if i < 0:
            return
        tid = self.logtabs.tabData(i)
        d = self.tabdata.get(tid)
        if d and self.script_dirty(d):
            if i != self.logtabs.currentIndex():
                self.logtabs.setCurrentIndex(i)
            if not ask(self, "Close", f"«{d['info']['label']}» has unsaved changes.  Close it and lose them?"):
                return
        if tid == self._cur_tab:
            self._cur_tab = None
        self.tabdata.pop(tid, None)
        self._last_replace = None
        self.logtabs.removeTab(i)                        # -> tab_changed (another tab, or none)
        if not self.logtabs.count():
            self._cur_tab = None; self.clear_selection()

    def close_log_tabs(self, pred):
        for i in reversed(range(self.logtabs.count())):
            if pred(self.tabdata[self.logtabs.tabData(i)]["info"]):
                self.close_tab(i)

    def refresh_tabs(self):
        """Keep the jobs shown in the tabs up to date (e.g. a running job that has just finished)."""
        for i in range(self.logtabs.count()):
            tid = self.logtabs.tabData(i); d = self.tabdata[tid]; key = d["info"].get("key")
            p = None
            for t in (self.run_l, self.run_c, self.hist_l, self.hist_c):
                it = t._items.get(key)
                p = it.data(0, Qt.ItemDataRole.UserRole) if it is not None else None
                if p:
                    break
            if not p or p == d["info"]:
                continue
            if d["info"].get("running") and not p.get("running") and p["kind"] == "cluster":
                s = self.cstate.get(p["cluster"])         # it has just finished: read its log once more
                if s:
                    s.logs.pop((p["id"], "out"), None); s.logs.pop((p["id"], "err"), None)
            d["info"] = p; self.set_tab_info(i, p)
            if tid == self._cur_tab:
                self.sel = p

    def clear_selection(self):
        self.sel = None; self.set_text("", True); self.title.setText(NO_SEL)
        self.set_stream_buttons(None); self.update_script_buttons()
