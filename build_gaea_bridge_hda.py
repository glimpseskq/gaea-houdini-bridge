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


# The callback snippets stored on the buttons. Button callbacks run in their own
# namespace (they cannot see the HDA's PythonModule directly), so all logic lives
# in houdini_gaea_bridge and the callbacks only call into it.
EXPORT_CALLBACK = r"""
import hou
import houdini_gaea_bridge as ghb
node = kwargs['node']
cfg = ghb.config_from_node(node)
# Operate on the node feeding input 0:
src = node.inputs()[0] if node.inputs() else None
if src is None:
    raise hou.NodeError('Connect a HeightField into the Gaea Erosion Bridge input.')
res = ghb.export_for_gaea(src, cfg)
node.parm('_status').set(res['message'])
hou.ui.setStatusMessage(res['message'])
""".strip()

IMPORT_CALLBACK = r"""
import hou
import houdini_gaea_bridge as ghb
node = kwargs['node']
cfg = ghb.config_from_node(node)
res = ghb.import_gaea_outputs(node, cfg)
node.parm('_status').set(res['message'])
hou.ui.setStatusMessage(res['message'])
""".strip()

OPEN_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.open_in_gaea(ghb.config_from_node(kwargs['node']))
""".strip()


def _build_parm_template_group():
    g = hou.ParmTemplateGroup()

    g.append(hou.MenuParmTemplate(
        "downsample", "Resolution", ("1", "2", "3", "4"),
        menu_labels=("Full", "1/2", "1/3", "1/4"), default_value=0,
        help="Export resolution relative to the input HeightField. The terrain keeps "
             "its size; only the pixel count drops (1/2 = half the voxels per axis)."))

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

    world = hou.FolderParmTemplate("gaea_world", "Gaea Terrain Size",
                                   folder_type=hou.folderType.Simple)
    world.addParmTemplate(hou.FloatParmTemplate(
        "gaea_width", "Width (m)", 1, default_value=(0.0,), min=0.0, max=20000.0,
        help="Gaea Terrain Definition > Scale. 0 = read from the .terrain project."))
    world.addParmTemplate(hou.FloatParmTemplate(
        "gaea_height", "Height (m)", 1, default_value=(0.0,), min=0.0, max=10000.0,
        help="Gaea Terrain Definition > Height. 0 = read from the .terrain project."))
    g.append(world)

    # Action buttons.
    g.append(hou.ButtonParmTemplate("export", "1. Export to Gaea",
                                    script_callback=EXPORT_CALLBACK,
                                    script_callback_language=hou.scriptLanguage.Python))
    g.append(hou.ButtonParmTemplate("open_gaea", "Open Project in Gaea",
                                    script_callback=OPEN_CALLBACK,
                                    script_callback_language=hou.scriptLanguage.Python))
    g.append(hou.ButtonParmTemplate("do_import", "2. Import Results",
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

        definition.setComment(
            "GH-Bridge: file-based Gaea<->Houdini bridge (manual GUI build). "
            "Requires houdini_gaea_bridge.py on the Python path.")
        definition.save(hda_path, asset)

        hou.hda.installFile(hda_path)
        print("Installed HDA: {}".format(hda_path))
        return hda_path
    finally:
        container.destroy()


if __name__ == "__main__":
    build()
