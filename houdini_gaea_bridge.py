"""GH-Bridge: file-based procedural bridge between Houdini and QuadSpinner Gaea 2.0.

This module automates the *Houdini side* of a manual file bridge:

    1. Export a HeightField SOP to an input EXR (+ a manifest describing the job).
    2. You build/update the erosion graph in the Gaea GUI and press Build (F8).
    3. Import the resulting EXR maps back into Houdini as named HeightField layers.

It talks to Gaea only through plain files on disk and (optionally) by launching the
installed Gaea GUI with a project file. It does not touch licensing, SwarmHost, the
CLI build engine, or any protection mechanism, so it is stable on any licensed Gaea.

Design notes
------------
* Everything that needs Houdini imports ``hou`` lazily, so the pure-logic helpers
  (config, hashing, manifest, cache, cleanup) can be unit-tested without Houdini.
* Node type / parameter names are resolved from candidate lists, so the module keeps
  working across SideFX Labs and native HeightField node naming.

Quick use from a Houdini shelf tool::

    import houdini_gaea_bridge as ghb
    ghb.run_from_selection()        # step 1: export
    # ... build in Gaea GUI, press F8 ...
    ghb.import_from_selection()     # step 3: import
"""

import json
import os
import hashlib
import subprocess
from dataclasses import dataclass, field, asdict


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

# Working directory for the on-disk exchange. Override per-machine via
# gh_bridge_config.json next to this file, or the Bridge Dir parameter on the HDA.
DEFAULT_BRIDGE_DIR = r"C:/Temp/gh_bridge"
DEFAULT_INPUT_NAME = "input_height.exr"
DEFAULT_OUTPUT_SUBDIR = "output"
MANIFEST_NAME = "gh_bridge_manifest.json"
CONFIG_NAME = "gh_bridge_config.json"

# Houdini layer name -> exact filename configured on the matching Gaea Export node.
# Missing files are skipped on import with a warning (not an error), so you can keep
# this list broad and only wire up the Export nodes you actually need in Gaea.
DEFAULT_OUTPUT_MAPS = {
    "height": "output_height.exr",
    "erosion": "output_erosion.exr",
    "flow": "output_flow.exr",
    "wear": "output_wear.exr",
    "sediment": "output_sediment.exr",
}

# Gaea terrain definition (Project > Terrain Definition). Gaea maps a 0..1 height
# value to 0..Height metres over a Width-metre wide terrain. Used when the size can't
# be read from the .terrain file and isn't set explicitly.
GAEA_DEFAULT_WIDTH = 5000.0
GAEA_DEFAULT_HEIGHT = 2500.0

# Node-type and parameter candidates (native + SideFX Labs variants).
HEIGHTFIELD_OUTPUT_NODE_TYPES = ("heightfield_output", "labs::heightfield_output")
HEIGHTFIELD_FILE_NODE_TYPES = ("heightfield_file", "labs::heightfield_file")
OUTPUT_FILE_PARM_CANDIDATES = ("output", "output_file", "filename", "file")
OUTPUT_EXECUTE_PARM_CANDIDATES = ("execute", "savetodisk", "render")
FILE_PARM_CANDIDATES = ("file", "filename")


# --------------------------------------------------------------------------- #
# Configuration (pure logic, no hou dependency)
# --------------------------------------------------------------------------- #

@dataclass
class BridgeConfig:
    """All paths and options for one bridge job."""

    bridge_dir: str = DEFAULT_BRIDGE_DIR
    input_name: str = DEFAULT_INPUT_NAME
    output_subdir: str = DEFAULT_OUTPUT_SUBDIR
    output_maps: dict = field(default_factory=lambda: dict(DEFAULT_OUTPUT_MAPS))
    # Export resolution relative to the source HeightField: 1 = full, 2 = 1/2,
    # 3 = 1/3, 4 = 1/4 of its voxel resolution (the world size stays the same).
    downsample: int = 1
    # Path to the Gaea project (.terrain in Gaea 2.0). Used only to open the GUI.
    gaea_project: str = ""
    # Path to the installed Gaea executable, for the optional "open in GUI" action.
    gaea_exe: str = ""
    # Gaea world size in metres. 0 = read it from gaea_project, else the defaults.
    gaea_width: float = 0.0
    gaea_height: float = 0.0

    # -- derived paths ----------------------------------------------------- #

    @property
    def input_path(self):
        return os.path.join(self.bridge_dir, self.input_name)

    @property
    def output_dir(self):
        return os.path.join(self.bridge_dir, self.output_subdir)

    @property
    def manifest_path(self):
        return os.path.join(self.bridge_dir, MANIFEST_NAME)

    def output_path(self, layer_name):
        return os.path.join(self.output_dir, self.output_maps[layer_name])

    # -- serialization ----------------------------------------------------- #

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {f for f in cls.__dataclass_fields__}  # noqa: N806
        clean = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**clean)

    @classmethod
    def load(cls, path=None, overrides=None):
        """Build a config from defaults, an optional JSON file, and overrides.

        Precedence (low -> high): dataclass defaults -> JSON file -> ``overrides``.
        The JSON file defaults to ``gh_bridge_config.json`` next to this module.
        """
        data = {}
        if path is None:
            here = os.path.dirname(os.path.abspath(__file__))
            candidate = os.path.join(here, CONFIG_NAME)
            path = candidate if os.path.isfile(candidate) else None
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                data.update(json.load(handle) or {})
        if overrides:
            data.update({k: v for k, v in overrides.items() if v is not None})
        return cls.from_dict(data)


# --------------------------------------------------------------------------- #
# Hashing, manifest & cache (pure logic, no hou dependency)
# --------------------------------------------------------------------------- #

def _normalise_dict(data):
    """Stable JSON text for a dict, so the signature is order-independent."""
    return json.dumps(data or {}, sort_keys=True, ensure_ascii=False)


def compute_file_hash(path, chunk_size=1 << 20):
    """SHA-1 of a file's bytes, or ``None`` if the file is missing."""
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_signature(input_path, downsample, output_maps):
    """One hash over the input EXR plus the settings that affect a rebuild.

    If any of these change, the Gaea result is stale and should be rebuilt.
    """
    digest = hashlib.sha1()
    file_hash = compute_file_hash(input_path)
    digest.update((file_hash or "no-input").encode("utf-8"))
    digest.update(str(downsample).encode("utf-8"))
    digest.update(_normalise_dict(dict(output_maps or {})).encode("utf-8"))
    return digest.hexdigest()


def read_manifest(config):
    """Return the stored manifest dict, or ``None`` if there is none/invalid."""
    path = config.manifest_path
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, OSError):
        return None


def write_manifest(config, signature, extra=None):
    """Persist the job manifest next to the input EXR.

    ``extra`` adds informational keys (e.g. the source ``height_range``); they are
    not part of the signature.
    """
    import time

    os.makedirs(config.bridge_dir, exist_ok=True)
    manifest = {
        "signature": signature,
        "downsample": config.downsample,
        "input": config.input_name,
        "output_dir": config.output_subdir,
        "output_maps": dict(config.output_maps),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    manifest.update(extra or {})
    with open(config.manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return manifest


# --------------------------------------------------------------------------- #
# Height scale matching (pure logic, no hou dependency)
# --------------------------------------------------------------------------- #

def _find_terrain_definition(data):
    """Depth-first search for the dict holding Gaea's terrain Width/Height."""
    if isinstance(data, dict):
        if {"Width", "Height", "Ratio"} <= set(data):
            return data
        children = data.values()
    elif isinstance(data, list):
        children = data
    else:
        return None
    for child in children:
        found = _find_terrain_definition(child)
        if found is not None:
            return found
    return None


def read_gaea_terrain_size(path):
    """Return ``(width, height)`` in metres from a Gaea 2 ``.terrain`` file, or None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        return None
    definition = _find_terrain_definition(data)
    if definition is None:
        return None
    try:
        width, height = float(definition["Width"]), float(definition["Height"])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def gaea_world_size(config):
    """Resolve the Gaea world size: explicit config -> .terrain file -> defaults.

    Returns ``(width, height, source)`` where source is "config", "project" or
    "default".
    """
    if config.gaea_width > 0 and config.gaea_height > 0:
        return float(config.gaea_width), float(config.gaea_height), "config"
    from_project = read_gaea_terrain_size(config.gaea_project)
    if from_project:
        return from_project[0], from_project[1], "project"
    return GAEA_DEFAULT_WIDTH, GAEA_DEFAULT_HEIGHT, "default"


def height_encoding(low, high, houdini_width, gaea_width, gaea_height):
    """How to store Houdini heights in a 0..1 image so Gaea shows them 1:1.

    Gaea displays value ``v`` as ``v * gaea_height`` metres on a ``gaea_width``-wide
    terrain. To keep the Houdini height/width proportions, one Gaea unit (1.0) must
    equal ``span = houdini_width * gaea_height / gaea_width`` Houdini units:

        v = (h - offset) / span          (export)
        h = v * span + offset            (import)

    ``max_value`` > 1 means the relief is taller than Gaea's Height allows and will
    be clamped unless Gaea's File node has "Allow Unclamped" on.
    """
    if houdini_width <= 0 or gaea_width <= 0 or gaea_height <= 0:
        raise ValueError("Sizes must be positive")
    span = float(houdini_width) * float(gaea_height) / float(gaea_width)
    return {
        "offset": float(low),
        "span": span,
        "max_value": (float(high) - float(low)) / span,
    }


def downsampled_resolution(res_x, res_y, downsample):
    """Export image size for a HeightField of ``res_x`` x ``res_y`` voxels.

    ``downsample`` 1 = full, 2 = 1/2, 3 = 1/3, 4 = 1/4 ... (rounded, at least 1 px).
    """
    try:
        factor = int(downsample)
    except (TypeError, ValueError):
        raise ValueError("downsample must be a positive integer, got {!r}".format(
            downsample))
    if factor < 1:
        raise ValueError("downsample must be >= 1, got {}".format(factor))
    return (max(1, int(round(res_x / float(factor)))),
            max(1, int(round(res_y / float(factor)))))


def downsample_label(downsample):
    return "full" if int(downsample) == 1 else "1/{}".format(int(downsample))


def outputs_present(config):
    """True if every configured output map exists and is non-empty."""
    for layer_name in config.output_maps:
        path = config.output_path(layer_name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
    return True


def is_cache_valid(config):
    """True if the current input + settings match the manifest and outputs exist.

    When this is true a fresh Gaea build is unnecessary: the maps on disk already
    correspond to the current input and settings.
    """
    manifest = read_manifest(config)
    if not manifest:
        return False
    signature = compute_signature(
        config.input_path, config.downsample, config.output_maps
    )
    if manifest.get("signature") != signature:
        return False
    return outputs_present(config)


# --------------------------------------------------------------------------- #
# Temp-file management (pure logic, no hou dependency)
# --------------------------------------------------------------------------- #

def _is_within(child, parent):
    """True if ``child`` resolves to a path inside ``parent`` (or equal to it)."""
    parent_real = os.path.realpath(parent)
    child_real = os.path.realpath(child)
    try:
        return os.path.commonpath([parent_real, child_real]) == parent_real
    except ValueError:
        # Different drives on Windows raise ValueError.
        return False


def cleanup_temp(config, remove_input=True, remove_outputs=False):
    """Delete bridge temp files to save SSD space.

    Only files inside ``config.bridge_dir`` are ever removed. Returns the list of
    deleted paths. ``remove_outputs`` defaults to False so results survive by default.
    """
    removed = []
    targets = []
    if remove_input:
        targets.append(config.input_path)
        targets.append(config.manifest_path)
    if remove_outputs:
        targets.extend(config.output_path(layer) for layer in config.output_maps)

    for path in targets:
        if os.path.isfile(path) and _is_within(path, config.bridge_dir):
            os.remove(path)
            removed.append(path)
    return removed


# --------------------------------------------------------------------------- #
# Houdini helpers (require hou)
# --------------------------------------------------------------------------- #

def _set_first_available_parm(node, candidates, value):
    """Set the first existing parameter from ``candidates`` and return its name."""
    for name in candidates:
        parm = node.parm(name)
        if parm is not None:
            parm.set(value)
            return name
    raise RuntimeError(
        "Node {} has none of the expected parameters: {}".format(
            node.path(), ", ".join(candidates)
        )
    )


def _find_parm_by_label(node, label):
    target_label = label.strip().casefold()
    for parm in node.parms():
        template = parm.parmTemplate()
        if template.label().strip().casefold() == target_label:
            return parm
    return None


def _press_first_available(node, candidates, labels):
    """Press the first matching button parm (by name, then by label)."""
    for name in candidates:
        parm = node.parm(name)
        if parm is not None:
            parm.pressButton()
            return name
    for label in labels:
        parm = _find_parm_by_label(node, label)
        if parm is not None:
            parm.pressButton()
            return parm.name()
    raise RuntimeError(
        "Could not find a save/execute button on {} (tried {} / {})".format(
            node.path(), ", ".join(candidates), ", ".join(labels)
        )
    )


def _create_node_from_candidates(parent, type_names, node_name):
    last_error = None
    for type_name in type_names:
        try:
            return parent.createNode(type_name, node_name=node_name)
        except Exception as error:  # hou.OperationFailed and friends
            last_error = error
    raise RuntimeError(
        "Could not create any of {} under {}: {}".format(
            ", ".join(type_names), parent.path(), last_error
        )
    )


def heightfield_info(source_node):
    """Measure the source's ``height`` layer.

    Returns ``{"min", "max", "width", "resolution"}``: height range, the size of the
    largest horizontal axis in Houdini units, and the voxel resolution ``(x, y)``.
    """
    geometry = source_node.geometry()
    if geometry is None:
        raise RuntimeError("{} has no geometry".format(source_node.path()))
    has_name = geometry.findPrimAttrib("name") is not None
    for prim in geometry.prims():
        if has_name and prim.attribValue("name") == "height":
            res_x, res_y = prim.resolution()[:2]
            voxel = prim.voxelSize()
            width = max(res_x * voxel[0], res_y * voxel[1])
            return {
                "min": float(prim.volumeMin()),
                "max": float(prim.volumeMax()),
                "width": float(width),
                "resolution": (int(res_x), int(res_y)),
            }
    raise RuntimeError(
        "{} has no 'height' layer. Connect a HeightField.".format(source_node.path())
    )


def _configure_height_channels(export_node, encoding):
    """Write the ``height`` layer as grayscale RGB, encoded for Gaea's world scale.

    heightfield_output leaves the channel layers empty by default and fills them with
    a constant 1.0 (a flat terrain in Gaea), so the layer is set explicitly. Heights
    are remapped from ``offset..offset+span`` to 0..1 (see ``height_encoding``).
    """
    _set_first_available_parm(export_node, ("out_format",), "rgb")
    low = encoding["offset"]
    high = encoding["offset"] + encoding["span"]
    for channel in ("red", "green", "blue"):
        if export_node.parm(channel + "_channel") is None:
            raise RuntimeError(
                "{} has no '{}_channel' parameter; cannot select the height "
                "layer.".format(export_node.path(), channel)
            )
        export_node.parm(channel + "_channel").set("height")
        export_node.parm(channel + "_out_range").set("manual")
        export_node.parmTuple(channel + "_from_range").set((low, high))
        export_node.parmTuple(channel + "_to_range").set((0.0, 1.0))


def _export_heightfield(source_node, input_path, encoding, image_size):
    """Write the source ``height`` layer to ``input_path`` using ``encoding``.

    ``image_size`` is ``(x, y)`` in pixels; heightfield_output resamples the layer
    to it, so the exported terrain keeps its world size at a lower resolution.
    """
    parent = source_node.parent()
    os.makedirs(os.path.dirname(input_path), exist_ok=True)

    export_node = _create_node_from_candidates(
        parent, HEIGHTFIELD_OUTPUT_NODE_TYPES, "gaea_bridge_export"
    )
    try:
        export_node.setInput(0, source_node)
        _configure_height_channels(export_node, encoding)
        _set_first_available_parm(export_node, ("specify_res",), 1)
        _set_first_available_parm(export_node, ("resolutionx",), int(image_size[0]))
        _set_first_available_parm(export_node, ("resolutiony",), int(image_size[1]))
        file_parm_name = None
        for name in OUTPUT_FILE_PARM_CANDIDATES:
            if export_node.parm(name) is not None:
                export_node.parm(name).set(input_path)
                file_parm_name = name
                break
        if file_parm_name is None:
            label_parm = _find_parm_by_label(export_node, "Filename")
            if label_parm is None:
                label_parm = _find_parm_by_label(export_node, "Output File")
            if label_parm is None:
                raise RuntimeError(
                    "Could not find the output-file parameter on {}".format(
                        export_node.path()
                    )
                )
            label_parm.set(input_path)

        _press_first_available(
            export_node,
            OUTPUT_EXECUTE_PARM_CANDIDATES,
            ("Save to Disk", "Render", "Execute"),
        )
    finally:
        export_node.destroy()

    if not os.path.isfile(input_path):
        raise RuntimeError("Houdini did not create the input EXR: {}".format(input_path))
    if os.path.getsize(input_path) == 0:
        raise RuntimeError("Houdini created an empty input EXR: {}".format(input_path))


def _get_or_create_node(parent, type_names, node_name):
    node = parent.node(node_name)
    if node is not None:
        if node.type().name() not in type_names:
            raise RuntimeError(
                "Cannot reuse {}: expected a {} node".format(node.path(), type_names[0])
            )
        return node
    return _create_node_from_candidates(parent, type_names, node_name)


def _set_parm_if_present(node, name, value):
    parm = node.parm(name)
    if parm is not None:
        parm.set(value)


def _configure_import_node(node, layer_name, path, width):
    """Load ``path`` as the height (first layer) or as a mask layer named after it."""
    _set_first_available_parm(node, FILE_PARM_CANDIDATES, path)
    is_height = layer_name == "height"
    _set_parm_if_present(node, "type", "heightfield" if is_height else "mask")
    _set_parm_if_present(node, "layer", layer_name)
    # Defaults are "add" + clamp at 0: both would distort the imported values.
    _set_parm_if_present(node, "layermode", "replace")
    _set_parm_if_present(node, "doclampmin", 0)
    _set_parm_if_present(node, "doclampmax", 0)
    _set_parm_if_present(node, "heightscale", 1.0)
    if width:
        _set_parm_if_present(node, "sizemethod", "size")
        _set_parm_if_present(node, "size", float(width))


def _height_rescale_snippet(encoding):
    return "// Written by houdini_gaea_bridge: Gaea 0..1 -> Houdini units.\n" \
        "f@height = f@height * {!r} + {!r};\n".format(
            float(encoding["span"]), float(encoding["offset"])
        )


def _import_maps(parent, config, encoding=None, width=None):
    """Import each available output map; chain them so one node holds all layers.

    The height layer comes first (it creates the HeightField at ``width``), the
    rest are added as masks. With an ``encoding`` the heights are converted back to
    Houdini units by a Volume Wrangle at the end of the chain.

    Returns ``(final_node, imported_layers, skipped_layers)``.
    """
    imported = {}
    skipped = []
    previous = None
    layers = sorted(config.output_maps, key=lambda name: name != "height")
    for layer_name in layers:
        path = config.output_path(layer_name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            skipped.append(layer_name)
            continue

        node = _get_or_create_node(
            parent, HEIGHTFIELD_FILE_NODE_TYPES, "gaea_import_{}".format(layer_name)
        )
        _configure_import_node(node, layer_name, path, width)
        node.setInput(0, previous)
        imported[layer_name] = node
        previous = node

    if not imported:
        raise RuntimeError(
            "No Gaea output maps were found in {}. Build the project in Gaea "
            "(F8) first, then import.".format(config.output_dir)
        )
    if encoding and "height" in imported:
        rescale = _get_or_create_node(
            parent, ("volumewrangle",), "gaea_import_height_rescale"
        )
        rescale.parm("snippet").set(_height_rescale_snippet(encoding))
        rescale.setInput(0, previous)
        previous = rescale
    previous.cook(force=True)
    previous.setDisplayFlag(True)
    previous.setRenderFlag(True)
    return previous, imported, skipped


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _require_node(source_node):
    if source_node is None:
        raise ValueError("A source HeightField SOP node is required")
    if not hasattr(source_node, "parent"):
        raise TypeError("source_node must be a Houdini hou.Node")


def export_for_gaea(source_node, config=None, use_cache=True):
    """Write the HeightField to the input EXR and record a manifest.

    Returns a dict with keys: ``input``, ``signature``, ``cache_hit``, ``message``.
    When ``use_cache`` is true and nothing relevant changed since the last build, the
    manifest already matches and ``cache_hit`` is True (no Gaea rebuild needed).
    """
    _require_node(source_node)
    config = config or BridgeConfig.load()
    os.makedirs(config.bridge_dir, exist_ok=True)
    os.makedirs(config.output_dir, exist_ok=True)

    info = heightfield_info(source_node)
    gaea_width, gaea_height, gaea_source = gaea_world_size(config)
    encoding = height_encoding(
        info["min"], info["max"], info["width"], gaea_width, gaea_height
    )
    image_size = downsampled_resolution(
        info["resolution"][0], info["resolution"][1], config.downsample
    )
    _export_heightfield(source_node, config.input_path, encoding, image_size)
    signature = compute_signature(
        config.input_path, config.downsample, config.output_maps
    )

    if use_cache and is_cache_valid(config):
        return {
            "input": config.input_path,
            "signature": signature,
            "cache_hit": True,
            "encoding": encoding,
            "message": "Cache valid: input and settings are unchanged and Gaea "
            "outputs already exist. No rebuild needed.",
        }

    write_manifest(config, signature, extra={
        "height_range": [info["min"], info["max"]],
        "houdini_width": info["width"],
        "houdini_resolution": list(info["resolution"]),
        "image_size": list(image_size),
        "gaea_world": {"width": gaea_width, "height": gaea_height,
                       "source": gaea_source},
        "height_encoding": encoding,
    })

    notes = []
    if info["max"] - info["min"] < 1e-6:
        notes.append("WARNING: source terrain is flat.")
    if encoding["max_value"] > 1.0:
        notes.append(
            "WARNING: relief is taller than Gaea's Height allows (peak = {:.2f}). "
            "Raise Height in Gaea's Terrain Definition to at least {:.0f} m, or "
            "enable 'Allow Unclamped' on the File node.".format(
                encoding["max_value"], gaea_height * encoding["max_value"]
            )
        )
    return {
        "input": config.input_path,
        "signature": signature,
        "cache_hit": False,
        "encoding": encoding,
        "message": "Exported {} ({}x{} px, {} of {}x{}). Houdini {:.0f} units wide, "
        "relief {:.1f} -> Gaea {:.0f} x {:.0f} m ({}): peak at {:.3f}. Build in Gaea "
        "(F8), then import.{}".format(
            config.input_path, image_size[0], image_size[1],
            downsample_label(config.downsample), info["resolution"][0],
            info["resolution"][1], info["width"],
            info["max"] - info["min"], gaea_width, gaea_height, gaea_source,
            encoding["max_value"], "  " + " ".join(notes) if notes else "",
        ),
    }


def import_gaea_outputs(source_node, config=None):
    """Import the Gaea output EXRs as named layers after a manual GUI build.

    Heights are converted back to Houdini units with the encoding the last export
    recorded in the manifest, so the terrain returns at its original scale.
    """
    _require_node(source_node)
    config = config or BridgeConfig.load()
    manifest = read_manifest(config) or {}
    encoding = manifest.get("height_encoding")
    width = manifest.get("houdini_width")
    merged, imported, skipped = _import_maps(
        source_node.parent(), config, encoding=encoding, width=width
    )
    note = "" if encoding else (
        "  WARNING: no height encoding in the manifest (export again); heights "
        "left in Gaea's 0..1 range."
    )
    return {
        "node": merged,
        "imported": imported,
        "skipped": skipped,
        "message": "Imported layers: {}{}{}".format(
            ", ".join(imported) or "none",
            "  (skipped: {})".format(", ".join(skipped)) if skipped else "",
            note,
        ),
    }


def open_in_gaea(config=None):
    """Optionally launch the installed Gaea GUI with the configured project.

    This only starts the normal Gaea application; it does not pass any build or
    licensing flags. Returns the launched path, or raises if not configured.
    """
    config = config or BridgeConfig.load()
    if not config.gaea_exe or not os.path.isfile(config.gaea_exe):
        raise RuntimeError(
            "gaea_exe is not configured or does not exist: {!r}. Set it in "
            "{} or the HDA's Gaea Exe parameter.".format(config.gaea_exe, CONFIG_NAME)
        )
    args = [config.gaea_exe]
    if config.gaea_project and os.path.isfile(config.gaea_project):
        args.append(config.gaea_project)
    subprocess.Popen(args)  # fire-and-forget; user drives the GUI
    return args


# --------------------------------------------------------------------------- #
# HDA integration
# --------------------------------------------------------------------------- #

def overrides_from_node(node):
    """Translate the HDA's parameters into a ``BridgeConfig.load`` overrides dict.

    Lives here (not in the HDA's PythonModule) so button callbacks only depend on
    this module: fixing the logic never requires rebuilding the HDA. Works with any
    object exposing ``parm(name)``, so it is unit-testable without Houdini.
    """

    def _eval(name, default, as_string=False):
        parm = node.parm(name)
        if parm is None:
            return default
        return parm.evalAsString() if as_string else parm.eval()

    output_maps = {}
    for layer, filename in DEFAULT_OUTPUT_MAPS.items():
        if _eval("layer_" + layer, True):
            output_maps[layer] = filename

    downsample = _eval("downsample", "", as_string=True)
    return {
        "bridge_dir": _eval("bridge_dir", None, as_string=True) or None,
        "downsample": int(downsample) if downsample else None,
        "gaea_project": _eval("gaea_project", None, as_string=True),
        "gaea_exe": _eval("gaea_exe", None, as_string=True),
        "gaea_width": _eval("gaea_width", None),
        "gaea_height": _eval("gaea_height", None),
        "output_maps": output_maps,
    }


def config_from_node(node):
    """Build a BridgeConfig for an HDA node: defaults -> JSON file -> node parms."""
    return BridgeConfig.load(overrides=overrides_from_node(node))


# --------------------------------------------------------------------------- #
# Selection-driven entry points (for shelf tools)
# --------------------------------------------------------------------------- #

def _selected_source():
    import hou

    selected_nodes = hou.selectedNodes()
    if len(selected_nodes) != 1:
        raise RuntimeError("Select exactly one source HeightField SOP node")
    return selected_nodes[0]


def run_from_selection(config=None):
    """Step 1: export the selected HeightField for a manual Gaea GUI build."""
    result = export_for_gaea(_selected_source(), config=config)
    print(result["message"])
    return result


def import_from_selection(config=None):
    """Step 3: import Gaea outputs onto the selected node's parent."""
    result = import_gaea_outputs(_selected_source(), config=config)
    print(result["message"])
    return result
