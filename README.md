# GH-Bridge — a file-based Gaea ⇄ Houdini bridge

**English** | [Русский](README.ru.md)

A bridge between **SideFX Houdini** and **QuadSpinner Gaea 2.0** that works through files
on disk. It automates the Houdini side: it exports a HeightField for Gaea and brings the
finished erosion maps back as named HeightField layers.

```
Houdini                         disk                          Gaea 2.0 (GUI)
───────                         ────                          ──────────────
HeightField ── Export ──▶  input_height.exr  ──▶  File node ─▶ erosion graph
                           manifest.json                          │ Build (F8)
HeightField ◀── Import ──  output/output_*.exr ◀── Export nodes ◀─┘
 + height/erosion/flow/wear/sediment layers
```

## Features

- **One-click Export / Import.** The `Gaea Erosion Bridge` HDA puts the UI right in your
  SOP network.
- **Named layers.** `height`, `erosion`, `flow`, `wear` and `sediment` arrive in the
  HeightField as separate layers. Missing files are skipped with a warning, not an error.
- **Hash-based cache.** The bridge computes a signature from the input, resolution and
  parameters. If nothing has changed and the Gaea outputs already exist, it tells you no
  rebuild is needed.
- **Job manifest.** `gh_bridge_manifest.json` records what was exported and with which
  parameters.
- **Safe cleanup.** `cleanup_temp` never deletes files outside the bridge working folder.
- **Hands off Gaea.** The bridge does not use the CLI build engine, SwarmHost, licensing or
  copy protection. It only (optionally) launches Gaea as a regular application, so it works
  with any licensed install.

## Requirements

- Houdini with Python 3 (tested with Houdini 20.5).
- Gaea 2.0.
- Windows. The default paths assume Windows, but you can change them in the config or on
  the node.

## Repository contents

| File | Purpose |
|------|---------|
| `houdini_gaea_bridge.py` | Core module: export/import, cache, manifest, cleanup. |
| `build_gaea_bridge_hda.py` | Run once inside Houdini to build the `gaea_erosion_bridge` HDA. |
| `tests/test_bridge_logic.py` | Unit tests for the logic; they do not need a running Houdini. |

## Installation

### 1. Make the module visible to Houdini

Pick one of the options.

**Option A: `houdini.env`** (convenient if you plan to pull updates to the repository).
Add this line to `<Documents>/houdiniXX.X/houdini.env`:

```
PYTHONPATH = "C:/path/to/BridgeHGH;$PYTHONPATH"
```

**Option B: copy the file.** Put `houdini_gaea_bridge.py` into
`<Documents>/houdiniXX.X/scripts/python/` (create the folder if it does not exist).

Restart Houdini and check in the Python Shell:

```python
import houdini_gaea_bridge as ghb; print(ghb.__file__)
```

### 2. Build the HDA

In Houdini, open **Windows ▸ Python Shell** and run:

```python
exec(open(r"C:/path/to/BridgeHGH/build_gaea_bridge_hda.py").read())
```

The console prints `Installed HDA: ...\otls\gaea_erosion_bridge.hda`, and a
**Gaea Erosion Bridge** node appears in the SOP **Tab** menu.

The HDA is installed into your user `otls` folder. To remove the asset, delete
`gaea_erosion_bridge.hda` from that folder.

## Usage

### In Houdini: export

1. Build a terrain, for example `heightfield` ▸ `heightfield_noise`.
2. Create a **Gaea Erosion Bridge** node and connect the HeightField to its input.
3. On the **Paths** tab, set **Bridge Dir** (default: `C:/Temp/gh_bridge`). For the
   **Open Project in Gaea** button, also set **Gaea Project** (`.terrain`) and **Gaea Exe**.
4. Press **1 ▸ Export to Gaea**. `Bridge Dir` now contains `input_height.exr`,
   `gh_bridge_manifest.json` and an empty `output/` folder.

### In Gaea: build

1. Open the project (manually or with the **Open Project in Gaea** button).
2. Load `input_height.exr` with a **File** node and build your erosion graph.
3. Attach **Export** nodes to the outputs you need. Point them at `<Bridge Dir>/output/`
   and use the file names from the table below.
4. Press **Build (F8)**.

| Houdini layer | File in `<Bridge Dir>/output/` |
|---------------|--------------------------------|
| `height`   | `output_height.exr` |
| `erosion`  | `output_erosion.exr` |
| `flow`     | `output_flow.exr` |
| `wear`     | `output_wear.exr` |
| `sediment` | `output_sediment.exr` |

You don't have to wire up every output. Turn off the layers you don't need on the
**Import Layers** tab, and the bridge won't expect them.

### In Houdini: import

Press **2 ▸ Import Results**. The node outputs a HeightField with the layers it found, and
the **Status** field lists the imported and skipped layers.

### Node parameters

| Parameter | What it does |
|-----------|--------------|
| **Gaea Preset** | Which graph you intend to build in Gaea (Erosion / Hydro / Thermal / Texturing). |
| **Resolution** | 1k / 2k / 4k / 8k. |
| **Seed**, **Simulation Controls** | Duration, Erosion Scale, Sedimentation, Rainfall. |
| **Import Layers** | Which layers to expect on import. |
| **Paths** | Bridge working folder, Gaea project, path to `Gaea.exe`. |

> **Note:** Gaea Preset, Seed and Simulation Controls **do not drive Gaea directly**. You
> build and tune the graph by hand in the GUI. These values are written to the manifest as
> a description of the job and are part of the cache hash, so changing them marks the
> result as stale and asks you to rebuild.

### Cache

On a repeated **Export**, the bridge compares the signature (hash of the input EXR,
resolution, parameters and layer set) with the manifest. If everything matches and the
output files are in place, Status reports that no Gaea rebuild is needed. Any change to the
terrain or the parameters invalidates the cache.

### Without the HDA: shelf tools

You can also call the module directly, for example from a shelf tool:

```python
import houdini_gaea_bridge as ghb

ghb.run_from_selection()      # export the selected HeightField node
# ... build in Gaea (F8) ...
ghb.import_from_selection()   # import the results next to the selected node
```

## Configuration

You can set defaults without editing the code: put a `gh_bridge_config.json` file next to
`houdini_gaea_bridge.py`. Every field is optional.

```json
{
  "bridge_dir": "C:/Temp/gh_bridge",
  "resolution": 4096,
  "gaea_exe": "C:/Program Files/QuadSpinner/Gaea 2.0/Gaea.exe",
  "gaea_project": "D:/Projects/erosion.terrain",
  "output_maps": {
    "height": "output_height.exr",
    "flow": "output_flow.exr"
  }
}
```

Precedence, from lowest to highest: defaults in the code → `gh_bridge_config.json` →
HDA node parameters.

## Development and tests

The tests cover the logic that does not depend on `hou`: config and overrides, path
building, the hash signature, the manifest, `is_cache_valid`, and the safety of
`cleanup_temp`.

Run them with any Python 3.7+:

```bash
python tests/test_bridge_logic.py -v
```

or with `hython` from your Houdini install if you have no standalone Python:

```powershell
& "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" tests/test_bridge_logic.py -v
```

Expected result: `Ran 15 tests ... OK`.

## Limitations

- You build in the Gaea GUI by hand. The bridge does not run the computation itself.
- File names on the Gaea Export nodes must match what the bridge expects (see the table
  above or `output_maps` in the config).

---

This project is not affiliated with QuadSpinner or SideFX. Gaea and Houdini are trademarks
of their respective owners.
