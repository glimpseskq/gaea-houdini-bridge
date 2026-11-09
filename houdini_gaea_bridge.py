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
    # Gaea world size in metres. 0 = read it from gaea_project, else the defaults.
    gaea_width: float = 0.0
    gaea_height: float = 0.0
    # Imported layer to copy into the "mask" layer ("" = leave mask alone).
    mask_layer: str = DEFAULT_MASK_LAYER

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
    def usable(path):
        return os.path.isfile(path) and os.path.getsize(path) > 0

    exact = config.output_path(layer_name)
    if usable(exact):
        return exact
    wanted = config.output_maps[layer_name].strip().casefold()
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


def check_project_paths(config):
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
    file_nodes = _collect_nodes(data, "File", [])
    if not any(_same_path(node.get("FileName", ""), config.input_path)
               for node in file_nodes):
        problems.append("No File node in the project loads {} (found: {}).".format(
            config.input_path,
            ", ".join(repr(node.get("FileName", "")) for node in file_nodes) or "none"))

    exports = _collect_nodes(data, "Export", [])
    wanted = {os.path.splitext(name)[0].strip().casefold(): layer
              for layer, name in config.output_maps.items()}
    matched = set()
    for node in exports:
        layer = wanted.get(str(node.get("Name", "")).strip().casefold())
        if layer is None:
            continue
        name = str(node.get("Name", "")).strip()
        if node.get("Location") != "Custom" or not _same_path(
                node.get("OutputPath", ""), config.output_dir):
            problems.append("Export node {!r} should use Location 'Custom' with "
                            "Output Path {}.".format(name, config.output_dir))
        if node.get("Format", "EXR") != "EXR":
            problems.append("Export node {!r} should use Format EXR (is {}).".format(
                name, node.get("Format")))
        matched.add(layer)
    if "height" not in matched:
        problems.append("No Export node named {!r} in the project.".format(
            os.path.splitext(config.output_maps.get("height", "output_height"))[0]))
    return problems


def updated_outputs(config, since):
    """Layers whose output file was (re)written at or after ``since`` (epoch s)."""
    layers = []
    for layer in config.output_maps:
        path = find_output_file(config, layer)
        if path is not None and os.path.getmtime(path) >= since - 1.0:
            layers.append(layer)
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


def _coerce(name, value):
    kind = EROSION2_TYPES[name]
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(round(float(value)))
    return float(value)


def _same_value(name, a, b):
    if EROSION2_TYPES[name] == "float":
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)), abs(float(b)))
    return _coerce(name, a) == _coerce(name, b)


def find_erosion_node(data, node_name=""):
    """The Erosion2 node dict of a parsed .terrain. With several, pick by name."""
    nodes = _collect_nodes(data, "Erosion2", [])
    if not nodes:
        raise ValueError("The Gaea project has no Erosion2 node.")
    if node_name:
        for node in nodes:
            if str(node.get("Name", "")).strip() == node_name.strip():
                return node
        raise ValueError("No Erosion2 node named {!r} (found: {}).".format(
            node_name, ", ".join(repr(n.get("Name")) for n in nodes)))
    if len(nodes) > 1:
        raise ValueError("The project has several Erosion2 nodes ({}); set Erosion "
                         "Node to the one feeding the exports.".format(
                             ", ".join(repr(n.get("Name")) for n in nodes)))
    return nodes[0]


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
    node = find_erosion_node(data, config.erosion_node)
    changed = []
    for name, value in sorted(config.erosion_params.items()):
        if name not in EROSION2_DEFAULTS:
            continue  # unknown parameter: skip
        current = node.get(name, EROSION2_DEFAULTS[name])
        try:
            if _same_value(name, value, current):
                continue
            node[name] = _coerce(name, value)
        except (TypeError, ValueError):
            continue
        changed.append(name)
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
    problems += check_project_paths(config)
    if config.use_template and config.gaea_project and os.path.isfile(
            config.gaea_project):
        try:
            find_erosion_node(_load_project(config.gaea_project), config.erosion_node)
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
            digest = hashlib.sha1(prim.allVoxelsAsString())
            digest.update(repr((tuple(prim.resolution()), tuple(voxel))).encode("utf-8"))
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
    # Heights + how they're encoded for Gaea + how Gaea will build them.
    content_hash = hashlib.sha1((
        info["content_hash"] + _normalise_dict(encoding)
        + _normalise_dict(build_settings(config))
    ).encode("utf-8")).hexdigest()
    signature = compute_signature(
        config.input_path, config.downsample, config.output_maps, content_hash
    )

    if use_cache and is_cache_valid(config, content_hash):
        return {
            "input": config.input_path,
            "signature": signature,
            "cache_hit": True,
            "encoding": encoding,
            "message": "Cache valid: input and settings are unchanged and Gaea "
            "outputs already exist. No rebuild needed.",
        }

    _export_heightfield(source_node, config.input_path, encoding, image_size)
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
        "gaea_swarm": _eval("gaea_swarm", None, as_string=True) or None,
        "build_resolution": _int_or_none(_eval("build_resolution", None, as_string=True)),
        "ignore_cache": _bool_or_none(_eval("ignore_cache", None)),
        "show_console": _bool_or_none(_eval("show_console", None)),
        "use_template": _bool_or_none(_eval("use_template", None)),
        "erosion_node": _eval("erosion_node", None, as_string=True),
        "erosion_params": _erosion_params_from_node(node) or None,
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
            "{} is not an up-to-date Gaea Erosion Bridge node. Rebuild the HDA "
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

    with hou.undos.group("Gaea Erosion Bridge: import"):
        _set_parm_if_present(node, "_import_stamp", stamp)
        for layer in layers:
            node.parm("_{}_file".format(layer)).set(found[layer] or "")
        for layer in MASK_LAYERS:
            node.parm("_{}_peak".format(layer)).set(0.0)
        node.parm("_enc_span").set(float(encoding.get("span", 0.0)))
        node.parm("_enc_offset").set(float(encoding.get("offset", 0.0)))
        node.parm("_houdini_width").set(float(manifest.get("houdini_width") or 0.0))
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
    if removed:
        message += "  Removed old import nodes: {}.".format(", ".join(removed))
    return {"node": node, "imported": imported, "skipped": skipped,
            "mask_peaks": peaks, "message": message}


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


STATUS_PARMS = ("_status", "_status_erosion")  # Bridge tab, Erosion tab


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


def build_in_gaea(node, then_import=None, wait=None):
    """Build the node's Gaea project with Gaea.Swarm.exe, then import the result.

    ``then_import`` None follows the node's Auto Import After Build toggle. In the
    Houdini UI the build runs in the background: Status shows the elapsed time and
    the import happens when Swarm exits. Without a UI (hython) it blocks until
    done. Returns the SwarmBuild, or None when the build couldn't start.
    """
    import hou

    if then_import is None:
        parm = node.parm("import_after_build")
        then_import = True if parm is None else bool(parm.eval())
    if is_building(node):
        _set_status(node, "A Gaea build is already running for this node.")
        return None
    config = config_from_node(node)
    problems = validate_build(config)
    if problems:
        _set_status(node, "Cannot build: " + "  ".join(problems))
        return None

    note = ""
    command = None
    if config.use_template:
        copy = template_build_path(config)
        changed = write_template_copy(config, copy)
        command = swarm_command(config, project=copy)
        note = " (template; changed: {})".format(", ".join(changed) or "nothing")
    job = SwarmBuild(config, command=command).start()
    job.note = note
    if wait is None:
        wait = not hou.isUIAvailable()
    if wait:
        job.wait()
        _finish_build(node, job, then_import)
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
        try:
            _finish_build(target, job, then_import)
        except Exception as error:
            _set_status(target, "Gaea build finished, but import failed: {}".format(
                error))

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
    """Stop the node's running Swarm build, if any."""
    entry = _stop_build_watch(node.sessionId())
    if entry is None or entry["job"].poll() is not None:
        _set_status(node, "No Gaea build is running.")
        return False
    entry["job"].cancel()
    _set_status(node, "Gaea build cancelled after {:.0f} s.".format(
        entry["job"].elapsed))
    return True


def run_full_cycle(node, wait=None):
    """Export -> build in Gaea (Swarm) -> import, all from the node.

    When the export cache says nothing changed and the outputs exist, the build is
    skipped and the existing outputs are imported.
    """
    source = node.inputs()[0] if node.inputs() else None
    if source is None:
        raise RuntimeError("Connect a HeightField into the Gaea Erosion Bridge input.")
    config = config_from_node(node)
    export = export_for_gaea(source, config)
    if export["cache_hit"]:
        result = import_into_node(node, config)
        _set_status(node, "Nothing changed since the last build.  " + result["message"])
        return None
    _set_status(node, export["message"])
    return build_in_gaea(node, then_import=True, wait=wait)


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
