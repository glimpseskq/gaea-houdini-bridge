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
        self.cfg.use_template = True
        changed, node, data = self._copy()
        # The test template's Export has no Custom path: the copy fixes it too.
        self.assertEqual(changed, ["Duration", "Enable", "RainShadow", "export paths"])
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


class Erosion1TemplateTests(unittest.TestCase):
    """The older Gaea Erosion node, driven by the Gaea Erosion Bridge HDA."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(bridge_dir=os.path.join(self.tmp, "bridge"),
                                    gaea_project=os.path.join(self.tmp, "t.terrain"),
                                    erosion_kind="Erosion", use_template=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _template(self, **values):
        nodes = {"1": dict({"$type": "QuadSpinner.Gaea.Nodes.Erosion, Gaea.Nodes",
                            "Name": "Erosion"}, **values),
                 "2": {"$type": "QuadSpinner.Gaea.Nodes.Export, Gaea.Nodes",
                       "Name": "output_height"}}
        with open(self.cfg.gaea_project, "w", encoding="utf-8") as handle:
            json.dump({"Assets": {"$values": [{"Terrain": {"Nodes": nodes}}]}}, handle)

    def test_spec_is_complete_and_sane(self):
        names = [spec[0] for spec in ghb.EROSION1_PARAMS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 20)  # everything but the Information block
        for name, _, _, kind, default, low, high in ghb.EROSION1_PARAMS:
            if kind == "enum":
                self.assertIn(default, low, name)
            elif kind != "bool":
                self.assertTrue(low <= default <= high, name)
        self.assertEqual(ghb.erosion_spec("Erosion"), ghb.EROSION1_PARAMS)
        self.assertEqual(ghb.erosion_spec("Erosion2"), ghb.EROSION2_PARAMS)
        with self.assertRaises(ValueError):
            ghb.erosion_spec("Erosion3")

    def test_read_fills_gaea_defaults(self):
        self._template(Duration=0.3812292, Seed=40454, AreaEffect="RockSoftness")
        values = ghb.read_erosion_params(self.cfg.gaea_project, kind="Erosion")
        self.assertAlmostEqual(values["Duration"], 0.3812292)
        self.assertEqual(values["Seed"], 40454)
        self.assertEqual(values["AreaEffect"], "RockSoftness")
        self.assertEqual(values["BiasType"], "Altitude")  # default
        self.assertIs(values["RealScale"], True)
        self.assertEqual(values["FeatureScale"], 2000)

    def test_wrong_kind_is_an_error(self):
        self._template()
        with self.assertRaises(ValueError):  # no Erosion2 in this project
            ghb.read_erosion_params(self.cfg.gaea_project, kind="Erosion2")

    def test_copy_writes_changed_values_including_enums(self):
        self._template(Duration=0.3812292, Seed=40454)
        self.cfg.erosion_params = ghb.read_erosion_params(self.cfg.gaea_project,
                                                          kind="Erosion")
        self.cfg.erosion_params.update(Duration=0.2, AreaEffect="PrecipitationAmount",
                                       BiasType="Slope", Reverse=True,
                                       Deterministic="bogus")
        dst = os.path.join(self.tmp, "copy.terrain")
        changed = ghb.write_template_copy(self.cfg, dst)
        self.assertEqual(changed, ["AreaEffect", "BiasType", "Deterministic",
                                   "Duration", "Reverse", "export paths"])
        with open(dst, encoding="utf-8") as handle:
            node = ghb.find_erosion_node(json.load(handle), kind="Erosion")
        self.assertEqual(node["Duration"], 0.2)
        self.assertEqual(node["AreaEffect"], "PrecipitationAmount")
        self.assertEqual(node["BiasType"], "Slope")
        self.assertIs(node["Reverse"], True)
        self.assertIs(node["Deterministic"], True)  # a truthy string -> bool
        self.assertEqual(node["Seed"], 40454)
        self.assertNotIn("Strength", node)  # unchanged defaults stay unwritten

    def test_bad_enum_value_is_skipped(self):
        self._template()
        self.cfg.erosion_params = {"AreaEffect": "Everything"}
        changed = ghb.write_template_copy(self.cfg, os.path.join(self.tmp, "c"))
        self.assertNotIn("AreaEffect", changed)

    def test_kind_is_part_of_the_cache_signature(self):
        self._template()
        first = ghb.build_settings(self.cfg)
        self.cfg.erosion_kind = "Erosion2"
        self.assertNotEqual(first, ghb.build_settings(self.cfg))


class SharedPathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = os.path.join(self.tmp, "prefs", "settings.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project(self, relpath, kind):
        path = os.path.normpath(os.path.join(self.tmp, relpath))
        nodes = {"1": {"$type": "QuadSpinner.Gaea.Nodes.{}, Gaea.Nodes".format(kind),
                       "Id": 1, "Name": kind}}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"Assets": {"$values": [{"Terrain": {"Nodes": nodes}}]}}, handle)
        return path

    def test_save_and_load_shared_paths(self):
        self.assertEqual(ghb.load_shared_paths(self.settings), {})
        saved = ghb.save_shared_paths(
            {"gaea_exe": "E:/Gaea/Gaea.exe", "gaea_swarm": "", "other": "x"},
            self.settings)
        self.assertEqual(saved, {"gaea_exe": "E:/Gaea/Gaea.exe"})
        # Empty values keep what is saved; other keys in the file survive.
        ghb.save_shared_paths({"gaea_exe": "", "gaea_projects_dir": "D:/P"},
                              self.settings)
        self.assertEqual(ghb.load_shared_paths(self.settings),
                         {"gaea_exe": "E:/Gaea/Gaea.exe", "gaea_projects_dir": "D:/P"})

    def test_shared_paths_fill_only_empty_fields(self):
        cfg = ghb.BridgeConfig(gaea_exe="C:/mine/Gaea.exe")
        filled = ghb.apply_shared_paths(
            cfg, {"gaea_exe": "E:/Gaea/Gaea.exe", "gaea_swarm": "E:/Gaea/Gaea.Swarm.exe"})
        self.assertEqual(filled, ["gaea_swarm"])
        self.assertEqual(cfg.gaea_exe, "C:/mine/Gaea.exe")
        self.assertEqual(cfg.gaea_swarm, "E:/Gaea/Gaea.Swarm.exe")

    def test_project_found_by_name_first(self):
        by_content = self._project("a/other.terrain", "Erosion2")
        by_name = self._project("b/sub/Erosion2.terrain", "Erosion")  # name wins
        self.assertEqual(ghb.find_project_for_kind(self.tmp, "Erosion2"), by_name)
        self.assertEqual(ghb.project_name_for_kind("Erosion"), "erosion.terrain")
        self.assertIsNotNone(by_content)

    def test_project_found_by_node_kind(self):
        self._project("x/mountain.terrain", "Erosion2")
        old = self._project("x/valley.terrain", "Erosion")
        self._project(ghb.TEMPLATE_BUILD_NAME, "Erosion")  # the bridge's own copy
        self.assertEqual(ghb.find_project_for_kind(self.tmp, "Erosion"), old)
        self.assertIsNone(ghb.find_project_for_kind(self.tmp, "Erosion3"))
        self.assertIsNone(ghb.find_project_for_kind(os.path.join(self.tmp, "nope"),
                                                    "Erosion"))

    def test_copy_points_the_input_file_node_at_the_bridge_dir(self):
        cfg = ghb.BridgeConfig(bridge_dir=os.path.join(self.tmp, "node_a"),
                               gaea_project=os.path.join(self.tmp, "p.terrain"))
        nodes = {
            "1": {"$type": "QuadSpinner.Gaea.Nodes.File, Gaea.Nodes", "Id": 1,
                  "FileName": "D:\\elsewhere\\input_height.exr"},
            "2": {"$type": "QuadSpinner.Gaea.Nodes.File, Gaea.Nodes", "Id": 2,
                  "FileName": ""},  # the mask's File node
            "3": {"$type": "QuadSpinner.Gaea.Nodes.Erosion2, Gaea.Nodes", "Id": 3,
                  "Name": "Erosion2", "Ports": {"$values": [
                      {"Name": "In", "Record": {"From": 1, "ToPort": "In"}},
                      {"Name": "Mask", "Record": {"From": 2, "ToPort": "Mask"}}]}},
            "4": {"$type": "QuadSpinner.Gaea.Nodes.Export, Gaea.Nodes", "Id": 4,
                  "Name": "output_height"},
        }
        with open(cfg.gaea_project, "w", encoding="utf-8") as handle:
            json.dump({"Assets": {"$values": [{"Terrain": {"Nodes": nodes}}]}}, handle)
        self.assertEqual(ghb.check_project_paths(cfg, fix_mask_paths=True), [])
        dst = os.path.join(self.tmp, "copy.terrain")
        changed = ghb.write_template_copy(cfg, dst)
        self.assertEqual(changed, ["input file", "mask file", "export paths"])
        with open(dst, encoding="utf-8") as handle:
            copy = json.load(handle)["Assets"]["$values"][0]["Terrain"]["Nodes"]
        self.assertTrue(ghb._same_path(copy["1"]["FileName"], cfg.input_path))
        self.assertTrue(ghb._same_path(copy["2"]["FileName"], cfg.mask_input_path))

    def test_resolve_project_fills_an_empty_setting(self):
        found = self._project("erosion.terrain", "Erosion")
        cfg = ghb.BridgeConfig(erosion_kind="Erosion", gaea_projects_dir=self.tmp)
        self.assertEqual(ghb.resolve_project(cfg), ("auto", found))
        self.assertEqual(cfg.gaea_project, found)
        explicit = ghb.BridgeConfig(gaea_project="D:/x.terrain",
                                    gaea_projects_dir=self.tmp)
        self.assertEqual(ghb.resolve_project(explicit), ("set", "D:/x.terrain"))
        none = ghb.BridgeConfig(erosion_kind="Erosion2", gaea_projects_dir=self.tmp)
        self.assertEqual(ghb.resolve_project(none), ("none", self.tmp))
        self.assertIn("Gaea Projects Dir", " ".join(ghb.check_project_paths(none)))


class _Vector3:
    """Like hou.Vector3 in 19.5: indexable and iterable floats, no slicing."""

    def __init__(self, *values):
        self._v = [float(v) for v in values]

    def __getitem__(self, index):
        if isinstance(index, slice):
            raise TypeError("hou.Vector3 does not support slicing")
        return self._v[index]

    def __iter__(self):
        return iter(self._v)

    def __len__(self):
        return 3


class _BBox:
    def __init__(self, size):
        self._size = size

    def sizevec(self):
        return _Vector3(*self._size)


class _Volume195:
    """A 500x500 HeightField with 2-unit voxels as Houdini 19.5 exposes it:
    resolution() is a Vector3 of floats and there is no voxelSize()."""

    def __init__(self, index_to_pos=True):
        self._index_to_pos = index_to_pos

    def resolution(self):
        return _Vector3(500, 500, 1)

    def indexToPos(self, index):
        if not self._index_to_pos:
            raise AttributeError("indexToPos")
        x, y, _ = index
        return _Vector3(-499 + 2 * x, 0, -499 + 2 * y)  # HF lies in XZ

    def boundingBox(self):
        return _BBox((998.0, 150.0, 998.0))  # centre to centre: (res - 1) * voxel


class _Volume20(_Volume195):
    def resolution(self):
        return (500, 500, 1)

    def voxelSize(self):
        return _Vector3(2, 2, 2)


class HoudiniVersionCompatTests(unittest.TestCase):
    def test_resolution_is_ints_on_both_versions(self):
        self.assertEqual(ghb.volume_resolution(_Volume195()), (500, 500, 1))
        self.assertEqual(ghb.volume_resolution(_Volume20()), (500, 500, 1))

    def test_voxel_size_houdini20(self):
        self.assertEqual(ghb.volume_voxel_size(_Volume20()), (2.0, 2.0))

    def test_voxel_size_houdini195_from_index_to_pos(self):
        self.assertEqual(ghb.volume_voxel_size(_Volume195()), (2.0, 2.0))

    def test_voxel_size_bbox_fallback(self):
        size = ghb.volume_voxel_size(_Volume195(index_to_pos=False))
        self.assertAlmostEqual(size[0], 2.0)
        self.assertAlmostEqual(size[1], 2.0)

    def test_sources_parse_as_python_37(self):
        # Houdini 19.5 ships Python 3.9 (3.7 builds exist): no newer syntax.
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("houdini_gaea_bridge.py", "build_gaea_bridge_hda.py",
                     os.path.join("tools", "houdini_compat_check.py")):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                ast.parse(handle.read(), filename=name, feature_version=(3, 7))


class TintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = ghb.BridgeConfig(bridge_dir=os.path.join(self.tmp, "b"),
                                    gaea_project=os.path.join(self.tmp, "p.terrain"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project(self, tint=True, shared=False):
        """The user's graph, smaller: File -> Erosion2 -> output_height, plus
        Erosion2 -> two Tint -> Combine -> Export " output_tint_combine".
        ``shared``: the first Tint also feeds another Export, so a tint-less
        build must keep it."""
        def port(name, source=None, source_port="Out"):
            value = {"Name": name, "Type": "In"}
            if source is not None:
                value["Record"] = {"From": source, "FromPort": source_port,
                                   "ToPort": name}
            return value

        def node(kind, node_id, name, ports, **extra):
            return dict({"$type": "QuadSpinner.Gaea.Nodes.{}, Gaea.Nodes".format(kind),
                         "Id": node_id, "Name": name, "Ports": {"$values": ports}},
                        **extra)

        nodes = {
            "1": node("File", 1, "File", [], FileName=self.cfg.input_path),
            "2": node("Erosion2", 2, "Erosion2", [port("In", 1)]),
            "3": node("Export", 3, "output_height", [port("In", 2)],
                      Location="Custom", OutputPath=self.cfg.output_dir),
        }
        if tint:
            nodes["10"] = node("Tint", 10, "Tint", [port("In", 2)])
            nodes["11"] = node("Tint", 11, "Tint", [port("In", 2, "Flow")])
            nodes["12"] = node("Combine", 12, "Combine",
                               [port("In", 10), port("Input2", 11)])
            nodes["13"] = node("Export", 13, " output_tint_combine", [port("In", 12)])
            if shared:
                nodes["14"] = node("Export", 14, "output_extra", [port("In", 10)],
                                   Location="Custom", OutputPath=self.cfg.output_dir)
        with open(self.cfg.gaea_project, "w", encoding="utf-8") as handle:
            json.dump({"Assets": {"$values": [{"Terrain": {"Nodes": nodes}}]}}, handle)

    def _copy_nodes(self, dst):
        with open(dst, encoding="utf-8") as handle:
            nodes = json.load(handle)["Assets"]["$values"][0]["Terrain"]["Nodes"]
        return sorted(int(key) for key in nodes if not key.startswith("$"))

    def test_export_name_matches_tint(self):
        self.assertEqual(ghb._bridge_export_name(
            self.cfg, {"Name": " Output_Tint_Combine"}), "tint")
        self.assertIsNone(ghb._bridge_export_name(self.cfg, {"Name": "output_tint"}))

    def test_always_builds_a_copy(self):
        self.assertTrue(self.cfg.builds_copy)
        self.cfg.build_tint = False
        self.assertTrue(self.cfg.builds_copy)

    def test_build_tint_off_leaves_the_tint_chain_out(self):
        self._project()
        self.cfg.build_tint = False
        dst = os.path.join(self.tmp, "copy.terrain")
        changed = ghb.write_template_copy(self.cfg, dst)
        self.assertIn("tint skipped (4 nodes)", changed)
        self.assertEqual(self._copy_nodes(dst), [1, 2, 3])

    def test_nodes_feeding_other_exports_stay(self):
        self._project(shared=True)
        self.cfg.build_tint = False
        dst = os.path.join(self.tmp, "copy.terrain")
        changed = ghb.write_template_copy(self.cfg, dst)
        self.assertIn("tint skipped (3 nodes)", changed)
        self.assertEqual(self._copy_nodes(dst), [1, 2, 3, 10, 14])

    def test_build_tint_on_keeps_the_chain_and_fixes_its_export(self):
        self._project()
        self.cfg.use_template = True  # a copy is built anyway
        dst = os.path.join(self.tmp, "copy.terrain")
        changed = ghb.write_template_copy(self.cfg, dst)
        self.assertFalse(any(name.startswith("tint skipped") for name in changed))
        self.assertIn("export paths", changed)
        self.assertEqual(self._copy_nodes(dst), [1, 2, 3, 10, 11, 12, 13])
        with open(dst, encoding="utf-8") as handle:
            export = json.load(handle)["Assets"]["$values"][0]["Terrain"]["Nodes"]["13"]
        self.assertEqual(export["Location"], "Custom")
        self.assertTrue(ghb._same_path(export["OutputPath"], self.cfg.output_dir))

    def test_no_tint_export_means_nothing_to_strip(self):
        self._project(tint=False)
        self.cfg.build_tint = False
        dst = os.path.join(self.tmp, "copy.terrain")
        changed = ghb.write_template_copy(self.cfg, dst)
        self.assertFalse(any(name.startswith("tint skipped") for name in changed))
        self.assertEqual(self._copy_nodes(dst), [1, 2, 3])

    def test_tint_export_checked_in_plain_builds(self):
        self._project()  # the tint export writes to the build folder
        problems = " ".join(ghb.check_project_paths(self.cfg))
        self.assertIn("output_tint_combine", problems)
        self.assertEqual(ghb.check_project_paths(self.cfg, fix_mask_paths=True), [])

    def test_tint_file_found_with_odd_name(self):
        odd = os.path.join(self.cfg.output_dir, " Output_Tint_Combine.exr")
        _write(odd)
        self.assertEqual(ghb.find_tint_file(self.cfg), odd)

    def test_display_path_is_new_per_build(self):
        source = os.path.join(self.cfg.output_dir, "output_tint_combine.exr")
        _write(source, b"first")
        first = ghb.tint_display_path(self.cfg, source)
        self.assertEqual(first, ghb.tint_display_path(self.cfg, source))
        with open(first, "rb") as handle:
            self.assertEqual(handle.read(), b"first")
        os.remove(source)  # Gaea writing a new build
        _write(source, b"second build")
        os.utime(source, ns=(1, 2_000_000_000_000_000_000))
        second = ghb.tint_display_path(self.cfg, source)
        self.assertNotEqual(first, second)
        self.assertFalse(os.path.exists(first))  # old map cleaned up
        with open(second, "rb") as handle:
            self.assertEqual(handle.read(), b"second build")
        self.assertEqual(os.path.dirname(second), os.path.join(
            self.cfg.bridge_dir, ghb.TINT_DISPLAY_SUBDIR))

    def test_fresh_display_path_is_always_new(self):
        source = os.path.join(self.cfg.output_dir, "output_tint_combine.exr")
        _write(source, b"map")
        first = ghb.tint_display_path(self.cfg, source, fresh=True)
        second = ghb.tint_display_path(self.cfg, source, fresh=True)
        self.assertNotEqual(first, second)
        self.assertFalse(os.path.exists(first))
        with open(second, "rb") as handle:
            self.assertEqual(handle.read(), b"map")

    def test_rewritten_tint_listed_after_layers(self):
        _write(os.path.join(self.cfg.output_dir, "output_height.exr"))
        _write(os.path.join(self.cfg.output_dir, " output_tint_combine.exr"))
        self.assertEqual(ghb.updated_outputs(self.cfg, 0), ["height", "tint"])

    def test_build_tint_in_cache_signature(self):
        self._project()
        first = ghb.build_settings(self.cfg)
        self.cfg.build_tint = False
        self.assertNotEqual(first, ghb.build_settings(self.cfg))


class MaskInputTests(unittest.TestCase):
    def test_auto_prefers_a_painted_mask(self):
        self.assertEqual(ghb.choose_mask_layer(
            {"height": (-60, 80), "mask": (0, 0.7)}), ("mask", 0.0, 1.0))

    def test_auto_falls_back_to_height_for_empty_mask(self):
        # e.g. a heightfield_noise used as a procedural mask
        self.assertEqual(ghb.choose_mask_layer(
            {"height": (-60, 80), "mask": (0, 0)}), ("height", -60.0, 80.0))

    def test_auto_single_volume_and_out_of_range_mask(self):
        self.assertEqual(ghb.choose_mask_layer({"density": (0, 5)}),
                         ("density", 0.0, 5.0))
        self.assertEqual(ghb.choose_mask_layer({"mask": (-1, 3)}),
                         ("mask", -1.0, 3.0))  # stretched, not clamped

    def test_constant_layers(self):
        layer, low, high = ghb.choose_mask_layer({"height": (5, 5)}, "height")
        self.assertTrue(low < 5 <= high)  # maps to 1
        layer, low, high = ghb.choose_mask_layer({"height": (0, 0)}, "height")
        self.assertTrue(low <= 0 < high)  # maps to 0

    def test_explicit_layer(self):
        self.assertEqual(ghb.choose_mask_layer(
            {"height": (0, 10), "mask": (0, 1)}, "height")[0], "height")
        with self.assertRaises(ValueError):
            ghb.choose_mask_layer({"mask": (0, 1)}, "flow")
        with self.assertRaises(ValueError):
            ghb.choose_mask_layer({})

    def _project(self, tmp, mask_path):
        cfg = ghb.BridgeConfig(bridge_dir=os.path.join(tmp, "b"),
                               gaea_project=os.path.join(tmp, "p.terrain"))
        nodes = {
            "828": {"$type": "QuadSpinner.Gaea.Nodes.File, Gaea.Nodes", "Id": 828,
                    "Name": "File", "FileName": cfg.input_path},
            "883": {"$type": "QuadSpinner.Gaea.Nodes.File, Gaea.Nodes", "Id": 883,
                    "Name": "File", "FileName": mask_path},
            "363": {"$type": "QuadSpinner.Gaea.Nodes.Erosion2, Gaea.Nodes",
                    "Id": 363, "Name": "Erosion2", "Ports": {"$values": [
                        {"Name": "In", "Record": {"From": 828, "ToPort": "In"}},
                        {"Name": "Mask", "Record": {"From": 883, "ToPort": "Mask"}}]}},
            "475": {"$type": "QuadSpinner.Gaea.Nodes.Export, Gaea.Nodes",
                    "Name": "output_height", "Location": "Custom",
                    "OutputPath": cfg.output_dir},
        }
        with open(cfg.gaea_project, "w", encoding="utf-8") as handle:
            json.dump({"Assets": {"$values": [{"Terrain": {"Nodes": nodes}}]}}, handle)
        return cfg

    def test_mask_file_detected_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp, "")
            with open(cfg.gaea_project, encoding="utf-8") as handle:
                masks = ghb.mask_file_nodes(json.load(handle))
            self.assertEqual([m["Id"] for m in masks], [883])
            problems = " ".join(ghb.check_project_paths(cfg))
            self.assertIn("Mask port", problems)
            self.assertIn("input_mask.exr", problems)
            # Template mode fixes it in the copy instead of complaining.
            self.assertEqual(ghb.check_project_paths(cfg, fix_mask_paths=True), [])

    def test_mask_file_ok_when_pointing_to_the_mask(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp, "")
            cfg = self._project(tmp, cfg.mask_input_path)
            self.assertEqual(ghb.check_project_paths(cfg), [])

    def test_template_copy_points_mask_file_to_the_mask(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp, "")
            dst = os.path.join(tmp, "copy.terrain")
            self.assertIn("mask file", ghb.write_template_copy(cfg, dst))
            with open(dst, encoding="utf-8") as handle:
                data = json.load(handle)
            mask = ghb.mask_file_nodes(data)[0]
            self.assertTrue(ghb._same_path(mask["FileName"], cfg.mask_input_path))
            height = data["Assets"]["$values"][0]["Terrain"]["Nodes"]["828"]
            self.assertTrue(ghb._same_path(height["FileName"], cfg.input_path))


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

    def parmTuple(self, name):  # noqa: N802 (mirrors the hou API)
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
