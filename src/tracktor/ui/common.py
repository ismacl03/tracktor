"""Shared UI pieces: colours, icons, notifications, English dialogs, log highlighter, tray / taskbar."""
import html, os, re, shutil, subprocess

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtWidgets import QAbstractItemView, QDialogButtonBox, QHeaderView, QMessageBox, QTreeWidget, QTreeWidgetItem
try:
    from PyQt6.QtDBus import QDBusConnection, QDBusMessage
except ImportError:          # no D-Bus: no counter on the taskbar icon
    QDBusConnection = None

from ..config import APP
from ..scripts import natural_key


# columns of the cluster status panel (each cluster can hide some: Settings > Clusters)
INFO_COLS = ["Partition", "Time limit", "Idle nodes", "Down", "Free CPUs", "Free GPUs", "Free mem", "All R/PD",
             "Mine R/PD"]
QUEUE_COLS = ["Job ID", "User", "Partition", "Name", "State", "Time", "Limit", "Nodes", "CPUs", "Mem", "GPUs",
              "Nodes / reason"]


def fmt_mb(mb):
    """MB -> "512M" / "8G" / "1.5T" """
    if mb is None:
        return ""
    if mb >= 1024 ** 2:
        return f"{mb / 1024 ** 2:.1f}T"
    if mb >= 1024:
        g = mb / 1024
        return f"{g:.0f}G" if g >= 10 or g == int(g) else f"{g:.1f}G"
    return f"{mb:.0f}M"


RED, GREY, ORANGE, GREEN = QColor("#d04040"), QColor("#888888"), QColor("#d08000"), QColor("#2e9e48")


_HERE = os.path.dirname(os.path.realpath(__file__))
ICON_DIRS = [os.path.normpath(os.path.join(_HERE, "..", "..", "..", "icons")),     # in the repository (src/tracktor/ui)
             os.path.expanduser("~/.local/share/tracktor/icons")]                # installed


def icon_file(name):
    return next((p for p in (os.path.join(d, name + ".svg") for d in ICON_DIRS) if os.path.exists(p)), None)


_ICONS = {}


def app_icon():
    """The Tracktor icon: icons/tracktor.svg (next to this script or installed), or from the icon theme."""
    if "app" not in _ICONS:
        f = icon_file("tracktor")
        _ICONS["app"] = QIcon(f) if f else QIcon.fromTheme("tracktor")
    return _ICONS["app"]


TRAY = None     # tray icon (if any): fallback for notifications when notify-send is missing


def notify(title, body, error=False):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", APP, "-i", "dialog-error" if error else "tracktor", title,
                          html.escape(body, quote=False)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    elif TRAY is not None:
        TRAY.showMessage(title, body)


class SortItem(QTreeWidgetItem):
    """Row of a sortable grouped list: groups keep their order (whatever the direction); inside a group, rows are
    compared by the key stored in SORT_ROLE (numbers, times…) or naturally by their text.  Empty values go last."""
    SORT_ROLE = Qt.ItemDataRole.UserRole + 2
    RANK_ROLE = Qt.ItemDataRole.UserRole + 3

    def __lt__(self, other):
        t = self.treeWidget()
        col = t.sortColumn() if t else 0
        desc = t is not None and t.header().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        ra, rb = self.data(0, self.RANK_ROLE), other.data(0, self.RANK_ROLE)
        if ra is not None and rb is not None:
            return ra > rb if desc else ra < rb
        a, b = self.data(col, self.SORT_ROLE), other.data(col, self.SORT_ROLE)
        if a is None and b is None:
            a = natural_key(self.text(col)) if self.text(col) else None
            b = natural_key(other.text(col)) if other.text(col) else None
        if a is None or b is None:                   # empty: at the end in both directions
            return (a is not None) != desc if (a is None) != (b is None) else False
        return a < b


SORT_TIP = "Click a column to sort by it (again: reverse; a third time: back to the usual order)"


def sortable(t, rebuild, default=None):
    """Sort a list by clicking its headers: ascending, descending, then the usual order again (`default`, a
    (column, order), or the order it was filled in: `rebuild()` fills it again).  Whoever fills the list calls
    apply_sort(t) at the end."""
    t._sort, t._default, t._rebuild = None, default, rebuild
    t.setSortingEnabled(False)
    h = t.header(); h.setSectionsClickable(True); h.setSortIndicatorShown(False); h.setToolTip(SORT_TIP)
    h.sectionClicked.connect(lambda col: sort_click(t, col))


def sort_click(t, col):
    A, D = Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder
    cur = t._sort
    t._sort = (col, A) if not cur or cur[0] != col else (col, D) if cur[1] == A else None
    if t._sort or t._default:
        apply_sort(t)
    else:
        t._rebuild()


def apply_sort(t):
    h = t.header()
    if t._sort or t._default:
        t.sortItems(*(t._sort or t._default))
    h.setSortIndicatorShown(t._sort is not None)
    if t._sort:
        h.setSortIndicator(*t._sort)


class JobItem(SortItem):
    """Row of the Running / History tables (their UserRole+1..+3 are already used for group, header and key)."""
    SORT_ROLE = Qt.ItemDataRole.UserRole + 10
    RANK_ROLE = Qt.ItemDataRole.UserRole + 11


# ───────────────────────────── dialogs in English ─────────────────────────────
# Qt's standard buttons (OK, Cancel, Yes…) come translated to the desktop's language; Tracktor is in English.
def english_buttons(bb):
    SB = QDialogButtonBox.StandardButton
    for k, txt in ((SB.Ok, "OK"), (SB.Cancel, "Cancel"), (SB.RestoreDefaults, "Restore defaults"),
                   (SB.Close, "Close"), (SB.Yes, "Yes"), (SB.No, "No")):
        b = bb.button(k)
        if b is not None:
            b.setText(txt)
    return bb


def ask(parent, title, text):
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    yes = box.addButton("Yes", QMessageBox.ButtonRole.YesRole); box.addButton("No", QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(yes); box.exec()
    return box.clickedButton() is yes


def tell(parent, title, text, icon=QMessageBox.Icon.Warning):
    box = QMessageBox(icon, title, text, parent=parent)
    box.addButton("OK", QMessageBox.ButtonRole.AcceptRole); box.exec()


# ───────────────────────────── log: error highlighting ─────────────────────────────
class LogHighlighter(QSyntaxHighlighter):
    """Error lines in red and warnings in orange (Traceback, ...Error, nan, Killed, out of memory, ...)."""
    ERR = re.compile(r"Traceback|Error\b|ERROR|Exception|\bnan\b|\bNaN\b|FAILED|Killed|[Oo]ut of memory|\bOOM\b|"
                     r"Segmentation fault|core dumped|slurmstepd: error|DUE TO TIME LIMIT|CANCELLED AT")
    WARN = re.compile(r"[Ww]arning|WARNING|\bWARN\b")

    def __init__(self, doc):
        super().__init__(doc)
        self.err, self.warn = QTextCharFormat(), QTextCharFormat()
        self.err.setForeground(QColor("#e05050")); self.warn.setForeground(QColor("#d08000"))

    def highlightBlock(self, text):
        if self.ERR.search(text):
            self.setFormat(0, len(text), self.err)
        elif self.WARN.search(text):
            self.setFormat(0, len(text), self.warn)


# ───────────────────────────── system tray ─────────────────────────────
def tray_icon(n):
    """Tray icon with the number of running jobs in a red badge."""
    base = app_icon()
    if not base.isNull():
        pm = base.pixmap(64, 64)
    else:
        pm = QPixmap(64, 64); pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#2e7d32")); p.setPen(Qt.PenStyle.NoPen); p.drawRoundedRect(4, 4, 56, 56, 12, 12)
        p.end()
    if n:
        p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#d04040")); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(26, 26, 38, 38)
        f = QFont(); f.setBold(True); f.setPixelSize(24 if n < 10 else 18); p.setFont(f); p.setPen(QColor("white"))
        p.drawText(26, 26, 38, 38, Qt.AlignmentFlag.AlignCenter, str(n) if n < 100 else "99+")
        p.end()
    return QIcon(pm)


def taskbar_count(n):
    """Number on the launcher / taskbar icon (KDE Plasma, Ubuntu dock…: the Unity LauncherEntry D-Bus API)."""
    if QDBusConnection is None:
        return
    try:
        msg = QDBusMessage.createSignal("/com/canonical/unity/launcherentry/tracktor",
                                        "com.canonical.Unity.LauncherEntry", "Update")
        msg.setArguments(["application://tracktor.desktop", {"count": n, "count-visible": n > 0}])
        QDBusConnection.sessionBus().send(msg)
    except Exception:
        pass


# ───────────────────────────── main window ─────────────────────────────
def tree(headers):
    t = QTreeWidget(); t.setHeaderLabels(headers); t.setUniformRowHeights(True)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    for i in range(len(headers)):
        t.header().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    t.header().setStretchLastSection(True)       # the last visible column takes the remaining space
    t.setStyleSheet("QTreeView::item { padding-right: 14px; }")    # room between columns
    t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    t._layout, t._items, t._groups = None, {}, {}
    t._color_col, t._xcols, t._hover = len(headers) - 1, (), None
    t.setMouseTracking(True)
    t._sort = None                               # (column, order) chosen by clicking a header, or None
    t.header().setSectionsClickable(True); t.header().setSortIndicatorShown(False)
    return t


NO_SEL = "Click a job to see its output  (double click: open it in a new tab)"
