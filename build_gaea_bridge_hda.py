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

    input ─┬─────────────────────────────────────────────────────┐
           └ height (Gaea or input) → flow → wear → sediment       │
             → process (VEX: height scale; per mask normalize,     │
               remap, gamma, invert, strength) → blur per mask     │
             ├→ Show Mask into "mask", drop masks ── switch → output 1 "Terrain"
             ├→ flow into "mask", drop the rest ──── switch → output 2 "Flow"
             ├→ wear into "mask", drop the rest ──── switch → output 3 "Wear"
             └→ sediment into "mask", drop the rest ─ switch → output 4 "Sediment"

Outputs 2-4 have the same layout as output 1 (height + mask), so each plugs
straight into the Mask input of HeightField nodes.
"""

import os

import hou

import houdini_gaea_bridge as ghb


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
ghb.set_status(node, res['message'])
""".strip()

IMPORT_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
ghb.set_status(node, ghb.import_into_node(node)['message'])
""".strip()

CLEAR_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
ghb.clear_node_import(node)
ghb.set_status(node, 'Import cleared: the node passes its input through.')
""".strip()

AUTO_IMPORT_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
watching = ghb.sync_auto_import(node)
if node.evalParm('auto_import'):
    ghb.set_status(node,
        'Auto Import on: watching for new Gaea GUI builds.' if watching else
        'Auto Import needs the Houdini UI (not available in hython/batch).')
else:
    ghb.set_status(node, 'Auto Import off.')
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

BUILD_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.build_in_gaea(kwargs['node'])
""".strip()

CYCLE_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.run_full_cycle(kwargs['node'])
""".strip()

CANCEL_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.cancel_build(kwargs['node'])
""".strip()

TEMPLATE_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.on_template_toggled(kwargs['node'])
""".strip()

READ_TEMPLATE_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.read_template_into_node(kwargs['node'])
""".strip()

RESET_EROSION_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.read_template_into_node(kwargs['node'], defaults=True)
""".strip()

OPEN_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.open_in_gaea(ghb.config_from_node(kwargs['node']))
""".strip()


# --------------------------------------------------------------------------- #
# Expressions used inside the asset (relative to its child nodes: "../parm")
# --------------------------------------------------------------------------- #

def _has_layer_expr(layer):
    """HScript: Import found a file for this mask."""
    return 'strlen(chs("../_{0}_file")) > 0'.format(layer)


# Mask layers are dropped from every output once the wanted one is in "mask".
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


def _show_mask_vex():
    """Terrain output preview: the mask chosen in Show Mask goes into "mask"."""
    lines = ['string sel = chs("../view_mask");']
    for layer in MASK_LAYERS:
        lines.append('if (sel == "{0}") f@mask = f@{0};'.format(layer))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Internal network
# --------------------------------------------------------------------------- #

# Parameters of Houdini's own nodes are looked up from candidate names, so a
# renamed parameter in another Houdini version is found or reported clearly.
OPTIONAL_PARMS_MISSING = []


def _setp(node, candidates, value, expression=False, required=True):
    """Set (or put an HScript expression on) the first existing parameter."""
    if isinstance(candidates, str):
        candidates = (candidates,)
    for name in candidates:
        parm = node.parm(name)
        if parm is None:
            continue
        if expression:
            parm.setExpression(value, hou.exprLanguage.Hscript)
        else:
            parm.set(value)
        return parm
    message = "{} has no parameter {} in Houdini {}".format(
        node.type().name(), " / ".join(candidates), hou.applicationVersionString())
    if required:
        raise RuntimeError(message + ". Run tools/houdini_compat_check.py and report "
                           "its output.")
    OPTIONAL_PARMS_MISSING.append(message)
    return None


def _file_node(parent, name, source, layer_type, layer):
    node = parent.createNode("heightfield_file", name)
    if source is not None:
        node.setInput(0, source)
    _setp(node, ("filename", "file"), 'chs("../_{}_file")'.format(layer),
          expression=True)
    _setp(node, "type", layer_type)
    # Mask layers must land in a layer named after them.
    _setp(node, ("layer", "layername"), layer, required=layer_type == "mask")
    _setp(node, "layermode", "replace", required=False)
    _setp(node, "doclampmin", 0, required=False)
    _setp(node, "doclampmax", 0, required=False)
    _setp(node, "heightscale", 1.0, required=False)
    _setp(node, "sizemethod", "size", required=False)
    _setp(node, "size", 'if(ch("../_houdini_width") > 0, ch("../_houdini_width"), 1000)',
          expression=True, required=False)
    return node


def _switch(parent, name, inputs, index_expr):
    node = parent.createNode("switch", name)
    for i, src in enumerate(inputs):
        node.setInput(i, src)
    _setp(node, "input", "if({}, 1, 0)".format(index_expr), expression=True)
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
    _setp(process, "snippet", _process_vex())
    prev = process

    for layer in MASK_LAYERS:
        blur = subnet.createNode("heightfield_blur", "blur_" + layer)
        blur.setInput(0, prev)
        _setp(blur, ("layer", "blurlayer"), layer)
        _setp(blur, "radius", 'ch("../{}_blur")'.format(layer), expression=True)
        prev = _switch(subnet, "blur_" + layer + "_switch", (prev, blur),
                       'ch("../{}_blur") > 0 && {}'.format(layer, _has_layer_expr(layer)))
    processed = prev

    def mask_branch(name, vex):
        """Copy a layer into "mask" (VEX), then drop the Gaea mask layers."""
        copy = subnet.createNode("volumewrangle", name + "_to_mask")
        copy.setInput(0, processed)
        _setp(copy, "snippet", vex)
        clean = subnet.createNode("blast", name + "_only")
        clean.setInput(0, copy)
        _setp(clean, "group", EXTRA_LAYERS_GROUP)
        return clean

    # Output 1 - Terrain: height; "mask" shows the Show Mask choice (None = the
    # input's own mask, empty for a Gaea result).
    branches = [("TERRAIN", mask_branch("terrain", _show_mask_vex()))]
    # Outputs 2-4 - one per Gaea mask, as "mask" on the same height.
    for layer in MASK_LAYERS:
        branches.append((layer.upper(), mask_branch(
            layer, "f@mask = f@{};\n".format(layer))))

    for index, (name, branch) in enumerate(branches):
        result = _switch(subnet, name.lower() + "_imported_switch", (source, branch),
                         'ch("../_imported")')
        out = subnet.createNode("output", name)
        out.setInput(0, result)
        _setp(out, "outputidx", index)
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


def _button(name, label, callback, help_text=None):
    return hou.ButtonParmTemplate(name, label, script_callback=callback,
                                  script_callback_language=hou.scriptLanguage.Python,
                                  help=help_text)


def _mask_folder(layer, label, help_text):
    folder = hou.FolderParmTemplate(layer + "_folder", label,
                                    folder_type=hou.folderType.Collapsible)

    def add(template):
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


def _erosion_parm(name, label, kind, default, low, high):
    parm_name = ghb.EROSION_PARM_PREFIX + name
    help_text = "Gaea Erosion2 '{}' (default {}).".format(name, default)
    if kind == "bool":
        return hou.ToggleParmTemplate(parm_name, label, default_value=bool(default),
                                      help=help_text)
    if kind == "int":
        return hou.IntParmTemplate(parm_name, label, 1, default_value=(int(default),),
                                   min=low, max=high, min_is_strict=True,
                                   max_is_strict=True, help=help_text)
    return hou.FloatParmTemplate(parm_name, label, 1, default_value=(float(default),),
                                 min=low, max=high, min_is_strict=True,
                                 max_is_strict=True, help=help_text)


def _status_parm(name):
    status = hou.StringParmTemplate(name, "Status", 1, default_value=("",))
    status.setConditional(hou.parmCondType.DisableWhen, "{ 1 == 1 }")  # read-only
    return status


def _erosion_tab():
    """Erosion2 parameters of the template project, shown in template mode."""
    tab = hou.FolderParmTemplate("erosion_tab", "Erosion",
                                 folder_type=hou.folderType.Tabs)
    # Same action and status as on the Bridge tab, so the loop "tweak -> rebuild"
    # never leaves this tab.
    tab.addParmTemplate(_button(
        "run_cycle_erosion", "Export > Build > Import", CYCLE_CALLBACK,
        "Same as on the Bridge tab: export, build in Gaea with these values, import."))
    tab.addParmTemplate(_status_parm("_status_erosion"))
    tab.addParmTemplate(hou.ToggleParmTemplate(
        "use_template", "Use Erosion Template", default_value=False,
        script_callback=TEMPLATE_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="The Gaea Project is a template whose Erosion2 node feeds the Export "
             "nodes. Build in Gaea then builds a copy of it with the values below; "
             "the original project is not changed. Builds in the Gaea GUI (F8) "
             "don't see these values."))
    hidden = "{ use_template == 0 }"

    node_name = hou.StringParmTemplate(
        "erosion_node", "Erosion Node", 1, default_value=("",),
        help="Name of the Erosion2 node to drive. Empty = the only Erosion2 in the "
             "project.")
    node_name.setConditional(hou.parmCondType.HideWhen, hidden)
    tab.addParmTemplate(node_name)
    for name, label, callback, help_text in (
            ("read_template", "Read from Project", READ_TEMPLATE_CALLBACK,
             "Load the Erosion2 values saved in the Gaea project."),
            ("reset_erosion", "Reset to Gaea Defaults", RESET_EROSION_CALLBACK,
             "Set every value to Gaea's default for Erosion2.")):
        button = _button(name, label, callback, help_text)
        button.setConditional(hou.parmCondType.HideWhen, hidden)
        tab.addParmTemplate(button)

    folders = {}
    for name, label, group, kind, default, low, high in ghb.EROSION2_PARAMS:
        folder = folders.get(group)
        if folder is None:
            folder = hou.FolderParmTemplate(
                "ero_" + group.lower().replace(" ", "_"), group,
                folder_type=hou.folderType.Collapsible)
            folder.setConditional(hou.parmCondType.HideWhen, hidden)
            folders[group] = folder
        template = _erosion_parm(name, label, kind, default, low, high)
        if group == "Orographic Influence" and name != "Enable":
            off = "{ ero_Enable == 0 }"
            if name == "Direction":
                off += " { ero_DirectionalPrecipitation == 0 }"
            template.setConditional(hou.parmCondType.DisableWhen, off)
        folder.addParmTemplate(template)
    for folder in folders.values():
        tab.addParmTemplate(folder)
    return tab


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
    bridge.addParmTemplate(_button(
        "run_cycle", "Export > Build > Import", CYCLE_CALLBACK,
        "Whole round trip without opening Gaea: export the input, build the Gaea "
        "project with Gaea.Swarm.exe in the background, import the result."))
    bridge.addParmTemplate(_button("export", "1. Export to Gaea", EXPORT_CALLBACK))
    bridge.addParmTemplate(_button(
        "build_gaea", "2. Build in Gaea", BUILD_CALLBACK,
        "Build the Gaea project with Gaea.Swarm.exe (no Gaea window). With Auto "
        "Import After Build on, the result is imported when it finishes."))
    bridge.addParmTemplate(_button("do_import", "3. Import Results", IMPORT_CALLBACK))
    bridge.addParmTemplate(_button("cancel_build", "Cancel Build", CANCEL_CALLBACK))
    bridge.addParmTemplate(_button("open_gaea", "Open Project in Gaea", OPEN_CALLBACK))

    build = hou.FolderParmTemplate("build_options", "Gaea Build Options",
                                   folder_type=hou.folderType.Collapsible)
    build.addParmTemplate(hou.MenuParmTemplate(
        "build_resolution", "Build Resolution",
        ("0", "512", "1024", "2048", "4096", "8192"),
        menu_labels=("From Project", "512", "1K", "2K", "4K", "8K"), default_value=0,
        help="Resolution Gaea builds at (Swarm --resolution). From Project = the "
             "project's Terrain Definition."))
    build.addParmTemplate(hou.ToggleParmTemplate(
        "ignore_cache", "Ignore Baked Cache", default_value=False,
        help="Swarm --ignorecache: rebuild nodes Gaea has baked."))
    build.addParmTemplate(hou.ToggleParmTemplate(
        "show_console", "Show Build Window", default_value=False,
        help="Show Swarm's console window during the build (for diagnostics)."))
    bridge.addParmTemplate(build)

    bridge.addParmTemplate(hou.ToggleParmTemplate(
        "import_after_build", "Auto Import After Build", default_value=True,
        help="Import the result as soon as 2. Build in Gaea finishes. Off: build "
             "only, import later with 3. Import Results. (Export > Build > Import "
             "always imports.)"))
    bridge.addParmTemplate(hou.ToggleParmTemplate(
        "auto_import", "Auto Import (Gaea GUI builds)", default_value=False,
        script_callback=AUTO_IMPORT_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="Watch the output folder and import automatically when a build made in "
             "the Gaea GUI (F8) writes new files (checked every second; imports once "
             "the files stop changing). Stays on when the scene is reopened."))
    bridge.addParmTemplate(_button("clear_import", "Clear Import", CLEAR_CALLBACK))
    bridge.addParmTemplate(_status_parm("_status"))
    g.append(bridge)

    g.append(_erosion_tab())

    # --- Layers tab ---------------------------------------------------- #
    layers = hou.FolderParmTemplate("layers_tab", "Gaea Layers",
                                    folder_type=hou.folderType.Tabs)
    layers.addParmTemplate(hou.MenuParmTemplate(
        "height_source", "Height", ("gaea", "input"),
        menu_labels=("Gaea Result", "Input HeightField"), default_value=0,
        help="Gaea Result: the eroded terrain from Gaea. Input HeightField: keep the "
             "original height and only add the Gaea masks to it."))
    layers.addParmTemplate(hou.MenuParmTemplate(
        "view_mask", "Show Mask", ("none",) + MASK_LAYERS,
        menu_labels=("None", "Flow", "Wear", "Sediment"), default_value=0,
        help="Preview on the Terrain output: put this mask into its 'mask' layer so "
             "it shows in red on the terrain. Set back to None before using the "
             "Terrain output downstream, or HeightField nodes will use that mask. "
             "Each mask always has its own output (Flow, Wear, Sediment)."))
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
    paths.addParmTemplate(hou.StringParmTemplate(
        "gaea_swarm", "Gaea Swarm Exe", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference,
        help="Gaea's command-line builder. Empty = Gaea.Swarm.exe next to Gaea Exe."))
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
    g.append(_hidden(hou.ToggleParmTemplate("_ero_loaded", "Erosion values loaded",
                                            default_value=False)))
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
    del OPTIONAL_PARMS_MISSING[:]
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
        definition.setMaxNumOutputs(1 + len(MASK_LAYERS))
        _set_io_labels(definition, inputs=("HeightField",),
                       outputs=("Terrain",) + tuple(l.capitalize() for l in MASK_LAYERS))
        for section, script in (("OnLoaded", ON_LOADED_SCRIPT),
                                ("OnDeleted", ON_DELETED_SCRIPT)):
            definition.addSection(section, script)
            definition.setExtraFileOption(section + "/IsPython", True)

        definition.setComment(
            "GH-Bridge: file-based Gaea<->Houdini bridge (manual GUI build). "
            "Requires houdini_gaea_bridge.py on the Python path.")
        definition.save(hda_path, asset)

        hou.hda.installFile(hda_path)
        print("Installed HDA: {} (Houdini {})".format(
            hda_path, hou.applicationVersionString()))
        for message in OPTIONAL_PARMS_MISSING:
            print("  note: {} (optional, skipped)".format(message))
        return hda_path
    finally:
        container.destroy()


if __name__ == "__main__":
    build()
