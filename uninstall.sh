#!/bin/bash
# Remove the files installed by install.sh.  Your settings, clusters and history are kept unless you pass --purge.
desk=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
rm -f ~/.local/bin/track ~/.local/bin/tracktor ~/.local/share/applications/tracktor.desktop "$desk/tracktor.desktop" \
      ~/.local/share/icons/hicolor/scalable/apps/tracktor.svg ~/.local/share/icons/hicolor/*/apps/tracktor.png
rm -rf ~/.local/share/tracktor
if [ "$1" = "--purge" ]; then
  rm -rf ~/.config/tracktor ~/.local/state/tracktor ~/.cache/tracktor
  echo "Tracktor removed, including settings, history and temporary logs."
else
  echo "Tracktor removed.  Settings and history kept in ~/.config/tracktor and ~/.local/state/tracktor"
  echo "(run ./uninstall.sh --purge to delete them too)."
fi
