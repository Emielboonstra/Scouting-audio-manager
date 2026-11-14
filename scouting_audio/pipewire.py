"""PipeWire mixer: one shared monitor per source, one DSP sink per zone."""

import json
import logging
import math
import os
import shlex
import subprocess
import time
import wave
from array import array

from .config import SOURCES

LOG = logging.getLogger(__name__)
PREFIX = "scouting_audio_"
TEST = "scouting_test"


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=4,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise RuntimeError(f"{args[0]}: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def pulse(*args):
    return run("pactl", *map(str, args))


def listing(kind):
    return json.loads(pulse("-f", "json", "list", kind))


def modules_by_index():
    # Some pactl versions omit the module index in their JSON output.
    modules = {}
    for line in pulse("list", "short", "modules").splitlines():
        fields = line.split("\t")
        if len(fields) >= 3 and fields[0].isdigit():
            index, name, argument = fields[:3]
            modules[int(index)] = {"name": name, "argument": argument}
    return modules


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def bus(key):
    return PREFIX + key


def linked(node, target):
    return {"node.name": node, "target.object": target, "node.autoconnect": True,
            "node.dont-fallback": True, "node.dont-move": True, "node.dont-reconnect": True}


def zone_graph(zid, zone, rate, buffer):
    name = bus("zone_" + zid)
    filters = [("bass", "bq_lowshelf", 100), ("mid", "bq_peaking", 1000),
               ("treble", "bq_highshelf", 10000)]
    nodes, links, inputs, outputs = [], [], [], []
    for side in ("l", "r"):
        chain = []
        for key, label, frequency in filters:
            chain.append((side + key, label, {"Freq": frequency, "Q": 0.707,
                                               "Gain": zone[key] if zone["eq"] else 0}))
        delay = side + "delay"
        if zone["delay"]:
            chain.append((delay, "delay", {"Delay (s)": zone["delay"] / 1000}))
        for node, label, control in chain:
            spec = {"type": "builtin", "name": node, "label": label, "control": control}
            if label == "delay":
                spec["config"] = {"max-delay": 0.5}
            nodes.append(spec)
        inputs.append(chain[0][0] + ":In")
        outputs.append(chain[-1][0] + ":Out")
        links.extend({"output": a[0] + ":Out", "input": b[0] + ":In"}
                     for a, b in zip(chain, chain[1:]))
    return {
        "audio.channels": 2, "audio.position": ["FL", "FR"],
        "node.latency": f"{rate * buffer // 1000}/{rate}",
        "filter.graph": {"nodes": nodes, "links": links, "inputs": inputs, "outputs": outputs},
        "capture.props": {"node.name": name, "node.description": "Scouting zone " + zid,
                          "media.class": "Audio/Sink", "node.virtual": True,
                          "priority.session": 0, "node.param.Props": {"mute": True}},
        "playback.props": {**linked(name + "_output", zone["device"]), "node.passive": True},
    }


def eq_headroom(zone):
    boost = sum(max(0, zone[key]) for key in ("bass", "mid", "treble")) if zone["eq"] else 0
    return 10 ** (-boost / 20)


def device_label(item):
    return item.get("description") or item["name"]


def physical(item):
    props = item.get("properties", {})
    return not item["name"].startswith(PREFIX) and (
        props.get("device.api") == "alsa" or item["name"].startswith("alsa_")
        or (props.get("device.class") == "sound" and
            str(props.get("node.virtual", "false")).lower() != "true"))


class Mixer:
    def __init__(self, runtime):
        self.runtime = runtime
        self.filters = {}
        self.route_gains = {}
        self.zone_gains = {}
        self.eq_gains = {}
        self.filter_retry = {}
        self.outputs, self.inputs = {}, {}
        self.states = {}
        self.ready_buses = set()
        self.test_zones = set()
        self.test_process = None
        self.playing_buses = set()
        self.errors = []

    def update(self, cfg, suppressed):
        self.errors = []
        if "PipeWire" not in pulse("info"):
            raise RuntimeError("PipeWire Pulse-server niet beschikbaar")
        if self.test_process and self.test_process.poll() is not None:
            self.test_process = None
            self.test_zones.clear()
        sinks = {x["name"]: x for x in listing("sinks")}
        sources = {x["name"]: x for x in listing("sources")}
        modules = modules_by_index()
        self.outputs = {k: device_label(x) for k, x in sinks.items() if physical(x) or k.startswith("bluez_output.")}
        self.inputs = {k: device_label(x) for k, x in sources.items()
                       if physical(x) and not k.endswith(".monitor")}
        self.ready_buses.clear()
        for key in (*SOURCES, TEST):
            name = bus(key)
            if name not in sinks:
                description = "Scouting Audio" if key == "local" else SOURCES.get(key, "Scouting synchronisatietest")
                if key in SOURCES and key != "local":
                    description = "Scouting " + description
                pulse("load-module", "module-null-sink", "sink_name=" + name,
                      f'sink_properties="device.description=\'{description}\' priority.session=0"',
                      "channels=2", "channel_map=front-left,front-right")
            else:
                self.ready_buses.add(key)
        streams = listing("sink-inputs")
        active_outputs = {stream["sink"] for stream in streams
                          if not stream.get("corked") and not stream.get("mute")}
        self.playing_buses = {key for key in SOURCES
                              if sinks.get(bus(key), {}).get("index") in active_outputs}
        self.states = {}
        self.zone_gains.clear()
        for zid in set(self.filters) - cfg["zones"].keys():
            self.remove_filter(zid)
        self.filter_retry = {zid: until for zid, until in self.filter_retry.items()
                             if zid in cfg["zones"]}
        for zid, zone in cfg["zones"].items():
            self.states[zid] = self.ensure_zone(zid, zone, cfg, sinks)
        self.reconcile_routes(cfg, suppressed, sinks, sources, modules, streams)

    def remove_filter(self, zid):
        process, _, _ = self.filters.pop(zid)
        stop(process)
        self.zone_gains.pop(zid, None)
        self.eq_gains.pop(zid, None)

    def ensure_zone(self, zid, zone, cfg, sinks):
        name = bus("zone_" + zid)
        if zone["device"] not in self.outputs:
            if zid in self.filters:
                self.remove_filter(zid)
            return "Niet verbonden"
        if time.monotonic() < self.filter_retry.get(zid, 0):
            self.errors.append(f"{zone['name']}: PipeWire-filter niet beschikbaar")
            return "Filter wordt hersteld"
        args = json.dumps(zone_graph(zid, {**zone, "eq": False}, cfg["rate"], cfg["buffer"]),
                          separators=(",", ":"))
        if zid in self.filters:
            process, previous, started = self.filters[zid]
            if previous != args or process.poll() is not None or (name not in sinks and
                                                                 time.monotonic() - started > 4):
                self.remove_filter(zid)
                if previous == args:
                    self.filter_retry[zid] = time.monotonic() + 4
                    self.errors.append(f"{zone['name']}: PipeWire-filter niet beschikbaar")
                return "Filter wordt hersteld"
        if zid not in self.filters:
            if name in sinks:
                return "Filter wordt hersteld"
            process = subprocess.Popen(["pw-cli", "-m", "load-module",
                                        "libpipewire-module-filter-chain", args],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            self.filters[zid] = process, args, time.monotonic()
            return "Filter wordt gestart"
        if name not in sinks:
            return "Filter wordt gestart"
        if zone["enabled"]:
            return "Verbonden"
        return "Uitgeschakeld"

    def desired_routes(self, cfg, suppressed, sources):
        desired = {}
        for zid, zone in cfg["zones"].items():
            if self.states.get(zid) != "Verbonden":
                continue
            for sid, source in cfg["sources"].items():
                route = cfg["routes"].get(sid, {}).get(zid)
                if not route or not route["enabled"]:
                    continue
                capture = source["device"] or bus(sid) + ".monitor"
                if capture not in sources or (source["device"] and capture not in self.inputs):
                    continue
                gain = source["volume"] * route["volume"] / 10000
                if source["mute"] or route["mute"] or sid in suppressed:
                    gain = 0
                desired[(sid, zid)] = capture, route["delay"], gain
            if zid in self.test_zones and bus(TEST) + ".monitor" in sources:
                desired[(TEST, zid)] = bus(TEST) + ".monitor", 0, 1.0
        return desired

    def reconcile_routes(self, cfg, suppressed, sinks, sources, modules, streams):
        desired = self.desired_routes(cfg, suppressed, sources)
        names = {bus(f"route_{sid}.{zid}"): (sid, zid) for sid, zid in desired}
        stream_items = {x.get("properties", {}).get("node.name"): x for x in streams}
        applied = set()
        for module_id, module in modules.items():
            args = module.get("argument") or ""
            if module["name"] != "module-loopback" or "node.name=" + PREFIX + "route_" not in args:
                continue
            tokens = dict(token.split("=", 1) for token in shlex.split(args) if "=" in token)
            name = tokens.get("sink_input_properties", "").split(" ", 1)[0].removeprefix("node.name=")
            key = names.get(name)
            expected = self.route_options(key, desired[key], cfg) if key else []
            if key in applied or not key or tokens != {
                k: shlex.split(v)[0] for k, v in (part.split("=", 1) for part in expected)
            }:
                pulse("unload-module", module_id)
                continue
            if stream_items.get(name, {}).get("sink") != sinks[bus("zone_" + key[1])]["index"]:
                pulse("unload-module", module_id)
                continue
            applied.add(key)
        pending_zones = {zid for sid, zid in desired
                         if (sid, zid) not in applied or bus(f"route_{sid}.{zid}") not in stream_items}
        for zid, state in self.states.items():
            name = bus("zone_" + zid)
            if name not in sinks:
                continue
            zone = cfg["zones"][zid]
            level = cfg["master"] * zone["volume"] / 10000
            level *= eq_headroom(zone)
            mute = cfg["mute"] or zone["mute"] or state != "Verbonden" or zid in pending_zones
            self.set_zone_gain(zid, sinks[name], level, mute)
        active_indices = set()
        for key, value in desired.items():
            sid, zid = key
            if key not in applied:
                pulse("load-module", "module-loopback", *self.route_options(key, value, cfg))
                continue
            stream = stream_items.get(bus(f"route_{sid}.{zid}"))
            if stream is not None:
                index = stream["index"]
                active_indices.add(index)
                gain = round(100 * value[2] ** (1 / 3), 2)
                if self.route_gains.get(index) != (key, gain):
                    pulse("set-sink-input-volume", index, f"{gain}%")
                    self.route_gains[index] = key, gain
        self.route_gains = {idx: gain for idx, gain in self.route_gains.items() if idx in active_indices}
        for zid, zone in cfg["zones"].items():
            name = bus("zone_" + zid)
            if name in sinks and self.states.get(zid) == "Verbonden":
                try:
                    self.apply_equalizer(zid, sinks[name], zone)
                except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.errors.append(f"{zone['name']}: EQ-aanpassing mislukt ({exc})")

    def apply_equalizer(self, zid, sink, zone):
        serial = sink["index"]
        gains = tuple(zone[key] if zone["eq"] else 0 for key in ("bass", "mid", "treble"))
        if self.eq_gains.get(zid) == (serial, gains):
            return
        params = []
        for side in ("l", "r"):
            for band, value in zip(("bass", "mid", "treble"), gains):
                params.extend((side + band + ":Gain", float(value)))
        node = sink.get("properties", {}).get("object.id")
        if node is None:
            objects = json.loads(run("pw-dump"))
            node = next((obj["id"] for obj in objects
                         if obj.get("info", {}).get("props", {}).get("node.name") == sink["name"]), None)
        if node is None:
            raise RuntimeError("EQ-filter niet gevonden in de PipeWire-grafiek")
        run("pw-cli", "set-param", str(node), "Props", json.dumps({"params": params}))
        self.eq_gains[zid] = serial, gains

    def set_zone_gain(self, zid, sink, linear, mute):
        name = sink["name"]
        gain = round(100 * linear ** (1 / 3), 2)
        current = sink.get("volume", {})
        if mute and not sink.get("mute"):
            pulse("set-sink-mute", name, "1")
        if not current or any(abs(ch["value"] - gain * 655.36) > 5 for ch in current.values()):
            pulse("set-sink-volume", name, f"{gain}%")
        # Unmuting is deferred until every route has its assigned software gain.
        self.zone_gains[zid] = name, bool(sink.get("mute")) and not mute

    def finish_update(self):
        for name, unmute in self.zone_gains.values():
            if unmute:
                pulse("set-sink-mute", name, "0")

    @staticmethod
    def route_options(key, value, cfg):
        sid, zid = key
        capture, delay, _ = value
        stream = bus(f"route_{sid}.{zid}")
        props = " node.dont-fallback=true node.dont-move=true "
        props += f"node.latency={cfg['rate'] * cfg['buffer'] // 1000}/{cfg['rate']} node.rate=1/{cfg['rate']}"
        return ["source=" + shlex.quote(capture), "sink=" + bus("zone_" + zid),
                "source_dont_move=true", "sink_dont_move=true",
                "latency_msec=" + str(cfg["buffer"] + delay),
                "channels=2", "channel_map=front-left,front-right",
                f"sink_input_properties='node.name={stream}{props}'",
                f"source_output_properties='node.name={stream}_capture{props}'"]

    def start_test(self, zone_ids, cfg, suppressed):
        if self.test_process:
            raise ValueError("Er loopt al een synchronisatietest")
        if cfg["mute"] or not cfg["master"]:
            raise ValueError("Zet master mute uit en kies een laag hoorbaar niveau")
        self.test_zones = {zid for zid in zone_ids if self.states.get(zid) == "Verbonden"
                           and not cfg["zones"][zid]["mute"] and cfg["zones"][zid]["volume"]}
        if not self.test_zones:
            raise ValueError("Selecteer minstens een hoorbare zone")
        try:
            # Give both loopback streams time to negotiate before the first pulse.
            for _ in range(3):
                self.update(cfg, suppressed)
                self.finish_update()
                time.sleep(0.1)
            path = self.runtime / "sync.wav"
            rate = 48000
            samples = array("h", (int(1500 * math.sin(2 * math.pi * 850 * i / rate) *
                                      math.sin(math.pi * i / 4800) ** 2)
                                  for i in range(4800)))
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                output.writeframes((bytes(rate // 2) + samples.tobytes() +
                                    bytes(rate * 3 // 2)) * 4)
            self.test_process = subprocess.Popen(["paplay", "--device=" + bus(TEST), str(path)])
        except Exception:
            self.test_zones.clear()
            raise

    def disconnect(self):
        self.ready_buses.clear()
        self.playing_buses.clear()
        self.route_gains.clear()
        self.states.clear()
        self.zone_gains.clear()
        self.filter_retry.clear()
        self.outputs.clear()
        self.inputs.clear()
        stop(self.test_process)
        self.test_process = None
        self.test_zones.clear()
        for zid in list(self.filters):
            self.remove_filter(zid)

    def close(self):
        self.disconnect()
        try:
            for index, module in modules_by_index().items():
                args = module.get("argument") or ""
                if (module["name"] == "module-null-sink" and f"sink_name={PREFIX}" in args
                        or module["name"] == "module-loopback" and f"node.name={PREFIX}route_" in args):
                    pulse("unload-module", index)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            LOG.warning("PipeWire cleanup kon niet volledig worden uitgevoerd")
