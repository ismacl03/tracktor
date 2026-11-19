"""Main window: cluster files open in the viewer (edit, save over SSH, sbatch, run with bash)."""
import base64, html, re, shlex, threading, time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMessageBox, QPlainTextEdit, QVBoxLayout

from ..config import BASH_TIMEOUT, SCRIPT_EDIT_MAX
from ..local import fmt_dt
from ..scripts import sbatch_all, script_resources
from ..ssh import ssh_run
from .common import ask, english_buttons, LogHighlighter, tell


class EditorMixin:
    """Cluster files open in the viewer (edit, save over SSH, sbatch, run with bash)."""

    @staticmethod
    def script_info(cluster, path, run=""):
        return dict(kind="script", cluster=cluster, path=path, label=path.rsplit("/", 1)[-1], run=run,
                    key=("script", cluster, path))

    RUN_LABEL = {"sbatch": "Submit (sbatch)…", "bash": "Run (bash)…"}

    def fetch_script(self, info):
        c, s = self.cluster(info["cluster"]), self.cstate.get(info["cluster"])
        p = info["path"]
        if not c or not s or p in s.script_busy or p in s.script_text:
            return
        s.script_busy.add(p)
        f = shlex.quote(p)

        def run():
            try:
                ok, out, err = ssh_run(c["host"], f"if [ -f {f} ] && [ -r {f} ]; then stat -c %Y {f} && cat {f}; "
                                                  f"else echo @@NOFILE; fi", timeout=60, pre=c.get("pre", ""))
                if not ok:
                    s.script_text[p] = ("", 0, f"no connection to {c['name']}: {err}")
                elif out.startswith("@@NOFILE"):
                    s.script_text[p] = ("", 0, "this file no longer exists (or cannot be read)")
                else:
                    t, _, body = out.partition("\n")
                    s.script_text[p] = (body, int(t) if t.strip().isdigit() else 0, "")
            finally:
                s.script_busy.discard(p)
        threading.Thread(target=run, daemon=True).start()

    def cur_script(self):
        """-> tab data of the script shown, or None."""
        d = self.tabdata.get(self._cur_tab)
        return d if d and d["info"]["kind"] == "script" and self.sel is d["info"] else None

    @staticmethod
    def script_dirty(d):
        return d.get("edit") is not None and d.get("orig") is not None and d["edit"] != d["orig"]

    def set_text(self, txt, read_only):
        self._loading = True
        self.out.setPlainText(txt); self.out.setReadOnly(read_only)
        self._loading = False

    def show_script(self, force=False):
        d = self.cur_script()
        if not d:
            return
        info = d["info"]; s = self.cstate.get(info["cluster"])
        if not s:
            return
        if d.get("orig") is None:
            got = s.script_text.get(info["path"])
            if got is None:
                self.fetch_script(info)
                txt = "(reading the script from the cluster…)"
            elif got[2]:
                txt = f"(could not open it: {got[2]})\n\n{info['path']}"
            else:
                d["orig"], d["mtime"], force = got[0], got[1], True
            if d.get("orig") is None:
                if force or txt != self.out.toPlainText():
                    self.set_text(txt, True)
                self.update_script_buttons(); return
        if force:
            big = len(d["orig"].encode()) > SCRIPT_EDIT_MAX
            self.set_text(d["edit"] if d.get("edit") is not None else d["orig"], big)
            cur = d.get("cursor")
            if cur is not None:
                tc = self.out.textCursor(); tc.setPosition(min(cur, len(self.out.toPlainText()))); self.out.setTextCursor(tc)
            self.update_script_buttons()

    def on_text_edited(self):
        if self._loading:
            return
        d = self.cur_script()
        if not d or d.get("orig") is None:
            return
        d["edit"] = self.out.toPlainText()
        i = self.tab_index(self._cur_tab)
        if i >= 0:
            self.set_tab_info(i, d["info"])
        self.update_script_buttons()

    def update_script_buttons(self):
        d = self.cur_script()
        for b in (self.save_btn, self.reload_btn):
            b.setVisible(d is not None)
        run = d["info"].get("run") if d else ""
        self.run_btn.setVisible(run in ("sbatch", "bash"))
        if d:
            loaded = d.get("orig") is not None
            busy = d["info"]["key"] in self._running_scripts
            self.save_btn.setEnabled(loaded and self.script_dirty(d))
            self.run_btn.setEnabled(loaded and not busy)
            self.run_btn.setText(("Submitting…" if run == "sbatch" else "Running…") if busy
                                 else "sbatch…" if run == "sbatch" else "Run…")
            self.run_btn.setToolTip("Submit it with sbatch (asks first)" if run == "sbatch" else
                                    "Run it with bash on the login node and show its output (asks first)")

    def reload_script(self):
        d = self.cur_script()
        if not d:
            return
        if self.script_dirty(d) and not ask(self, "Reload", f"Discard your changes to «{d['info']['label']}» and "
                                                           f"read it again from {d['info']['cluster']}?"):
            return
        s = self.cstate.get(d["info"]["cluster"])
        if s:
            s.script_text.pop(d["info"]["path"], None)
        d.update(orig=None, edit=None, cursor=None)
        i = self.tab_index(self._cur_tab)
        if i >= 0:
            self.set_tab_info(i, d["info"])
        self.show_script(force=True)

    def save_script(self, then=None, force=False):
        """Write the edited script back to the cluster (refusing if it changed there since it was opened)."""
        d = self.cur_script()
        if not d or d.get("orig") is None:
            return
        info = d["info"]; c = self.cluster(info["cluster"])
        text = d["edit"] if d.get("edit") is not None else d["orig"]
        if not c:
            return
        if len(text.encode()) > SCRIPT_EDIT_MAX:
            tell(self, "Save", f"This script is too big to save from here (more than {SCRIPT_EDIT_MAX // 1000} KB)."); return
        f = shlex.quote(info["path"])
        b64 = base64.b64encode(text.encode()).decode()
        chk = "" if force else (f'[ "$(stat -c %Y {f})" = {int(d.get("mtime", 0))} ] || '
                                f'{{ echo "@@CHANGED"; exit 0; }}; ')
        script = (f'[ -f {f} ] || {{ echo @@GONE; exit 0; }}; {chk}'
                  f't=$(mktemp) && printf %s {b64} | base64 -d > "$t" && cat "$t" > {f}; r=$?; rm -f "$t"; '
                  f'[ $r = 0 ] && echo "@@OK $(stat -c %Y {f})"')
        self.save_btn.setEnabled(False)

        def done(ok, out, err):
            out = out.strip()
            if ok and out.startswith("@@OK"):
                d["orig"], d["mtime"] = text, int(out.split()[-1]) if out.split()[-1].isdigit() else 0
                if d.get("edit") == text:
                    d["edit"] = None
                s = self.cstate.get(info["cluster"])
                if s:
                    s.script_text[info["path"]] = (text, d["mtime"], "")
                i = self.find_tab(info["key"])
                if i >= 0:
                    self.set_tab_info(i, info)
                self.update_script_buttons()
                self.fetch_scripts(c)                    # its #SBATCH options may have changed
                if then:
                    then()
            elif ok and out.startswith("@@CHANGED"):
                self.update_script_buttons()
                if ask(self, "Save", f"«{info['label']}» has changed on {c['name']} since you opened it.\n\n"
                                     f"Overwrite it with your version?"):
                    self.save_script(then, force=True)
            else:
                self.update_script_buttons()
                why = "the file no longer exists" if out.startswith("@@GONE") else (out or err or "unknown error")
                tell(self, "Save", f"Could not save «{info['label']}» on {c['name']}:\n{why}")
        self.remote(c, script, done)

    def run_script(self, info):
        """The action of its category: submit it (sbatch) or run it (bash)."""
        if info.get("run") == "sbatch":
            self.sbatch_script(info)
        elif info.get("run") == "bash":
            self.bash_script(info)

    def saved_first(self, info, verb):
        """If the script is open with unsaved changes: offer to save it and then go on.  -> True if it can go on now."""
        i = self.find_tab(info["key"])
        d = self.tabdata[self.logtabs.tabData(i)] if i >= 0 else None
        if not (d and self.script_dirty(d)):
            return True
        if i != self.logtabs.currentIndex():
            self.logtabs.setCurrentIndex(i)
        box = QMessageBox(self); box.setWindowTitle(verb); box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"«{info['label']}» has unsaved changes.")
        save = box.addButton("Save and continue", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is save:
            self.save_script(then=lambda: self.run_script(info))
        return False

    def sbatch_script(self, info):
        c = self.cluster(info["cluster"])
        if not c or not self.saved_first(info, "Submit"):
            return
        i = self.find_tab(info["key"])
        d = self.tabdata[self.logtabs.tabData(i)] if i >= 0 else None
        folder, name = info["path"].rsplit("/", 1)
        s = self.cstate.get(c["name"])
        text = (d.get("orig") if d else None) or ((s.script_text.get(info["path"]) or ("",))[0] if s else "")
        o = sbatch_all(text) if text else next((x["opts"] for x in (s.scripts or []) if x["path"] == info["path"]), {})
        summary = "  ·  ".join(x for x in (o.get("job-name", ""), o.get("partition", "") and f"partition {o['partition']}",
                                           o.get("time", "") and f"time {o['time']}", script_resources(o)) if x)
        if not ask(self, "Submit", f"Submit this job on {c['name']}?\n\n    cd {folder}\n    sbatch {name}\n\n"
                                   + (summary or "(no #SBATCH options)")):
            return
        self._running_scripts.add(info["key"]); self.update_script_buttons()

        def done(ok, out, err):
            self._running_scripts.discard(info["key"]); self.update_script_buttons()
            out = out.strip()
            if ok and re.search(r"Submitted batch job \d+", out):
                self.repoll(c)                           # show it in "Running" right away
                tell(self, "Submit", f"{out}\n\n({c['name']}: {info['path']})", QMessageBox.Icon.Information)
            else:
                tell(self, "Submit", f"sbatch failed on {c['name']}:\n\n{out or err}")
        self.remote(c, f"cd {shlex.quote(folder)} && sbatch ./{shlex.quote(name)} 2>&1", done, timeout=60)

    def bash_script(self, info):
        c = self.cluster(info["cluster"])
        if not c or not self.saved_first(info, "Run"):
            return
        folder, name = info["path"].rsplit("/", 1)
        if not ask(self, "Run", f"Run this script on {c['name']}?\n\n    cd {folder}\n    bash {name}\n\n"
                                f"It runs on the login node, not as a Slurm job: fine for short things (e.g. a script "
                                f"that submits several jobs with sbatch), not for heavy work.  It is stopped after "
                                f"{BASH_TIMEOUT // 60} minutes; its output is shown when it ends."):
            return
        self._running_scripts.add(info["key"]); self.update_script_buttons()
        t0 = time.time()

        def done(ok, out, err):
            self._running_scripts.discard(info["key"]); self.update_script_buttons()
            self.repoll(c)                               # it may have submitted or cancelled jobs
            code = None
            m = re.search(r"@@EXIT (\d+)\s*$", out)
            if m:
                code, out = int(m.group(1)), out[:m.start()]
            if not ok:
                out = f"(no connection to {c['name']}: {err})"
            head = (f"bash {name}  ·  {c['name']}  ·  {fmt_dt(time.time() - t0)}  ·  "
                    + ("stopped: too long" if code == 124 else "exit code ?" if code is None else f"exit code {code}"))
            self.show_output(f"Output of {name}", head, out, error=code != 0)
        self.remote(c, f"cd {shlex.quote(folder)} && timeout {BASH_TIMEOUT} bash ./{shlex.quote(name)} </dev/null "
                       f"2>&1 | tail -c 200000; r=${{PIPESTATUS[0]}}; echo; echo \"@@EXIT $r\"", done, timeout=BASH_TIMEOUT + 60)

    def show_output(self, title, head, text, error=False):
        dlg = QDialog(self); dlg.setWindowTitle(title); dlg.resize(820, 520)
        lay = QVBoxLayout(dlg)
        lab = QLabel(f"<b style='color:{'#d04040' if error else '#2e9e48'}'>{html.escape(head)}</b>")
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); lay.addWidget(lab)
        ed = QPlainTextEdit(text.rstrip("\n") or "(no output)"); ed.setReadOnly(True); ed.setFont(self.out.font())
        LogHighlighter(ed.document()); lay.addWidget(ed, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); english_buttons(bb)
        bb.rejected.connect(dlg.reject); lay.addWidget(bb)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dlg.show()
