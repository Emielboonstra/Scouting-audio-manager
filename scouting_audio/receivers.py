"""Receiver lifecycle and MPRIS transport."""

import json
import logging
import os
import shutil
import subprocess
import time
from urllib.parse import urlsplit

from .config import MUSIC, config_dir
from .pipewire import bus, run, stop

LOG = logging.getLogger(__name__)
BINARIES = {"spotify": "spotifyd", "airplay": "shairport-sync", "music_assistant": "sendspin"}
DBUS_PLAYERS = {"spotify": "spotifyd", "airplay": "shairportsync", "music_assistant": "sendspin"}


class Receivers:
    def __init__(self, runtime, settings):
        self.runtime = runtime
        self.wanted = {key: settings[key]["auto"] for key in MUSIC}
        self.processes = {}
        self.retry = {}
        self.status = {}
        self.players = {}
        self.playing = set()
        self.winner = None
        self.suppressed = set()

    def command(self, key, settings):
        sink = bus(key)
        name = settings["name"]
        if key == "spotify":
            path = self.runtime / "spotifyd.toml"
            path.write_text(
                '[global]\nbackend = "pulseaudio"\nuse_mpris = true\n'
                'volume_controller = "softvol"\ninitial_volume = 65\n'
                f'device = "{sink}"\n'
                f'device_name = {json.dumps(name, ensure_ascii=False)}\n',
                encoding="utf-8",
            )
            return ["spotifyd", "--no-daemon", "--config-path", str(path)]
        if key == "airplay":
            path = self.runtime / "shairport.conf"
            path.write_text(
                "general = { name = " + json.dumps(name, ensure_ascii=False).replace("${", "$${") +
                '; output_backend = "pa"; port = 5100; mpris_service_bus = "session";'
                ' dbus_nametag = "ScoutingAudio"; };\n'
                f'pa = {{ sink = "{sink}"; }};\n'
                'metadata = { enabled = "yes"; include_cover_art = "yes"; };\n',
                encoding="utf-8",
            )
            return ["shairport-sync", "-c", str(path)]
        url = urlsplit(settings["url"])
        if url.scheme not in ("ws", "wss") or not url.hostname or url.username or url.password:
            raise ValueError("Gebruik een volledig ws:// of wss:// Sendspin-serveradres")
        directory = config_dir() / "sendspin"
        return ["sendspin", "daemon", "--settings-dir", str(directory), "--url", settings["url"],
                "--name", name, "--audio-device", "pulse", "--hardware-volume", "false"]

    def update(self, settings, ready, exclusive, active_audio):
        running = set()
        for key, executable in BINARIES.items():
            previous = self.processes.get(key)
            if previous and (not self.wanted[key] or previous[1] != settings[key]
                             or key not in ready or previous[0].poll() is not None):
                process, old_settings = self.processes.pop(key)
                had_exited = process.poll() is not None
                stop(process)
                if had_exited and old_settings == settings[key]:
                    self.retry[key] = time.monotonic() + 8
                    LOG.warning("%s gestopt met code %s", executable, process.returncode)
            if not self.wanted[key]:
                self.retry.pop(key, None)
                self.status[key] = "Uit"
                continue
            if not shutil.which(executable):
                self.status[key] = f"{executable} ontbreekt"
                continue
            if key not in ready:
                self.status[key] = "Wacht op PipeWire"
                continue
            if time.monotonic() < self.retry.get(key, 0):
                self.status[key] = "Gestopt · nieuwe poging volgt"
                continue
            if key not in self.processes:
                try:
                    command = self.command(key, settings[key])
                    env = {**os.environ, "PULSE_SINK": bus(key), "PULSE_PROP": "node.dont-fallback=true"}
                    self.processes[key] = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL), settings[key].copy()
                except (ValueError, OSError) as exc:
                    self.status[key] = str(exc)
                    continue
            running.add(key)
            self.status[key] = "Ontvanger draait"

        available = self.player_names() if running else []
        self.players = {}
        for key in running:
            prefix = DBUS_PLAYERS[key]
            match = next((p for p in available if p.lower().split(".")[0] == prefix
                          and (key != "spotify" or p == f"spotifyd.instance{self.processes[key][0].pid}")
                          and (key != "airplay" or p.endswith(".ScoutingAudio"))), None)
            if match:
                self.players[key] = match
        playing = {key for key in running
                   if (self.playerctl(key, "status") == "Playing" if key in self.players
                       else key in active_audio)}
        started = [key for key in MUSIC if key in playing - self.playing]
        if started:
            self.winner = started[-1]
        elif self.winner not in playing and playing:
            self.winner = next(key for key in MUSIC if key in playing)
        self.playing = playing
        self.suppressed = (playing - {self.winner}) if exclusive else set()
        if exclusive and started:
            for key in self.suppressed:
                self.playerctl(key, "pause")

    @staticmethod
    def player_names():
        try:
            return run("playerctl", "-l").splitlines()
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return []

    def playerctl(self, key, *args, strict=False):
        try:
            return run("playerctl", "--player", self.players[key], *args)
        except (KeyError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            if strict:
                raise ValueError("Deze speler ondersteunt de actie momenteel niet") from exc
            return ""

    def now_playing(self):
        key = self.winner if self.winner in self.playing else next(
            (source for source in MUSIC if source in self.players), None)
        if not key:
            return {}
        template = "{{title}}\n{{artist}}\n{{mpris:artUrl}}\n{{mpris:length}}\n{{position}}"
        meta = self.playerctl(key, "metadata", "--format", template).splitlines()
        meta += [""] * (5 - len(meta))
        title, artist, art, duration, position = meta[:5]

        def seconds(text):
            try:
                return max(0, int(float(text) / 1_000_000))
            except (ValueError, OverflowError):
                return 0

        return {"source": key, "title": title, "artist": artist, "art": art,
                "duration": seconds(duration), "position": seconds(position),
                "playing": key in self.playing, "controllable": key in self.players}

    def transport(self, key, action, position):
        if action in ("play-pause", "previous", "next"):
            self.playerctl(key, action, strict=True)
        elif action == "seek" and type(position) is int and 0 <= position <= 86400:
            self.playerctl(key, "position", str(position), strict=True)
        else:
            raise ValueError("Onbekende afspeelactie")

    def close(self):
        for process, _ in self.processes.values():
            stop(process)
        self.processes.clear()
