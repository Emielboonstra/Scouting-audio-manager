"""Configuration schema and atomic persistence shared by the daemon and UI."""

import copy
import json
import os
import re
import tempfile
from pathlib import Path

APP = "scouting-audio-manager"
SOURCES = {"local": "Computeraudio", "spotify": "Spotify Connect",
           "airplay": "AirPlay", "music_assistant": "Music Assistant"}
MUSIC = ("spotify", "airplay", "music_assistant")


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP


def runtime_dir():
    return Path(os.environ["XDG_RUNTIME_DIR"]) / APP


def defaults():
    return {
        "version": 1, "master": 30, "mute": True, "exclusive": True,
        "theme": "Systeem", "tray": False, "background": False,
        "rate": 48000, "buffer": 20,
        "zones": {}, "routes": {}, "profiles": {},
        "sources": {key: {"name": name, "volume": 75, "mute": False,
                          "device": ""} for key, name in SOURCES.items()},
        "services": {
            "spotify": {"name": "Scouting Audio", "auto": False},
            "airplay": {"name": "Scouting Audio", "auto": False},
            "music_assistant": {"name": "Scouting Audio", "auto": False, "url": ""},
        },
    }


def new_zone(device, name):
    return {"name": name, "device": device, "enabled": True, "mute": False,
            "volume": 30, "eq": False, "bass": 0, "mid": 0, "treble": 0, "delay": 0}


def new_route():
    return {"enabled": False, "volume": 100, "mute": False, "delay": 0}


def safe_id(value):
    return bool(re.fullmatch(r"[a-zA-Z0-9_]{1,64}", value))


def validate(config):
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Onbekend configuratieformaat")

    def fields(obj, schema):
        if not isinstance(obj, dict) or obj.keys() != schema.keys():
            raise ValueError("Ongeldige configuratievelden")
        for field, sample in schema.items():
            val = obj[field]
            if type(val) is not type(sample):
                raise ValueError(f"Ongeldig type voor {field}")
            if isinstance(val, str) and (
                len(val) > 500 or any(ord(c) < 32 for c in val)
            ):
                raise ValueError(f"Ongeldige tekst voor {field}")

    def bounds(obj, key, lower, upper):
        if not lower <= obj[key] <= upper:
            raise ValueError(f"{key} moet tussen {lower} en {upper} liggen")

    def mixer(obj):
        fields(obj, {key: defaults()[key] for key in PROFILE_KEYS})
        bounds(obj, "master", 0, 100)
        devices = set()
        for zid, zone in obj["zones"].items():
            if not safe_id(zid):
                raise ValueError("Ongeldige zone-ID")
            fields(zone, new_zone("", ""))
            if not zone["name"].strip() or not zone["device"] or zone["device"] in devices:
                raise ValueError("Elke zone vereist een naam en een eigen uitgang")
            devices.add(zone["device"])
            bounds(zone, "volume", 0, 100)
            bounds(zone, "delay", 0, 500)
            for key in ("bass", "mid", "treble"):
                bounds(zone, key, -12, 12)
        for sid, source in obj["sources"].items():
            if not safe_id(sid) or sid == "scouting_test":
                raise ValueError("Ongeldige bron-ID")
            fields(source, defaults()["sources"]["local"])
            bounds(source, "volume", 0, 100)
            if not source["name"].strip() or bool(source["device"]) == (sid in SOURCES):
                raise ValueError("Ongeldige bron")
        if not SOURCES.keys() <= obj["sources"].keys():
            raise ValueError("Ingebouwde bron ontbreekt")
        for sid, row in obj["routes"].items():
            if sid not in obj["sources"] or not isinstance(row, dict):
                raise ValueError("Onbekende bron in routing")
            for zid, route in row.items():
                if zid not in obj["zones"]:
                    raise ValueError("Onbekende zone in routing")
                fields(route, new_route())
                bounds(route, "volume", 0, 100)
                bounds(route, "delay", 0, 500)

    fields(config, defaults())
    mixer({key: config[key] for key in PROFILE_KEYS})
    fields(config["services"], defaults()["services"])
    for name, service in config["services"].items():
        fields(service, defaults()["services"][name])
        if not service["name"].strip():
            raise ValueError("Ontvangernaam mag niet leeg zijn")
    if config["rate"] not in (44100, 48000, 96000):
        raise ValueError("Ongeldige samplerate")
    bounds(config, "buffer", 5, 100)
    if config["theme"] not in ("Systeem", "Licht", "Donker"):
        raise ValueError("Ongeldig thema")
    for name, profile in config["profiles"].items():
        if not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise ValueError("Ongeldige profielnaam")
        mixer(profile)
    return config


PROFILE_KEYS = ("master", "mute", "zones", "sources", "routes")


def profile_snapshot(config):
    return copy.deepcopy({key: config[key] for key in PROFILE_KEYS})


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class Store:
    def __init__(self, path):
        self.path = path
        self.data = validate(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else defaults()
        self.revision = 0

    def replace(self, value, revision):
        if revision != self.revision:
            raise ValueError("Configuratie is intussen gewijzigd. Probeer opnieuw.")
        validate(value)
        atomic_json(self.path, value)
        self.data = value
        self.revision += 1
