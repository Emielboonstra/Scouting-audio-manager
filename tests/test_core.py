import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scouting_audio.config import Store, defaults, new_route, new_zone, profile_snapshot, validate
from scouting_audio.pipewire import Mixer, bus, eq_headroom, modules_by_index, zone_graph
from scouting_audio.receivers import Receivers


def mixer_config():
    config = defaults()
    config["zones"] = {"hall": new_zone("alsa_output.hall", "Grote zaal"),
                       "bar": new_zone("alsa_output.bar", "Bar")}
    config["routes"] = {
        "local": {"hall": dict(new_route(), enabled=True)},
        "spotify": {"hall": dict(new_route(), enabled=True, volume=80, delay=30),
                    "bar": dict(new_route(), enabled=True, volume=50)},
    }
    return config


class ConfigurationTest(unittest.TestCase):
    def test_defaults_are_silent_and_validation_is_strict(self):
        cfg = validate(defaults())
        self.assertTrue(cfg["mute"])
        self.assertFalse(any(item["auto"] for item in cfg["services"].values()))
        cfg["master"] = 101
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_profile_is_independent_and_persisted(self):
        cfg = mixer_config()
        cfg["profiles"]["Quiz"] = profile_snapshot(cfg)
        cfg["zones"]["hall"]["volume"] = 77
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.replace(cfg, 0)
            self.assertEqual(Store(path).data["zones"]["hall"]["volume"], 77)
            self.assertEqual(Store(path).data["profiles"]["Quiz"]["zones"]["hall"]["volume"], 30)
            with self.assertRaises(ValueError):
                store.replace(defaults(), 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), cfg)

    def test_duplicate_outputs_and_orphaned_routes_are_rejected(self):
        cfg = mixer_config()
        cfg["zones"]["bar"]["device"] = cfg["zones"]["hall"]["device"]
        with self.assertRaises(ValueError):
            validate(cfg)
        del cfg["zones"]["bar"]
        with self.assertRaises(ValueError):
            validate(cfg)


class PipeWireTest(unittest.TestCase):
    @patch("scouting_audio.pipewire.pulse", return_value="1\tmodule-loopback\tsource=x sink=y\t\n")
    def test_module_indices_come_from_short_text_list(self, pulse):
        self.assertEqual(modules_by_index(), {1: {"name": "module-loopback", "argument": "source=x sink=y"}})
        pulse.assert_called_once_with("list", "short", "modules")

    def test_stereo_eq_and_delay(self):
        zone = dict(new_zone("alsa_output.bar", "Bar"), eq=True, bass=6, delay=21)
        graph = zone_graph("bar", zone, 48000, 20)
        self.assertEqual(graph["node.latency"], "960/48000")
        self.assertEqual(graph["playback.props"]["target.object"], "alsa_output.bar")
        self.assertTrue(graph["playback.props"]["node.dont-fallback"])
        self.assertEqual(graph["filter.graph"]["outputs"], ["ldelay:Out", "rdelay:Out"])
        lows = [n for n in graph["filter.graph"]["nodes"] if n["label"] == "bq_lowshelf"]
        self.assertEqual([n["control"]["Gain"] for n in lows], [6, 6])
        self.assertAlmostEqual(eq_headroom(zone), 0.501187, places=6)

    def test_fan_out_and_source_priority(self):
        cfg = mixer_config()
        mixer = Mixer(Path("."))
        mixer.states = {"hall": "Verbonden", "bar": "Verbonden"}
        sources = {bus(key) + ".monitor" for key in ("spotify", "local")}
        desired = mixer.desired_routes(cfg, set(), sources)
        self.assertEqual(desired["spotify", "hall"], (bus("spotify") + ".monitor", 30, 0.6))
        self.assertEqual(desired["spotify", "bar"], (bus("spotify") + ".monitor", 0, 0.375))
        self.assertEqual(desired["local", "hall"][2], 0.75)
        suppressed = mixer.desired_routes(cfg, {"spotify"}, sources)
        self.assertEqual(suppressed["spotify", "bar"][2], 0)
        self.assertEqual(suppressed["local", "hall"][2], 0.75)
        mixer.states["bar"] = "Niet verbonden"
        self.assertNotIn(("spotify", "bar"), mixer.desired_routes(cfg, set(), sources))

    def test_capture_refuses_monitor_as_microphone(self):
        cfg = mixer_config()
        cfg["sources"]["mic"] = {"name": "Microfoon", "volume": 60,
                                 "mute": False, "device": bus("local") + ".monitor"}
        cfg["routes"]["mic"] = {"hall": dict(new_route(), enabled=True)}
        mixer = Mixer(Path("."))
        mixer.states = {"hall": "Verbonden"}
        sources = {bus("local") + ".monitor"}
        self.assertNotIn(("mic", "hall"), mixer.desired_routes(cfg, set(), sources))

    @patch("scouting_audio.pipewire.pulse")
    def test_mute_before_initial_route_and_unmute_after_gain(self, pulse):
        cfg = mixer_config()
        cfg["mute"] = False
        mixer = Mixer(Path("."))
        mixer.states = {"hall": "Verbonden"}
        name = bus("zone_hall")
        sinks = {name: {"name": name, "mute": False, "index": 3, "properties": {"object.id": "4"}}}
        with patch("scouting_audio.pipewire.run"):
            mixer.reconcile_routes(cfg, set(), sinks, {bus("local") + ".monitor"}, {}, [])
        self.assertEqual(pulse.call_args_list[0].args, ("set-sink-mute", name, "1"))
        self.assertIn("module-loopback", pulse.call_args_list[2].args)


class ReceiverTest(unittest.TestCase):
    def test_spotify_uses_librespot_via_spotifyd_and_mpris(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            cfg = defaults()["services"]
            receiver = Receivers(path, cfg)
            self.assertEqual(receiver.command("spotify", cfg["spotify"])[0], "spotifyd")
            self.assertIn("use_mpris = true", (path / "spotifyd.toml").read_text(encoding="utf-8"))

    def test_music_assistant_rejects_missing_or_invalid_url(self):
        cfg = defaults()["services"]
        receiver = Receivers(Path("."), cfg)
        with self.assertRaises(ValueError):
            receiver.command("music_assistant", cfg["music_assistant"])


if __name__ == "__main__":
    unittest.main()
