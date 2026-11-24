#!/bin/sh
# Installs the `focus` command: a private virtualenv with bleak + a launcher in ~/.local/bin.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"   # PYTHON=/path/to/python3 ./install.sh to pick another one
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/focus" <<SH
#!/bin/sh
exec "$DIR/.venv/bin/python" "$DIR/focus.py" "\$@"
SH
chmod +x "$HOME/.local/bin/focus"
echo "Installed: $HOME/.local/bin/focus -> $DIR/focus.py"
