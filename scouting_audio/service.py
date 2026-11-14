"""Headless systemd-user service."""

import fcntl
import json
import logging
import os
import signal
import socketserver
import subprocess
import time
import uuid

from .config import SOURCES, Store, config_dir, runtime_dir
from .ipc import MAX_REQUEST
from .pipewire import Mixer, run
from .receivers import Receivers

LOG = logging.getLogger(__name__)


class Engine:
    def __init__(self, runtime):
        self.store = Store(config_dir() / "config.json")
        self.mixer = Mixer(runtime)
        self.receivers = Receivers(runtime, self.store.data["services"])
        self.online = False
        self.message = ""
        self.playback = {}
        self.spotify_player = None
        self.spotify_level = None
        self.spotify_pending_until = 0.0
        self.instance = uuid.uuid4().hex

    def refresh(self):
        cfg = self.store.data
        try:
            self.mixer.update(cfg, self.receivers.suppressed)
            self.mixer.finish_update()
            self.online = True
            self.set_message(" · ".join(self.mixer.errors))
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.online = False
            self.set_message(str(exc))
            self.mixer.disconnect()
        self.receivers.update(cfg["services"], self.mixer.ready_buses, cfg["exclusive"],
                              self.mixer.playing_buses)
        self.sync_spotify_volume()
        self.playback = self.receivers.now_playing()

    def sync_spotify_volume(self):
        player = self.receivers.players.get("spotify")
        if player != self.spotify_player:
            self.spotify_player = player
            self.spotify_level = None
        if not player:
            return
        try:
            level = round(float(self.receivers.playerctl("spotify", "volume")) * 100)
        except (ValueError, OverflowError):
            return
        if not 0 <= level <= 100:
            return
        if self.spotify_level is None:
            self.set_spotify_volume(self.store.data["master"])
        elif level == self.spotify_level:
            self.spotify_pending_until = 0.0
        elif time.monotonic() >= self.spotify_pending_until:
            self.spotify_level = level
            if level != self.store.data["master"]:
                self.store.replace({**self.store.data, "master": level}, self.store.revision)

    def set_spotify_volume(self, level):
        if not self.spotify_player:
            return
        try:
            self.receivers.playerctl("spotify", "volume", str(level / 100), strict=True)
        except ValueError:
            return
        self.spotify_level = level
        self.spotify_pending_until = time.monotonic() + 3

    def set_message(self, message):
        if message and message != self.message:
            LOG.warning("%s", message)
        self.message = message

    def dispatch(self, request):
        action = request["action"]
        if action == "state":
            return {
                "config": self.store.data, "revision": self.store.revision, "instance": self.instance,
                "online": self.online,
                "message": self.message, "outputs": self.mixer.outputs, "inputs": self.mixer.inputs,
                "zones": self.mixer.states, "services": self.receivers.status,
                "wanted": self.receivers.wanted, "now": self.playback,
                "suppressed": sorted(self.receivers.suppressed),
                "active_sources": sorted(self.mixer.playing_buses),
            }
        if action == "save":
            if request["instance"] != self.instance:
                raise ValueError("De engine is herstart. Configuratie opnieuw geladen.")
            previous_master = self.store.data["master"]
            self.store.replace(request["config"], request["revision"])
            if self.store.data["master"] != previous_master:
                self.set_spotify_volume(self.store.data["master"])
        elif action == "receiver":
            key, wanted = request["source"], request["wanted"]
            if key not in self.receivers.wanted or type(wanted) is not bool:
                raise ValueError("Onbekende ontvanger")
            self.receivers.wanted[key] = wanted
            self.receivers.retry.pop(key, None)
        elif action == "transport":
            self.receivers.transport(request["source"], request["command"], request.get("position"))
        elif action == "test":
            self.mixer.start_test(request["zones"], self.store.data, self.receivers.suppressed)
        elif action == "autostart":
            option = "enable" if request["enabled"] else "disable"
            run("systemctl", "--user", option, "scouting-audio-manager.service")
        elif action == "logs":
            return run("journalctl", "--user", "-u", "scouting-audio-manager.service",
                       "-n", "100", "--no-pager")
        else:
            raise ValueError("Onbekende actie")
        return self.dispatch({"action": "state"})


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(1)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if not line or len(line) > MAX_REQUEST:
                return
            result = self.server.engine.dispatch(json.loads(line))
            response = {"ok": True, "data": result}
        except (ValueError, KeyError, TypeError, AttributeError, RuntimeError, OSError,
                subprocess.SubprocessError) as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.umask(0o077)
    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "engine.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = runtime / "engine.sock"
        path.unlink(missing_ok=True)
        engine = Engine(runtime)
        running = True

        def shutdown(*_args):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        try:
            with socketserver.UnixStreamServer(str(path), Handler) as server:
                server.engine = engine
                server.timeout = 0.2
                refresh_at = 0.0
                while running:
                    if time.monotonic() >= refresh_at:
                        engine.refresh()
                        refresh_at = time.monotonic() + 0.7
                    server.handle_request()
        finally:
            engine.receivers.close()
            engine.mixer.close()
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
