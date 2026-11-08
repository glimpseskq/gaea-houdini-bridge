"""Build and install the ``gaea_erosion_bridge`` Houdini Digital Asset.

Run this from inside Houdini (Windows > Python Shell, or a Python Source Editor)
after every update of the bridge. It builds a SOP subnet with the import network,
promotes the GH-Bridge UI, turns it into an HDA and installs it, so the node shows
up as ``gaea_erosion_bridge`` in the TAB menu.

    exec(open(r"C:/.../build_gaea_bridge_hda.py").read())

The node's buttons call ``houdini_gaea_bridge``, so make sure that module is on
Houdini's Python path (see README). The HDA never touches Gaea licensing.

Inside the asset
----------------
Until results are imported the node passes its input through. **Import** only
stores the found Gaea files (and the height encoding / mask peaks) in hidden
parameters; the network below reads them, so every mask setting on the node works
live, without re-importing:

    input ─┬──────────────────────────────────────────────────┐
           └ height (Gaea or input) → selected Gaea mask        │
             → process (VEX: height scale, normalize, remap,    │
               gamma, invert, strength) → blur                  │
             ├→ drop mask layers ──────────────── switch → output 1 "Terrain"
             └→ selected mask into "mask", drop the rest ─ switch → output 2 "Mask"

Output 2 has the same layout as output 1 (height + mask), so it plugs straight
into the Mask input of HeightField nodes.
"""

import os

import hou


ASSET_TYPE_NAME = "gaea_erosion_bridge"
ASSET_LABEL = "Gaea Erosion Bridge"

MASK_LAYERS = ("flow", "wear", "sediment")


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
res = ghb.import_into_node(node)
node.parm('_status').set(res['message'])
hou.ui.setStatusMessage(res['message'])
""".strip()

CLEAR_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
ghb.clear_node_import(node)
node.parm('_status').set('Import cleared: the node passes its input through.')
""".strip()

AUTO_IMPORT_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
watching = ghb.sync_auto_import(node)
if node.evalParm('auto_import'):
    node.parm('_status').set(
        'Auto Import on: watching for new Gaea builds.' if watching else
        'Auto Import needs the Houdini UI (not available in hython/batch).')
else:
    node.parm('_status').set('Auto Import off.')
""".strip()

# HDA event scripts: resume watching when a scene is opened, stop on delete.
ON_LOADED_SCRIPT = r"""
try:
    import houdini_gaea_bridge as ghb
    ghb.sync_auto_import(kwargs['node'])
except Exception as error:
    print('Gaea Erosion Bridge: could not resume Auto Import: {}'.format(error))
""".strip()

ON_DELETED_SCRIPT = r"""
try:
    import houdini_gaea_bridge as ghb
    ghb.stop_auto_import(kwargs['node'])
except Exception:
    pass
""".strip()

OPEN_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.open_in_gaea(ghb.config_from_node(kwargs['node']))
""".strip()


# --------------------------------------------------------------------------- #
# Expressions used inside the asset (relative to its child nodes: "../parm")
# --------------------------------------------------------------------------- #

def _has_layer_expr(layer):
    """HScript: this mask is the selected Mask Output and Import found its file."""
    return ('strcmp(chs("../mask_layer"), "{0}") == 0 && '
            'strlen(chs("../_{0}_file")) > 0'.format(layer))


# Mask layers are dropped from the Terrain output and, once copied into "mask",
# from the Mask output.
EXTRA_LAYERS_GROUP = " ".join("@name=" + layer for layer in MASK_LAYERS)


HEIGHT_FROM_GAEA_EXPR = 'strlen(chs("../_height_file")) > 0 && ch("../height_source") == 0'


def _process_vex():
    """Per-voxel processing of the imported layers (runs in a Volume Wrangle)."""
    lines = [
        "// Gaea Erosion Bridge: driven by the asset's parameters.",
        "",
        "// Height: Gaea 0..1 -> Houdini units, only when the height comes from Gaea.",
        'if (chi("../height_source") == 0 && strlen(chs("../_height_file")) > 0'
        ' && chf("../_enc_span") > 0)',
        '    f@height = f@height * chf("../_enc_span") + chf("../_enc_offset");',
    ]
    block = "\n".join([
        "",
        'if (strlen(chs("../_{0}_file")) > 0) {{',
        "    float v = f@{0};",
        '    float peak = chf("../_{0}_peak");',
        '    if (chi("../{0}_normalize") && peak > 0) v /= peak;',
        '    float lo = chf("../{0}_rangemin");',
        '    float hi = max(chf("../{0}_rangemax"), lo + 1e-6);',
        "    v = fit(v, lo, hi, 0, 1);",
        '    v = pow(v, max(chf("../{0}_gamma"), 1e-3));',
        '    if (chi("../{0}_invert")) v = 1 - v;',
        '    f@{0} = v * chf("../{0}_strength");',
        "}}",
    ])
    for layer in MASK_LAYERS:
        lines.append(block.format(layer))
    return "\n".join(lines) + "\n"


def _mask_copy_vex():
    lines = ['string sel = chs("../mask_layer");']
    for layer in MASK_LAYERS:
        lines.append('if (sel == "{0}") f@mask = f@{0};'.format(layer))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Internal network
# --------------------------------------------------------------------------- #

def _file_node(parent, name, source, layer_type, layer):
    node = parent.createNode("heightfield_file", name)
    if source is not None:
        node.setInput(0, source)
    node.parm("filename").setExpression(
        'chs("../_{}_file")'.format(layer), hou.exprLanguage.Hscript)
    node.parm("type").set(layer_type)
    node.parm("layer").set(layer)
    node.parm("layermode").set("replace")
    node.parm("doclampmin").set(0)
    node.parm("doclampmax").set(0)
    node.parm("heightscale").set(1.0)
    node.parm("sizemethod").set("size")
    node.parm("size").setExpression(
        'if(ch("../_houdini_width") > 0, ch("../_houdini_width"), 1000)',
        hou.exprLanguage.Hscript)
    return node


def _switch(parent, name, inputs, index_expr):
    node = parent.createNode("switch", name)
    for i, src in enumerate(inputs):
        node.setInput(i, src)
    node.parm("input").setExpression(
        "if({}, 1, 0)".format(index_expr), hou.exprLanguage.Hscript)
    return node


def _build_network(subnet):
    source = subnet.indirectInputs()[0]

    gaea_height = _file_node(subnet, "file_height", None, "heightfield", "height")
    prev = _switch(subnet, "height_source_switch", (source, gaea_height),
                   HEIGHT_FROM_GAEA_EXPR)

    for layer in MASK_LAYERS:
        file_node = _file_node(subnet, "file_" + layer, prev, "mask", layer)
        prev = _switch(subnet, "use_" + layer, (prev, file_node),
                       _has_layer_expr(layer))

    raw = subnet.createNode("null", "LAYERS_RAW")
    raw.setInput(0, prev)

    process = subnet.createNode("volumewrangle", "process_layers")
    process.setInput(0, raw)
    process.parm("snippet").set(_process_vex())
    prev = process

    for layer in MASK_LAYERS:
        blur = subnet.createNode("heightfield_blur", "blur_" + layer)
        blur.setInput(0, prev)
        blur.parm("layer").set(layer)
        blur.parm("radius").setExpression(
            'ch("../{}_blur")'.format(layer), hou.exprLanguage.Hscript)
        prev = _switch(subnet, "blur_" + layer + "_switch", (prev, blur),
                       'ch("../{}_blur") > 0 && {}'.format(layer, _has_layer_expr(layer)))
    processed = prev

    # Output 1 - Terrain: height (and the input's own mask), no Gaea mask layers.
    terrain = subnet.createNode("blast", "terrain_only")
    terrain.setInput(0, processed)
    terrain.parm("group").set(EXTRA_LAYERS_GROUP)

    # Output 2 - Mask: the selected mask in "mask", on the same height.
    mask_copy = subnet.createNode("volumewrangle", "selected_to_mask")
    mask_copy.setInput(0, processed)
    mask_copy.parm("snippet").set(_mask_copy_vex())
    mask_only = subnet.createNode("blast", "mask_only")
    mask_only.setInput(0, mask_copy)
    mask_only.parm("group").set(EXTRA_LAYERS_GROUP)

    # "Show Mask on Terrain": output 1 shows the selected mask in red on the terrain.
    terrain_view = _switch(subnet, "show_mask_switch", (terrain, mask_only),
                           'ch("../show_mask")')

    for index, (name, branch) in enumerate((("TERRAIN", terrain_view),
                                            ("MASK", mask_only))):
        result = _switch(subnet, name.lower() + "_imported_switch", (source, branch),
                         'ch("../_imported")')
        out = subnet.createNode("output", name)
        out.setInput(0, result)
        out.parm("outputidx").set(index)
        if index == 0:
            out.setDisplayFlag(True)
            out.setRenderFlag(True)
    subnet.layoutChildren()


# --------------------------------------------------------------------------- #
# Parameter interface
# --------------------------------------------------------------------------- #

def _hidden(template):
    template.hide(True)
    return template


def _button(name, label, callback):
    return hou.ButtonParmTemplate(name, label, script_callback=callback,
                                  script_callback_language=hou.scriptLanguage.Python)


def _mask_folder(layer, label, help_text):
    folder = hou.FolderParmTemplate(layer + "_folder", label,
                                    folder_type=hou.folderType.Collapsible)
    # Only the selected Mask Output is computed; the other sections keep their
    # settings for when you switch to them.
    inactive = '{{ mask_layer != "{}" }}'.format(layer)

    def add(template):
        template.setConditional(hou.parmCondType.DisableWhen, inactive)
        if not template.help():
            template.setHelp(help_text)
        folder.addParmTemplate(template)

    add(hou.ToggleParmTemplate(
        layer + "_normalize", "Normalize", default_value=True,
        help="Divide by the layer's peak so it spans 0..1. Gaea writes some masks "
             "with tiny values (wear ~0.05)."))
    add(hou.FloatParmTemplate(
        layer + "_range", "Input Range", 2, default_value=(0.0, 1.0),
        min=0.0, max=1.0, naming_scheme=hou.parmNamingScheme.MinMax,
        help="Values at or below Min become 0, at or above Max become 1. Raise Min to "
             "cut faint areas, lower Max to boost weak ones."))
    add(hou.FloatParmTemplate(
        layer + "_gamma", "Gamma", 1, default_value=(1.0,), min=0.1, max=4.0,
        help="< 1 widens the mask, > 1 tightens it to the strongest areas."))
    add(hou.ToggleParmTemplate(layer + "_invert", "Invert", default_value=False))
    add(hou.FloatParmTemplate(
        layer + "_strength", "Strength", 1, default_value=(1.0,), min=0.0, max=2.0,
        help="Final multiplier."))
    add(hou.FloatParmTemplate(
        layer + "_blur", "Blur Radius", 1, default_value=(0.0,), min=0.0, max=50.0,
        help="Smooth the final mask (HeightField Blur radius, scene units). 0 = off."))
    return folder


def _build_parm_template_group():
    g = hou.ParmTemplateGroup()

    # --- Bridge tab ---------------------------------------------------- #
    bridge = hou.FolderParmTemplate("bridge_tab", "Bridge",
                                    folder_type=hou.folderType.Tabs)
    bridge.addParmTemplate(hou.MenuParmTemplate(
        "downsample", "Export Resolution", ("1", "2", "3", "4"),
        menu_labels=("Full", "1/2", "1/3", "1/4"), default_value=0,
        help="Export resolution relative to the input HeightField. The terrain keeps "
             "its size; only the pixel count drops (1/2 = half the voxels per axis)."))
    bridge.addParmTemplate(_button("export", "1. Export to Gaea", EXPORT_CALLBACK))
    bridge.addParmTemplate(_button("open_gaea", "Open Project in Gaea", OPEN_CALLBACK))
    bridge.addParmTemplate(_button("do_import", "2. Import Results", IMPORT_CALLBACK))
    bridge.addParmTemplate(hou.ToggleParmTemplate(
        "auto_import", "Auto Import", default_value=False,
        script_callback=AUTO_IMPORT_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="Watch the output folder and import automatically when Gaea writes a "
             "new build (checked every second; imports once the files stop "
             "changing). Stays on when the scene is reopened."))
    bridge.addParmTemplate(_button("clear_import", "Clear Import", CLEAR_CALLBACK))
    status = hou.StringParmTemplate("_status", "Status", 1, default_value=("",))
    status.setConditional(hou.parmCondType.DisableWhen, "{ 1 == 1 }")  # read-only
    bridge.addParmTemplate(status)
    g.append(bridge)

    # --- Layers tab ---------------------------------------------------- #
    layers = hou.FolderParmTemplate("layers_tab", "Gaea Layers",
                                    folder_type=hou.folderType.Tabs)
    layers.addParmTemplate(hou.MenuParmTemplate(
        "height_source", "Height", ("gaea", "input"),
        menu_labels=("Gaea Result", "Input HeightField"), default_value=0,
        help="Gaea Result: the eroded terrain from Gaea. Input HeightField: keep the "
             "original height and only add the Gaea masks to it."))
    layers.addParmTemplate(hou.MenuParmTemplate(
        "mask_layer", "Mask Output", MASK_LAYERS,
        menu_labels=("Flow", "Wear", "Sediment"), default_value=0,
        help="Gaea mask sent to the node's second output (Mask), as the 'mask' layer "
             "on the same height. Plug it into the Mask input of any HeightField "
             "node, or process it on its own. The first output (Terrain) carries no "
             "Gaea masks."))
    layers.addParmTemplate(hou.ToggleParmTemplate(
        "show_mask", "Show Mask on Terrain", default_value=False,
        help="Preview: put the selected mask into the Terrain output's 'mask' layer, "
             "so it shows in red on the terrain. Turn off before using the Terrain "
             "output downstream, or HeightField nodes will use that mask."))
    layers.addParmTemplate(_mask_folder(
        "flow", "Flow", "Where water runs: thin channel networks (Erosion2 Flow)."))
    layers.addParmTemplate(_mask_folder(
        "wear", "Wear", "Where material was eroded away (Erosion2 Wear)."))
    layers.addParmTemplate(_mask_folder(
        "sediment", "Sediment", "Where eroded material was deposited "
        "(Erosion2 Deposits)."))
    g.append(layers)

    # --- Paths tab ----------------------------------------------------- #
    paths = hou.FolderParmTemplate("paths_tab", "Paths",
                                   folder_type=hou.folderType.Tabs)
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
    paths.addParmTemplate(hou.FloatParmTemplate(
        "gaea_width", "Gaea Width (m)", 1, default_value=(0.0,), min=0.0, max=20000.0,
        help="Gaea Terrain Definition > Scale. 0 = read from the .terrain project."))
    paths.addParmTemplate(hou.FloatParmTemplate(
        "gaea_height", "Gaea Height (m)", 1, default_value=(0.0,), min=0.0,
        max=10000.0,
        help="Gaea Terrain Definition > Height. 0 = read from the .terrain project."))
    g.append(paths)

    # --- Hidden state written by Import ------------------------------- #
    g.append(_hidden(hou.ToggleParmTemplate("_imported", "Imported",
                                            default_value=False)))
    g.append(_hidden(hou.StringParmTemplate("_import_stamp", "Import stamp", 1,
                                            default_value=("",))))
    for layer in ("height",) + MASK_LAYERS:
        g.append(_hidden(hou.StringParmTemplate(
            "_{}_file".format(layer), layer + " file", 1, default_value=("",))))
    for layer in MASK_LAYERS:
        g.append(_hidden(hou.FloatParmTemplate(
            "_{}_peak".format(layer), layer + " peak", 1, default_value=(0.0,))))
    for name in ("_enc_span", "_enc_offset", "_houdini_width"):
        g.append(_hidden(hou.FloatParmTemplate(name, name, 1, default_value=(0.0,))))

    return g


def _set_io_labels(definition, inputs, outputs):
    """Write input/output labels into the asset's DialogScript section."""
    section = definition.sections()["DialogScript"]
    lines = [line for line in section.contents().splitlines()
             if not line.strip().startswith(("inputlabel", "outputlabel"))]
    labels = ["    inputlabel\t{}\t\"{}\"".format(i + 1, text)
              for i, text in enumerate(inputs)]
    labels += ["    outputlabel\t{}\t\"{}\"".format(i + 1, text)
               for i, text in enumerate(outputs)]
    for i, line in enumerate(lines):
        if line.strip().startswith("label"):
            lines[i + 1:i + 1] = labels
            break
    section.setContents("\n".join(lines) + "\n")


def build(install_dir=None):
    """Create, define and install the HDA. Returns the path to the .hda file."""
    obj = hou.node("/obj")
    container = obj.createNode("geo", "GHBRIDGE_TEMP_BUILD")
    try:
        subnet = container.createNode("subnet", ASSET_TYPE_NAME)
        # Give the subnet the interface first so the "../parm" expressions inside
        # resolve while the network is built.
        subnet.setParmTemplateGroup(_build_parm_template_group())
        _build_network(subnet)

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
        definition.setMaxNumOutputs(2)
        _set_io_labels(definition, inputs=("HeightField",),
                       outputs=("Terrain", "Mask"))
        for section, script in (("OnLoaded", ON_LOADED_SCRIPT),
                                ("OnDeleted", ON_DELETED_SCRIPT)):
            definition.addSection(section, script)
            definition.setExtraFileOption(section + "/IsPython", True)

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
