#!/bin/bash
# Install Tracktor for the current user (no root needed):
#   ~/.local/bin/{track,tracktor}, the icons, and a "Tracktor" launcher in the application menu (and on the desktop).
# Usage:  ./install.sh [extra folders to watch ...]
#   e.g.  ./install.sh ~/projects/foo/.jobs      (a project whose own track wrapper writes to .jobs)
# Run it again after `git pull` to update.
set -e
here="$(cd "$(dirname "$0")" && pwd)"

if ! python3 -c "import PyQt6.QtWidgets" 2>/dev/null; then
  echo "Tracktor needs PyQt6 for the system python3.  Install it with your package manager, e.g.:"
  echo "  Debian/Ubuntu:  sudo apt install python3-pyqt6"
  echo "  Fedora:         sudo dnf install python3-pyqt6"
  echo "  Arch:           sudo pacman -S python-pyqt6"
  echo "  (or:            python3 -m pip install --user PyQt6)"
  exit 1
fi

mkdir -p ~/.local/bin ~/.local/state/tracktor ~/.config/tracktor ~/.local/share/applications \
         ~/.local/share/tracktor/icons ~/.local/share/tracktor/lib ~/.local/share/icons/hicolor/scalable/apps
# track is only replaced when it changed (running jobs are executing it)
cmp -s "$here/track" ~/.local/bin/track || install -m 755 "$here/track" ~/.local/bin/
install -m 755 "$here/tracktor" ~/.local/bin/
# the program itself: the tracktor package (the launcher in ~/.local/bin finds it there)
rm -rf ~/.local/share/tracktor/lib/tracktor.new
cp -r "$here/src/tracktor" ~/.local/share/tracktor/lib/tracktor.new
find ~/.local/share/tracktor/lib/tracktor.new -name __pycache__ -prune -exec rm -rf {} +
rm -rf ~/.local/share/tracktor/lib/tracktor
mv ~/.local/share/tracktor/lib/tracktor.new ~/.local/share/tracktor/lib/tracktor
install -m 644 "$here"/icons/*.svg ~/.local/share/tracktor/icons/
install -m 644 "$here/icons/tracktor.svg" ~/.local/share/icons/hicolor/scalable/apps/tracktor.svg

# extra folders watched by Tracktor (one per line in ~/.config/tracktor/dirs)
touch ~/.config/tracktor/dirs
for extra in "$@"; do
  extra=$(realpath -m "$extra")
  grep -qxF "$extra" ~/.config/tracktor/dirs || echo "$extra" >> ~/.config/tracktor/dirs
done

cat > ~/.local/share/applications/tracktor.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Tracktor
GenericName=Job monitor
Comment=Watch local jobs and Slurm cluster jobs
Exec=$HOME/.local/bin/tracktor
Icon=tracktor
Terminal=false
Categories=Utility;Monitor;
StartupWMClass=tracktor
DESK
desk=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
if [ -d "$desk" ]; then
  cp ~/.local/share/applications/tracktor.desktop "$desk/"; chmod +x "$desk/tracktor.desktop"
fi
command -v update-desktop-database >/dev/null && update-desktop-database ~/.local/share/applications 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q ~/.local/share/icons/hicolor 2>/dev/null || true

echo "Installed: track and tracktor in ~/.local/bin, and a 'Tracktor' launcher in the application menu${desk:+ and on $desk}."
echo "Watched folders: ~/.local/state/tracktor $(paste -sd' ' ~/.config/tracktor/dirs)"
command -v notify-send >/dev/null || echo "Tip: install notify-send (libnotify) for desktop notifications."
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "NOTE: add ~/.local/bin to your PATH";; esac
