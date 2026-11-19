"""Entry point: `tracktor` opens the window (single instance); `tracktor --list` prints the jobs (no display needed)."""
import os, re, sys

from . import __version__

USAGE = """usage: tracktor [--list [--local | --clusters] [--hours N]]

  (no options)      open the window, or bring the one already running to the front
  --list, -l        print the running jobs (local and on the clusters) and those finished in the last 24 h;
                    no display needed (e.g. over SSH)
  --version
Opened over SSH (ssh -Y), it starts its own window there, separate from the one on the desktop."""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["-h"], ["--help"]):
        print(USAGE); return
    if argv[:1] == ["--version"]:
        print(f"tracktor {__version__}"); return
    if argv[:1] in (["--list"], ["-l"], ["list"]):
        from .cli import list_main
        sys.exit(list_main(argv[1:]))
    if argv:
        print(USAGE, file=sys.stderr); sys.exit(2)
    gui()


def instance_name():
    """Single instance per user and display: over SSH (with X forwarding) the window must open on the remote
    screen, not ask the one on the desktop to show itself."""
    name = os.environ.get("TRACKTOR_INSTANCE")
    if name:
        return name
    name = f"tracktor-{os.getuid()}"
    if os.environ.get("SSH_CONNECTION"):
        name += "-ssh-" + re.sub(r"[^A-Za-z0-9]", "_", os.environ.get("DISPLAY", "") or "nodisplay")
    return name


def gui():
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("tracktor: no display to open the window on.\n"
              "  Over SSH: connect with  ssh -Y <host>  to see the window, or run  tracktor --list  for a text summary.",
              file=sys.stderr)
        sys.exit(1)
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    from PyQt6.QtWidgets import QApplication

    from .config import APP, load_file_types, load_settings
    from .ui.common import app_icon, taskbar_count
    from .ui.main_window import Main

    load_settings(); load_file_types()
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP)
    app.setDesktopFileName("tracktor")        # so the desktop shows the launcher's icon, not a generic one
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)      # with the tray, closing the window does not quit
    # Single instance: if one is already running (e.g. hidden in the tray), ask it to show itself and exit.
    name = instance_name()
    sock = QLocalSocket(); sock.connectToServer(name)
    if sock.waitForConnected(300):
        sock.write(b"show"); sock.flush(); sock.waitForBytesWritten(300)
        sys.exit(0)
    QLocalServer.removeServer(name)
    server = QLocalServer(); server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption); server.listen(name)
    app.aboutToQuit.connect(lambda: taskbar_count(0))
    w = Main(); w.show()
    server.newConnection.connect(lambda: (server.nextPendingConnection(), w.show_window()))
    sys.exit(app.exec())
