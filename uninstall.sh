#!/bin/sh
set -eu
if [ "$(uname -s)" != Linux ] || [ "$(id -u)" -eq 0 ]; then
    echo "Voer dit script als gewone Linux-gebruiker uit." >&2
    exit 1
fi
data=${XDG_DATA_HOME:-"$HOME/.local/share"}
conf=${XDG_CONFIG_HOME:-"$HOME/.config"}
app="$data/scouting-audio-manager"
unit="$conf/systemd/user/scouting-audio-manager.service"
if [ -f "$unit" ]; then
    systemctl --user disable --now scouting-audio-manager.service
    rm -f -- "$unit"
    systemctl --user daemon-reload
fi
rm -f -- "$data/applications/scouting-audio-manager.desktop" \
    "$data/icons/hicolor/scalable/apps/scouting-audio-manager.svg" "$app/launch"
if [ -d "$app/venv" ] && [ -f "$app/venv/pyvenv.cfg" ]; then
    rm -rf -- "$app/venv"
fi
rmdir "$app" 2>/dev/null || true
echo "Applicatie verwijderd. De configuratie staat in $conf/scouting-audio-manager."
