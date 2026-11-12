"""GH-Bridge: file-based procedural bridge between Houdini and QuadSpinner Gaea 2.0.

This module automates the round trip between Houdini and Gaea:

    1. Export a HeightField SOP to an input EXR (+ a manifest describing the job).
    2. Build the Gaea project: either in the Gaea GUI (F8), or from Houdini through
       Gaea's official command-line builder, Gaea.Swarm.exe.
    3. Import the resulting EXR maps back into Houdini as HeightField layers.

It talks to Gaea only through plain files on disk and Gaea's own executables (the
GUI and Gaea.Swarm.exe). It never touches licensing or protection: whatever the
installed Gaea edition allows or refuses applies unchanged.

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
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

# Working directory for the on-disk exchange. Override per-machine via
# gh_bridge_config.json next to this file, or the Bridge Dir parameter on the HDA.
DEFAULT_BRIDGE_DIR = r"C:/Temp/gh_bridge"
DEFAULT_INPUT_NAME = "input_height.exr"
# Mask from the node's second input, read in Gaea by the File node wired into a
# "Mask" port (e.g. Erosion2's). Without a mask input it's all white (no masking).
DEFAULT_MASK_INPUT_NAME = "input_mask.exr"
DEFAULT_OUTPUT_SUBDIR = "output"
MANIFEST_NAME = "gh_bridge_manifest.json"
CONFIG_NAME = "gh_bridge_config.json"

# Houdini layer name -> exact filename configured on the matching Gaea Export node.
# Missing files are skipped on import with a warning (not an error), so you can keep
# this list broad and only wire up the Export nodes you actually need in Gaea.
DEFAULT_OUTPUT_MAPS = {
    "height": "output_height.exr",
    "flow": "output_flow.exr",
    "wear": "output_wear.exr",
    "sediment": "output_sediment.exr",
}

# Color maps of the Gaea Tint nodes, one per layer: a Tint fed by Erosion2's Out,
# Flow, Wear or Deposits writes output_tint_<layer>. Shown on the terrain on
# request (Tint tab), never imported as a layer.
TINT_LAYERS = ("height", "flow", "wear", "sediment")
DEFAULT_TINT_OUTPUTS = {layer: "output_tint_{}.exr".format(layer)
                        for layer in TINT_LAYERS}

# Mask layers (everything but height) are normalized to 0..1 on import, and one of
# them can be copied into the "mask" layer, which Houdini draws in the viewport and
# which HeightField nodes use by default.
DEFAULT_MASK_LAYER = "flow"
MASK_LAYERS = ("flow", "wear", "sediment")

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
    # Path to the Gaea project (.terrain in Gaea 2.0): opened in the GUI, built by
    # Swarm, and the source of the Gaea world size.
    gaea_project: str = ""
    # Path to the installed Gaea executable, for the optional "open in GUI" action.
    gaea_exe: str = ""
    # Gaea's command-line builder. "" = Gaea.Swarm.exe next to gaea_exe.
    gaea_swarm: str = ""
    # Swarm build options: resolution override (0 = the project's), ignore Gaea's
    # baked cache, and show Swarm's console window instead of hiding it.
    build_resolution: int = 0
    ignore_cache: bool = False
    show_console: bool = False
    # Erosion template mode: gaea_project is a template whose Erosion2 node feeds
    # the Export nodes. Swarm then builds a copy with these Erosion2 values
    # ({Gaea property name: value}); the original project stays untouched.
    use_template: bool = False
    erosion_node: str = ""  # Erosion2 node name; "" = the only one in the project
    erosion_params: dict = field(default_factory=dict)
    # Tint (Tint tab): per layer, the Export name of its Tint color map, which is
    # only shown on the terrain (Show Tint), never output as a layer. With use_tint
    # the build copy gets these Tint values ({layer: {Gaea property: value}}); the
    # Tint node of a layer is the one upstream of its Export.
    tint_outputs: dict = field(default_factory=lambda: dict(DEFAULT_TINT_OUTPUTS))
    use_tint: bool = False
    tint_params: dict = field(default_factory=dict)

    @property
    def builds_copy(self):
        """Swarm builds a copy of the project with Houdini's values."""
        return bool(self.use_template or self.use_tint)
    # Gaea world size in metres. 0 = read it from gaea_project, else the defaults.
    gaea_width: float = 0.0
    gaea_height: float = 0.0
    # Imported layer to copy into the "mask" layer ("" = leave mask alone).
    mask_layer: str = DEFAULT_MASK_LAYER
    # Mask sent to Gaea from the node's second input.
    mask_input_name: str = DEFAULT_MASK_INPUT_NAME
    # Layer of the mask input to use: "auto" (mask, else height, else the only
    # volume) or a layer name.
    mask_input_layer: str = "auto"

    # -- derived paths ----------------------------------------------------- #

    @property
    def input_path(self):
        return os.path.join(self.bridge_dir, self.input_name)

    @property
    def mask_input_path(self):
        return os.path.join(self.bridge_dir, self.mask_input_name)

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


def compute_signature(input_path, downsample, output_maps, content_hash=None):
    """One hash over the input plus the settings that affect a rebuild.

    If any of these change, the Gaea result is stale and should be rebuilt. The
    input is identified by ``content_hash`` when given (the export passes a hash of
    the HeightField's heights: the EXR file itself embeds a write timestamp, so its
    bytes differ on every export), else by the input file's bytes.
    """
    digest = hashlib.sha1()
    input_id = content_hash or compute_file_hash(input_path)
    digest.update((input_id or "no-input").encode("utf-8"))
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


def find_output_file(config, layer_name):
    """Path of the Gaea output for ``layer_name``, or None if there is none.

    Tries the exact name first, then any file in the output folder whose name
    matches ignoring case and surrounding spaces (a Gaea Export node named
    " output_flow" writes " output_flow.exr"). Empty files don't count.
    """
    return _find_named_output(config, config.output_maps[layer_name])


def find_tint_file(config, layer):
    """Path of the Gaea Tint color map of ``layer``, or None."""
    name = config.tint_outputs.get(layer)
    return _find_named_output(config, name) if name else None


def find_tint_files(config):
    """{layer: path} of the Tint color maps that exist."""
    found = {}
    for layer in config.tint_outputs:
        path = find_tint_file(config, layer)
        if path is not None:
            found[layer] = path
    return found


TINT_DISPLAY_SUBDIR = "tint_display"


def tint_display_path(config, layer, source, fresh=False):
    """The Tint map of ``layer`` under a name unique to this build, for display.
    ``fresh``: a name never used before (Load Tint).

    Gaea rewrites output_tint_<layer>.exr under the same name, and Houdini's
    viewport keeps showing the texture it loaded for a path. So the display uses
    <Bridge Dir>/tint_display/tint_<layer>_<build stamp>.exr: a hard link to the
    output (no copy, no extra space), or a copy where links are not possible.
    Older maps of the layer are removed (if Windows still holds one open, it is
    left for next time). Falls back to ``source`` on any error.
    """
    try:
        info = os.stat(source)
        folder = os.path.join(config.bridge_dir, TINT_DISPLAY_SUBDIR)
        os.makedirs(folder, exist_ok=True)
        prefix = "tint_{}_".format(layer)
        name = "{}{}_{}".format(prefix, info.st_mtime_ns, info.st_size)
        if fresh:
            name += "_r{}".format(time.time_ns())
        name += ".exr"
        target = os.path.join(folder, name)
        if not os.path.isfile(target):
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
        for old in os.listdir(folder):
            if old.startswith(prefix) and old != name:
                try:
                    os.remove(os.path.join(folder, old))
                except OSError:
                    pass
        return target
    except OSError:
        return source


def _find_named_output(config, filename):
    def usable(path):
        return os.path.isfile(path) and os.path.getsize(path) > 0

    exact = os.path.join(config.output_dir, filename)
    if usable(exact):
        return exact
    wanted = filename.strip().casefold()
    try:
        names = sorted(os.listdir(config.output_dir))
    except OSError:
        return None
    for name in names:
        if name.strip().casefold() == wanted:
            path = os.path.join(config.output_dir, name)
            if usable(path):
                return path
    return None


def outputs_present(config):
    """True if at least one configured output map exists (missing ones are skipped
    on import, so a partial set of Gaea Export nodes is a valid result)."""
    return any(find_output_file(config, name) for name in config.output_maps)


def import_postprocess_snippet(encoding, mask_peaks, mask_layer):
    """VEX for the Volume Wrangle that finishes an import.

    * ``encoding``: convert ``height`` from Gaea's 0..1 back to Houdini units.
    * ``mask_peaks``: ``{layer: max value}``; each mask is divided by its peak so it
      spans 0..1 (Gaea writes e.g. wear as 0..0.05). Peaks <= 0 are left alone.
    * ``mask_layer``: copy that layer into ``mask`` (shown in the viewport).
    """
    lines = ["// Written by houdini_gaea_bridge on import."]
    if encoding:
        lines.append("f@height = f@height * {!r} + {!r};".format(
            float(encoding["span"]), float(encoding["offset"])))
    for layer in sorted(mask_peaks):
        peak = float(mask_peaks[layer])
        if peak > 0:
            lines.append("f@{0} = clamp(f@{0} / {1!r}, 0, 1);".format(layer, peak))
    if mask_layer:
        lines.append("f@mask = f@{};".format(mask_layer))
    return "\n".join(lines) + "\n"


def is_cache_valid(config, content_hash=None):
    """True if the current input + settings match the manifest and outputs exist.

    When this is true a fresh Gaea build is unnecessary: the maps on disk already
    correspond to the current input and settings.
    """
    manifest = read_manifest(config)
    if not manifest or not os.path.isfile(config.input_path):
        return False
    signature = compute_signature(
        config.input_path, config.downsample, config.output_maps, content_hash
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
# Building with Gaea.Swarm.exe (pure logic + subprocess, no hou dependency)
# --------------------------------------------------------------------------- #

SWARM_EXE_NAME = "Gaea.Swarm.exe"


def swarm_exe_path(config):
    """Gaea.Swarm.exe to use: the explicit setting, else the one next to Gaea.exe."""
    if config.gaea_swarm:
        return config.gaea_swarm
    if config.gaea_exe:
        return os.path.join(os.path.dirname(config.gaea_exe), SWARM_EXE_NAME)
    return ""


def swarm_command(config, project=None):
    """Argument list for a non-interactive Swarm build of ``project`` (default:
    ``config.gaea_project``)."""
    command = [swarm_exe_path(config), "--Filename", project or config.gaea_project,
               "--silent"]
    if config.build_resolution:
        command += ["--resolution", str(int(config.build_resolution))]
    if config.ignore_cache:
        command.append("--ignorecache")
    return command


def _same_path(a, b):
    def norm(path):
        return os.path.normcase(os.path.normpath(os.path.abspath(path.strip())))
    return bool(a) and bool(b) and norm(a) == norm(b)


def _collect_nodes(data, kind, found):
    """All Gaea node dicts of ``kind`` ("File", "Export") in a .terrain structure."""
    if isinstance(data, dict):
        node_type = str(data.get("$type", ""))
        if ".Nodes.{},".format(kind) in node_type:
            found.append(data)
        for value in data.values():
            _collect_nodes(value, kind, found)
    elif isinstance(data, list):
        for value in data:
            _collect_nodes(value, kind, found)
    return found


def mask_file_nodes(data):
    """File nodes of a .terrain that feed a port named "Mask" (e.g. Erosion2's)."""
    sources = set()

    def walk(value):
        if isinstance(value, dict):
            record = value.get("Record")
            if isinstance(record, dict) and str(record.get("ToPort", "")).strip() \
                    .casefold() == "mask":
                sources.add(record.get("From"))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return [node for node in _collect_nodes(data, "File", [])
            if node.get("Id") in sources]


def choose_mask_layer(ranges, preferred="auto"):
    """Pick the mask input's layer and the range that maps it to 0..1.

    ``ranges``: ``{volume name: (min, max)}`` of the mask input. Returns
    ``(layer, low, high)``: the bridge writes ``clamp(fit(layer, low, high, 0, 1))``.

    * ``preferred`` other than "auto" must exist.
    * "auto": ``mask`` if it holds anything (a HeightField always has a mask layer,
      empty unless something painted it), else ``height`` (e.g. a noise HeightField
      used as a mask), else the only volume.
    * A ``mask`` already within 0..1 is kept as is (0.3 stays 0.3); anything else
      is stretched from its min..max to 0..1. A constant layer maps to all 1
      (or all 0 when it is <= 0).
    """
    if not ranges:
        raise ValueError("The mask input has no volumes.")
    name = (preferred or "auto").strip()
    if name.casefold() != "auto":
        if name not in ranges:
            raise ValueError("The mask input has no layer {!r} (has: {}).".format(
                name, ", ".join(sorted(ranges))))
        layer = name
    elif "mask" in ranges and ranges["mask"][1] > 1e-6:
        layer = "mask"
    elif "height" in ranges and ranges["height"][1] - ranges["height"][0] > 1e-6:
        layer = "height"
    elif len(ranges) == 1:
        layer = next(iter(ranges))
    else:
        layer = "mask" if "mask" in ranges else sorted(ranges)[0]
    low, high = (float(v) for v in ranges[layer])
    if layer == "mask" and low >= -1e-6 and high <= 1.0 + 1e-6:
        return layer, 0.0, 1.0
    if high - low <= 1e-6:
        return (layer, high - 1.0, high) if high > 0 else (layer, high, high + 1.0)
    return layer, low, high


def _bridge_export_name(config, export):
    """Which bridge output a Gaea Export node writes ("height", "flow", ... or
    "tint_height", "tint_flow", ...), matched on its name ignoring case and
    spaces; None for other exports."""
    name = str(export.get("Name", "")).strip().casefold()
    names = dict(config.output_maps)
    names.update(("tint_" + layer, filename)
                 for layer, filename in config.tint_outputs.items())
    for key, filename in names.items():
        if os.path.splitext(filename)[0].strip().casefold() == name:
            return key
    return None


def check_project_paths(config, fix_mask_paths=False):
    """Check that the saved Gaea project reads and writes the bridge's files.

    Swarm builds the project *as saved*, so its File node must load the input EXR
    and its Export nodes must write EXR files into the output folder. Returns a
    list of human-readable problems (empty when everything matches).
    """
    if not config.gaea_project or not os.path.isfile(config.gaea_project):
        return ["Gaea project not found: {!r}.".format(config.gaea_project)]
    try:
        with open(config.gaea_project, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (ValueError, OSError) as error:
        return ["Could not read the Gaea project: {}.".format(error)]

    problems = []
    masks = mask_file_nodes(data)
    if not fix_mask_paths:  # template builds point them to the mask automatically
        for node in masks:
            if not _same_path(node.get("FileName", ""), config.mask_input_path):
                problems.append(
                    "The File node {!r} feeding a Mask port should load {} (loads "
                    "{!r}); or turn on Use Erosion Template, which sets it.".format(
                        node.get("Name"), config.mask_input_path,
                        node.get("FileName", "")))
    file_nodes = [node for node in _collect_nodes(data, "File", [])
                  if node not in masks]
    if not any(_same_path(node.get("FileName", ""), config.input_path)
               for node in file_nodes):
        problems.append("No File node in the project loads {} (found: {}).".format(
            config.input_path,
            ", ".join(repr(node.get("FileName", "")) for node in file_nodes) or "none"))

    exports = _collect_nodes(data, "Export", [])
    matched = set()
    for node in exports:
        layer = _bridge_export_name(config, node)
        if layer is None:
            continue
        matched.add(layer)
        if fix_mask_paths:  # build copies fix Location/Output Path/Format
            continue
        name = str(node.get("Name", "")).strip()
        if node.get("Location") != "Custom" or not _same_path(
                node.get("OutputPath", ""), config.output_dir):
            problems.append("Export node {!r} should use Location 'Custom' with "
                            "Output Path {}.".format(name, config.output_dir))
        if node.get("Format", "EXR") != "EXR":
            problems.append("Export node {!r} should use Format EXR (is {}).".format(
                name, node.get("Format")))
    if config.use_tint and not any(key.startswith("tint_") for key in matched):
        problems.append("No Tint Export node in the project (name them {}).".format(
            ", ".join(os.path.splitext(name)[0]
                      for name in config.tint_outputs.values())))
    if "height" not in matched:
        problems.append("No Export node named {!r} in the project.".format(
            os.path.splitext(config.output_maps.get("height", "output_height"))[0]))
    return problems


def updated_outputs(config, since):
    """Layers whose output file was (re)written at or after ``since`` (epoch s);
    rewritten Tint maps follow as "tint_<layer>"."""
    layers = []
    for layer in config.output_maps:
        path = find_output_file(config, layer)
        if path is not None and os.path.getmtime(path) >= since - 1.0:
            layers.append(layer)
    if layers:
        for layer, path in find_tint_files(config).items():
            if os.path.getmtime(path) >= since - 1.0:
                layers.append("tint_" + layer)
    return layers


class SwarmBuild:
    """A Gaea.Swarm.exe build running in the background.

    Swarm needs a real console: with its output redirected it fails on start
    ("invalid handle"). So it gets its own console window, hidden unless
    ``config.show_console``. Its exit code is 0 even when it builds nothing, so
    success is judged by which output files were written (``updated_layers``).
    """

    def __init__(self, config, command=None):
        self.config = config
        self.command = command or swarm_command(config)
        self.started = time.time()
        self.note = ""
        self.on_done = None
        self._process = None

    def start(self):
        kwargs = {"cwd": os.path.dirname(self.command[0]) or None}
        if os.name == "nt":
            info = subprocess.STARTUPINFO()
            if not self.config.show_console:
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = info
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        self.started = time.time()
        self._process = subprocess.Popen(self.command, **kwargs)
        return self

    @property
    def elapsed(self):
        return time.time() - self.started

    def poll(self):
        """None while running, else Swarm's exit code."""
        return self._process.poll() if self._process else None

    def wait(self, timeout=None):
        return self._process.wait(timeout=timeout)

    def cancel(self):
        if self._process and self._process.poll() is None:
            self._process.kill()

    def updated_layers(self):
        return updated_outputs(self.config, self.started)

    def result_message(self):
        """Status line once the process has exited; ``(ok, message)``."""
        layers = self.updated_layers()
        if layers:
            return True, "Gaea build finished in {:.0f} s ({}).".format(
                self.elapsed, ", ".join(layers))
        return False, (
            "Gaea build ended after {:.0f} s (exit code {}) without writing new "
            "files to {}. Check the project paths, or turn on Show Build Window to "
            "see Swarm's messages.".format(self.elapsed, self.poll(),
                                           self.config.output_dir))


# --------------------------------------------------------------------------- #
# Erosion template: Erosion2 parameters driven from Houdini
# --------------------------------------------------------------------------- #

# Erosion2 parameters as declared by Gaea 2.3 (QuadSpinner.Gaea.Nodes.Erosion2):
# (property, label, group, type, default, min, max). Two-component properties
# (Slope, Altitude in Orographic Influence) are not exposed.
EROSION2_PARAMS = (
    ("Duration", "Duration", "General", "float", 10.0, 1.0, 200.0),
    ("Downcutting", "Downcutting", "General", "float", 0.25, 0.0, 1.0),
    ("ErosionScale", "Erosion Scale", "General", "float", 1000.0, 1.0, 20000.0),
    ("Seed", "Seed", "General", "int", 0, 0, 65535),
    ("NoErosion", "No Erosion", "General", "bool", False, 0, 1),
    ("SuspendedLoadDischargeAmount", "Suspended Load", "Sediment Discharge",
     "float", 1.0, 0.0, 1.0),
    ("SuspendedLoadDischargeAngle", "Suspended Discharge Angle", "Sediment Discharge",
     "float", 24.0, 0.0, 45.0),
    ("BedLoadDischargeAmount", "Bed Load", "Sediment Discharge", "float", 0.0, 0.0, 1.0),
    ("BedLoadDischargeAngle", "Bed Load Discharge Angle", "Sediment Discharge",
     "float", 15.0, 0.0, 45.0),
    ("CoarseSedimentsDischargeAmount", "Coarse Sediments", "Sediment Discharge",
     "float", 0.0, 0.0, 1.0),
    ("CoarseSedimentsDischargeAngle", "Coarse Discharge Angle", "Sediment Discharge",
     "float", 10.0, 0.0, 45.0),
    ("DepositionBoost", "Deposition Boost", "Sediment Discharge", "float", 0.0, 0.0, 1.0),
    ("Shape", "Shape", "Shape", "float", 0.2, 0.0, 1.0),
    ("ShapeSharpness", "Shape Sharpness", "Shape", "float", 0.6, 0.0, 1.0),
    ("ShapeDetailScale", "Shape Detail Scale", "Shape", "float", 0.25, 0.0, 1.0),
    ("Enable", "Enable", "Orographic Influence", "bool", False, 0, 1),
    ("DirectionalPrecipitation", "Directional Precipitation", "Orographic Influence",
     "bool", False, 0, 1),
    ("Direction", "Direction", "Orographic Influence", "int", 0, 0, 360),
    ("RainShadow", "Rain Shadow", "Orographic Influence", "float", 0.8, 0.0, 1.0),
    ("Reverse", "Reverse", "Orographic Influence", "bool", False, 0, 1),
)
EROSION2_DEFAULTS = {spec[0]: spec[4] for spec in EROSION2_PARAMS}
EROSION2_TYPES = {spec[0]: spec[3] for spec in EROSION2_PARAMS}
TEMPLATE_BUILD_NAME = "gaea_template_build.terrain"

# Tint parameters as declared by Gaea 2.3 (QuadSpinner.Gaea.Nodes.Tint), same
# layout as EROSION2_PARAMS. Colors are stored as {"R", "G", "B"} (0..1), the
# Enhance enum by name.
TINT_ENHANCE = ("None", "Autolevel", "Equalize")
TINT_PARAMS = (
    ("Start", "Start", "Tint", "color", (0.4, 0.4, 0.4), 0.0, 1.0),
    ("End", "End", "Tint", "color", (1.0, 1.0, 1.0), 0.0, 1.0),
    ("Hue", "Hue", "Tint", "float", 0.0, -1.0, 1.0),
    ("Saturation", "Saturation", "Tint", "float", 0.0, -1.0, 1.0),
    ("Lightness", "Lightness", "Tint", "float", 0.0, -1.0, 1.0),
    ("Enhance", "Enhance", "Processing", "enum", "Autolevel", TINT_ENHANCE, None),
    ("Reverse", "Reverse", "Processing", "bool", False, 0, 1),
)
TINT_DEFAULTS = {spec[0]: spec[4] for spec in TINT_PARAMS}
TINT_TYPES = {spec[0]: spec[3] for spec in TINT_PARAMS}
PARAM_TYPES = dict(EROSION2_TYPES)


def _coerce_kind(kind, value):
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(round(float(value)))
    if kind == "color":
        if isinstance(value, dict):
            value = (value.get("R", 0.0), value.get("G", 0.0), value.get("B", 0.0))
        return tuple(float(component) for component in value)[:3]
    if kind == "enum":
        return str(value)
    return float(value)


def _same_kind(kind, a, b):
    a, b = _coerce_kind(kind, a), _coerce_kind(kind, b)
    if kind == "float":
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    if kind == "color":
        return all(abs(x - y) <= 1e-6 for x, y in zip(a, b))
    return a == b


def _to_gaea(kind, value):
    """Value as Gaea stores it in a .terrain."""
    value = _coerce_kind(kind, value)
    if kind == "color":
        return {"R": value[0], "G": value[1], "B": value[2]}
    return value


def _coerce(name, value):
    return _coerce_kind(EROSION2_TYPES[name], value)


def _same_value(name, a, b):
    return _same_kind(EROSION2_TYPES[name], a, b)


def find_gaea_node(data, kind, node_name="", setting="Node"):
    """The ``kind`` node ("Erosion2", "Tint") of a parsed .terrain. With several,
    pick by name."""
    nodes = _collect_nodes(data, kind, [])
    if not nodes:
        raise ValueError("The Gaea project has no {} node.".format(kind))
    if node_name:
        for node in nodes:
            if str(node.get("Name", "")).strip() == node_name.strip():
                return node
        raise ValueError("No {} node named {!r} (found: {}).".format(
            kind, node_name, ", ".join(repr(n.get("Name")) for n in nodes)))
    if len(nodes) > 1:
        raise ValueError("The project has several {} nodes ({}); set {} to the one "
                         "to drive.".format(kind, ", ".join(
                             repr(n.get("Name")) for n in nodes), setting))
    return nodes[0]


def find_erosion_node(data, node_name=""):
    """The Erosion2 node dict of a parsed .terrain. With several, pick by name."""
    return find_gaea_node(data, "Erosion2", node_name, "Erosion Node")


def _port_sources(node):
    """[(port name, source node id)] of a Gaea node's connected input ports."""
    ports = node.get("Ports")
    if isinstance(ports, dict):
        ports = ports.get("$values")
    sources = []
    for port in ports if isinstance(ports, list) else ():
        record = port.get("Record") if isinstance(port, dict) else None
        if isinstance(record, dict) and record.get("From") is not None:
            sources.append((str(port.get("Name", "")), record.get("From")))
    return sources


def _nodes_by_id(data):
    nodes = {}

    def walk(value):
        if isinstance(value, dict):
            if "Id" in value and ".Nodes." in str(value.get("$type", "")):
                nodes[value["Id"]] = value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return nodes


def tint_nodes(data, config):
    """{layer: Tint node dict} of a parsed .terrain: for each Export named like
    ``config.tint_outputs[layer]``, the nearest Tint upstream of it (through the
    "In" ports). Layers without such an Export or Tint are left out."""
    by_id = _nodes_by_id(data)
    found = {}
    for export in _collect_nodes(data, "Export", []):
        key = _bridge_export_name(config, export)
        if not key or not key.startswith("tint_"):
            continue
        node, seen = export, set()
        while node is not None and id(node) not in seen:
            seen.add(id(node))
            sources = dict(_port_sources(node))
            source = by_id.get(sources.get("In"))
            if source is not None and ".Nodes.Tint," in str(source.get("$type", "")):
                found[key[len("tint_"):]] = source
                break
            node = source
    return found


def read_node_params(node, specs):
    """Values of a Gaea node: stored values, Gaea defaults for the rest."""
    values = {}
    for spec in specs:
        name, kind, default = spec[0], spec[3], spec[4]
        try:
            values[name] = _coerce_kind(kind, node.get(name, default))
        except (TypeError, ValueError):
            values[name] = _coerce_kind(kind, default)
    return values


def apply_node_params(node, specs, values):
    """Write the values that differ from the node's current ones; unknown names
    are skipped. Returns the names that changed."""
    known = {spec[0]: spec for spec in specs}
    changed = []
    for name, value in sorted(values.items()):
        spec = known.get(name)
        if spec is None:
            continue
        kind, default = spec[3], spec[4]
        if kind == "enum" and str(value) not in spec[5]:
            continue
        try:
            if _same_kind(kind, value, node.get(name, default)):
                continue
            node[name] = _to_gaea(kind, value)
        except (TypeError, ValueError):
            continue
        changed.append(name)
    return changed


def read_tint_params(path, config):
    """{layer: Tint values} of the project's per-layer Tint nodes."""
    return {layer: read_node_params(node, TINT_PARAMS)
            for layer, node in tint_nodes(_load_project(path), config).items()}


def _load_project(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_erosion_params(path, node_name=""):
    """Erosion2 values of a project: stored values, Gaea defaults for the rest
    (Gaea only saves values that differ from the default)."""
    node = find_erosion_node(_load_project(path), node_name)
    values = {}
    for name in EROSION2_DEFAULTS:
        try:
            values[name] = _coerce(name, node.get(name, EROSION2_DEFAULTS[name]))
        except (TypeError, ValueError):
            values[name] = EROSION2_DEFAULTS[name]
    return values


def write_template_copy(config, destination):
    """Write a copy of the template with the Houdini Erosion2 values.

    Only values that differ from the template are written; parameters the bridge
    doesn't know are left as they are. Returns the names that were changed.
    """
    data = _load_project(config.gaea_project)
    changed = []
    if config.use_template:
        changed += apply_node_params(find_erosion_node(data, config.erosion_node),
                                     EROSION2_PARAMS, config.erosion_params)
    if config.use_tint:
        nodes = tint_nodes(data, config)
        for layer in sorted(nodes):
            changed += ["Tint {} {}".format(layer, name) for name in apply_node_params(
                nodes[layer], TINT_PARAMS, config.tint_params.get(layer) or {})]
    for mask in mask_file_nodes(data):
        if not _same_path(mask.get("FileName", ""), config.mask_input_path):
            mask["FileName"] = os.path.normpath(config.mask_input_path)
            changed.append("mask file")
    # The bridge's Export nodes must write EXR into the output folder.
    fixed = False
    for export in _collect_nodes(data, "Export", []):
        if _bridge_export_name(config, export) is None:
            continue
        if export.get("Location") != "Custom":
            export["Location"] = "Custom"
            fixed = True
        if not _same_path(export.get("OutputPath", ""), config.output_dir):
            export["OutputPath"] = os.path.normpath(config.output_dir)
            fixed = True
        if export.get("Format", "EXR") != "EXR":
            export["Format"] = "EXR"
            fixed = True
        if export.get("IfFileExists", "Overwrite") != "Overwrite":
            export["IfFileExists"] = "Overwrite"
            fixed = True
    if fixed:
        changed.append("export paths")
    folder = os.path.dirname(destination)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    return changed


def template_build_path(config):
    return os.path.join(config.bridge_dir, TEMPLATE_BUILD_NAME)


def build_settings(config):
    """Everything besides the input heights that changes what Gaea produces:
    part of the export cache signature."""
    settings = {"build_resolution": int(config.build_resolution or 0)}
    if config.gaea_project and os.path.isfile(config.gaea_project):
        info = os.stat(config.gaea_project)  # a re-saved project means rebuild
        settings["project"] = [info.st_size, info.st_mtime_ns // 1000000]
    if config.use_template:
        settings["template"] = {"node": config.erosion_node,
                                "params": dict(config.erosion_params)}
    if config.use_tint:
        settings["tint"] = {
            layer: {name: _to_gaea(TINT_TYPES[name], value)
                    for name, value in (values or {}).items() if name in TINT_TYPES}
            for layer, values in config.tint_params.items()}
    return settings


def validate_build(config):
    """Problems that stop a Swarm build before it starts (empty list = go)."""
    problems = []
    exe = swarm_exe_path(config)
    if not exe or not os.path.isfile(exe):
        problems.append("Gaea.Swarm.exe not found ({!r}). Set Gaea Exe or Gaea Swarm "
                        "Exe on the Paths tab.".format(exe))
    if not os.path.isfile(config.input_path):
        problems.append("No exported input yet: press Export first.")
    problems += check_project_paths(config, fix_mask_paths=config.builds_copy)
    if config.builds_copy and config.gaea_project and os.path.isfile(
            config.gaea_project):
        try:
            data = _load_project(config.gaea_project)
            if config.use_template:
                find_erosion_node(data, config.erosion_node)
            if config.use_tint and not tint_nodes(data, config):
                problems.append("No Tint node feeds the Tint Export nodes ({}).".format(
                    ", ".join(os.path.splitext(name)[0]
                              for name in config.tint_outputs.values())))
        except (ValueError, OSError) as error:
            problems.append(str(error))
    return problems


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


# --------------------------------------------------------------------------- #
# Houdini version compatibility (19.5 and 20.x)
# --------------------------------------------------------------------------- #
# Known differences, checked against the SideFX docs of both versions:
# * hou.Volume.voxelSize() exists only from Houdini 20; indexToPos() exists in both.
# * hou.Volume.resolution() returns a hou.Vector3 of floats in 19.5, a tuple of
#   ints in 20.x.
# * HeightField Output's resolution toggle is "Use Resolution" in 19.5 and part of
#   "Resolution" in 20.x.
# Parameters are looked up from candidate names, and every export is read back
# and checked, so an unknown difference fails loudly instead of exporting a flat
# terrain. tools/houdini_compat_check.py reports what a given Houdini has.

HFOUT_FORMAT_PARMS = ("out_format", "format")
HFOUT_USE_RES_PARMS = ("specify_res", "use_res", "useres", "use_resolution",
                       "douseres")
HFOUT_CHANNEL_PARMS = {
    "layer": ("{}_channel", "{}channel", "{}_layer"),
    "range": ("{}_out_range", "{}_outrange", "{}_range_mode"),
    "from": ("{}_from_range", "{}_fromrange"),
    "to": ("{}_to_range", "{}_torange"),
}


def houdini_version_string():
    try:
        import hou
        return hou.applicationVersionString()
    except Exception:  # outside Houdini
        return "unknown"


def volume_resolution(prim):
    """``(x, y, z)`` voxel counts as ints (19.5 returns a hou.Vector3 of floats)."""
    return tuple(int(round(float(value))) for value in prim.resolution())


def _distance(a, b):
    return sum((float(b[i]) - float(a[i])) ** 2 for i in range(3)) ** 0.5


def volume_voxel_size(prim):
    """``(x, y)`` voxel size in world units along the volume's first two axes.

    Uses voxelSize() where it exists (Houdini 20+), else the distance between
    neighbouring voxel centres (indexToPos, available in 19.5 too), else the
    bounding box (a HeightField spans (res - 1) voxels between voxel centres).
    """
    voxel_size = getattr(prim, "voxelSize", None)
    if voxel_size is not None:
        try:
            size = voxel_size()
            return float(size[0]), float(size[1])
        except Exception:
            pass
    res = volume_resolution(prim)
    try:
        origin = prim.indexToPos((0, 0, 0))
        size_x = _distance(origin, prim.indexToPos((1, 0, 0))) if res[0] > 1 else 0.0
        size_y = _distance(origin, prim.indexToPos((0, 1, 0))) if res[1] > 1 else 0.0
        if size_x > 0 or size_y > 0:
            return (size_x or size_y), (size_y or size_x)
    except Exception:
        pass
    extent = prim.boundingBox().sizevec()
    horizontal = sorted((float(extent[0]), float(extent[1]), float(extent[2])))[1:]
    return (horizontal[1] / max(res[0] - 1, 1), horizontal[0] / max(res[1] - 1, 1))


def _find_parm(node, candidates, tuple_parm=False):
    lookup = node.parmTuple if tuple_parm else node.parm
    for name in candidates:
        parm = lookup(name)
        if parm is not None:
            return parm
    return None


def _missing_parm_error(node, what, candidates):
    return RuntimeError(
        "{} has no {} parameter (tried {}) in Houdini {}. Run "
        "tools/houdini_compat_check.py and report its output.".format(
            node.type().name(), what, ", ".join(candidates), houdini_version_string()))


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
            res = volume_resolution(prim)
            res_x, res_y = res[0], res[1]
            voxel = volume_voxel_size(prim)
            width = max(res_x * voxel[0], res_y * voxel[1])
            digest = hashlib.sha1(prim.allVoxelsAsString())
            digest.update(repr((res, tuple(round(v, 6) for v in voxel)))
                          .encode("utf-8"))
            return {
                "min": float(prim.volumeMin()),
                "max": float(prim.volumeMax()),
                "width": float(width),
                "resolution": (int(res_x), int(res_y)),
                "content_hash": digest.hexdigest(),
            }
    raise RuntimeError(
        "{} has no 'height' layer. Connect a HeightField.".format(source_node.path())
    )


def _configure_height_channels(export_node, encoding, layer="height"):
    """Write ``layer`` as grayscale RGB, encoded for Gaea's world scale.

    heightfield_output leaves the channel layers empty by default and fills them with
    a constant 1.0 (a flat terrain in Gaea), so the layer is set explicitly. Heights
    are remapped from ``offset..offset+span`` to 0..1 (see ``height_encoding``).
    """
    format_parm = _find_parm(export_node, HFOUT_FORMAT_PARMS)
    if format_parm is None:
        raise _missing_parm_error(export_node, "image Format", HFOUT_FORMAT_PARMS)
    format_parm.set("rgb")
    low = encoding["offset"]
    high = encoding["offset"] + encoding["span"]
    for channel in ("red", "green", "blue"):
        parms = {}
        for role, patterns in HFOUT_CHANNEL_PARMS.items():
            names = tuple(pattern.format(channel) for pattern in patterns)
            parm = _find_parm(export_node, names, tuple_parm=role in ("from", "to"))
            if parm is None:
                raise _missing_parm_error(
                    export_node, "{} channel '{}'".format(channel, role), names)
            parms[role] = parm
        fill = export_node.parm("fill_{}_channel".format(channel))
        if fill is not None:
            fill.set(1)
        parms["layer"].set(layer)
        parms["range"].set("manual")
        parms["from"].set((low, high))
        parms["to"].set((0.0, 1.0))


def _set_export_resolution(export_node, image_size):
    """Turn on the resolution override and set it (names differ across versions)."""
    toggle = _find_parm(export_node, HFOUT_USE_RES_PARMS)
    if toggle is None:
        toggle = _find_parm_by_label(export_node, "Use Resolution")
    if toggle is not None:
        toggle.set(1)
    size = _find_parm(export_node, ("resolution", "res"), tuple_parm=True)
    if size is not None and len(size) >= 2:
        size.set((int(image_size[0]), int(image_size[1])))
        return
    x_parm = _find_parm(export_node, ("resolutionx", "resx"))
    y_parm = _find_parm(export_node, ("resolutiony", "resy"))
    if x_parm is None or y_parm is None:
        raise _missing_parm_error(export_node, "Resolution", ("resolution",))
    x_parm.set(int(image_size[0]))
    y_parm.set(int(image_size[1]))


def _verify_export(parent, path, encoding, flat):
    """Read the exported EXR back and check it holds the encoded terrain.

    Guards against version differences that would silently write something else
    (an unassigned channel exports a constant 1.0, i.e. a flat terrain in Gaea).
    """
    reader = _create_node_from_candidates(
        parent, HEIGHTFIELD_FILE_NODE_TYPES, "gaea_bridge_export_check")
    try:
        _set_first_available_parm(reader, FILE_PARM_CANDIDATES, path)
        _set_parm_if_present(reader, "type", "heightfield")
        _set_parm_if_present(reader, "doclampmin", 0)
        _set_parm_if_present(reader, "doclampmax", 0)
        _set_parm_if_present(reader, "heightscale", 1.0)
        reader.cook(force=True)
        low = high = None
        for prim in reader.geometry().prims():
            if prim.attribValue("name") == "height":
                low, high = float(prim.volumeMin()), float(prim.volumeMax())
    finally:
        reader.destroy()
    if low is None:
        raise RuntimeError("Could not read back the exported EXR {}.".format(path))
    expected = encoding["max_value"]
    tolerance = 0.05 * max(expected, 0.01) + 0.002
    if flat:
        return
    # Downsampling smooths peaks, so only require: not constant, within 0..expected,
    # and reaching at least half of the expected peak.
    if (high - low < 1e-6 or low < -tolerance or high > expected + tolerance
            or high < 0.5 * expected):
        raise RuntimeError(
            "The exported EXR doesn't contain the terrain: values {:.4f}..{:.4f}, "
            "expected 0..{:.4f}. HeightField Output behaves differently in Houdini "
            "{}; run tools/houdini_compat_check.py and report its output.".format(
                low, high, expected, houdini_version_string()))


def _export_heightfield(source_node, input_path, encoding, image_size,
                        layer="height", verify=True):
    """Write the source's ``layer`` to ``input_path`` using ``encoding``.

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
        _configure_height_channels(export_node, encoding, layer=layer)
        _set_export_resolution(export_node, image_size)
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
    if verify:
        _verify_export(parent, input_path, encoding,
                       flat=encoding["max_value"] < 1e-9)


def _volume_ranges(geometry):
    """``({name: (min, max)}, {name: prim number})`` of the volumes in ``geometry``
    (unnamed volumes get "volume<N>")."""
    import hou

    ranges, numbers = {}, {}
    has_name = geometry.findPrimAttrib("name") is not None
    for index, prim in enumerate(geometry.prims()):
        if prim.type() != hou.primType.Volume:
            continue
        name = prim.attribValue("name") if has_name else ""
        name = name or "volume{}".format(index)
        if name not in ranges:
            ranges[name] = (float(prim.volumeMin()), float(prim.volumeMax()))
            numbers[name] = prim.number()
    return ranges, numbers


class MaskNetwork:
    """Temporary nodes that put the mask input onto the terrain's grid as "mask".

    Any mask works: a HeightField (its ``mask`` layer, or ``height`` for a
    procedural noise HeightField), any other volume, or plain geometry (polygons,
    curves...) turned into a mask with HeightField Mask by Object. Its resolution,
    size and position don't matter; outside its area the mask is 0. Without a mask
    input the mask is 1 everywhere (Gaea erodes the whole terrain).

    Use as a context manager: the nodes are deleted on exit.
    """

    def __init__(self, source_node, mask_node, preferred_layer="auto"):
        self.source = source_node
        self.mask_input = mask_node
        self.preferred = preferred_layer
        self.nodes = []
        self.output = None
        self.info = {}

    def _create(self, type_names, name, inputs):
        if isinstance(type_names, str):
            type_names = (type_names,)
        node = _create_node_from_candidates(self.source.parent(), type_names, name)
        self.nodes.append(node)
        for index, src in enumerate(inputs):
            node.setInput(index, src)
        return node

    def _wrangle(self, name, source, snippet):
        node = self._create("volumewrangle", name, (source,))
        _set_first_available_parm(node, ("snippet",), snippet)
        return node

    def build(self):
        mask = self.mask_input
        geometry = mask.geometry() if mask is not None else None
        if mask is None or geometry is None or not geometry.prims():
            self.output = self._wrangle("gaea_bridge_mask_full", self.source,
                                        "f@mask = 1;\n")
            self.info = {"source": "none (all white)"}
            return self
        ranges, numbers = _volume_ranges(geometry)
        if ranges:
            layer, low, high = choose_mask_layer(ranges, self.preferred)
            # 1. In the mask input: the chosen volume becomes "mask" (any other
            #    "mask" is dropped), mapped to 0..1. Its border is set to constant
            #    0: HeightField height layers use "streak", which would smear the
            #    edge values over the whole terrain outside the mask's area.
            rename = self._create("attribwrangle", "gaea_bridge_mask_pick", (mask,))
            _set_first_available_parm(rename, ("class",), "primitive")
            _set_first_available_parm(rename, ("snippet",), (
                "if (@primnum == {0}) {{\n"
                "    s@name = \"mask\";\n"
                "    setprimintrinsic(0, \"volumebordertype\", @primnum, \"constant\");\n"
                "    setprimintrinsic(0, \"volumebordervalue\", @primnum, 0.0);\n"
                "}}\n"
                "else if (s@name == \"mask\") removeprim(0, @primnum, 1);\n"
            ).format(numbers[layer]))
            remap = self._wrangle(
                "gaea_bridge_mask_remap", rename,
                "f@mask = clamp(fit(f@mask, {!r}, {!r}, 0, 1), 0, 1);\n".format(
                    float(low), float(high)))
            # 2. On the terrain: start from an empty mask and replace it with the
            #    mask input where they overlap; outside its area the mask stays 0.
            base = self._wrangle("gaea_bridge_mask_clear", self.source,
                                 "f@mask = 0;\n")
            self.output = self._create("heightfield_layer",
                                       "gaea_bridge_mask_resample", (base, remap))
            _set_first_available_parm(self.output, ("layer",), "mask")
            _set_parm_if_present(self.output, "mode", "replace")
            self.info = {"source": "layer", "layer": layer, "range": [low, high]}
        else:
            # Plain geometry: mask where it is, seen from above.
            self.output = self._create("heightfield_maskbyobject",
                                       "gaea_bridge_mask_object", (self.source, mask))
            self.info = {"source": "geometry (Mask by Object)"}
        self.output.cook(force=True)
        errors = self.output.errors()
        if errors:
            raise RuntimeError("Could not build the mask: " + " ".join(errors))
        return self

    def mask_prim(self):
        for prim in self.output.geometry().prims():
            if prim.attribValue("name") == "mask":
                return prim
        raise RuntimeError("The mask network produced no 'mask' layer.")

    def content_hash(self):
        prim = self.mask_prim()
        digest = hashlib.sha1(prim.allVoxelsAsString())
        digest.update(_normalise_dict(self.info).encode("utf-8"))
        return digest.hexdigest()

    def coverage(self):
        """Mean mask value (0..1): how much of the terrain is masked in."""
        values = self.mask_prim().allVoxels()
        return sum(values) / float(len(values) or 1)

    def __enter__(self):
        return self.build()

    def __exit__(self, *exc):
        for node in reversed(self.nodes):
            try:
                node.destroy()
            except Exception:
                pass
        return False


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


def _layer_peaks(node, layer_names):
    """``{layer: max value}`` of the named volume layers in ``node``'s geometry."""
    peaks = {}
    for prim in node.geometry().prims():
        name = prim.attribValue("name")
        if name in layer_names:
            peaks[name] = float(prim.volumeMax())
    return peaks


def _import_maps(parent, config, encoding=None, width=None):
    """Import each available output map; chain them so one node holds all layers.

    The height layer comes first (it creates the HeightField at ``width``), the
    rest are added as mask layers. A Volume Wrangle at the end converts heights
    back to Houdini units (``encoding``), normalizes the masks to 0..1 and copies
    ``config.mask_layer`` into ``mask``.

    Returns ``(final_node, imported_layers, skipped_layers, mask_peaks)``.
    """
    imported = {}
    skipped = []
    previous = None
    layers = sorted(config.output_maps, key=lambda name: name != "height")
    for layer_name in layers:
        path = find_output_file(config, layer_name)
        if path is None:
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
    mask_names = [name for name in imported if name != "height"]
    mask_peaks = _layer_peaks(previous, mask_names)
    mask_layer = config.mask_layer if config.mask_layer in imported else ""

    # Replaced by gaea_import_postprocess; drop it so it doesn't linger unwired.
    legacy = parent.node("gaea_import_height_rescale")
    if legacy is not None:
        legacy.destroy()
    post = _get_or_create_node(parent, ("volumewrangle",), "gaea_import_postprocess")
    post.parm("snippet").set(import_postprocess_snippet(
        encoding if "height" in imported else None, mask_peaks, mask_layer
    ))
    post.setInput(0, previous)
    post.cook(force=True)
    post.setDisplayFlag(True)
    post.setRenderFlag(True)
    return post, imported, skipped, mask_peaks


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _require_node(source_node):
    if source_node is None:
        raise ValueError("A source HeightField SOP node is required")
    if not hasattr(source_node, "parent"):
        raise TypeError("source_node must be a Houdini hou.Node")


def export_for_gaea(source_node, config=None, use_cache=True, mask_node=None):
    """Write the HeightField and the mask to the input EXRs and record a manifest.

    ``mask_node`` is the node's mask input (None = all white). Returns a dict with
    keys: ``input``, ``signature``, ``cache_hit``, ``message``. When ``use_cache``
    is true and nothing relevant changed since the last build, the manifest
    already matches and ``cache_hit`` is True (no Gaea rebuild needed).
    """
    _require_node(source_node)
    config = config or BridgeConfig.load()
    os.makedirs(config.bridge_dir, exist_ok=True)
    os.makedirs(config.output_dir, exist_ok=True)
    with MaskNetwork(source_node, mask_node, config.mask_input_layer) as mask:
        return _export_with_mask(source_node, config, use_cache, mask)


def _export_with_mask(source_node, config, use_cache, mask):
    info = heightfield_info(source_node)
    gaea_width, gaea_height, gaea_source = gaea_world_size(config)
    encoding = height_encoding(
        info["min"], info["max"], info["width"], gaea_width, gaea_height
    )
    image_size = downsampled_resolution(
        info["resolution"][0], info["resolution"][1], config.downsample
    )
    # Heights + how they're encoded for Gaea + how Gaea will build them.
    content_hash = hashlib.sha1((
        info["content_hash"] + _normalise_dict(encoding)
        + _normalise_dict(build_settings(config)) + mask.content_hash()
    ).encode("utf-8")).hexdigest()
    signature = compute_signature(
        config.input_path, config.downsample, config.output_maps, content_hash
    )

    if use_cache and os.path.isfile(config.mask_input_path) and \
            is_cache_valid(config, content_hash):
        return {
            "input": config.input_path,
            "signature": signature,
            "cache_hit": True,
            "encoding": encoding,
            "message": "Cache valid: input and settings are unchanged and Gaea "
            "outputs already exist. No rebuild needed.",
        }

    _export_heightfield(source_node, config.input_path, encoding, image_size)
    coverage = mask.coverage()
    _export_heightfield(mask.output, config.mask_input_path,
                        {"offset": 0.0, "span": 1.0, "max_value": 1.0}, image_size,
                        layer="mask", verify=False)
    mask_info = dict(mask.info, file=config.mask_input_name, coverage=coverage)
    write_manifest(config, signature, extra={
        "mask": mask_info,
        "height_range": [info["min"], info["max"]],
        "houdini_width": info["width"],
        "houdini_resolution": list(info["resolution"]),
        "image_size": list(image_size),
        "gaea_world": {"width": gaea_width, "height": gaea_height,
                       "source": gaea_source},
        "height_encoding": encoding,
    })

    notes = ["Mask: {} ({:.0%} of the terrain).".format(
        mask_info.get("layer") and "layer '{}'".format(mask_info["layer"])
        or mask_info["source"], coverage)]
    if mask.mask_input is not None and coverage < 1e-4:
        notes.append("WARNING: the mask is empty here; Gaea won't change anything. "
                     "Check that it overlaps the terrain.")
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
    merged, imported, skipped, mask_peaks = _import_maps(
        source_node.parent(), config, encoding=encoding, width=width
    )
    return {
        "node": merged,
        "imported": imported,
        "skipped": skipped,
        "mask_peaks": mask_peaks,
        "message": import_message(
            list(imported), skipped, mask_peaks, config.mask_layer, bool(encoding)
        ),
    }


def import_message(imported, skipped, mask_peaks, mask_layer, has_encoding):
    """Status line for an import."""
    parts = ["Imported layers: {}.".format(", ".join(imported) or "none")]
    if mask_peaks:
        parts.append("Gaea mask peaks: {}.".format(", ".join(
            "{} {:.3g}".format(name, peak) for name, peak in sorted(mask_peaks.items())
        )))
    if mask_layer in imported:
        parts.append("Mask output = {}.".format(mask_layer))
    if skipped:
        parts.append("Not found: {}.".format(", ".join(skipped)))
    if "height" in imported and not has_encoding:
        parts.append("WARNING: no height encoding in the manifest (export again); "
                     "heights left in Gaea's 0..1 range.")
    return "  ".join(parts)


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

    downsample = _eval("downsample", "", as_string=True)
    mask_layer = _eval("mask_layer", None, as_string=True)
    if mask_layer == "none":
        mask_layer = ""
    return {
        "bridge_dir": _eval("bridge_dir", None, as_string=True) or None,
        "downsample": int(downsample) if downsample else None,
        "gaea_project": _eval("gaea_project", None, as_string=True),
        "gaea_exe": _eval("gaea_exe", None, as_string=True),
        "gaea_width": _eval("gaea_width", None),
        "gaea_height": _eval("gaea_height", None),
        "mask_layer": mask_layer,
        "mask_input_layer": _eval("mask_input_layer", None, as_string=True) or None,
        "gaea_swarm": _eval("gaea_swarm", None, as_string=True) or None,
        "build_resolution": _int_or_none(_eval("build_resolution", None, as_string=True)),
        "ignore_cache": _bool_or_none(_eval("ignore_cache", None)),
        "show_console": _bool_or_none(_eval("show_console", None)),
        "use_template": _bool_or_none(_eval("use_template", None)),
        "erosion_node": _eval("erosion_node", None, as_string=True),
        "erosion_params": _erosion_params_from_node(node) or None,
        "use_tint": _bool_or_none(_eval("use_tint", None)),
        "tint_params": _tint_params_from_node(node) or None,
    }


EROSION_PARM_PREFIX = "ero_"


def _erosion_params_from_node(node):
    """{Gaea property: value} from the node's ero_* parameters (missing ones are
    skipped)."""
    values = {}
    for name in EROSION2_DEFAULTS:
        parm = node.parm(EROSION_PARM_PREFIX + name)
        if parm is not None:
            values[name] = _coerce(name, parm.eval())
    return values


def read_template_into_node(node, defaults=False):
    """Fill the node's Erosion parameters from its template project (or with Gaea's
    defaults). Returns a status message."""
    if defaults:
        values = dict(EROSION2_DEFAULTS)
        source = "Gaea defaults"
    else:
        config = config_from_node(node)
        if not config.gaea_project or not os.path.isfile(config.gaea_project):
            message = "Set Gaea Project on the Paths tab to read the template."
            _set_status(node, message)
            return message
        values = read_erosion_params(config.gaea_project, config.erosion_node)
        source = os.path.basename(config.gaea_project)
    for name, value in values.items():
        parm = node.parm(EROSION_PARM_PREFIX + name)
        if parm is not None:
            parm.set(int(value) if isinstance(value, bool) else value)
    if node.parm("_ero_loaded") is not None:
        node.parm("_ero_loaded").set(1)
    message = "Erosion parameters loaded from {}.".format(source)
    _set_status(node, message)
    return message


def on_template_toggled(node):
    """Turning the template mode on for the first time reads the project values."""
    if node.evalParm("use_template") and not node.evalParm("_ero_loaded"):
        try:
            return read_template_into_node(node)
        except (ValueError, OSError) as error:
            message = "Could not read the template: {}".format(error)
            _set_status(node, message)
            return message
    return None


TINT_PARM_PREFIX = "tint_"


def tint_parm_name(layer, name):
    """HDA parameter of a layer's Tint value, e.g. tint_flow_Start."""
    return "{}{}_{}".format(TINT_PARM_PREFIX, layer, name)


def _tint_params_from_node(node):
    """{layer: {Gaea property: value}} from the node's tint_<layer>_* parameters."""
    params = {}
    for layer in TINT_LAYERS:
        values = {}
        for name, _, _, kind, _, _, _ in TINT_PARAMS:
            parm_name = tint_parm_name(layer, name)
            if kind == "color":
                parm = node.parmTuple(parm_name)
                if parm is not None:
                    values[name] = _coerce_kind(kind, parm.eval())
            else:
                parm = node.parm(parm_name)
                if parm is not None:
                    values[name] = _coerce_kind(
                        kind, parm.evalAsString() if kind == "enum" else parm.eval())
        if values:
            params[layer] = values
    return params


def read_tint_into_node(node, defaults=False):
    """Fill the node's per-layer Tint parameters from the project's Tint nodes
    (or with Gaea's defaults)."""
    if defaults:
        defaults_values = {name: _coerce_kind(TINT_TYPES[name], value)
                           for name, value in TINT_DEFAULTS.items()}
        per_layer = {layer: defaults_values for layer in TINT_LAYERS}
        source = "Gaea defaults"
    else:
        config = config_from_node(node)
        if not config.gaea_project or not os.path.isfile(config.gaea_project):
            message = "Set Gaea Project on the Paths tab to read the Tint nodes."
            _set_status(node, message)
            return message
        per_layer = read_tint_params(config.gaea_project, config)
        source = os.path.basename(config.gaea_project)
    for layer, values in per_layer.items():
        for name, value in values.items():
            kind = TINT_TYPES[name]
            parm_name = tint_parm_name(layer, name)
            parm = node.parmTuple(parm_name) if kind == "color" else node.parm(parm_name)
            if parm is None:
                continue
            parm.set(int(value) if kind == "bool" else value)
    _set_parm_if_present(node, "_tint_loaded", 1)
    if not per_layer:
        message = ("No Tint nodes found in {} (a Tint feeding an Export named "
                   "output_tint_height/flow/wear/sediment).".format(source))
    else:
        message = "Tint parameters ({}) loaded from {}.".format(
            ", ".join(layer for layer in TINT_LAYERS if layer in per_layer), source)
    _set_status(node, message)
    return message


def on_tint_toggled(node):
    """Turning Use Tint Parameters on for the first time reads the project values."""
    if node.evalParm("use_tint") and not node.evalParm("_tint_loaded"):
        try:
            return read_tint_into_node(node)
        except (ValueError, OSError) as error:
            message = "Could not read the Tint node: {}".format(error)
            _set_status(node, message)
            return message
    return None


# Tint menu: "none" or a layer. show_tint (hidden) holds what is displayed:
# 0 = nothing, 1..4 = TINT_LAYERS[show_tint - 1]; only the buttons set it.
TINT_MENU = ("none",) + TINT_LAYERS


def _tint_layer(node):
    """The Tint menu's choice: "none" or a layer name."""
    parm = node.parm("tint_layer")
    value = parm.evalAsString() if parm is not None else "none"
    return value if value in TINT_MENU else "none"


def refresh_viewport():
    """Redraw the scene viewers. Never runs "glcache -c": the viewport draws
    HeightFields from textures too, and an unchanged terrain isn't uploaded again,
    so it vanishes. New tint maps get new file names instead (tint_display_path)."""
    import hou

    if not hou.isUIAvailable():
        return
    try:
        for pane in hou.ui.paneTabs():
            if pane.type() == hou.paneTabType.SceneViewer:
                for viewport in pane.viewports():
                    viewport.draw()
    except Exception:  # noqa: BLE001 - a redraw is only a nicety
        pass


def _set_tint_display(node, index):
    """Switch the Terrain output's display (0 = no tint, 1..4 = a layer's Quick
    Shade), recook and redraw."""
    _set_parm_if_present(node, "show_tint", index)
    node.cook(force=True)
    refresh_viewport()


def reset_quick_shade(node):
    """Reset: no tint on the terrain (the Tint menu is left as it is)."""
    _set_tint_display(node, 0)
    message = "Tint display reset."
    _set_status(node, message)
    return message


def load_quick_shade(node):
    """Load Tint: show the tint chosen in the Tint menu as the Terrain output's
    mask (luminance of the Tint map); None hides it.

    Every press loads the map again: the current tint is taken off and the
    layer's map is re-read from the output folder under a new file name, so
    pressing Load again reloads the tint. Returns the status message.
    """
    layer = _tint_layer(node)
    if layer == "none":
        _set_tint_display(node, 0)
        message = "Tint hidden."
        _set_status(node, message)
        return message
    _set_tint_display(node, 0)
    source = None
    if node.evalParm("_imported"):
        config = config_from_node(node)
        source = find_tint_file(config, layer)
    if source is None:
        _set_parm_if_present(node, "_tint_file_" + layer, "")
        message = ("No {0} tint map: build and import with an Export named "
                   "output_tint_{0} after its Tint node.".format(layer))
        _set_status(node, message)
        return message
    path = tint_display_path(config, layer, source, fresh=True)
    _set_parm_if_present(node, "_tint_file_" + layer, path)
    _set_tint_display(node, TINT_LAYERS.index(layer) + 1)
    message = "Tint loaded: {} ({}).".format(layer, os.path.basename(path).strip())
    _set_status(node, message)
    return message


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value):
    return None if value is None else bool(value)


def config_from_node(node):
    """Build a BridgeConfig for an HDA node: defaults -> JSON file -> node parms."""
    return BridgeConfig.load(overrides=overrides_from_node(node))


def node_input(node, index):
    """The node wired into input ``index``, or None."""
    inputs = node.inputs()
    return inputs[index] if index < len(inputs) else None


def export_from_node(node, use_cache=True):
    """Export the HDA node's first input (terrain) and second input (mask)."""
    source = node_input(node, 0)
    if source is None:
        raise RuntimeError("Connect a HeightField into the Gaea Erosion2 Bridge input.")
    return export_for_gaea(source, config_from_node(node), use_cache=use_cache,
                           mask_node=node_input(node, 1))


# Nodes the pre-HDA import created next to the bridge node; superseded by the
# network inside the asset.
LEGACY_IMPORT_NODES = (
    "gaea_import_height", "gaea_import_erosion", "gaea_import_flow",
    "gaea_import_wear", "gaea_import_sediment", "gaea_import_postprocess",
    "gaea_import_height_rescale",
)


def _remove_legacy_import_nodes(parent):
    removed = []
    for name in LEGACY_IMPORT_NODES:
        node = parent.node(name)
        if node is not None and node.type().name() in (
            HEIGHTFIELD_FILE_NODE_TYPES + ("volumewrangle",)
        ):
            node.destroy()
            removed.append(name)
    return removed


def import_into_node(node, config=None, set_display=True):
    """Import Gaea's outputs into a ``gaea_erosion_bridge`` node.

    Only the node's hidden parameters change: the found file paths, the height
    encoding from the manifest and each mask's peak. The network inside the asset
    reads them, so all mask settings on the node stay live. Output 1 (Terrain)
    carries the height; outputs 2-4 carry flow, wear and sediment as ``mask``.
    """
    import hou

    if node.parm("_imported") is None:
        raise RuntimeError(
            "{} is not an up-to-date Gaea Erosion2 Bridge node. Rebuild the HDA "
            "(build_gaea_bridge_hda.py).".format(node.path())
        )
    config = config or config_from_node(node)
    manifest = read_manifest(config) or {}
    encoding = manifest.get("height_encoding") or {}

    layers = ("height",) + MASK_LAYERS
    found = {
        layer: find_output_file(config, layer) if layer in config.output_maps else None
        for layer in layers
    }
    if not any(found.values()):
        raise RuntimeError(
            "No Gaea output maps were found in {}. Build the project in Gaea "
            "(F8) first, then import.".format(config.output_dir)
        )
    stamp = output_stamp(config)

    with hou.undos.group("Gaea Erosion2 Bridge: import"):
        _set_parm_if_present(node, "_import_stamp", stamp)
        for layer in layers:
            node.parm("_{}_file".format(layer)).set(found[layer] or "")
        for layer in MASK_LAYERS:
            node.parm("_{}_peak".format(layer)).set(0.0)
        node.parm("_enc_span").set(float(encoding.get("span", 0.0)))
        node.parm("_enc_offset").set(float(encoding.get("offset", 0.0)))
        node.parm("_houdini_width").set(float(manifest.get("houdini_width") or 0.0))
        tint = find_tint_files(config)
        for layer in TINT_LAYERS:
            # A per-build file name, so the viewport loads the new map by itself.
            _set_parm_if_present(node, "_tint_file_" + layer,
                                 tint_display_path(config, layer, tint[layer])
                                 if layer in tint else "")
        node.parm("_imported").set(1)

        # Same path as last time -> make the file nodes re-read the new build.
        for layer in layers:
            child = node.node("file_" + layer)
            if child is not None and found[layer] and child.parm("reload") is not None:
                child.parm("reload").pressButton()

        # Peaks of the raw Gaea values drive Normalize. Read them from each file
        # node directly, so a mask that is currently disabled still gets its peak.
        peaks = {}
        for layer in MASK_LAYERS:
            child = node.node("file_" + layer)
            if found[layer] and child is not None:
                child.cook(force=True)
                peaks.update(_layer_peaks(child, [layer]))
        for layer, peak in peaks.items():
            node.parm("_{}_peak".format(layer)).set(peak)

        node.cook(force=True)
        removed = _remove_legacy_import_nodes(node.parent())
        if set_display:
            node.setDisplayFlag(True)
            node.setRenderFlag(True)

    imported = [layer for layer in layers if found[layer]]
    skipped = [layer for layer in layers if not found[layer]]
    message = import_message(
        imported, skipped, peaks, "", bool(encoding.get("span"))
    )
    if tint:
        message += "  Tint maps loaded: {} (Tint tab > Load Tint).".format(
            ", ".join(layer for layer in TINT_LAYERS if layer in tint))
    if removed:
        message += "  Removed old import nodes: {}.".format(", ".join(removed))
    return {"node": node, "imported": imported, "skipped": skipped,
            "mask_peaks": peaks, "tint": tint, "message": message}


def clear_node_import(node):
    """Forget the imported files: the node passes its input through again.

    Also switches Auto Import off, otherwise it would bring the files right back.
    """
    if node.parm("auto_import") is not None and node.parm("auto_import").eval():
        node.parm("auto_import").set(0)
        stop_auto_import(node)
    node.parm("_imported").set(0)
    _set_parm_if_present(node, "_import_stamp", "")
    for layer in ("height",) + MASK_LAYERS:
        node.parm("_{}_file".format(layer)).set("")
    for layer in TINT_LAYERS:
        _set_parm_if_present(node, "_tint_file_" + layer, "")
    _set_parm_if_present(node, "show_tint", 0)


# --------------------------------------------------------------------------- #
# Auto Import: re-import when Gaea writes a new build
# --------------------------------------------------------------------------- #

AUTO_IMPORT_INTERVAL = 1.0  # seconds between checks of the output folder
AUTO_IMPORT_SETTLE = 1.5    # files must be unchanged this long before importing
AUTO_IMPORT_ERROR_BACKOFF = 5.0


def output_stamp(config):
    """Fingerprint of the Gaea outputs on disk: name, size and mtime of each file.

    Changes whenever Gaea writes a new build. Empty string when nothing is there.
    """
    entries = []
    for layer in sorted(config.output_maps):
        path = find_output_file(config, layer)
        if path is None:
            continue
        try:
            info = os.stat(path)
        except OSError:
            continue
        entries.append([layer, os.path.basename(path), info.st_size,
                        info.st_mtime_ns // 1000000])
    return json.dumps(entries) if entries else ""


class SettleDetector:
    """Decides when a new set of Gaea outputs is complete enough to import.

    Gaea writes the Export files one after another, so the folder is "changed but
    still being written" for a moment. ``update`` returns True only once the
    fingerprint differs from the last import *and* has stayed the same for
    ``settle`` seconds.
    """

    def __init__(self, settle=AUTO_IMPORT_SETTLE):
        self.settle = settle
        self._candidate = None
        self._since = 0.0

    def update(self, stamp, imported_stamp, now):
        if not stamp or stamp == imported_stamp:
            self._candidate = None
            return False
        if stamp != self._candidate:
            self._candidate = stamp
            self._since = now
            return False
        return now - self._since >= self.settle


def _auto_import_registry():
    """Watchers by node session id. Kept on hou.session so a module reload doesn't
    orphan running callbacks."""
    import hou

    registry = getattr(hou.session, "_ghb_auto_import", None)
    if registry is None:
        registry = {}
        hou.session._ghb_auto_import = registry
    return registry


def auto_import_step(node, detector, now):
    """One check of the output folder for ``node``. Imports when a new build has
    settled; returns the status message then, else None."""
    import hou

    if is_building(node):  # a Swarm build imports by itself when it finishes
        return None
    config = config_from_node(node)
    if detector.update(output_stamp(config), node.evalParm("_import_stamp"), now):
        with hou.undos.disabler():
            result = import_into_node(node, config, set_display=False)
        return "Auto-imported {}.  {}".format(time.strftime("%H:%M:%S"),
                                              result["message"])
    return None


def stop_auto_import(node_or_id):
    """Stop watching for a node (or its session id). Safe to call when idle."""
    import hou

    key = node_or_id if isinstance(node_or_id, int) else node_or_id.sessionId()
    callback = _auto_import_registry().pop(key, None)
    if callback is not None and hou.isUIAvailable():
        try:
            hou.ui.removeEventLoopCallback(callback)
        except hou.OperationFailed:
            pass


def start_auto_import(node):
    """Watch the node's output folder from Houdini's UI event loop.

    Returns False without a UI (hython, batch): there is no event loop to use.
    """
    import hou

    if not hou.isUIAvailable():
        return False
    key = node.sessionId()
    stop_auto_import(key)
    detector = SettleDetector()
    state = {"next": 0.0}

    def tick():
        now = time.monotonic()
        if now < state["next"]:
            return
        state["next"] = now + AUTO_IMPORT_INTERVAL
        target = hou.nodeBySessionId(key)
        try:
            if target is None or not target.evalParm("auto_import"):
                stop_auto_import(key)
                return
            message = auto_import_step(target, detector, now)
        except hou.ObjectWasDeleted:
            stop_auto_import(key)
            return
        except Exception as error:  # keep watching; Gaea may still be writing
            state["next"] = now + AUTO_IMPORT_ERROR_BACKOFF
            message = "Auto import failed: {}".format(error)
        if message:
            try:
                set_status(target, message)
            except hou.ObjectWasDeleted:
                stop_auto_import(key)

    hou.ui.addEventLoopCallback(tick)
    _auto_import_registry()[key] = tick
    return True


# --------------------------------------------------------------------------- #
# Build in Gaea from Houdini (Gaea.Swarm.exe)
# --------------------------------------------------------------------------- #

BUILD_POLL_INTERVAL = 0.5


def _build_registry():
    """Running builds by node session id (on hou.session, survives reloads)."""
    import hou

    registry = getattr(hou.session, "_ghb_builds", None)
    if registry is None:
        registry = {}
        hou.session._ghb_builds = registry
    return registry


def is_building(node):
    entry = _build_registry().get(node.sessionId())
    return entry is not None and entry["job"].poll() is None


STATUS_PARMS = ("_status", "_status_erosion", "_status_tint")  # per tab


def set_status(node, message):
    """Show ``message`` in every Status field of the node and Houdini's status bar."""
    import hou

    try:
        for name in STATUS_PARMS:
            parm = node.parm(name)
            if parm is not None:
                parm.set(message)
    except hou.ObjectWasDeleted:
        return
    if hou.isUIAvailable():
        hou.ui.setStatusMessage(message)


_set_status = set_status


def _finish_build(node, job, then_import):
    """Report a finished build and import its result."""
    import hou

    ok, message = job.result_message()
    message = message.replace("Gaea build", "Gaea build" + job.note, 1)
    if ok and then_import:
        with hou.undos.disabler():
            result = import_into_node(node, job.config, set_display=False)
        message += "  " + result["message"]
    elif ok:
        message += "  Press 3. Import Results to load it."
    _set_status(node, message)
    return ok, message


def _call_done(on_done, ok):
    if on_done is not None:
        try:
            on_done(ok)
        except Exception:  # a finished build must never raise from here
            pass


def build_in_gaea(node, then_import=None, wait=None, on_done=None):
    """Build the node's Gaea project with Gaea.Swarm.exe, then import the result.

    ``then_import`` None follows the node's Auto Import After Build toggle. In the
    Houdini UI the build runs in the background: Status shows the elapsed time and
    the import happens when Swarm exits. Without a UI (hython) it blocks until
    done. ``on_done(ok)`` is called once the build is over (also when it couldn't
    start, failed or was cancelled). Returns the SwarmBuild, or None when the
    build couldn't start.
    """
    import hou

    if then_import is None:
        parm = node.parm("import_after_build")
        then_import = True if parm is None else bool(parm.eval())
    if is_building(node):
        _set_status(node, "A Gaea build is already running for this node.")
        _call_done(on_done, False)
        return None
    config = config_from_node(node)
    problems = validate_build(config)
    if problems:
        _set_status(node, "Cannot build: " + "  ".join(problems))
        _call_done(on_done, False)
        return None

    note = ""
    command = None
    if config.builds_copy:
        copy = template_build_path(config)
        changed = write_template_copy(config, copy)
        command = swarm_command(config, project=copy)
        note = " (template; changed: {})".format(", ".join(changed) or "nothing")
    job = SwarmBuild(config, command=command).start()
    job.note = note
    job.on_done = on_done
    if wait is None:
        wait = not hou.isUIAvailable()
    if wait:
        job.wait()
        ok = False
        try:
            ok, _ = _finish_build(node, job, then_import)
        finally:
            _call_done(on_done, ok)
        return job

    key = node.sessionId()
    state = {"next": 0.0}

    def tick():
        now = time.monotonic()
        if now < state["next"]:
            return
        state["next"] = now + BUILD_POLL_INTERVAL
        target = hou.nodeBySessionId(key)
        if target is None:
            job.cancel()
            _stop_build_watch(key)
            return
        code = job.poll()
        if code is None:
            _set_status(target, "Building in Gaea{}... {:.0f} s".format(
                job.note, job.elapsed))
            return
        _stop_build_watch(key)
        ok = False
        try:
            ok, _ = _finish_build(target, job, then_import)
        except Exception as error:
            _set_status(target, "Gaea build finished, but import failed: {}".format(
                error))
        finally:
            _call_done(job.on_done, ok)

    _build_registry()[key] = {"job": job, "callback": tick}
    hou.ui.addEventLoopCallback(tick)
    _set_status(node, "Building in Gaea{}...".format(note))
    return job


def _stop_build_watch(key):
    import hou

    entry = _build_registry().pop(key, None)
    if entry and hou.isUIAvailable():
        try:
            hou.ui.removeEventLoopCallback(entry["callback"])
        except hou.OperationFailed:
            pass
    return entry


def cancel_build(node):
    """Stop the node's running Swarm build (or a pending auto rebuild) and unlock."""
    _cancel_pending_rebuild(node.sessionId())
    entry = _stop_build_watch(node.sessionId())
    if entry is None or entry["job"].poll() is not None:
        set_locked(node, False)
        _set_status(node, "No Gaea build is running.")
        return False
    entry["job"].cancel()
    _call_done(getattr(entry["job"], "on_done", None), False)
    set_locked(node, False)
    _set_status(node, "Gaea build cancelled after {:.0f} s.".format(
        entry["job"].elapsed))
    return True


def run_full_cycle(node, wait=None, on_done=None):
    """Export -> build in Gaea (Swarm) -> import, all from the node.

    When the export cache says nothing changed and the outputs exist, the build is
    skipped and the existing outputs are imported. ``on_done(ok)`` is called when
    everything is over (see ``build_in_gaea``).
    """
    try:
        export = export_from_node(node)
        config = config_from_node(node)
        if export["cache_hit"]:
            result = import_into_node(node, config)
            _set_status(node, "Nothing changed since the last build.  "
                        + result["message"])
            _call_done(on_done, True)
            return None
    except Exception:
        _call_done(on_done, False)
        raise
    _set_status(node, export["message"])
    return build_in_gaea(node, then_import=True, wait=wait, on_done=on_done)


# --------------------------------------------------------------------------- #
# Auto Build on Change (Erosion tab) and node locking
# --------------------------------------------------------------------------- #

AUTO_REBUILD_DELAY = 0.75  # s after the last change (a slider drag fires many)


def set_locked(node, locked):
    """Lock or unlock every parameter of the node (they are disabled while the
    hidden _locked toggle is on; Cancel Build stays available)."""
    import hou

    try:
        parm = node.parm("_locked")
        if parm is not None:
            parm.set(1 if locked else 0)
    except hou.ObjectWasDeleted:
        pass


def _pending_registry():
    import hou

    registry = getattr(hou.session, "_ghb_pending_rebuilds", None)
    if registry is None:
        registry = {}
        hou.session._ghb_pending_rebuilds = registry
    return registry


def _cancel_pending_rebuild(key):
    import hou

    entry = _pending_registry().pop(key, None)
    if entry and hou.isUIAvailable():
        try:
            hou.ui.removeEventLoopCallback(entry["callback"])
        except hou.OperationFailed:
            pass
    return entry is not None


def _start_auto_rebuild(node):
    import hou

    key = node.sessionId()

    def done(ok):
        target = hou.nodeBySessionId(key)
        if target is not None:
            set_locked(target, False)

    try:
        run_full_cycle(node, on_done=done)
    except Exception as error:
        set_locked(node, False)
        _set_status(node, "Auto build failed: {}".format(error))


def on_erosion_parm_changed(node):
    """Callback of every Erosion and Tint parameter: with Auto Build on Change,
    lock the node and run Export > Build > Import once the values stop changing."""
    import hou

    toggle = node.parm("auto_rebuild")
    driven = node.evalParm("use_template") or (
        node.parm("use_tint") is not None and node.evalParm("use_tint"))
    if toggle is None or not toggle.eval() or not driven:
        return False
    set_locked(node, True)
    if not hou.isUIAvailable():
        _start_auto_rebuild(node)
        return True

    key = node.sessionId()
    registry = _pending_registry()
    due = time.monotonic() + AUTO_REBUILD_DELAY
    if key in registry:
        registry[key]["due"] = due
        return True

    def tick():
        entry = registry.get(key)
        if entry is None or time.monotonic() < entry["due"]:
            return
        _cancel_pending_rebuild(key)
        target = hou.nodeBySessionId(key)
        if target is not None:
            _start_auto_rebuild(target)

    registry[key] = {"due": due, "callback": tick}
    hou.ui.addEventLoopCallback(tick)
    _set_status(node, "Erosion changed: rebuilding in Gaea. The node is locked until "
                      "the result is imported.")
    return True


def unlock_after_load(node):
    """Builds don't survive closing a scene: never reopen a node locked."""
    set_locked(node, False)


def sync_auto_import(node):
    """Start or stop watching to match the node's Auto Import toggle. Called by
    the toggle and when a scene with the node is loaded."""
    if node.parm("auto_import") is not None and node.evalParm("auto_import"):
        return start_auto_import(node)
    stop_auto_import(node)
    return False


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
