#!/bin/sh
set -e
dir=$(cd "$(dirname "$0")" && pwd)
name=clawd-tg-bot
config="$HOME/.config/$name"
units="$HOME/.config/systemd/user"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required"; exit 1; }

[ -d "$dir/.venv" ] || python3 -m venv "$dir/.venv"
"$dir/.venv/bin/python" -m pip install -q -r "$dir/requirements.txt"

mkdir -p "$config" "$units" "$HOME/.local/bin"
sed "s|@DIR@|$dir|g" "$dir/deploy/$name.service" > "$units/$name.service"
ln -sf "$dir/deploy/$name" "$HOME/.local/bin/$name"
systemctl --user daemon-reload

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: add ~/.local/bin to PATH to use the $name command" ;;
esac

if [ -t 0 ] && [ -t 1 ]; then
  echo "installed. starting setup"
  echo
  exec "$dir/.venv/bin/python" "$dir/setup_wizard.py"
fi
if [ ! -f "$config/env" ]; then
  cp "$dir/deploy/env.example" "$config/env"
  chmod 600 "$config/env"
fi
echo "installed. next: $name setup, then $name on"
