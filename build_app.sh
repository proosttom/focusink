#!/bin/sh
# Builds FocusInk.app (py2app, no Xcode tools needed) and installs it into ~/Applications.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
[ -x .venv/bin/python ] || ./install.sh
.venv/bin/pip install -q -r requirements-app.txt
rm -rf build dist 2>/dev/null || { sleep 1; rm -rf build dist; }   # Finder/Spotlight can hold dist briefly
.venv/bin/python setup_app.py py2app >/dev/null
mkdir -p "$HOME/Applications"
pkill -x FocusInk 2>/dev/null || true
rm -rf "$HOME/Applications/FocusInk.app"
cp -R dist/FocusInk.app "$HOME/Applications/"
cp sprint.example.md sprint.md   # fresh test sprint on every build
echo "Installed: $HOME/Applications/FocusInk.app (sprint.md reset from sprint.example.md)"
