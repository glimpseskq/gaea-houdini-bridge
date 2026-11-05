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

# Node-type and parameter candidates (native + SideFX Labs variants).
HEIGHTFIELD_OUTPUT_NODE_TYPES = ("heightfield_output", "labs::heightfield_output")
HEIGHTFIELD_FILE_NODE_TYPES = ("heightfield_file", "labs::heightfield_file")
OUTPUT_FILE_PARM_CANDIDATES = ("output_file", "filename", "file")
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
    resolution: int = 2048
    # Path to the Gaea project (.terrain in Gaea 2.0). Used only to open the GUI.
    gaea_project: str = ""
    # Path to the installed Gaea executable, for the optional "open in GUI" action.
    gaea_exe: str = ""
    # Extra parameters recorded in the manifest and folded into the cache signature
    # (e.g. seed, duration, preset). They do not drive Gaea in manual mode; they make
    # the cache invalidate when you change intent and document the job for Gaea setup.
    params: dict = field(default_factory=dict)

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

def _normalise_params(params):
    """Stable JSON text for a params dict, so the signature is order-independent."""
    return json.dumps(params or {}, sort_keys=True, ensure_ascii=False)


def compute_file_hash(path, chunk_size=1 << 20):
    """SHA-1 of a file's bytes, or ``None`` if the file is missing."""
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_signature(input_path, params, resolution, output_maps):
    """One hash over the input EXR plus the parameters that affect a rebuild.

    If any of these change, the Gaea result is stale and should be rebuilt.
    """
    digest = hashlib.sha1()
    file_hash = compute_file_hash(input_path)
    digest.update((file_hash or "no-input").encode("utf-8"))
    digest.update(_normalise_params(params).encode("utf-8"))
    digest.update(str(resolution).encode("utf-8"))
    digest.update(_normalise_params(dict(output_maps or {})).encode("utf-8"))
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


def write_manifest(config, signature):
    """Persist the job manifest next to the input EXR."""
    import time

    os.makedirs(config.bridge_dir, exist_ok=True)
    manifest = {
        "signature": signature,
        "resolution": config.resolution,
        "input": config.input_name,
        "output_dir": config.output_subdir,
        "output_maps": dict(config.output_maps),
        "params": dict(config.params),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(config.manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return manifest


def outputs_present(config):
    """True if every configured output map exists and is non-empty."""
    for layer_name in config.output_maps:
        path = config.output_path(layer_name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
    return True


def is_cache_valid(config):
    """True if the current input + params match the manifest and outputs exist.

    When this is true a fresh Gaea build is unnecessary: the maps on disk already
    correspond to the current input and parameters.
    """
    manifest = read_manifest(config)
    if not manifest:
        return False
    signature = compute_signature(
        config.input_path, config.params, config.resolution, config.output_maps
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


def _export_heightfield(source_node, input_path):
    parent = source_node.parent()
    os.makedirs(os.path.dirname(input_path), exist_ok=True)

    export_node = _create_node_from_candidates(
        parent, HEIGHTFIELD_OUTPUT_NODE_TYPES, "gaea_bridge_export"
    )
    try:
        export_node.setInput(0, source_node)
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


def _get_or_create_import_node(parent, layer_name):
    node_name = "gaea_import_{}".format(layer_name)
    node = parent.node(node_name)
    if node is not None:
        if node.type().name() not in HEIGHTFIELD_FILE_NODE_TYPES:
            raise RuntimeError(
                "Cannot reuse {}: it is not a HeightField File node".format(node.path())
            )
        return node
    return _create_node_from_candidates(parent, HEIGHTFIELD_FILE_NODE_TYPES, node_name)


def _set_layer_name(node, layer_name):
    """Best-effort: tell the HeightField File node which layer to write into."""
    for parm_name in ("layername", "layer", "name"):
        parm = node.parm(parm_name)
        if parm is not None:
            parm.set(layer_name)
            return


def _import_maps(parent, config):
    """Import each available output map; chain them so one node holds all layers.

    Returns ``(merged_node, imported_layers, skipped_layers)``.
    """
    imported = {}
    skipped = []
    previous = None
    for layer_name in config.output_maps:
        path = config.output_path(layer_name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            skipped.append(layer_name)
            continue

        node = _get_or_create_import_node(parent, layer_name)
        _set_first_available_parm(node, FILE_PARM_CANDIDATES, path)
        _set_layer_name(node, layer_name)
        if previous is not None:
            node.setInput(0, previous)
        node.cook(force=True)
        imported[layer_name] = node
        previous = node

    if not imported:
        raise RuntimeError(
            "No Gaea output maps were found in {}. Build the project in Gaea "
            "(F8) first, then import.".format(config.output_dir)
        )
    if previous is not None:
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

    _export_heightfield(source_node, config.input_path)
    signature = compute_signature(
        config.input_path, config.params, config.resolution, config.output_maps
    )

    if use_cache and is_cache_valid(config):
        return {
            "input": config.input_path,
            "signature": signature,
            "cache_hit": True,
            "message": "Cache valid: input and parameters are unchanged and Gaea "
            "outputs already exist. No rebuild needed.",
        }

    write_manifest(config, signature)
    return {
        "input": config.input_path,
        "signature": signature,
        "cache_hit": False,
        "message": "Exported {} ({}px). Build the project in Gaea (F8), then "
        "import.".format(config.input_path, config.resolution),
    }


def import_gaea_outputs(source_node, config=None):
    """Import the Gaea output EXRs as named layers after a manual GUI build."""
    _require_node(source_node)
    config = config or BridgeConfig.load()
    merged, imported, skipped = _import_maps(source_node.parent(), config)
    return {
        "node": merged,
        "imported": imported,
        "skipped": skipped,
        "message": "Imported layers: {}{}".format(
            ", ".join(imported) or "none",
            "  (skipped: {})".format(", ".join(skipped)) if skipped else "",
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
