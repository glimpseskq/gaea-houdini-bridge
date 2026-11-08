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
- **Terrain and mask as separate outputs.** Output 1 is the eroded terrain; output 2 is
  the chosen Gaea mask (`flow`, `wear` or `sediment`) as a HeightField mask, ready for the
  Mask input of any HeightField node. Missing files are skipped, not treated as errors.
- **1:1 proportions.** Heights are rescaled to Gaea's world size, so the terrain looks the
  same in both apps and returns to Houdini at its original scale.
- **Resolution control.** Export at full, 1/2, 1/3 or 1/4 of the HeightField resolution.
- **Hash-based cache.** The bridge computes a signature from the input, resolution and
  layer set. If nothing has changed and the Gaea outputs already exist, it tells you no
  rebuild is needed.
- **Job manifest.** `gh_bridge_manifest.json` records what was exported, at which
  resolution and with which height encoding.
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
`gaea_erosion_bridge.hda` from that folder. After updating the repository, run the build
again (and reload the module or restart Houdini); existing nodes pick up the new version.

## Usage

### In Houdini: export

1. Build a terrain, for example `heightfield` ▸ `heightfield_noise`.
2. Create a **Gaea Erosion Bridge** node and connect the HeightField to its input.
3. On the **Paths** tab, set **Bridge Dir** (default: `C:/Temp/gh_bridge`) and
   **Gaea Project** (`.terrain`; the bridge reads the Gaea world size from it). For the
   **Open Project in Gaea** button, also set **Gaea Exe**.
4. On the **Bridge** tab, pick an **Export Resolution**: Full, 1/2, 1/3 or 1/4 of the
   HeightField resolution.
5. Press **1. Export to Gaea**. `Bridge Dir` now contains `input_height.exr`,
   `gh_bridge_manifest.json` and an empty `output/` folder.

   The `height` layer is written as a grayscale RGB EXR. Heights are rescaled to Gaea's
   world size so the terrain keeps its proportions 1:1 (see [Height scale](#height-scale)).

### In Gaea: build

1. Open the project (manually or with the **Open Project in Gaea** button).
2. Load `input_height.exr` with a **File** node and build your erosion graph.
3. Attach an **Export** node to each output you need. On every Export node set Format
   **EXR**, Location **Custom**, Output Path `<Bridge Dir>/output` (a folder) and
   If File Exists **Overwrite**, then rename the node: in Custom mode Gaea names the file
   after the node.
4. Save the project and press **Build (F8)**.

| Erosion2 output | Export node name | Houdini layer |
|-----------------|------------------|---------------|
| Out       | `output_height`   | `height`   |
| Flow      | `output_flow`     | `flow`     |
| Wear      | `output_wear`     | `wear`     |
| Deposits  | `output_sediment` | `sediment` |

You don't have to wire up every output: the bridge imports whatever it finds. File names
are matched ignoring case and leading/trailing spaces.

### In Houdini: import

Press **2. Import Results**. From then on the **Gaea Erosion Bridge** node itself outputs
the result (no extra nodes are created in your network) on two outputs:

| Output | Contents |
|--------|----------|
| 1 · **Terrain** | The HeightField from Gaea: `height` (plus an empty `mask`), no Gaea masks. |
| 2 · **Mask** | The mask chosen in **Mask Output**, as the `mask` layer on the same `height`. |

Output 2 has the standard HeightField layout, so you can wire it into the **Mask** input
(second input) of HeightField nodes such as Noise, Erode or Distort, or process it on its
own (Blur, Mask by Feature…) before using it. Viewing it shows the mask in red on the
terrain.

The **Status** field lists what was found. The terrain comes back at its original world
size and height; its resolution is that of the images Gaea exported.

Import only stores the found files in the node; all settings on the **Gaea Layers** tab
work live, without importing again. After a new Gaea build, press **2. Import Results**
again to load it, or turn on **Auto Import** and just press F8 in Gaea. **Clear Import**
makes the node pass its input through again.

**Auto Import** checks the output folder once a second. When Gaea has written new files
and they have stopped changing for 1.5 s (Gaea writes them one after another), the node
imports them and reports it in Status. The node remembers what it last imported, so with
Auto Import on it also picks up a build made while the scene was closed. It runs in the
Houdini UI only (not in hython or batch renders) and doesn't change your display flag.

### Node parameters

**Bridge tab**

| Parameter | What it does |
|-----------|--------------|
| **Export Resolution** | Full / 1/2 / 1/3 / 1/4 of the input HeightField resolution. The terrain keeps its size; only the pixel count drops (a 500×500 HeightField at 1/2 exports as 250×250). |
| **1. Export to Gaea** / **2. Import Results** | The two steps of the round trip. |
| **Open Project in Gaea** | Launches Gaea with the project from **Paths**. |
| **Auto Import** | Import automatically when Gaea writes a new build (see above). |
| **Clear Import** | Forget the imported files; the node passes its input through. Also turns Auto Import off. |

**Gaea Layers tab**

| Parameter | What it does |
|-----------|--------------|
| **Height** | *Gaea Result*: the eroded terrain. *Input HeightField*: keep the original height (on both outputs) and only take the mask from Gaea. |
| **Mask Output** | Flow / Wear / Sediment: which Gaea mask goes to output 2. Only that one is loaded. |
| **Show Mask on Terrain** | Preview: also put the selected mask into the Terrain output's `mask`, so it shows in red on the terrain. Turn it off before using the Terrain output downstream, otherwise HeightField nodes will use that mask. |
| **Flow / Wear / Sediment** | One settings section per mask, see below. Each keeps its settings, so you can switch between masks; only the selected one is active. |

Each mask section has:

| Setting | Effect |
|---------|--------|
| **Normalize** | Divide by the layer's peak so it spans 0..1. On by default: Gaea writes some masks with tiny values (`wear` peaks around 0.05), which would look empty. The raw peaks are shown in Status. |
| **Input Range** | Min/Max remap: values at or below Min become 0, at or above Max become 1. Raise Min to drop faint areas, lower Max to boost weak ones. |
| **Gamma** | < 1 widens the mask, > 1 tightens it to the strongest areas. |
| **Invert** | 1 − value. |
| **Strength** | Final multiplier. |
| **Blur Radius** | Smooth the final mask (scene units, 0 = off). |

What the masks mean (Erosion2 outputs): **flow** is where water runs (thin channel
networks), **wear** is where material was eroded away, **sediment** is where it was
deposited.

To use several Gaea masks at once, add another Gaea Erosion Bridge node with the same
paths and a different **Mask Output** (pressing Import on it reads the same files).
Converting a HeightField to polygons turns its `mask` into a point attribute.

**Paths tab**

| Parameter | What it does |
|-----------|--------------|
| **Bridge Dir** | Working folder shared with Gaea. |
| **Gaea Project**, **Gaea Exe** | The `.terrain` file (also the source of the world size) and `Gaea.exe`. |
| **Gaea Width / Height** | Gaea world size in metres. 0 = read from the Gaea project. |

The erosion itself is set up and tuned in the Gaea GUI; the node only moves data.
Gaea builds at the resolution in its own Terrain Definition, so lower that too if you
want faster builds.

### Height scale

Gaea displays an image value `v` (0..1) as `v × Height` metres on a terrain `Width`
metres wide (Project ▸ Terrain Definition). The bridge encodes Houdini heights so the
height-to-width ratio is the same in both apps:

```
span = houdini_width × gaea_height / gaea_width
v    = (h − min) / span        # export
h    = v × span + min          # import
```

The Gaea world size is taken from, in order: the **Gaea Terrain Size** parameters on
the node (if non-zero) → the `.terrain` file in **Gaea Project** → 5000 × 2500 m. The
encoding is saved to the manifest (`height_encoding`), and **Import** uses it to restore
the original heights, so the terrain returns at its Houdini scale. If the relief is
taller than Gaea's Height allows, Status shows a warning with the minimum Height to set.

Tip: erosion in Gaea depends on world size. For physically matching results, set Gaea's
Terrain Definition width to your Houdini terrain size; the bridge adapts automatically.

### Cache

On a repeated **Export**, the bridge compares the signature (hash of the input EXR,
resolution and layer set) with the manifest. If everything matches and the output files
are in place, Status reports that no Gaea rebuild is needed. Any change to the terrain,
the resolution or the Gaea world size invalidates the cache.

### Without the HDA: shelf tools

You can also call the module directly, for example from a shelf tool:

```python
import houdini_gaea_bridge as ghb

ghb.run_from_selection()      # export the selected HeightField node
# ... build in Gaea (F8) ...
ghb.import_from_selection()   # import the results next to the selected node
```

Without the HDA, import creates a small chain of `gaea_import_*` nodes next to the
selected node (masks normalized, chosen layer copied into `mask`), without the live
settings of the node.

## Configuration

You can set defaults without editing the code: put a `gh_bridge_config.json` file next to
`houdini_gaea_bridge.py`. Every field is optional.

```json
{
  "bridge_dir": "C:/Temp/gh_bridge",
  "downsample": 2,
  "gaea_exe": "C:/Program Files/QuadSpinner/Gaea 2.0/Gaea.exe",
  "gaea_project": "D:/Projects/erosion.terrain",
  "gaea_width": 0,
  "gaea_height": 0,
  "output_maps": {
    "height": "output_height.exr",
    "flow": "output_flow.exr"
  }
}
```

`downsample` is 1 (full), 2 (1/2), 3 (1/3) or 4 (1/4). `gaea_width`/`gaea_height` of 0 mean
"read from `gaea_project`".

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

Expected result: `Ran 38 tests ... OK`.

## Limitations

- You build in the Gaea GUI by hand. The bridge does not run the computation itself.
- File names on the Gaea Export nodes must match what the bridge expects (see the table
  above or `output_maps` in the config).

---

This project is not affiliated with QuadSpinner or SideFX. Gaea and Houdini are trademarks
of their respective owners.
