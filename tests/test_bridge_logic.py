"""Unit tests for the hou-independent logic of houdini_gaea_bridge.

Run from the project root:

    python -I tests/test_bridge_logic.py

These cover config loading/overrides, path building, the cache signature, manifest
round-trips, is_cache_valid, and cleanup_temp safety. They never import ``hou``.
"""

import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import houdini_gaea_bridge as ghb  # noqa: E402


def _write(path, data=b"data"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


class ConfigTests(unittest.TestCase):
    def test_default_paths(self):
        cfg = ghb.BridgeConfig(bridge_dir="/tmp/gh")
        self.assertEqual(cfg.input_path, os.path.join("/tmp/gh", "input_height.exr"))
        self.assertEqual(cfg.output_dir, os.path.join("/tmp/gh", "output"))
        self.assertTrue(cfg.manifest_path.endswith(ghb.MANIFEST_NAME))
        self.assertEqual(
            cfg.output_path("height"),
            os.path.join("/tmp/gh", "output", "output_height.exr"),
        )

    def test_overrides_win_and_ignore_none(self):
        cfg = ghb.BridgeConfig.load(
            path="", overrides={"bridge_dir": "/a", "downsample": None}
        )
        self.assertEqual(cfg.bridge_dir, "/a")
        self.assertEqual(cfg.downsample, 1)  # None override ignored

    def test_from_dict_drops_unknown_keys(self):
        # Also covers old configs that still carry "resolution" / "params".
        cfg = ghb.BridgeConfig.from_dict(
            {"bridge_dir": "/x", "bogus": 1, "resolution": 4096, "params": {}}
        )
        self.assertEqual(cfg.bridge_dir, "/x")

    def test_load_from_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "c.json")
            with open(cfg_path, "w", encoding="utf-8") as handle:
                json.dump({"bridge_dir": "/from/json", "downsample": 2}, handle)
            cfg = ghb.BridgeConfig.load(path=cfg_path)
            self.assertEqual(cfg.bridge_dir, "/from/json")
            self.assertEqual(cfg.downsample, 2)


class DownsampleTests(unittest.TestCase):
    def test_fractions(self):
        self.assertEqual(ghb.downsampled_resolution(500, 500, 1), (500, 500))
        self.assertEqual(ghb.downsampled_resolution(500, 500, 2), (250, 250))
        self.assertEqual(ghb.downsampled_resolution(500, 500, 3), (167, 167))
        self.assertEqual(ghb.downsampled_resolution(500, 300, 4), (125, 75))

    def test_never_below_one_pixel(self):
        self.assertEqual(ghb.downsampled_resolution(2, 1, 4), (1, 1))

    def test_rejects_bad_factor(self):
        for bad in (0, -2, "x", None):
            with self.assertRaises(ValueError):
                ghb.downsampled_resolution(500, 500, bad)

    def test_label(self):
        self.assertEqual(ghb.downsample_label(1), "full")
        self.assertEqual(ghb.downsample_label(3), "1/3")


class SignatureAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(bridge_dir=self.tmp, downsample=1)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sig(self):
        return ghb.compute_signature(
            self.cfg.input_path, self.cfg.downsample, self.cfg.output_maps,
        )

    def test_signature_changes_with_input(self):
        _write(self.cfg.input_path, b"aaa")
        s1 = self._sig()
        _write(self.cfg.input_path, b"bbb")
        s2 = self._sig()
        self.assertNotEqual(s1, s2)

    def test_signature_changes_with_downsample_and_layers(self):
        _write(self.cfg.input_path, b"aaa")
        base = self._sig()
        self.cfg.downsample = 2
        self.assertNotEqual(base, self._sig())
        self.cfg.downsample = 1
        self.cfg.output_maps = {"height": "output_height.exr"}
        self.assertNotEqual(base, self._sig())

    def test_signature_stable_for_same_input(self):
        _write(self.cfg.input_path, b"aaa")
        self.assertEqual(self._sig(), self._sig())

    def test_manifest_roundtrip(self):
        _write(self.cfg.input_path, b"aaa")
        sig = self._sig()
        ghb.write_manifest(self.cfg, sig)
        manifest = ghb.read_manifest(self.cfg)
        self.assertEqual(manifest["signature"], sig)
        self.assertEqual(manifest["downsample"], 1)
        self.assertIn("height", manifest["output_maps"])

    def test_read_manifest_missing_and_corrupt(self):
        self.assertIsNone(ghb.read_manifest(self.cfg))
        _write(self.cfg.manifest_path, b"not json {")
        self.assertIsNone(ghb.read_manifest(self.cfg))

    def test_is_cache_valid_full_cycle(self):
        _write(self.cfg.input_path, b"aaa")
        # No manifest yet -> invalid.
        self.assertFalse(ghb.is_cache_valid(self.cfg))
        ghb.write_manifest(self.cfg, self._sig())
        # Manifest matches but outputs missing -> invalid.
        self.assertFalse(ghb.is_cache_valid(self.cfg))
        for layer in self.cfg.output_maps:
            _write(self.cfg.output_path(layer), b"out")
        # Now valid.
        self.assertTrue(ghb.is_cache_valid(self.cfg))
        # Change the input -> stale.
        _write(self.cfg.input_path, b"changed")
        self.assertFalse(ghb.is_cache_valid(self.cfg))

    def test_outputs_present_rejects_empty(self):
        for layer in self.cfg.output_maps:
            _write(self.cfg.output_path(layer), b"x")
        self.assertTrue(ghb.outputs_present(self.cfg))
        _write(self.cfg.output_path("height"), b"")  # empty file
        self.assertFalse(ghb.outputs_present(self.cfg))


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(bridge_dir=self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_removes_input_and_manifest_keeps_outputs(self):
        _write(self.cfg.input_path)
        _write(self.cfg.manifest_path)
        for layer in self.cfg.output_maps:
            _write(self.cfg.output_path(layer))
        removed = ghb.cleanup_temp(self.cfg, remove_input=True, remove_outputs=False)
        self.assertFalse(os.path.isfile(self.cfg.input_path))
        self.assertFalse(os.path.isfile(self.cfg.manifest_path))
        self.assertTrue(os.path.isfile(self.cfg.output_path("height")))
        self.assertIn(self.cfg.input_path, removed)

    def test_remove_outputs_option(self):
        for layer in self.cfg.output_maps:
            _write(self.cfg.output_path(layer))
        ghb.cleanup_temp(self.cfg, remove_input=False, remove_outputs=True)
        self.assertFalse(os.path.isfile(self.cfg.output_path("flow")))

    def test_never_deletes_outside_bridge_dir(self):
        # A sibling of bridge_dir, reached via a relative input_name that escapes it.
        parent = os.path.dirname(self.tmp)
        outside = os.path.join(parent, "precious.exr")
        _write(outside)
        try:
            cfg = ghb.BridgeConfig(
                bridge_dir=self.tmp,
                input_name=os.path.join("..", "precious.exr"),
            )
            # Sanity: the crafted input really does resolve outside bridge_dir.
            self.assertFalse(ghb._is_within(cfg.input_path, cfg.bridge_dir))
            removed = ghb.cleanup_temp(cfg, remove_input=True)
            self.assertTrue(os.path.isfile(outside), "file outside bridge_dir deleted")
            self.assertNotIn(outside, removed)
        finally:
            if os.path.isfile(outside):
                os.remove(outside)

    def test_is_within_helper(self):
        self.assertTrue(ghb._is_within(os.path.join(self.tmp, "a", "b"), self.tmp))
        self.assertFalse(ghb._is_within(os.path.dirname(self.tmp), self.tmp))


class HeightScaleTests(unittest.TestCase):
    TERRAIN = {
        "$id": "1",
        "Assets": {"$values": [{"Terrain": {
            "Nodes": {"741": {"Width": 1, "Name": "not the definition"}},
            "Width": 5000.0, "Height": 2500.0, "Ratio": 0.5,
        }}]},
    }

    def _terrain_file(self, tmp, data):
        path = os.path.join(tmp, "p.terrain")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def test_reads_terrain_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._terrain_file(tmp, self.TERRAIN)
            self.assertEqual(ghb.read_gaea_terrain_size(path), (5000.0, 2500.0))

    def test_unreadable_terrain_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.terrain")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("not json")
            self.assertIsNone(ghb.read_gaea_terrain_size(bad))
            self.assertIsNone(ghb.read_gaea_terrain_size(os.path.join(tmp, "nope")))
            self.assertIsNone(ghb.read_gaea_terrain_size(""))

    def test_world_size_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._terrain_file(tmp, self.TERRAIN)
            explicit = ghb.BridgeConfig(gaea_project=path, gaea_width=1000,
                                        gaea_height=400)
            self.assertEqual(ghb.gaea_world_size(explicit), (1000.0, 400.0, "config"))
            project = ghb.BridgeConfig(gaea_project=path)
            self.assertEqual(ghb.gaea_world_size(project), (5000.0, 2500.0, "project"))
        default = ghb.BridgeConfig(gaea_project="")
        self.assertEqual(
            ghb.gaea_world_size(default),
            (ghb.GAEA_DEFAULT_WIDTH, ghb.GAEA_DEFAULT_HEIGHT, "default"),
        )

    def test_encoding_preserves_proportions(self):
        # 1000-unit Houdini terrain, relief -63..85, Gaea world 5000 x 2500 m.
        enc = ghb.height_encoding(-63.0, 85.0, 1000.0, 5000.0, 2500.0)
        self.assertAlmostEqual(enc["span"], 500.0)
        self.assertAlmostEqual(enc["offset"], -63.0)
        self.assertAlmostEqual(enc["max_value"], 148.0 / 500.0)
        # Relief/width ratio in Houdini == ratio Gaea will display.
        gaea_relief_m = enc["max_value"] * 2500.0
        self.assertAlmostEqual(gaea_relief_m / 5000.0, 148.0 / 1000.0)
        # Round trip: h -> v -> h.
        v = (40.0 - enc["offset"]) / enc["span"]
        self.assertAlmostEqual(v * enc["span"] + enc["offset"], 40.0)

    def test_encoding_flags_relief_taller_than_gaea(self):
        enc = ghb.height_encoding(0.0, 900.0, 1000.0, 5000.0, 2500.0)
        self.assertGreater(enc["max_value"], 1.0)

    def test_encoding_rejects_zero_sizes(self):
        with self.assertRaises(ValueError):
            ghb.height_encoding(0.0, 1.0, 0.0, 5000.0, 2500.0)


class _FakeParm:
    def __init__(self, value):
        self._value = value

    def eval(self):
        return self._value

    def evalAsString(self):  # noqa: N802 (mirrors the hou API)
        return str(self._value)


class _FakeNode:
    """Stands in for an HDA instance: parm(name) -> parm or None."""

    def __init__(self, values):
        self._parms = {k: _FakeParm(v) for k, v in values.items()}

    def parm(self, name):
        return self._parms.get(name)


class NodeOverridesTests(unittest.TestCase):
    HDA_VALUES = {
        "downsample": "3",
        "gaea_width": 1000.0,
        "gaea_height": 500.0,
        "layer_height": 1,
        "layer_erosion": 1,
        "layer_flow": 0,
        "layer_wear": 1,
        "layer_sediment": 0,
        "bridge_dir": "/bridge",
        "gaea_project": "/p.terrain",
        "gaea_exe": "/Gaea.exe",
    }

    def test_full_node(self):
        cfg = ghb.BridgeConfig.load(
            path="", overrides=ghb.overrides_from_node(_FakeNode(self.HDA_VALUES))
        )
        self.assertEqual(cfg.bridge_dir, "/bridge")
        self.assertEqual(cfg.downsample, 3)
        self.assertEqual(cfg.gaea_exe, "/Gaea.exe")
        self.assertEqual((cfg.gaea_width, cfg.gaea_height), (1000.0, 500.0))
        self.assertEqual(sorted(cfg.output_maps), ["erosion", "height", "wear"])
        self.assertEqual(cfg.output_maps["height"], "output_height.exr")

    def test_missing_parms_fall_back_to_defaults(self):
        overrides = ghb.overrides_from_node(_FakeNode({}))
        cfg = ghb.BridgeConfig.load(path="", overrides=overrides)
        self.assertEqual(cfg.bridge_dir, ghb.DEFAULT_BRIDGE_DIR)
        self.assertEqual(cfg.downsample, 1)
        self.assertEqual((cfg.gaea_width, cfg.gaea_height), (0.0, 0.0))
        self.assertEqual(cfg.output_maps, ghb.DEFAULT_OUTPUT_MAPS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
