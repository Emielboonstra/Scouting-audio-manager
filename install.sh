#!/bin/sh
set -eu

if [ "$(uname -s)" != Linux ] || [ "$(id -u)" -eq 0 ]; then
    echo "Voer dit script als gewone gebruiker in een Linux-desktopsessie uit." >&2
    exit 1
fi
case "${1:-}" in
    ""|--autostart) ;;
    *) echo "Gebruik: sh install.sh [--autostart]" >&2; exit 1 ;;
esac
for cmd in python3 pactl paplay pw-cli pw-dump wpctl systemctl; do
    command -v "$cmd" >/dev/null || { echo "Ontbrekende dependency: $cmd" >&2; exit 1; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'
systemctl --user show-environment >/dev/null
if ! LC_ALL=C pactl info | grep -q PipeWire; then
    echo "PipeWire met pipewire-pulse moet actief zijn." >&2
    exit 1
fi

src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
data=${XDG_DATA_HOME:-"$HOME/.local/share"}
conf=${XDG_CONFIG_HOME:-"$HOME/.config"}
app="$data/scouting-audio-manager"
unit="$conf/systemd/user/scouting-audio-manager.service"
desktop="$data/applications/scouting-audio-manager.desktop"
icon="$data/icons/hicolor/scalable/apps/scouting-audio-manager.svg"
mkdir -p "$app" "$(dirname "$unit")" "$(dirname "$desktop")" "$(dirname "$icon")"
python3 -m venv "$app/venv"
"$app/venv/bin/pip" install "$src"
chmod 700 "$app"
# Render platform-specific path quoting without evaluating any path as shell code.
"$app/venv/bin/python" - "$src" "$app" "$unit" "$desktop" <<'PYTHON'
import shlex
import sys
from pathlib import Path

src, app, unit, desktop = map(Path, sys.argv[1:])
package = src / "packaging"
launcher = app / "launch"
launcher.write_text("#!/bin/sh\nsystemctl --user start scouting-audio-manager.service || true\n"
                    + "exec " + shlex.quote(str(app / "venv/bin/scouting-audio")) + ' "$@"\n')
launcher.chmod(0o755)
unit.write_text((package / unit.name).read_text().replace(
    "@ENGINE@", '"' + str(app / "venv/bin/scouting-audio-engine")
    .replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'))
desktop.write_text((package / desktop.name).read_text().replace(
    "@LAUNCHER@", '"' + str(launcher).replace("\\", "\\\\\\\\").replace('"', '\\\\"')
    .replace("$", '\\\\$').replace(chr(96), "\\\\" + chr(96)).replace("%", "%%") + '"'))
PYTHON
install -m 644 "$src/packaging/scouting-audio-manager.svg" "$icon"
systemctl --user daemon-reload
systemctl --user restart scouting-audio-manager.service
if [ "${1:-}" = --autostart ]; then
    systemctl --user enable scouting-audio-manager.service
fi
echo "Geïnstalleerd. Open Scouting Audio Manager vanuit het applicatiemenu."
