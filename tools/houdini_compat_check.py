"""Check that this Houdini version has everything the Gaea bridge relies on.

Run it in the Houdini version you want to use, either

    hython tools/houdini_compat_check.py

or from Houdini's Python Shell (with houdini_gaea_bridge on the Python path):

    exec(open(r"C:/path/to/BridgeHGH/tools/houdini_compat_check.py").read())

It creates temporary nodes under /obj, prints a report and deletes them. Nothing
is installed and no scene data is changed. Paste the report into an issue if
something shows as MISSING or FAIL.
"""

import os
import sys
import tempfile

try:
    _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _here not in sys.path:
        sys.path.insert(0, _here)
except NameError:  # exec() from the Python Shell: rely on the Python path
    pass

import hou  # noqa: E402

import houdini_gaea_bridge as ghb  # noqa: E402


def _check_parms(node, groups, report):
    """groups: [(what, candidates)]; any one candidate is enough."""
    for what, candidates in groups:
        found = [name for name in candidates
                 if node.parm(name) is not None or node.parmTuple(name) is not None]
        status = "ok     " if found else "MISSING"
        report.append("  {} {:<28} {}".format(
            status, what, found[0] if found else "(tried " + ", ".join(candidates) + ")"))


def run():
    report = ["Gaea bridge compatibility check",
              "Houdini {} | Python {} | module {}".format(
                  hou.applicationVersionString(), sys.version.split()[0], ghb.__file__)]
    geo = hou.node("/obj").createNode("geo", "ghb_compat_check")
    tmp = tempfile.mkdtemp(prefix="ghb_compat_")
    try:
        # --- node types ------------------------------------------------
        report.append("Node types:")
        for type_name in ("heightfield", "heightfield_noise", "heightfield_output",
                          "heightfield_file", "heightfield_blur", "volumewrangle",
                          "switch", "blast", "output", "null"):
            ok = hou.nodeType(hou.sopNodeTypeCategory(), type_name) is not None
            report.append("  {} {}".format("ok     " if ok else "MISSING", type_name))

        # --- hou.Volume ------------------------------------------------
        hf = geo.createNode("heightfield")
        noise = geo.createNode("heightfield_noise")
        noise.setInput(0, hf)
        prim = [p for p in noise.geometry().prims() if p.attribValue("name") == "height"][0]
        report.append("hou.Volume:")
        for method in ("voxelSize", "indexToPos", "resolution", "allVoxelsAsString",
                       "volumeMin", "volumeMax"):
            report.append("  {} {}".format(
                "ok     " if hasattr(prim, method) else "absent ", method))
        report.append("  resolution() -> {} {}".format(
            type(prim.resolution()).__name__, tuple(prim.resolution())))
        report.append("  bridge reads: resolution {}, voxel size {}".format(
            ghb.volume_resolution(prim), ghb.volume_voxel_size(prim)))

        # --- parameters the bridge sets --------------------------------
        out = geo.createNode("heightfield_output")
        report.append("heightfield_output parameters:")
        groups = [("output file", ghb.OUTPUT_FILE_PARM_CANDIDATES),
                  ("save to disk", ghb.OUTPUT_EXECUTE_PARM_CANDIDATES),
                  ("format", ghb.HFOUT_FORMAT_PARMS),
                  ("use resolution", ghb.HFOUT_USE_RES_PARMS),
                  ("resolution", ("resolution", "resolutionx", "res"))]
        for channel in ("red", "green", "blue"):
            for role, patterns in ghb.HFOUT_CHANNEL_PARMS.items():
                groups.append(("{} {}".format(channel, role),
                               tuple(p.format(channel) for p in patterns)))
        _check_parms(out, groups, report)
        report.append("  all: " + " ".join(p.name() for p in out.parms()))

        reader = geo.createNode("heightfield_file")
        report.append("heightfield_file parameters:")
        _check_parms(reader, [
            ("file", ("filename", "file")), ("type", ("type",)),
            ("layer name", ("layer", "layername")), ("layer mode", ("layermode",)),
            ("clamp min", ("doclampmin",)), ("clamp max", ("doclampmax",)),
            ("height scale", ("heightscale",)), ("size method", ("sizemethod",)),
            ("size", ("size",)), ("reload", ("reload",))], report)
        report.append("  all: " + " ".join(p.name() for p in reader.parms()))

        report.append("other parameters:")
        for type_name, groups in (
                ("heightfield_blur", [("layer", ("layer", "blurlayer")),
                                      ("radius", ("radius",))]),
                ("volumewrangle", [("snippet", ("snippet",))]),
                ("switch", [("input", ("input",))]),
                ("blast", [("group", ("group",))]),
                ("output", [("output index", ("outputidx",))])):
            report.append(" " + type_name)
            _check_parms(geo.createNode(type_name), groups, report)

        # --- end-to-end export (writes only into a temp folder) ----------
        report.append("Export test:")
        try:
            config = ghb.BridgeConfig(bridge_dir=tmp, downsample=2)
            result = ghb.export_for_gaea(noise, config, use_cache=False)
            report.append("  PASS   " + result["message"][:160])
        except Exception as error:  # report everything, keep going
            report.append("  FAIL   {}: {}".format(type(error).__name__, error))

        report.append("Mask import test:")
        try:
            path = os.path.join(tmp, "input_height.exr")
            mask = geo.createNode("heightfield_file")
            mask.setInput(0, noise)
            ghb._configure_import_node(mask, "flow", path, 1000.0)
            layers = [p.attribValue("name") for p in mask.geometry().prims()]
            report.append("  {}   layers: {}".format(
                "PASS" if "flow" in layers else "FAIL", ", ".join(layers)))
        except Exception as error:
            report.append("  FAIL   {}: {}".format(type(error).__name__, error))
    finally:
        geo.destroy()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    text = "\n".join(report)
    print(text)
    return text


run()
