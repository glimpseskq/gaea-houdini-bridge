# GH-Bridge — a file-based Gaea ⇄ Houdini bridge

**English** | [Русский](README.ru.md)

A bridge between **SideFX Houdini** and **QuadSpinner Gaea 2.0** that works through files
on disk. It exports a HeightField for Gaea, builds the Gaea project (in the Gaea GUI, or
from Houdini with Gaea's command-line builder, without opening Gaea) and brings the eroded
terrain and its masks back into Houdini.

```
Houdini                         disk                          Gaea 2.0
───────                         ────                          ────────
HeightField ── Export ──▶  input_height.exr  ──▶  File node ─▶ erosion graph
                           manifest.json                          │ Build: F8 in the GUI,
                                                                  │ or Gaea.Swarm.exe
Terrain + Mask ◀─ Import ─ output/output_*.exr ◀── Export nodes ◀─┘    started by Houdini
```

## Features

- **The whole round trip from Houdini.** One button exports, builds the Gaea project with
  `Gaea.Swarm.exe` in the background and imports the result; Gaea's window never opens.
  Building in the Gaea GUI works too.
- **Terrain and every mask as separate outputs.** Output 1 is the eroded terrain; outputs
  2-4 are the Gaea masks `flow`, `wear` and `sediment`, each as a HeightField mask ready
  for the Mask input of any HeightField node, each with its own settings. Missing files are
  skipped, not treated as errors.
- **1:1 proportions.** Heights are rescaled to Gaea's world size, so the terrain looks the
  same in both apps and returns to Houdini at its original scale.
- **Resolution control.** Export at full, 1/2, 1/3 or 1/4 of the HeightField resolution.
- **Hash-based cache.** The bridge hashes the input heights, resolution and layer set. If
  nothing has changed and the Gaea outputs already exist, it skips the build.
- **Job manifest.** `gh_bridge_manifest.json` records what was exported, at which
  resolution and with which height encoding.
- **Safe cleanup.** `cleanup_temp` never deletes files outside the bridge working folder.
- **Only official Gaea entry points.** The bridge uses files on disk, the Gaea GUI and
  Gaea's documented command-line builder `Gaea.Swarm.exe`. It never touches licensing or
  copy protection: whatever your Gaea edition allows or refuses applies unchanged.

## Requirements

- Houdini 19.5 or newer with Python 3 (developed and tested on 20.5; see
  [Houdini version compatibility](#houdini-version-compatibility)).
- Gaea 2.0.
- Windows. The default paths assume Windows, but you can change them in the config or on
  the node.

## Repository contents

| File | Purpose |
|------|---------|
| `houdini_gaea_bridge.py` | Core module: export/import, cache, manifest, cleanup. |
| `build_gaea_bridge_hda.py` | Run once inside Houdini to build the `gaea_erosion_bridge` HDA. |
| `tests/test_bridge_logic.py` | Unit tests for the logic; they do not need a running Houdini. |
| `tools/houdini_compat_check.py` | Checks that a Houdini version has everything the bridge uses. |

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
2. Create a **Gaea Erosion Bridge** node and connect the HeightField to its first input.
   Optionally connect a mask to the second input (see [Mask input](#mask-input)).
3. On the **Paths** tab, set **Bridge Dir** (default: `C:/Temp/gh_bridge`) and
   **Gaea Project** (`.terrain`; the bridge reads the Gaea world size from it). For the
   **Open Project in Gaea** button, also set **Gaea Exe**.
4. On the **Bridge** tab, pick an **Export Resolution**: Full, 1/2, 1/3 or 1/4 of the
   HeightField resolution.
5. Press **1. Export to Gaea**. `Bridge Dir` now contains `input_height.exr`,
   `input_mask.exr`, `gh_bridge_manifest.json` and an empty `output/` folder.

   The `height` layer is written as a grayscale RGB EXR. Heights are rescaled to Gaea's
   world size so the terrain keeps its proportions 1:1 (see [Height scale](#height-scale)).

### Mask input

The node's second input takes a mask that limits where Gaea erodes. It is written to
`<Bridge Dir>/input_mask.exr`, at the same size as the terrain (0 = untouched,
1 = full effect). Without a second input the mask is white everywhere, so the project
always gets a valid file.

Any mask works. It is resampled onto the terrain's grid, so its resolution, size and
position don't matter; outside its area the mask is 0. What is used (**Mask Input Layer**
on the Bridge tab, default **Auto**):

| Second input | Used as the mask |
|--------------|------------------|
| HeightField with a painted/computed `mask` (Mask by Feature, Paint, …) | its `mask` layer, as is (0.3 stays 0.3) |
| HeightField with an empty `mask` (e.g. `heightfield_noise`) | its `height`, stretched to 0..1 |
| Any other volume (e.g. a fog volume `density`) | that volume, stretched to 0..1 |
| Plain geometry (polygons, curves…) | where the geometry is, seen from above (HeightField Mask by Object) |

Set **Mask Input Layer** to a layer name to force one. Status reports which layer was used
and how much of the terrain it covers, and warns when the mask doesn't overlap the
terrain. The mask is part of the cache: changing it triggers a rebuild.

In Gaea, add a second **File** node and wire it into the **Mask** port of the node it
should limit (e.g. Erosion2). It must load `<Bridge Dir>/input_mask.exr`; with
**Use Erosion Template** the bridge sets that path in the build copy automatically, and
without it a build refuses to start, with the reason in Status, if the path is wrong.

### In Gaea: set up the project (once)

1. Open the project (manually or with the **Open Project in Gaea** button).
2. Load `input_height.exr` with a **File** node and build your erosion graph. For the mask,
   wire a second **File** node loading `input_mask.exr` into Erosion2's **Mask** port.
3. Attach an **Export** node to each output you need. On every Export node set Format
   **EXR**, Location **Custom**, Output Path `<Bridge Dir>/output` (a folder) and
   If File Exists **Overwrite**, then rename the node: in Custom mode Gaea names the file
   after the node.
4. **Save the project.** To build in the GUI, press **Build (F8)**; to build from Houdini,
   see the next section.

| Erosion2 output | Export node name | Houdini layer |
|-----------------|------------------|---------------|
| Out       | `output_height`   | `height`   |
| Flow      | `output_flow`     | `flow`     |
| Wear      | `output_wear`     | `wear`     |
| Deposits  | `output_sediment` | `sediment` |
| Tint (color node) | `output_tint` | shown on the terrain, not a layer (see [Tint](#tint)) |

You don't have to wire up every output: the bridge imports whatever it finds. File names
are matched ignoring case and leading/trailing spaces.

### In Houdini: build without opening Gaea

With **Gaea Project** and **Gaea Exe** set on the **Paths** tab, press
**Export > Build > Import**. The node exports the input, runs `Gaea.Swarm.exe` on the saved
project in the background (Houdini stays responsive; Status counts the seconds) and
imports the result when it finishes. If the input and settings haven't changed since the
last build, the build is skipped and the existing result is imported.

The steps are also available one by one: **1. Export to Gaea**, **2. Build in Gaea**,
**3. Import Results**. **Cancel Build** stops a running build.

Things to know:

- Swarm builds the project **as saved**. Save it in Gaea after editing the graph.
- Before a build the bridge reads the `.terrain` file and refuses to start, with the
  reason in Status, if its File node doesn't load `<Bridge Dir>/input_height.exr` or its
  Export nodes don't write EXR into `<Bridge Dir>/output`.
- Swarm reports success even when it builds nothing, so the bridge judges a build by the
  files it wrote. If none were written, Status says so; turn on **Show Build Window** to
  see Swarm's own messages.
- Gaea.Swarm.exe is found next to **Gaea Exe**; set **Gaea Swarm Exe** if it lives
  elsewhere. Whether command-line builds are available depends on your Gaea edition.
- Without the Houdini UI (hython, scripts) the build runs synchronously.

### Erosion template: tune Gaea's erosion from Houdini

Make a Gaea project once as a **template**: `File` (the bridge input) → your graph →
an **Erosion2** node → the Export nodes. Then turn on **Use Erosion Template** on the
**Erosion** tab. All Erosion2 parameters appear in Houdini, read from the project:

| Group | Parameters |
|-------|------------|
| General | Duration, Downcutting, Erosion Scale, Seed, No Erosion |
| Sediment Discharge | Suspended Load, Bed Load, Coarse Sediments (each with its Discharge Angle), Deposition Boost |
| Shape | Shape, Shape Sharpness, Shape Detail Scale |
| Orographic Influence | Enable, Directional Precipitation, Direction, Rain Shadow, Reverse |

Change them and press **Export > Build > Import** (or **2. Build in Gaea**): the bridge
writes a copy of the project, `<Bridge Dir>/gaea_template_build.terrain`, with your values
and builds that copy with Swarm. The template itself is never modified, so one template
can serve many Houdini scenes with different settings.

- Only values that differ from the template are written; everything else in the project
  (other nodes, unknown parameters) is left as it is.
- **Read from Project** reloads the values saved in the template (it also happens the first
  time you turn the mode on); **Reset to Gaea Defaults** sets Gaea's defaults.
- If the project has several Erosion2 nodes, set **Erosion Node** to the name of the one
  feeding the exports.
- The values are part of the cache: changing any of them makes the next
  **Export > Build > Import** rebuild; unchanged values reuse the last build.
- Orographic Influence's Slope and Altitude ranges are not exposed; they keep the
  template's values.
- With the mode off, Swarm builds the project exactly as saved, and builds in the Gaea GUI
  (F8) never see the Houdini values.
- **Auto Build on Change** (under the Export > Build > Import button on the Erosion tab)
  starts that cycle by itself whenever an Erosion value changes, once you stop changing it
  (0.75 s, so dragging a slider starts one build). From the first change until the result
  is imported, **all parameters of the node are locked**; only **Cancel Build** stays
  available and unlocks the node. A node is never reopened locked after a scene reload.

### Tint

A Gaea **Tint** node (for example on Erosion2's Out) with an Export node named
`output_tint` produces a color map of the terrain. It is built together with everything
else on every build and loaded by the import, but it is never an output or a layer of the
node: it is only shown on the terrain, when **Show Tint on Terrain** is on (Tint tab).
The display is a HeightField Quick Shade material with the color map as base texture, on
the Terrain output; the Terrain geometry itself stays the same.

The **Tint** tab works like the Erosion tab: turn on **Use Tint Parameters** and all Tint
values appear, read from the project (Start and End colors, Hue, Saturation, Lightness,
Enhance None/Autolevel/Equalize, Reverse). They go into the same build copy as the
Erosion values; the project itself is not changed. **Read from Project** /
**Reset to Gaea Defaults** reload the values, **Tint Node** picks the node if there are
several. The values are part of the cache, and **Auto Build on Change** reacts to them too.

Build copies also set the bridge's Export nodes (`output_*`, including `output_tint`) to
Location Custom, Output Path `<Bridge Dir>/output`, Format EXR, so a new Export node only
needs the right name.

### In Houdini: import

Press **3. Import Results** (or let **Build in Gaea** / **Auto Import** do it). From then
on the **Gaea Erosion Bridge** node itself outputs the result (no extra nodes are created
in your network) on four outputs:

| Output | Contents |
|--------|----------|
| 1 · **Terrain** | The HeightField from Gaea: `height` plus `mask` (empty, or the **Show Mask** preview). |
| 2 · **Flow** | The `flow` mask as the `mask` layer on the same `height`. |
| 3 · **Wear** | The `wear` mask, same layout. |
| 4 · **Sediment** | The `sediment` mask, same layout. |

Outputs 2-4 have the standard HeightField layout, so you can wire any of them into the
**Mask** input (second input) of HeightField nodes such as Noise, Erode or Distort, or
process them on their own (Blur, Mask by Feature…) first. Viewing one shows its mask in
red on the terrain. A mask whose file wasn't found comes out empty.

The **Status** field lists what was found. The terrain comes back at its original world
size and height. With **Keep Input Resolution** (on by default) every output is put back
onto the input HeightField's grid with HeightField Layer (Replace), so it has exactly the
input's resolution and voxel size; turn it off to keep the resolution Gaea built at.

Import only stores the found files in the node; all settings on the **Gaea Layers** tab
work live, without importing again. After a new build in the Gaea GUI, press
**3. Import Results** again to load it, or turn on **Auto Import (Gaea GUI builds)** and
just press F8 in Gaea. **Clear Import** makes the node pass its input through again.

**Auto Import After Build** (on by default) imports the result as soon as
**2. Build in Gaea** finishes; turn it off to build without touching the current result.
**Export > Build > Import** always imports.

**Auto Import (Gaea GUI builds)** checks the output folder once a second. When Gaea has written new files
and they have stopped changing for 1.5 s (Gaea writes them one after another), the node
imports them and reports it in Status. The node remembers what it last imported, so with
Auto Import on it also picks up a build made while the scene was closed. It runs in the
Houdini UI only (not in hython or batch renders) and doesn't change your display flag.
It pauses while a **Build in Gaea** is running, since that build imports by itself.

### Node parameters

**Bridge tab**

| Parameter | What it does |
|-----------|--------------|
| **Export Resolution** | Full / 1/2 / 1/3 / 1/4 of the input HeightField resolution. The terrain keeps its size; only the pixel count drops (a 500×500 HeightField at 1/2 exports as 250×250). |
| **Mask Input Layer** | Which layer of the second input becomes the Gaea mask: Auto, `mask`, `height` or any layer name (see [Mask input](#mask-input)). |
| **Export > Build > Import** | The whole round trip without opening Gaea. |
| **1. Export to Gaea** / **2. Build in Gaea** / **3. Import Results** | The same steps one at a time. |
| **Cancel Build** | Stop a running Swarm build. |
| **Open Project in Gaea** | Launches the Gaea GUI with the project from **Paths**. |
| **Gaea Build Options** | **Build Resolution** (From Project, 512…16K; Swarm `--resolution`), **Ignore Baked Cache** (`--ignorecache`), **Show Build Window** (Swarm's console, for diagnostics). |
| **Auto Import After Build** | Import as soon as **2. Build in Gaea** finishes (default on). |
| **Auto Import (Gaea GUI builds)** | Import automatically when a build in the Gaea GUI writes new files (see above). |
| **Clear Import** | Forget the imported files; the node passes its input through. Also turns Auto Import (Gaea GUI builds) off. |

**Erosion tab**

| Parameter | What it does |
|-----------|--------------|
| **Export > Build > Import**, **Status** | The same button and status line as on the Bridge tab, so you can tweak and rebuild without leaving this tab. |
| **Auto Build on Change** | Rebuild automatically when an Erosion value changes; the node stays locked until the result is imported. |
| **Cancel Build** | Stop the build and unlock the node. |
| **Use Erosion Template** | Build a copy of the project with the Erosion2 values below (see [Erosion template](#erosion-template-tune-gaeas-erosion-from-houdini)). The rest of the tab is shown only when this is on. |
| **Erosion Node** | Which Erosion2 node to drive; empty = the only one. |
| **Read from Project** / **Reset to Gaea Defaults** | Load the template's values / Gaea's defaults. |
| **General**, **Sediment Discharge**, **Shape**, **Orographic Influence** | The Erosion2 parameters, with Gaea's ranges. |

**Tint tab**

| Parameter | What it does |
|-----------|--------------|
| **Show Tint on Terrain** | Show Gaea's Tint color map on the Terrain output (display material only). |
| **Export > Build > Import**, **Status** | Same as on the Bridge tab. |
| **Use Tint Parameters** | Build with the Tint values below (see [Tint](#tint)). The rest of the tab is shown only when this is on. |
| **Tint Node**, **Read from Project**, **Reset to Gaea Defaults** | Which Tint node to drive; load its values / Gaea's defaults. |
| **Tint**, **Processing** | Start, End, Hue, Saturation, Lightness; Enhance, Reverse — with Gaea's ranges. |

**Gaea Layers tab**

| Parameter | What it does |
|-----------|--------------|
| **Height** | *Gaea Result*: the eroded terrain. *Input HeightField*: keep the original height (on all outputs) and only take the masks from Gaea. |
| **Keep Input Resolution** | Put every output back onto the input HeightField's grid (same resolution and voxel size as the input). Off: keep Gaea's build resolution. |
| **Show Mask** | None / Flow / Wear / Sediment: preview that mask in red on the Terrain output (it goes into the Terrain output's `mask`). Set back to None before using the Terrain output downstream, otherwise HeightField nodes will use that mask. |
| **Flow / Wear / Sediment** | One settings section per mask, see below. All three are always active, each for its own output. |

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

Converting a HeightField to polygons turns its `mask` into a point attribute.

**Paths tab**

| Parameter | What it does |
|-----------|--------------|
| **Bridge Dir** | Working folder shared with Gaea. |
| **Gaea Project**, **Gaea Exe** | The `.terrain` file (built by Swarm, opened in the GUI, source of the world size) and `Gaea.exe`. |
| **Gaea Swarm Exe** | Gaea's command-line builder. Empty = `Gaea.Swarm.exe` next to Gaea Exe. |
| **Gaea Width / Height** | Gaea world size in metres. 0 = read from the Gaea project. |

The erosion graph itself is set up and tuned in the Gaea GUI. Gaea builds at the
resolution in its Terrain Definition unless **Build Resolution** overrides it; lower it for
faster previews.

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

On a repeated **Export**, the bridge compares the signature (hash of the input heights and
their Gaea encoding, export resolution and layer set) with the manifest. If everything
matches and the output files are in place, nothing is re-exported, Status reports that no
Gaea rebuild is needed, and **Export > Build > Import** skips the build. Any change to the
terrain, the resolution or the Gaea world size invalidates the cache.

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
  "gaea_swarm": "",
  "build_resolution": 0,
  "ignore_cache": false,
  "show_console": false,
  "gaea_width": 0,
  "gaea_height": 0,
  "output_maps": {
    "height": "output_height.exr",
    "flow": "output_flow.exr"
  }
}
```

`downsample` is 1 (full), 2 (1/2), 3 (1/3) or 4 (1/4). `gaea_width`/`gaea_height` of 0 mean
"read from `gaea_project`". `gaea_swarm` empty means `Gaea.Swarm.exe` next to `gaea_exe`;
`build_resolution` 0 means the project's resolution.

Precedence, from lowest to highest: defaults in the code → `gh_bridge_config.json` →
HDA node parameters.

## Houdini version compatibility

The bridge is developed on Houdini 20.5 and handles the known differences of 19.5:

| Difference | Houdini 19.5 | Houdini 20.x | How the bridge handles it |
|------------|--------------|--------------|---------------------------|
| `hou.Volume.voxelSize()` | missing | present | Voxel size from `indexToPos()` (both versions), bounding box as a last resort |
| `hou.Volume.resolution()` | `hou.Vector3` of floats | tuple of ints | Converted to ints |
| HeightField Output resolution toggle | "Use Resolution" | inside "Resolution" | Found by several names and by label |

Parameters of Houdini's own nodes are looked up from a list of candidate names. Every
export is read back and checked, so if another version writes something unexpected
(for example a constant image, which Gaea shows as a flat terrain), Status shows a clear
error instead. Building the HDA prints a note for every optional parameter it couldn't
find.

To check a Houdini version, run in it:

```powershell
& "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" tools/houdini_compat_check.py
```

or, from Houdini's Python Shell:

```python
exec(open(r"C:/path/to/BridgeHGH/tools/houdini_compat_check.py").read())
```

It lists every node type, parameter and `hou.Volume` method the bridge uses, runs a test
export and mask import in a temporary folder, and cleans up after itself. If anything
shows as MISSING or FAIL, include the report in an issue.

## Development and tests

The tests cover the logic that does not depend on `hou`: config and overrides, path
building, the hash signature, the manifest, `is_cache_valid`, the safety of
`cleanup_temp`, the Swarm command and project-path checks, and build success detection
(with a stand-in process instead of Gaea).

Run them with any Python 3.7+:

```bash
python tests/test_bridge_logic.py -v
```

or with `hython` from your Houdini install if you have no standalone Python:

```powershell
& "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" tests/test_bridge_logic.py -v
```

Expected result: `Ran 73 tests ... OK`. The tests also simulate Houdini 19.5's
`hou.Volume` and check that the sources parse as Python 3.7.

## Limitations

- The structure of the Gaea graph is edited in the Gaea GUI. From Houdini you can change
  the Erosion2 values of a template, not add or rewire nodes.
- The Erosion2 parameter list matches Gaea 2.3. Template copies are edited as JSON; the
  `.terrain` format isn't a public API, so a future Gaea version may need an update.
- File names on the Gaea Export nodes must match what the bridge expects (see the table
  above or `output_maps` in the config).
- Swarm's console output can't be captured (Swarm fails when its output is redirected),
  so diagnostics are shown in its own window via **Show Build Window**.

---

This project is not affiliated with QuadSpinner or SideFX. Gaea and Houdini are trademarks
of their respective owners.
