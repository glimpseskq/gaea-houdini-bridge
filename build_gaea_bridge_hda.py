"""Build and install the ``gaea_erosion_bridge`` Houdini Digital Asset.

Run this ONCE from inside Houdini (Windows > Python Shell, or a Python Source
Editor). It creates a SOP subnet, promotes the GH-Bridge UI parameters and the
Export / Open / Import buttons, turns it into an HDA, and installs it so the node
shows up as ``gaea_erosion_bridge`` in the TAB menu.

    exec(open(r"C:/.../build_gaea_bridge_hda.py").read())

The node's buttons call ``houdini_gaea_bridge``, so make sure that module is on
Houdini's Python path (see README). The HDA is pure UI + wiring around that module;
it never touches Gaea licensing.
"""

import os

import hou


ASSET_TYPE_NAME = "gaea_erosion_bridge"
ASSET_LABEL = "Gaea Erosion Bridge"
ASSET_SECTIONS = 1  # one input


# The callback snippets stored on the buttons. They build a BridgeConfig from the
# node's own parameters and call the module. Kept as source so the HDA is portable.
EXPORT_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
cfg = ghb.BridgeConfig.load(overrides=_ghb_overrides(node))
# Operate on the node feeding input 0:
src = node.inputs()[0] if node.inputs() else None
if src is None:
    raise hou.NodeError('Connect a HeightField into the Gaea Erosion Bridge input.')
res = ghb.export_for_gaea(src, cfg)
node.parm('_status').set(res['message'])
hou.ui.setStatusMessage(res['message'])
""".strip()

IMPORT_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
cfg = ghb.BridgeConfig.load(overrides=_ghb_overrides(node))
res = ghb.import_gaea_outputs(node, cfg)
node.parm('_status').set(res['message'])
hou.ui.setStatusMessage(res['message'])
""".strip()

OPEN_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
cfg = ghb.BridgeConfig.load(overrides=_ghb_overrides(node))
ghb.open_in_gaea(cfg)
""".strip()

# Helper injected into the node's PythonModule so each callback can read the UI.
PYTHON_MODULE = r'''
def _ghb_overrides(node):
    """Translate the HDA's parameters into a BridgeConfig overrides dict."""
    layers = ("height", "erosion", "flow", "wear", "sediment")
    output_maps = {}
    for layer in layers:
        toggle = node.parm("layer_" + layer)
        if toggle is None or toggle.eval():
            output_maps[layer] = "output_%s.exr" % layer
    params = {
        "preset": node.parm("preset").evalAsString() if node.parm("preset") else "",
        "seed": node.parm("seed").eval() if node.parm("seed") else 0,
        "duration": node.parm("duration").eval() if node.parm("duration") else 0.0,
        "erosion_scale": node.parm("erosion_scale").eval() if node.parm("erosion_scale") else 0.0,
        "sedimentation": node.parm("sedimentation").eval() if node.parm("sedimentation") else 0.0,
        "rainfall": node.parm("rainfall").eval() if node.parm("rainfall") else 0.0,
    }
    overrides = {
        "bridge_dir": node.parm("bridge_dir").evalAsString(),
        "resolution": int(node.parm("resolution").evalAsString()),
        "gaea_project": node.parm("gaea_project").evalAsString(),
        "gaea_exe": node.parm("gaea_exe").evalAsString(),
        "output_maps": output_maps,
        "params": params,
    }
    return overrides
'''.strip()


def _build_parm_template_group():
    g = hou.ParmTemplateGroup()

    presets = ("Erosion", "Hydro", "Thermal", "Texturing")
    g.append(hou.MenuParmTemplate(
        "preset", "Gaea Preset", presets, menu_labels=presets,
        help="Which Gaea graph you intend to build (recorded in the manifest)."))

    resolutions = ("1024", "2048", "4096", "8192")
    res_labels = ("1k", "2k", "4k", "8k")
    g.append(hou.MenuParmTemplate(
        "resolution", "Resolution", resolutions, menu_labels=res_labels,
        default_value=1))

    g.append(hou.IntParmTemplate("seed", "Seed", 1, default_value=(0,),
                                 min=0, max=9999))

    sim = hou.FolderParmTemplate("sim", "Simulation Controls",
                                 folder_type=hou.folderType.Simple)
    sim.addParmTemplate(hou.FloatParmTemplate("duration", "Duration", 1,
                                              default_value=(0.5,), min=0.0, max=1.0))
    sim.addParmTemplate(hou.FloatParmTemplate("erosion_scale", "Erosion Scale", 1,
                                              default_value=(0.5,), min=0.0, max=1.0))
    sim.addParmTemplate(hou.FloatParmTemplate("sedimentation", "Sedimentation", 1,
                                              default_value=(0.3,), min=0.0, max=1.0))
    sim.addParmTemplate(hou.FloatParmTemplate("rainfall", "Rainfall", 1,
                                              default_value=(0.4,), min=0.0, max=1.0))
    g.append(sim)

    layers = hou.FolderParmTemplate("layers", "Import Layers",
                                    folder_type=hou.folderType.Simple)
    for layer, on in (("height", True), ("erosion", True), ("flow", True),
                      ("wear", True), ("sediment", True)):
        layers.addParmTemplate(hou.ToggleParmTemplate(
            "layer_" + layer, layer.capitalize(), default_value=on))
    g.append(layers)

    paths = hou.FolderParmTemplate("paths", "Paths",
                                   folder_type=hou.folderType.Simple)
    paths.addParmTemplate(hou.StringParmTemplate(
        "bridge_dir", "Bridge Dir", 1, default_value=("C:/Temp/gh_bridge",),
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Directory))
    paths.addParmTemplate(hou.StringParmTemplate(
        "gaea_project", "Gaea Project (.terrain)", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference))
    paths.addParmTemplate(hou.StringParmTemplate(
        "gaea_exe", "Gaea Exe", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference))
    g.append(paths)

    # Action buttons.
    g.append(hou.ButtonParmTemplate("export", "1  \u25b8  Export to Gaea",
                                    script_callback=EXPORT_CALLBACK,
                                    script_callback_language=hou.scriptLanguage.Python))
    g.append(hou.ButtonParmTemplate("open_gaea", "Open Project in Gaea",
                                    script_callback=OPEN_CALLBACK,
                                    script_callback_language=hou.scriptLanguage.Python))
    g.append(hou.ButtonParmTemplate("do_import", "2  \u25b8  Import Results",
                                    script_callback=IMPORT_CALLBACK,
                                    script_callback_language=hou.scriptLanguage.Python))

    status = hou.StringParmTemplate("_status", "Status", 1, default_value=("",))
    status.setConditional(hou.parmCondType.DisableWhen, "{ 1 == 1 }")  # read-only
    g.append(status)

    return g


def build(install_dir=None):
    """Create, define and install the HDA. Returns the path to the .hda file."""
    obj = hou.node("/obj")
    container = obj.createNode("geo", "GHBRIDGE_TEMP_BUILD")
    try:
        subnet = container.createNode("subnet", ASSET_TYPE_NAME)

        # Minimal internal graph: input passthrough -> OUT, so the node outputs its
        # input until results are imported next to it. The import nodes are created
        # at import time inside this subnet by the module.
        indirect = subnet.indirectInputs()[0]
        out = subnet.createNode("null", "OUT")
        out.setInput(0, indirect)
        out.setDisplayFlag(True)
        out.setRenderFlag(True)
        subnet.layoutChildren()

        if install_dir is None:
            install_dir = os.path.join(
                hou.text.expandString("$HOUDINI_USER_PREF_DIR"), "otls")
        os.makedirs(install_dir, exist_ok=True)
        hda_path = os.path.join(install_dir, ASSET_TYPE_NAME + ".hda")

        asset = subnet.createDigitalAsset(
            name=ASSET_TYPE_NAME,
            hda_file_name=hda_path,
            description=ASSET_LABEL,
            min_num_inputs=1,
            max_num_inputs=1,
            ignore_external_references=True,
        )

        definition = asset.type().definition()
        definition.setParmTemplateGroup(_build_parm_template_group())

        # Store the overrides helper in the asset's PythonModule section.
        sections = definition.sections()
        if "PythonModule" in sections:
            sections["PythonModule"].setContents(PYTHON_MODULE)
        else:
            definition.addSection("PythonModule", PYTHON_MODULE)
        # Expose the module to kwargs in callbacks.
        definition.setExtraFileOption("PythonModule/IsPython", True)

        definition.setComment(
            "GH-Bridge: file-based Gaea<->Houdini bridge (manual GUI build). "
            "Requires houdini_gaea_bridge.py on the Python path.")
        definition.save(hda_path, asset, hou.hdaOptions())

        hou.hda.installFile(hda_path)
        print("Installed HDA: {}".format(hda_path))
        return hda_path
    finally:
        container.destroy()


if __name__ == "__main__":
    build()
