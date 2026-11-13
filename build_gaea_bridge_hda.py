"""Build and install the GH-Bridge Houdini Digital Assets: ``gaea_erosion_bridge``
(Gaea Erosion2 Bridge) and ``gaea_erosion1_bridge`` (Gaea Erosion Bridge, for the
older Erosion node), one per Gaea erosion node kind.

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


# One HDA per Gaea erosion node. The Erosion2 type name stays "gaea_erosion_bridge"
# so nodes in saved scenes keep their definition; its TAB menu label and new node
# names say Erosion2, like in Gaea.
ASSETS = {
    "Erosion2": {"type_name": "gaea_erosion_bridge", "label": "Gaea Erosion2 Bridge",
                 "node_name": "gaea_erosion2_bridge"},
    "Erosion": {"type_name": "gaea_erosion1_bridge", "label": "Gaea Erosion Bridge",
                "node_name": None},  # Houdini already names them gaea_erosion1_bridge1
}
ASSET_TYPE_NAME = ASSETS["Erosion2"]["type_name"]
ASSET_LABEL = ASSETS["Erosion2"]["label"]
NODE_NAME = ASSETS["Erosion2"]["node_name"]

MASK_LAYERS = ("flow", "wear", "sediment")


# The callback snippets stored on the buttons. Button callbacks run in their own
# namespace (they cannot see the HDA's PythonModule directly), so all logic lives
# in houdini_gaea_bridge and the callbacks only call into it.
EXPORT_CALLBACK = r"""
import houdini_gaea_bridge as ghb
node = kwargs['node']
ghb.set_status(node, ghb.export_from_node(node)['message'])
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
    ghb.unlock_after_load(kwargs['node'])
    ghb.sync_auto_import(kwargs['node'])
except Exception as error:
    print('Gaea Erosion2 Bridge: could not resume Auto Import: {}'.format(error))
""".strip()

# New nodes are named gaea_erosion2_bridge1, 2, ... (Houdini names them after the
# type, gaea_erosion_bridge1); renamed nodes are left alone.
ON_CREATED_SCRIPT = r"""
import re
node = kwargs['node']
match = re.match(r'^gaea_erosion_bridge(\d*)$', node.name())
if match:
    try:
        node.setName('%s' + (match.group(1) or '1'), unique_name=True)
    except Exception:
        pass
""".strip() % NODE_NAME

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

EROSION_CHANGED_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.on_erosion_parm_changed(kwargs['node'])
""".strip()

READ_TEMPLATE_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.read_template_into_node(kwargs['node'])
""".strip()

RESET_EROSION_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.read_template_into_node(kwargs['node'], defaults=True)
""".strip()

TOGGLE_TINT_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.toggle_tint(kwargs['node'])
""".strip()

SHARED_PATHS_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.on_shared_path_changed(kwargs['node'])
""".strip()

CHECK_PATHS_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.show_paths(kwargs['node'])
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
        "// Gaea Erosion2 Bridge: driven by the asset's parameters.",
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
        # Put every layer back onto the input HeightField's grid, so the outputs
        # keep the input's voxel size and resolution instead of Gaea's.
        on_input_grid = subnet.createNode("heightfield_layer",
                                          name.lower() + "_to_input_grid")
        on_input_grid.setInput(0, source)
        on_input_grid.setInput(1, result)
        _setp(on_input_grid, "mode", "replace")
        _setp(on_input_grid, "layer", "*")
        result = _switch(subnet, name.lower() + "_grid_switch",
                         (result, on_input_grid), 'ch("../keep_input_res")')
        if index == 0:
            # Show / Hide Tint: the Tint map, converted to luminance, goes into
            # the Terrain output's "mask" layer and is drawn like Show Mask (a
            # layer change the viewport always follows). No extra layer.
            tint = subnet.createNode("heightfield_file", "tint_to_mask")
            tint.setInput(0, result)
            _setp(tint, ("filename", "file"), 'chs("../_tint_file")', expression=True)
            _setp(tint, "monoop", "lum", required=False)  # color -> luminance
            _setp(tint, "type", "mask")
            _setp(tint, ("layer", "layername"), "mask")
            _setp(tint, "layermode", "replace", required=False)
            _setp(tint, "doclampmin", 0, required=False)
            _setp(tint, "doclampmax", 0, required=False)
            result = _switch(
                subnet, "tint_display_switch", (result, tint),
                'ch("../_imported") && ch("../show_tint") && '
                'strlen(chs("../_tint_file")) > 0')
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


def _enum_label(token):
    """"ErosionStrength" -> "Erosion Strength"."""
    return "".join(" " + c if c.isupper() and i else c for i, c in enumerate(token))


def _erosion_parm(node_kind, name, label, kind, default, low, high):
    parm_name = ghb.EROSION_PARM_PREFIX + name
    help_text = "Gaea {} '{}' (default {}).".format(node_kind, name, default)
    # Every Erosion value triggers Auto Build on Change (if it's on).
    callback = dict(script_callback=EROSION_CHANGED_CALLBACK,
                    script_callback_language=hou.scriptLanguage.Python)
    if kind == "enum":
        return hou.MenuParmTemplate(
            parm_name, label, tuple(low), menu_labels=tuple(_enum_label(v) for v in low),
            default_value=list(low).index(default), help=help_text, **callback)
    if kind == "bool":
        return hou.ToggleParmTemplate(parm_name, label, default_value=bool(default),
                                      help=help_text, **callback)
    if kind == "int":
        return hou.IntParmTemplate(parm_name, label, 1, default_value=(int(default),),
                                   min=low, max=high, min_is_strict=True,
                                   max_is_strict=True, help=help_text, **callback)
    return hou.FloatParmTemplate(parm_name, label, 1, default_value=(float(default),),
                                 min=low, max=high, min_is_strict=True,
                                 max_is_strict=True, help=help_text, **callback)


# While a build started by Auto Build on Change runs, every parameter is disabled
# through this condition, except the ones that must stay usable.
LOCK_CONDITION = "{ _locked == 1 }"
NEVER_LOCKED = ("cancel_build", "cancel_build_erosion")


def _with_lock(template):
    """Add the lock condition to a parameter template (recursing into folders)."""
    if template.type() == hou.parmTemplateType.Folder:
        template.setParmTemplates([_with_lock(t) for t in template.parmTemplates()])
        return template
    if template.isHidden() or template.name() in NEVER_LOCKED:
        return template
    current = template.conditionals().get(hou.parmCondType.DisableWhen, "")
    template.setConditional(hou.parmCondType.DisableWhen,
                            (current + " " + LOCK_CONDITION).strip())
    return template


def _status_parm(name):
    status = hou.StringParmTemplate(name, "Status", 1, default_value=("",))
    status.setConditional(hou.parmCondType.DisableWhen, "{ 1 == 1 }")  # read-only
    return status


def _erosion_tab(node_kind):
    """Parameters of the project's erosion node (``node_kind``: "Erosion2" or
    "Erosion"), shown in template mode."""
    tab = hou.FolderParmTemplate("erosion_tab", "Erosion",
                                 folder_type=hou.folderType.Tabs)
    # Same action and status as on the Bridge tab, so the loop "tweak -> rebuild"
    # never leaves this tab.
    tab.addParmTemplate(_button(
        "run_cycle_erosion", "Export > Build > Import", CYCLE_CALLBACK,
        "Same as on the Bridge tab: export, build in Gaea with these values, import."))
    tab.addParmTemplate(hou.ToggleParmTemplate(
        "auto_rebuild", "Auto Build on Change", default_value=False,
        help="Run Export > Build > Import automatically whenever an Erosion value "
             "changes (once you stop changing it). While it runs, the whole node is "
             "locked until the result is imported; Cancel Build unlocks it. Needs "
             "Use Erosion Template."))
    tab.addParmTemplate(_button("cancel_build_erosion", "Cancel Build", CANCEL_CALLBACK,
                                "Stop the running build and unlock the node."))
    tab.addParmTemplate(_status_parm("_status_erosion"))
    tab.addParmTemplate(hou.ToggleParmTemplate(
        "use_template", "Use Erosion Template", default_value=False,
        script_callback=TEMPLATE_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="The Gaea Project is a template whose {0} node feeds the Export "
             "nodes. Build in Gaea then builds a copy of it with the values below; "
             "the original project is not changed. Builds in the Gaea GUI (F8) "
             "don't see these values.".format(node_kind)))
    hidden = "{ use_template == 0 }"

    node_name = hou.StringParmTemplate(
        "erosion_node", "Erosion Node", 1, default_value=("",),
        help="Name of the {0} node to drive. Empty = the only {0} in the "
             "project.".format(node_kind))
    node_name.setConditional(hou.parmCondType.HideWhen, hidden)
    tab.addParmTemplate(node_name)
    for name, label, callback, help_text in (
            ("read_template", "Read from Project", READ_TEMPLATE_CALLBACK,
             "Load the {} values saved in the Gaea project.".format(node_kind)),
            ("reset_erosion", "Reset to Gaea Defaults", RESET_EROSION_CALLBACK,
             "Set every value to Gaea's default for {}.".format(node_kind))):
        button = _button(name, label, callback, help_text)
        button.setConditional(hou.parmCondType.HideWhen, hidden)
        tab.addParmTemplate(button)

    folders = {}
    for name, label, group, kind, default, low, high in ghb.erosion_spec(node_kind):
        folder = folders.get(group)
        if folder is None:
            folder = hou.FolderParmTemplate(
                "ero_" + group.lower().replace(" ", "_"), group,
                folder_type=hou.folderType.Collapsible)
            folder.setConditional(hou.parmCondType.HideWhen, hidden)
            folders[group] = folder
        template = _erosion_parm(node_kind, name, label, kind, default, low, high)
        # Gaea's own greying-out of dependent values.
        if group == "Orographic Influence" and name != "Enable":  # Erosion2
            off = "{ ero_Enable == 0 }"
            if name == "Direction":
                off += " { ero_DirectionalPrecipitation == 0 }"
            template.setConditional(hou.parmCondType.DisableWhen, off)
        if group == "Selective Processing" and name != "AreaEffect":  # Erosion
            template.setConditional(hou.parmCondType.DisableWhen,
                                    "{ ero_AreaEffect == None }")
        folder.addParmTemplate(template)
    for folder in folders.values():
        tab.addParmTemplate(folder)
    return tab


def _tint_tab():
    """Build Tint toggle and the tint display button. The Tint settings
    themselves are the Gaea project's own."""
    tab = hou.FolderParmTemplate("tint_tab", "Tint", folder_type=hou.folderType.Tabs)
    tab.addParmTemplate(hou.ToggleParmTemplate(
        "build_tint", "Build Tint", default_value=True,
        script_callback=EROSION_CHANGED_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="Build the Tint color map too (the Export node output_tint_combine, "
             "with the Tint settings saved in the project). Off: that Export and "
             "the nodes feeding only it are left out of the build, so Gaea skips "
             "them. Part of the build cache."))
    # The display state is the hidden show_tint parameter, set only by the button.
    tab.addParmTemplate(_button(
        "toggle_tint", "Show / Hide Tint", TOGGLE_TINT_CALLBACK,
        "Show the built Tint map on the Terrain output as its mask (luminance, "
        "drawn like Show Mask); press again to hide it. Showing re-reads the map, "
        "so it is always the last import's."))
    tab.addParmTemplate(_button(
        "run_cycle_tint", "Export > Build > Import", CYCLE_CALLBACK,
        "Same as on the Bridge tab: build in Gaea (erosion and tint) and import."))
    tab.addParmTemplate(_status_parm("_status_tint"))
    return tab


def _build_parm_template_group(node_kind="Erosion2"):
    g = hou.ParmTemplateGroup()

    # --- Bridge tab ---------------------------------------------------- #
    bridge = hou.FolderParmTemplate("bridge_tab", "Bridge",
                                    folder_type=hou.folderType.Tabs)
    bridge.addParmTemplate(hou.MenuParmTemplate(
        "downsample", "Export Resolution", ("1", "2", "3", "4"),
        menu_labels=("Full", "1/2", "1/3", "1/4"), default_value=0,
        help="Export resolution relative to the input HeightField. The terrain keeps "
             "its size; only the pixel count drops (1/2 = half the voxels per axis)."))
    mask_layer = hou.StringParmTemplate(
        "mask_input_layer", "Mask Input Layer", 1, default_value=("auto",),
        menu_items=("auto", "mask", "height"),
        menu_labels=("Auto", "mask", "height"),
        menu_type=hou.menuType.StringReplace,
        help="Which layer of the second input (Mask) goes to Gaea. Auto: 'mask' if "
             "it has values, else 'height' stretched to 0..1 (a noise HeightField "
             "works as a mask), else the only volume. Plain geometry (no volumes) "
             "becomes a mask with Mask by Object. Without a second input the mask "
             "is white everywhere.")
    bridge.addParmTemplate(mask_layer)
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
        ("0", "512", "1024", "2048", "4096", "8192", "16384"),
        menu_labels=("From Project", "512", "1K", "2K", "4K", "8K", "16K"),
        default_value=0,
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

    g.append(_erosion_tab(node_kind))
    g.append(_tint_tab())

    # --- Layers tab ---------------------------------------------------- #
    layers = hou.FolderParmTemplate("layers_tab", "Gaea Layers",
                                    folder_type=hou.folderType.Tabs)
    layers.addParmTemplate(hou.MenuParmTemplate(
        "height_source", "Height", ("gaea", "input"),
        menu_labels=("Gaea Result", "Input HeightField"), default_value=0,
        help="Gaea Result: the eroded terrain from Gaea. Input HeightField: keep the "
             "original height and only add the Gaea masks to it."))
    layers.addParmTemplate(hou.ToggleParmTemplate(
        "keep_input_res", "Keep Input Resolution", default_value=True,
        help="Resample every output onto the input HeightField's grid (HeightField "
             "Layer, Replace), so it keeps the input's voxel size and resolution. "
             "Off: outputs keep the resolution Gaea built at."))
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
        "bridge_dir", "Bridge Dir", 1, default_value=("C:/Temp/gh_bridge/$OS",),
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Directory,
        help="Working folder exchanged with Gaea. $OS = this node's name, so every "
             "bridge node gets its own folder and they never overwrite each other. "
             "The build copy points the project's File and Export nodes here."))
    shared = dict(script_callback=SHARED_PATHS_CALLBACK,
                  script_callback_language=hou.scriptLanguage.Python)
    shared_note = (" Shared: set it on one bridge node and every node whose field "
                   "is empty uses it.")
    paths.addParmTemplate(hou.StringParmTemplate(
        "gaea_projects_dir", "Gaea Projects Dir", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Directory,
        help="Folder with your Gaea projects (subfolders included). With Gaea Project "
             "empty, this node finds its project there by itself: {0}.terrain first, "
             "else any project with a {1} node.{2}".format(
                 node_kind.lower(), node_kind, shared_note), **shared))
    paths.addParmTemplate(hou.StringParmTemplate(
        "gaea_project", "Gaea Project (.terrain)", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference,
        help="Empty = found automatically in Gaea Projects Dir (see there). Set it "
             "to use a specific project."))
    paths.addParmTemplate(hou.StringParmTemplate(
        "gaea_exe", "Gaea Exe", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference,
        help="Gaea.exe (Gaea.Swarm.exe is expected next to it)." + shared_note,
        **shared))
    paths.addParmTemplate(hou.StringParmTemplate(
        "gaea_swarm", "Gaea Swarm Exe", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference,
        help="Gaea's command-line builder. Empty = Gaea.Swarm.exe next to Gaea Exe."
             + shared_note, **shared))
    paths.addParmTemplate(_button(
        "check_paths", "Check Paths", CHECK_PATHS_CALLBACK,
        "Show the paths this node will use (shared ones and the auto-found "
        "project), and which are missing."))
    paths.addParmTemplate(_status_parm("_status_paths"))
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
    g.append(_hidden(hou.StringParmTemplate("_tint_file", "Tint map", 1,
                                            default_value=("",))))
    # Tint display state, set only by Show / Hide Tint.
    g.append(_hidden(hou.ToggleParmTemplate("show_tint", "Tint shown",
                                            default_value=False)))
    g.append(_hidden(hou.ToggleParmTemplate("_ero_loaded", "Erosion values loaded",
                                            default_value=False)))
    # Which Gaea erosion node this HDA drives (fixed per asset).
    g.append(_hidden(hou.StringParmTemplate("erosion_kind", "Erosion node kind", 1,
                                            default_value=(node_kind,))))
    for layer in ("height",) + MASK_LAYERS:
        g.append(_hidden(hou.StringParmTemplate(
            "_{}_file".format(layer), layer + " file", 1, default_value=("",))))
    for layer in MASK_LAYERS:
        g.append(_hidden(hou.FloatParmTemplate(
            "_{}_peak".format(layer), layer + " peak", 1, default_value=(0.0,))))
    for name in ("_enc_span", "_enc_offset", "_houdini_width"):
        g.append(_hidden(hou.FloatParmTemplate(name, name, 1, default_value=(0.0,))))
    g.append(_hidden(hou.ToggleParmTemplate("_locked", "Locked during auto build",
                                            default_value=False)))

    return hou.ParmTemplateGroup([_with_lock(t) for t in g.entries()])


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


def build(install_dir=None, node_kind="Erosion2"):
    """Create, define and install the HDA for one Gaea erosion node kind
    ("Erosion2" or "Erosion"). Returns the path to the .hda file."""
    asset_info = ASSETS[node_kind]
    type_name, label = asset_info["type_name"], asset_info["label"]
    del OPTIONAL_PARMS_MISSING[:]
    obj = hou.node("/obj")
    container = obj.createNode("geo", "GHBRIDGE_TEMP_BUILD")
    try:
        subnet = container.createNode("subnet", type_name)
        # Give the subnet the interface first so the "../parm" expressions inside
        # resolve while the network is built.
        subnet.setParmTemplateGroup(_build_parm_template_group(node_kind))
        _build_network(subnet)

        if install_dir is None:
            install_dir = os.path.join(
                hou.text.expandString("$HOUDINI_USER_PREF_DIR"), "otls")
        os.makedirs(install_dir, exist_ok=True)
        hda_path = os.path.join(install_dir, type_name + ".hda")

        asset = subnet.createDigitalAsset(
            name=type_name,
            hda_file_name=hda_path,
            description=label,
            min_num_inputs=1,
            max_num_inputs=2,  # 1: terrain, 2: optional mask sent to Gaea
            ignore_external_references=True,
        )

        definition = asset.type().definition()
        definition.setParmTemplateGroup(_build_parm_template_group(node_kind))
        definition.setMaxNumOutputs(1 + len(MASK_LAYERS))
        _set_io_labels(definition, inputs=("HeightField", "Mask (optional)"),
                       outputs=("Terrain",) + tuple(l.capitalize() for l in MASK_LAYERS))
        scripts = [("OnLoaded", ON_LOADED_SCRIPT), ("OnDeleted", ON_DELETED_SCRIPT)]
        if asset_info["node_name"]:
            scripts.insert(0, ("OnCreated", ON_CREATED_SCRIPT))
        for section, script in scripts:
            definition.addSection(section, script)
            definition.setExtraFileOption(section + "/IsPython", True)

        definition.setComment(
            "GH-Bridge: file-based Gaea<->Houdini bridge (manual GUI build). "
            "Requires houdini_gaea_bridge.py on the Python path.")
        definition.save(hda_path, asset)

        hou.hda.installFile(hda_path)
        print("Installed HDA: {} ({}, Houdini {})".format(
            hda_path, label, hou.applicationVersionString()))
        for message in OPTIONAL_PARMS_MISSING:
            print("  note: {} (optional, skipped)".format(message))
        return hda_path
    finally:
        container.destroy()


def build_all(install_dir=None):
    """Install both bridges: Gaea Erosion2 Bridge and Gaea Erosion Bridge."""
    return [build(install_dir, node_kind) for node_kind in ASSETS]


if __name__ == "__main__":
    build_all()
