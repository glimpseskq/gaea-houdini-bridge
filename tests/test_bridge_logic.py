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
            path="", overrides={"bridge_dir": "/a", "resolution": None}
        )
        self.assertEqual(cfg.bridge_dir, "/a")
        self.assertEqual(cfg.resolution, 2048)  # None override ignored

    def test_from_dict_drops_unknown_keys(self):
        cfg = ghb.BridgeConfig.from_dict({"bridge_dir": "/x", "bogus": 1})
        self.assertEqual(cfg.bridge_dir, "/x")

    def test_load_from_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "c.json")
            with open(cfg_path, "w", encoding="utf-8") as handle:
                json.dump({"bridge_dir": "/from/json", "resolution": 4096}, handle)
            cfg = ghb.BridgeConfig.load(path=cfg_path)
            self.assertEqual(cfg.bridge_dir, "/from/json")
            self.assertEqual(cfg.resolution, 4096)


class SignatureAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(bridge_dir=self.tmp, resolution=2048)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sig(self):
        return ghb.compute_signature(
            self.cfg.input_path, self.cfg.params, self.cfg.resolution,
            self.cfg.output_maps,
        )

    def test_signature_changes_with_input(self):
        _write(self.cfg.input_path, b"aaa")
        s1 = self._sig()
        _write(self.cfg.input_path, b"bbb")
        s2 = self._sig()
        self.assertNotEqual(s1, s2)

    def test_signature_changes_with_params_and_resolution(self):
        _write(self.cfg.input_path, b"aaa")
        base = self._sig()
        self.cfg.params = {"seed": 7}
        self.assertNotEqual(base, self._sig())
        self.cfg.params = {}
        self.cfg.resolution = 4096
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
        self.assertEqual(manifest["resolution"], 2048)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
