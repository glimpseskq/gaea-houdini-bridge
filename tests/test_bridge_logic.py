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

    def test_content_hash_ignores_file_bytes(self):
        # The exported EXR embeds a timestamp, so the export identifies the input
        # by a hash of the heights instead of the file's bytes.
        _write(self.cfg.input_path, b"export 1")
        sig = lambda: ghb.compute_signature(  # noqa: E731
            self.cfg.input_path, 1, self.cfg.output_maps, content_hash="heights-A")
        first = sig()
        _write(self.cfg.input_path, b"export 2, same heights")
        self.assertEqual(first, sig())
        self.assertNotEqual(first, ghb.compute_signature(
            self.cfg.input_path, 1, self.cfg.output_maps, content_hash="heights-B"))

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
        _write(self.cfg.output_path("height"), b"")  # empty file only
        self.assertFalse(ghb.outputs_present(self.cfg))

    def test_outputs_present_accepts_partial_set(self):
        # Not every Gaea Export node has to exist.
        _write(self.cfg.output_path("height"), b"x")
        self.assertTrue(ghb.outputs_present(self.cfg))


class OutputLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(bridge_dir=self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exact_name(self):
        _write(self.cfg.output_path("flow"))
        self.assertEqual(ghb.find_output_file(self.cfg, "flow"),
                         self.cfg.output_path("flow"))

    def test_tolerates_spaces_and_case(self):
        # A Gaea Export node named " Output_Sediment" writes " Output_Sediment.exr".
        odd = os.path.join(self.cfg.output_dir, " Output_Sediment.exr")
        _write(odd)
        self.assertEqual(ghb.find_output_file(self.cfg, "sediment"), odd)

    def test_missing_or_empty(self):
        self.assertIsNone(ghb.find_output_file(self.cfg, "wear"))  # no folder yet
        _write(os.path.join(self.cfg.output_dir, " output_wear.exr"), b"")
        self.assertIsNone(ghb.find_output_file(self.cfg, "wear"))


class AutoImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(bridge_dir=self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stamp_empty_without_outputs(self):
        self.assertEqual(ghb.output_stamp(self.cfg), "")

    def test_stamp_changes_with_new_build(self):
        _write(self.cfg.output_path("height"), b"build1")
        first = ghb.output_stamp(self.cfg)
        self.assertEqual(first, ghb.output_stamp(self.cfg))  # stable
        _write(self.cfg.output_path("height"), b"build-two")  # new size
        self.assertNotEqual(first, ghb.output_stamp(self.cfg))
        second = ghb.output_stamp(self.cfg)
        _write(self.cfg.output_path("wear"), b"x")  # a new layer appears
        self.assertNotEqual(second, ghb.output_stamp(self.cfg))

    def test_detector_waits_for_files_to_settle(self):
        det = ghb.SettleDetector(settle=1.5)
        # Nothing new: never import.
        self.assertFalse(det.update("A", "A", 0.0))
        self.assertFalse(det.update("", "A", 1.0))
        # Gaea starts writing: B, then C while still writing.
        self.assertFalse(det.update("B", "A", 10.0))
        self.assertFalse(det.update("C", "A", 11.0))
        self.assertFalse(det.update("C", "A", 12.0))  # only 1 s stable
        self.assertTrue(det.update("C", "A", 12.6))   # 1.6 s stable -> import

    def test_detector_resets_after_import(self):
        det = ghb.SettleDetector(settle=1.0)
        det.update("B", "A", 0.0)
        self.assertTrue(det.update("B", "A", 2.0))
        # After the import the node's stamp is "B": nothing to do.
        self.assertFalse(det.update("B", "B", 3.0))
        self.assertFalse(det.update("B", "B", 10.0))


class SwarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(
            bridge_dir=os.path.join(self.tmp, "bridge"),
            gaea_exe=os.path.join(self.tmp, "Gaea", "Gaea.exe"),
            gaea_project=os.path.join(self.tmp, "p.terrain"),
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project(self, file_name=None, out_dir=None, export_format=None,
                 export_name="output_height"):
        export = {"$type": "QuadSpinner.Gaea.Nodes.Export, Gaea.Nodes",
                  "Name": export_name, "Location": "Custom",
                  "OutputPath": out_dir or self.cfg.output_dir}
        if export_format:
            export["Format"] = export_format
        data = {"Assets": {"$values": [{"Terrain": {"Nodes": {
            "1": {"$type": "QuadSpinner.Gaea.Nodes.File, Gaea.Nodes",
                  "Name": "File", "FileName": file_name or self.cfg.input_path},
            "2": export,
        }}}]}}
        with open(self.cfg.gaea_project, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_swarm_next_to_gaea_or_explicit(self):
        self.assertEqual(ghb.swarm_exe_path(self.cfg),
                         os.path.join(self.tmp, "Gaea", "Gaea.Swarm.exe"))
        self.cfg.gaea_swarm = "/custom/Gaea.Swarm.exe"
        self.assertEqual(ghb.swarm_exe_path(self.cfg), "/custom/Gaea.Swarm.exe")

    def test_command(self):
        cmd = ghb.swarm_command(self.cfg)
        self.assertEqual(cmd[1:], ["--Filename", self.cfg.gaea_project, "--silent"])
        self.cfg.build_resolution = 1024
        self.cfg.ignore_cache = True
        cmd = ghb.swarm_command(self.cfg)
        self.assertIn("--ignorecache", cmd)
        self.assertEqual(cmd[cmd.index("--resolution") + 1], "1024")

    def test_project_paths_ok(self):
        # Slashes/case differ from the bridge paths but point to the same place.
        self._project(file_name=self.cfg.input_path.replace("\\", "/"),
                      export_name=" Output_Height")
        self.assertEqual(ghb.check_project_paths(self.cfg), [])

    def test_project_paths_problems(self):
        self._project(file_name="D:/elsewhere/in.exr", out_dir="D:/elsewhere",
                      export_format="GaeaRaw")
        problems = " ".join(ghb.check_project_paths(self.cfg))
        self.assertIn("No File node", problems)
        self.assertIn("Output Path", problems)
        self.assertIn("Format EXR", problems)

    def test_project_without_height_export(self):
        self._project(export_name="something_else")
        self.assertIn("output_height", " ".join(ghb.check_project_paths(self.cfg)))

    def test_missing_project_and_swarm_block_the_build(self):
        problems = " ".join(ghb.validate_build(self.cfg))
        self.assertIn("Gaea.Swarm.exe not found", problems)
        self.assertIn("Export first", problems)
        self.assertIn("project not found", problems)

    def test_success_judged_by_written_files(self):
        out = self.cfg.output_path("height")
        os.makedirs(os.path.dirname(out))
        script = "open(r'{}', 'wb').write(b'built')".format(out)
        job = ghb.SwarmBuild(self.cfg, command=[sys.executable, "-c", script]).start()
        self.assertEqual(job.wait(timeout=60), 0)
        ok, message = job.result_message()
        self.assertTrue(ok, message)
        self.assertEqual(job.updated_layers(), ["height"])

    def test_exit_code_zero_but_nothing_written_is_a_failure(self):
        # Gaea.Swarm.exe returns 0 even for a missing project.
        job = ghb.SwarmBuild(self.cfg, command=[sys.executable, "-c", "pass"]).start()
        job.wait(timeout=60)
        ok, message = job.result_message()
        self.assertFalse(ok)
        self.assertIn("without writing new files", message)


class ErosionTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(bridge_dir=os.path.join(self.tmp, "bridge"),
                                    gaea_project=os.path.join(self.tmp, "t.terrain"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _template(self, *erosion_nodes):
        nodes = {str(i): dict({"$type": "QuadSpinner.Gaea.Nodes.Erosion2, Gaea.Nodes",
                               "Name": "Erosion2"}, **extra)
                 for i, extra in enumerate(erosion_nodes)}
        nodes["99"] = {"$type": "QuadSpinner.Gaea.Nodes.Export, Gaea.Nodes",
                       "Name": "output_height", "Mystery": 7}
        with open(self.cfg.gaea_project, "w", encoding="utf-8") as handle:
            json.dump({"Assets": {"$values": [{"Terrain": {"Nodes": nodes}}]}}, handle)

    def _copy(self):
        dst = ghb.template_build_path(self.cfg)
        changed = ghb.write_template_copy(self.cfg, dst)
        with open(dst, encoding="utf-8") as handle:
            data = json.load(handle)
        return changed, ghb.find_erosion_node(data, self.cfg.erosion_node), data

    def test_spec_is_complete_and_sane(self):
        names = [spec[0] for spec in ghb.EROSION2_PARAMS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 20)  # Slope/Altitude (Float2) are skipped
        for name, _, _, kind, default, low, high in ghb.EROSION2_PARAMS:
            if kind != "bool":
                self.assertTrue(low <= default <= high, name)

    def test_read_fills_gaea_defaults(self):
        self._template({"ErosionScale": 289.51, "Downcutting": 0.55, "Seed": 13864})
        values = ghb.read_erosion_params(self.cfg.gaea_project)
        self.assertAlmostEqual(values["ErosionScale"], 289.51)
        self.assertEqual(values["Seed"], 13864)
        self.assertEqual(values["Duration"], 10.0)        # not saved -> default
        self.assertIs(values["Enable"], False)

    def test_copy_writes_only_changed_values(self):
        self._template({"ErosionScale": 289.51, "Seed": 13864})
        self.cfg.erosion_params = dict(ghb.read_erosion_params(self.cfg.gaea_project))
        self.cfg.erosion_params.update(Duration=40.0, Enable=True, RainShadow=0.5)
        self.cfg.erosion_params["Bogus"] = 1  # unknown parameters are skipped
        changed, node, data = self._copy()
        self.assertEqual(changed, ["Duration", "Enable", "RainShadow"])
        self.assertEqual(node["Duration"], 40.0)
        self.assertIs(node["Enable"], True)
        self.assertEqual(node["Seed"], 13864)
        self.assertNotIn("Bogus", node)
        self.assertNotIn("Shape", node)  # untouched defaults stay unwritten
        # The rest of the project survives, and the original is unchanged.
        self.assertIn('"Mystery": 7', json.dumps(data))
        self.assertEqual(ghb.read_erosion_params(self.cfg.gaea_project)["Duration"], 10.0)

    def test_several_erosion_nodes_need_a_name(self):
        self._template({"Name": "Erosion2"}, {"Name": "Final", "Duration": 30.0})
        with self.assertRaises(ValueError):
            ghb.read_erosion_params(self.cfg.gaea_project)
        self.assertEqual(ghb.read_erosion_params(self.cfg.gaea_project, "Final")
                         ["Duration"], 30.0)
        with self.assertRaises(ValueError):
            ghb.read_erosion_params(self.cfg.gaea_project, "Missing")

    def test_build_settings_drive_the_cache(self):
        self._template({})
        base = ghb.build_settings(self.cfg)
        self.cfg.use_template = True
        self.cfg.erosion_params = {"Duration": 10.0}
        with_template = ghb.build_settings(self.cfg)
        self.assertNotEqual(base, with_template)
        self.cfg.erosion_params = {"Duration": 20.0}
        self.assertNotEqual(with_template, ghb.build_settings(self.cfg))
        self.cfg.build_resolution = 512
        self.assertEqual(ghb.build_settings(self.cfg)["build_resolution"], 512)

    def test_swarm_builds_the_copy(self):
        cmd = ghb.swarm_command(self.cfg, project="C:/b/gaea_template_build.terrain")
        self.assertEqual(cmd[2], "C:/b/gaea_template_build.terrain")


class PostprocessSnippetTests(unittest.TestCase):
    ENC = {"span": 500.0, "offset": -62.5, "max_value": 0.3}

    def test_full_snippet(self):
        vex = ghb.import_postprocess_snippet(
            self.ENC, {"wear": 0.05, "flow": 1.0}, "wear"
        )
        self.assertIn("f@height = f@height * 500.0 + -62.5;", vex)
        self.assertIn("f@wear = clamp(f@wear / 0.05, 0, 1);", vex)
        self.assertIn("f@flow = clamp(f@flow / 1.0, 0, 1);", vex)
        self.assertTrue(vex.rstrip().endswith("f@mask = f@wear;"))

    def test_skips_empty_masks_and_optional_parts(self):
        vex = ghb.import_postprocess_snippet(None, {"sediment": 0.0}, "")
        self.assertNotIn("height", vex)
        self.assertNotIn("sediment", vex)  # all-zero layer: nothing to normalize
        self.assertNotIn("f@mask", vex)


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
        "mask_layer": "wear",
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
        self.assertEqual(cfg.mask_layer, "wear")
        # The node no longer filters layers: everything found is imported.
        self.assertEqual(cfg.output_maps, ghb.DEFAULT_OUTPUT_MAPS)

    def test_mask_none_means_no_viewport_mask(self):
        overrides = ghb.overrides_from_node(_FakeNode({"mask_layer": "none"}))
        cfg = ghb.BridgeConfig.load(path="", overrides=overrides)
        self.assertEqual(cfg.mask_layer, "")

    def test_missing_parms_fall_back_to_defaults(self):
        overrides = ghb.overrides_from_node(_FakeNode({}))
        cfg = ghb.BridgeConfig.load(path="", overrides=overrides)
        self.assertEqual(cfg.bridge_dir, ghb.DEFAULT_BRIDGE_DIR)
        self.assertEqual(cfg.downsample, 1)
        self.assertEqual((cfg.gaea_width, cfg.gaea_height), (0.0, 0.0))
        self.assertEqual(cfg.mask_layer, ghb.DEFAULT_MASK_LAYER)
        self.assertEqual(cfg.output_maps, ghb.DEFAULT_OUTPUT_MAPS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
