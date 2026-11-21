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
                 "node_name": "gaea_erosion2_bridge", "profile": "erosion"},
    "Erosion": {"type_name": "gaea_erosion1_bridge", "label": "Gaea Erosion Bridge",
                "node_name": None,  # Houdini already names them gaea_erosion1_bridge1
                "profile": "erosion"},
    # Color profile: the project writes one RGB map (output_supercolor), shown as
    # a texture on a polygon mesh and as color_r/g/b layers; the height is the
    # input's.
    "SuperColor": {"type_name": "gaea_supercolor_bridge",
                   "label": "Gaea SuperColor Bridge", "node_name": None,
                   "profile": "color"},
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

CYCLE_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.run_full_cycle(kwargs['node'])
""".strip()

CHAIN_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.build_chain(kwargs['node'])
""".strip()

CLEAR_CACHE_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.clear_cache(kwargs['node'])
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

SUPERCOLOR_CHANGED_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.on_supercolor_parm_changed(kwargs['node'])
""".strip()

PALETTE_PREVIEW_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.update_palette_preview(kwargs['node'])
""".strip()

CHOOSE_PALETTE_CALLBACK = r"""
import houdini_gaea_bridge as ghb
ghb.choose_palette(kwargs['node'])
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


def _file_node(parent, name, source, layer_type, layer, file_parm=None, channel=None):
    """HeightField File reading the hidden ``_<file_parm>_file`` parameter
    (default: the layer's) into ``layer``; ``channel``: which image channel a
    mask takes (heightfield_file "monoop": red / green / blue / lum)."""
    node = parent.createNode("heightfield_file", name)
    if source is not None:
        node.setInput(0, source)
    _setp(node, ("filename", "file"), 'chs("../_{}_file")'.format(file_parm or layer),
          expression=True)
    _setp(node, "type", layer_type)
    if channel:
        _setp(node, "monoop", channel, required=False)
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
            # Show / Hide Tint: the Terrain output becomes the same colored mesh
            # as the SuperColor bridge's output 1 (polygons + the Tint map as a
            # texture or as Cd); hidden, it stays the plain HeightField.
            tint_layers = result
            for channel, layer in COLOR_CHANNEL_LAYERS:
                tint_layers = _file_node(subnet, "file_tint_" + layer[-1], tint_layers,
                                         "mask", layer, file_parm="tint",
                                         channel=channel)
            tint_mesh = _colored_mesh(subnet, result, tint_layers, "tint", "tint_")
            result = _switch(
                subnet, "tint_display_switch", (result, tint_mesh),
                'ch("../_imported") && ch("../show_tint") && '
                'strlen(chs("../_tint_file")) > 0')
        out = subnet.createNode("output", name)
        out.setInput(0, result)
        _setp(out, "outputidx", index)
        if index == 0:
            out.setDisplayFlag(True)
            out.setRenderFlag(True)
    subnet.layoutChildren()


# Color profile: the imported map's channels as HeightField layers (output 2).
COLOR_CHANNEL_LAYERS = (("red", "color_r"), ("green", "color_g"), ("blue", "color_b"))
COLOR_FILE_PARM = ghb.COLOR_LAYERS[0]  # "supercolor": hidden _supercolor_file
COLOR_IMPORTED_EXPR = 'ch("../_imported") && strlen(chs("../_{}_file")) > 0'.format(
    COLOR_FILE_PARM)


def _mesh_density_expr(res_info):
    """Polygons per axis = HeightField voxels per axis * Density; Mesh Resolution
    caps the longer axis, so a 4K terrain doesn't become 16M polygons."""
    return ('min(1, ch("../mesh_res") / max(1, max(detail("../{0}", "resx", 0), '
            'detail("../{0}", "resy", 0))))'.format(res_info))  # HScript max(): 2 args


def _colored_mesh(subnet, source, layered, file_parm, prefix=""):
    """HeightField ``source`` as polygons carrying the color map of the hidden
    ``_<file_parm>_file`` parameter: as a Principled Shader texture (Color Mode
    Texture) or as Cd sampled from ``layered``'s color_r/g/b layers (Point
    Color). Convert HeightField writes uv matching the map written by the
    export, so the texture lands where Gaea computed it. Returns the mesh node;
    ``prefix`` keeps the node names apart when a network has several meshes."""
    res_info = subnet.createNode("attribwrangle", prefix + "res_info")
    res_info.setInput(0, source)
    _setp(res_info, "class", "detail")
    _setp(res_info, "snippet",
          'vector r = volumeres(0, "height");\ni@resx = (int)r.x;\ni@resy = (int)r.y;\n')
    convert = subnet.createNode("convertheightfield", prefix + "to_polygons")
    convert.setInput(0, source)
    _setp(convert, "conversion", "poly")
    _setp(convert, "lod", _mesh_density_expr(res_info.name()), expression=True)

    # The material lives inside the asset: a Principled Shader whose base color
    # is the imported map (a per-build file name, see display_path).
    matnet = subnet.createNode("matnet", prefix + "shader")
    shader = matnet.createNode("principledshader::2.0", file_parm)
    _setp(shader, "basecolor_useTexture", 1)
    _setp(shader, "basecolor_texture", 'chs("../../_{}_file")'.format(file_parm),
          expression=True)
    _setp(shader, "rough", 1.0, required=False)
    _setp(shader, "metallic", 0.0, required=False)
    _setp(shader, "reflect", 0.0, required=False)
    assign = subnet.createNode("material", prefix + "assign_color")
    assign.setInput(0, convert)
    _setp(assign, "shop_materialpath1", "../{}/{}".format(matnet.name(), file_parm))

    # Color Mode "Point Color": Cd sampled from the layers (no material), for
    # viewports/pipelines without textures. HeightField volumes are one voxel
    # thick, so sample at the volumes' own height.
    bake = subnet.createNode("attribwrangle", prefix + "bake_cd")
    bake.setInput(0, convert)
    bake.setInput(1, layered)
    _setp(bake, "class", "point")
    _setp(bake, "snippet", "\n".join([
        "vector p = @P;",
        "p.y = getbbox_center(1).y;",
        'v@Cd = set(volumesample(1, "color_r", p), volumesample(1, "color_g", p),',
        '           volumesample(1, "color_b", p));', ""]))
    return _switch(subnet, prefix + "color_mode_switch", (assign, bake),
                   'strcmp(chs("../color_mode"), "cd") == 0')


def _build_color_network(subnet):
    source = subnet.indirectInputs()[0]

    # Output 2 - the input HeightField plus color_r / color_g / color_b layers.
    prev = source
    for channel, layer in COLOR_CHANNEL_LAYERS:
        prev = _file_node(subnet, "file_" + layer, prev, "mask", layer,
                          file_parm=COLOR_FILE_PARM, channel=channel)
    layered = _switch(subnet, "layers_imported_switch", (source, prev),
                      COLOR_IMPORTED_EXPR)
    out_hf = subnet.createNode("output", "HEIGHTFIELD")
    out_hf.setInput(0, layered)
    _setp(out_hf, "outputidx", 1)

    # Output 1 - the input HeightField as polygons with the color map; before
    # the import, the bare polygons.
    colored = _colored_mesh(subnet, source, prev, COLOR_FILE_PARM)
    shown = _switch(subnet, "mesh_imported_switch",
                    (subnet.node("to_polygons"), colored), COLOR_IMPORTED_EXPR)
    out_mesh = subnet.createNode("output", "MESH")
    out_mesh.setInput(0, shown)
    _setp(out_mesh, "outputidx", 0)
    out_mesh.setDisplayFlag(True)
    out_mesh.setRenderFlag(True)
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
    # Every value triggers Auto Build on Change (if it's on); SuperColor's also
    # refresh the palette preview.
    callback = dict(script_callback=(SUPERCOLOR_CHANGED_CALLBACK if node_kind == "SuperColor"
                                     else EROSION_CHANGED_CALLBACK),
                    script_callback_language=hou.scriptLanguage.Python)
    if kind == "float2":
        return hou.FloatParmTemplate(
            parm_name, label, 2, default_value=tuple(float(v) for v in default),
            min=low, max=high, min_is_strict=True, max_is_strict=True,
            naming_scheme=hou.parmNamingScheme.MinMax, help=help_text, **callback)
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


# While a build started by Auto Build on Change or Build Chain runs, every
# parameter is disabled through this condition, except the Cancel buttons.
LOCK_CONDITION = "{ _locked == 1 }"
# Each tab with actions has the same row: Build | Build Chain | Cancel + Status.
# Suffixes keep the parameter names of earlier versions (run_cycle_erosion, ...).
ACTION_SUFFIXES = ("", "_erosion", "_tint", "_color")
NEVER_LOCKED = tuple("cancel_build" + suffix for suffix in ACTION_SUFFIXES)


def _action_row(suffix=""):
    """Build | Build Chain | Cancel on one line, with the Status field below."""
    build = _button(
        "run_cycle" + suffix, "Build", CYCLE_CALLBACK,
        "Export > Build > Import: export the input, build the Gaea project with "
        "Gaea.Swarm.exe in the background (no Gaea window), import the result. "
        "Unchanged input and settings: the last result is imported at once.")
    chain = _button(
        "build_chain" + suffix, "Build Chain", CHAIN_CALLBACK,
        "Build every Gaea bridge node above this one, in order (each exports the "
        "result of the ones before it), then this one. Unchanged nodes come "
        "straight from their cache. The nodes are locked until the chain ends; "
        "Cancel on any of them stops it.")
    cancel = _button("cancel_build" + suffix, "Cancel", CANCEL_CALLBACK,
                     "Stop the running build (or Build Chain) and unlock the node.")
    build.setJoinWithNext(True)
    chain.setJoinWithNext(True)
    return [build, chain, cancel, _status_parm("_status" + suffix)]


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
    """Parameters of the project's Gaea node (``node_kind``: "Erosion2",
    "Erosion" or "SuperColor"), shown in template mode. The parameter names stay
    the erosion ones (use_template, ero_*) for every kind."""
    is_erosion = ASSETS[node_kind]["profile"] == "erosion"
    tab = hou.FolderParmTemplate("erosion_tab", "Erosion" if is_erosion else node_kind,
                                 folder_type=hou.folderType.Tabs)
    # Same actions and status as on the Bridge tab, so the loop "tweak -> rebuild"
    # never leaves this tab.
    for template in _action_row("_erosion"):
        tab.addParmTemplate(template)
    tab.addParmTemplate(hou.ToggleParmTemplate(
        "auto_rebuild", "Auto Build on Change", default_value=False,
        help="Run Build automatically whenever a {} value changes (once you stop "
             "changing it). While it runs, the whole node is locked until the result "
             "is imported; Cancel unlocks it. Needs Use {} Template.".format(
                 node_kind, "Erosion" if is_erosion else node_kind)))
    tab.addParmTemplate(hou.ToggleParmTemplate(
        "use_template", "Use {} Template".format("Erosion" if is_erosion else node_kind),
        default_value=False, script_callback=TEMPLATE_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="The Gaea Project is a template whose {0} node feeds the Export "
             "nodes. Build in Gaea then builds a copy of it with the values below; "
             "the original project is not changed. Builds in the Gaea GUI (F8) "
             "don't see these values.".format(node_kind)))
    hidden = "{ use_template == 0 }"

    node_name = hou.StringParmTemplate(
        "erosion_node", "{} Node".format("Erosion" if is_erosion else node_kind), 1,
        default_value=("",),
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
        if node_kind == "SuperColor" and name == "LibraryItem":
            folder.addParmTemplate(_button(
                "choose_palette", "Choose Palette...", CHOOSE_PALETTE_CALLBACK,
                "Open a window with every palette of the Library as a thumbnail; "
                "a click sets Library + Palette here (Auto Build on Change applies)."))
            # The palette behind Library + Palette, read from Gaea's .clut files.
            preview = hou.RampParmTemplate(
                ghb.PALETTE_PREVIEW_PARM, "Palette Preview", hou.rampParmType.Color,
                default_value=2, color_type=hou.colorType.RGB,
                help="The chosen Gaea palette (Library + Palette, Reverse), read "
                     "from Gaea's Resources/Color/<Library>.clut next to Gaea Exe. "
                     "Preview only: edits here change nothing; Gaea applies Range "
                     "and Bias on top.")
            preview.setConditional(hou.parmCondType.DisableWhen, "{ 1 == 1 }")
            folder.addParmTemplate(preview)
            info = hou.StringParmTemplate(ghb.PALETTE_INFO_PARM, "Palette Info", 1,
                                          default_value=("",))
            info.setConditional(hou.parmCondType.DisableWhen, "{ 1 == 1 }")
            folder.addParmTemplate(info)
            folder.addParmTemplate(_button(
                "refresh_palette", "Refresh Palette Preview", PALETTE_PREVIEW_CALLBACK,
                "Re-read the palette (after setting Gaea Exe)."))
    for folder in folders.values():
        tab.addParmTemplate(folder)
    return tab


def _tint_tab():
    """Build Tint toggle and the tint display button. The Tint settings
    themselves are the Gaea project's own."""
    tab = hou.FolderParmTemplate("tint_tab", "Tint", folder_type=hou.folderType.Tabs)
    for template in _action_row("_tint"):
        tab.addParmTemplate(template)
    tab.addParmTemplate(hou.ToggleParmTemplate(
        "build_tint", "Build Tint", default_value=False,
        script_callback=EROSION_CHANGED_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="Build the Tint color map too (the Export node output_tint_combine, "
             "with the Tint settings saved in the project). Off: that Export and "
             "the nodes feeding only it are left out of the build, so Gaea skips "
             "them. Part of the build cache."))
    # The display state is the hidden show_tint parameter, set only by the button.
    tab.addParmTemplate(_button(
        "toggle_tint", "Show / Hide Tint", TOGGLE_TINT_CALLBACK,
        "Show the built Tint map on the Terrain output: the HeightField becomes "
        "polygons with the map as a texture or as point colors (Color Mode, Mesh "
        "Resolution below); press again to get the plain HeightField back. Showing "
        "re-reads the map, so it is always the last import's."))
    for template in _mesh_display_parms("the Terrain output while the Tint is shown"):
        template.setConditional(hou.parmCondType.DisableWhen, "{ show_tint == 0 }")
        tab.addParmTemplate(template)
    return tab


def _mesh_display_parms(what="output 1"):
    """Color Mode and Mesh Resolution of a colored mesh (see _colored_mesh)."""
    return [
        hou.MenuParmTemplate(
            "color_mode", "Color Mode", ("texture", "cd"),
            menu_labels=("Texture (material)", "Point Color (Cd)"), default_value=0,
            help="How {} carries the color map. Texture: a Principled Shader "
                 "inside the node with the map as base color (sharp at any mesh "
                 "resolution; needs the viewport's materials/textures on). Point "
                 "Color: Cd sampled per point from the map (no material; detail "
                 "limited by Mesh Resolution; also what most exporters and other "
                 "DCCs pick up).".format(what)),
        hou.IntParmTemplate(
            "mesh_res", "Mesh Resolution", 1, default_value=(1024,), min=32, max=8192,
            min_is_strict=True,
            help="Polygons along the longer side of {} (the colored mesh). The "
                 "HeightField's own voxel count is the ceiling: 1024 on a 4K terrain "
                 "keeps every 4th voxel (~1M quads); 4096 = full detail (~16M quads, "
                 "slow).".format(what))]


def _color_tab():
    """Color profile: the mesh density and the import status."""
    tab = hou.FolderParmTemplate("color_tab", "Color", folder_type=hou.folderType.Tabs)
    for template in _action_row("_color"):
        tab.addParmTemplate(template)
    for template in _mesh_display_parms("output 1"):
        tab.addParmTemplate(template)
    return tab


def _build_parm_template_group(node_kind="Erosion2"):
    profile = ASSETS[node_kind]["profile"]
    g = hou.ParmTemplateGroup()

    # --- Bridge tab ---------------------------------------------------- #
    bridge = hou.FolderParmTemplate("bridge_tab", "Bridge",
                                    folder_type=hou.folderType.Tabs)
    # Main actions first: what is pressed all the time.
    for template in _action_row(""):
        bridge.addParmTemplate(template)
    bridge.addParmTemplate(hou.SeparatorParmTemplate("bridge_sep1"))

    # Resolution: what decides speed vs. detail.
    bridge.addParmTemplate(hou.MenuParmTemplate(
        "downsample", "Export Resolution", ("1", "2", "3", "4"),
        menu_labels=("Full", "1/2", "1/3", "1/4"), default_value=0,
        help="Export resolution relative to the input HeightField. The terrain keeps "
             "its size; only the pixel count drops (1/2 = half the voxels per axis)."))
    bridge.addParmTemplate(hou.MenuParmTemplate(
        "build_resolution", "Build Resolution",
        ("0", "512", "1024", "2048", "4096", "8192", "16384"),
        menu_labels=("From Project", "512", "1K", "2K", "4K", "8K", "16K"),
        default_value=0,
        help="Resolution Gaea builds at (Swarm --resolution). From Project = the "
             "project's Terrain Definition. Lower = faster previews."))
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

    # Building in the Gaea window instead of Swarm: rarely needed, folded away.
    gui = hou.FolderParmTemplate("gui_workflow", "Gaea GUI Workflow",
                                 folder_type=hou.folderType.Collapsible)
    export = _button("export", "Export", EXPORT_CALLBACK,
                     "Write the input (and mask) for Gaea without building.")
    open_gaea = _button("open_gaea", "Open in Gaea", OPEN_CALLBACK,
                        "Open the Gaea Project in the Gaea window; build there with "
                        "F8.")
    do_import = _button("do_import", "Import", IMPORT_CALLBACK,
                        "Load the maps Gaea last wrote to the output folder.")
    export.setJoinWithNext(True)
    open_gaea.setJoinWithNext(True)
    for template in (export, open_gaea, do_import):
        gui.addParmTemplate(template)
    gui.addParmTemplate(hou.ToggleParmTemplate(
        "auto_import", "Auto Import (Gaea GUI builds)", default_value=False,
        script_callback=AUTO_IMPORT_CALLBACK,
        script_callback_language=hou.scriptLanguage.Python,
        help="Watch the output folder and import automatically when a build made in "
             "the Gaea GUI (F8) writes new files (checked every second; imports once "
             "the files stop changing). Stays on when the scene is reopened."))
    bridge.addParmTemplate(gui)

    advanced = hou.FolderParmTemplate("build_options", "Advanced",
                                      folder_type=hou.folderType.Collapsible)
    advanced.addParmTemplate(hou.ToggleParmTemplate(
        "ignore_cache", "Ignore Gaea Baked Nodes", default_value=False,
        help="Swarm --ignorecache: also rebuild nodes baked inside the Gaea project."))
    advanced.addParmTemplate(hou.ToggleParmTemplate(
        "show_console", "Show Build Window", default_value=False,
        help="Show Swarm's console window during the build (for diagnostics)."))
    bridge.addParmTemplate(advanced)

    bridge.addParmTemplate(hou.SeparatorParmTemplate("bridge_sep2"))
    clear_import = _button(
        "clear_import", "Clear Import", CLEAR_CALLBACK,
        "Forget the imported result: the node passes its input through. Files stay "
        "on disk, so the next Build may come straight from the cache.")
    clear_import.setJoinWithNext(True)
    bridge.addParmTemplate(clear_import)
    bridge.addParmTemplate(_button(
        "clear_cache", "Clear Cache", CLEAR_CACHE_CALLBACK,
        "Delete every file this node wrote to its Bridge Dir (exported input and "
        "mask, build copy of the project, Gaea's outputs, display copies) and clear "
        "the import. Frees disk space; the next Build starts from scratch. Asks "
        "first."))
    g.append(bridge)

    g.append(_erosion_tab(node_kind))
    if profile == "color":
        g.append(_color_tab())
    else:
        g.append(_tint_tab())
        g.append(_layers_tab())

    g.append(_paths_tab(node_kind))

    # --- Hidden state written by Import ------------------------------- #
    g.append(_hidden(hou.ToggleParmTemplate("_imported", "Imported",
                                            default_value=False)))
    g.append(_hidden(hou.StringParmTemplate("_import_stamp", "Import stamp", 1,
                                            default_value=("",))))
    g.append(_hidden(hou.ToggleParmTemplate("_ero_loaded", "Template values loaded",
                                            default_value=False)))
    # Which Gaea node this HDA drives (fixed per asset).
    g.append(_hidden(hou.StringParmTemplate("erosion_kind", "Gaea node kind", 1,
                                            default_value=(node_kind,))))
    g.append(_hidden(hou.FloatParmTemplate("_houdini_width", "_houdini_width", 1,
                                           default_value=(0.0,))))
    g.append(_hidden(hou.ToggleParmTemplate("_locked", "Locked during auto build",
                                            default_value=False)))
    if profile == "color":
        for layer in ghb.COLOR_LAYERS:
            g.append(_hidden(hou.StringParmTemplate(
                "_{}_file".format(layer), layer + " file", 1, default_value=("",))))
    else:
        g.append(_hidden(hou.StringParmTemplate("_tint_file", "Tint map", 1,
                                                default_value=("",))))
        # Tint display state, set only by Show / Hide Tint.
        g.append(_hidden(hou.ToggleParmTemplate("show_tint", "Tint shown",
                                                default_value=False)))
        for layer in ("height",) + MASK_LAYERS:
            g.append(_hidden(hou.StringParmTemplate(
                "_{}_file".format(layer), layer + " file", 1, default_value=("",))))
        for layer in MASK_LAYERS:
            g.append(_hidden(hou.FloatParmTemplate(
                "_{}_peak".format(layer), layer + " peak", 1, default_value=(0.0,))))
        for name in ("_enc_span", "_enc_offset"):
            g.append(_hidden(hou.FloatParmTemplate(name, name, 1, default_value=(0.0,))))

    return hou.ParmTemplateGroup([_with_lock(t) for t in g.entries()])


def _layers_tab():
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
    return layers


def _paths_tab(node_kind):
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
        "gaea_project", "Gaea Project (.terrain)", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference,
        help="Empty = found automatically: {0}.terrain, else any project with a {1} "
             "node, in the scene folder ($HIP) or next to houdini_gaea_bridge.py "
             "(subfolders included). Set it to use a specific project.".format(
                 node_kind.lower(), node_kind)))
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
    # Read from the project; only for projects whose size can't be read.
    world = hou.FolderParmTemplate("world_override", "World Size Override",
                                   folder_type=hou.folderType.Collapsible)
    world.addParmTemplate(hou.FloatParmTemplate(
        "gaea_width", "Gaea Width (m)", 1, default_value=(0.0,), min=0.0, max=20000.0,
        help="Gaea Terrain Definition > Scale. 0 = read from the .terrain project."))
    world.addParmTemplate(hou.FloatParmTemplate(
        "gaea_height", "Gaea Height (m)", 1, default_value=(0.0,), min=0.0,
        max=10000.0,
        help="Gaea Terrain Definition > Height. 0 = read from the .terrain project."))
    paths.addParmTemplate(world)
    return paths


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
    color = asset_info["profile"] == "color"
    del OPTIONAL_PARMS_MISSING[:]
    obj = hou.node("/obj")
    container = obj.createNode("geo", "GHBRIDGE_TEMP_BUILD")
    try:
        subnet = container.createNode("subnet", type_name)
        # Give the subnet the interface first so the "../parm" expressions inside
        # resolve while the network is built.
        subnet.setParmTemplateGroup(_build_parm_template_group(node_kind))
        if color:
            _build_color_network(subnet)
        else:
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
        if color:
            inputs = ("HeightField", "Texture mask (optional)")
            outputs = ("Colored Mesh", "HeightField + color_r/g/b")
        else:
            inputs = ("HeightField", "Mask (optional)")
            outputs = ("Terrain",) + tuple(l.capitalize() for l in MASK_LAYERS)
        definition.setMaxNumOutputs(len(outputs))
        _set_io_labels(definition, inputs=inputs, outputs=outputs)
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
    """Install every bridge: Gaea Erosion2 Bridge, Gaea Erosion Bridge and Gaea
    SuperColor Bridge."""
    return [build(install_dir, node_kind) for node_kind in ASSETS]


if __name__ == "__main__":
    build_all()
