# GH-Bridge — Gaea ⇄ Houdini bridge

**English** | [Русский](README.ru.md)

A Houdini node that sends a HeightField to **Gaea 2**, builds the Gaea project with
`Gaea.Swarm.exe` (Gaea never opens) and brings the eroded terrain, its masks and tint
maps back into Houdini.

```
HeightField ─▶ input_height.exr ─▶ Gaea: File ─▶ Erosion2 ─▶ Export nodes
Terrain + masks ◀── output/output_*.exr ◀─────────────────────────┘
```

## Features

- **One button**: Export > Build > Import, in the background.
- **Four outputs**: Terrain, Flow, Wear, Sediment (masks ready for HeightField nodes).
- **Erosion2 / Erosion parameters in Houdini**: change them and rebuild; the Gaea
  project itself is never modified (a copy is built).
- **Mask input**: the second input limits where Gaea erodes (any mask or geometry).
- **1:1 scale**, export resolution Full–1/4, build resolution up to 16K, build cache.
- Houdini 19.5+ (tested on 20.5), Gaea 2.3, Windows.

## Installation

1. Make the module visible to Houdini: add to `houdini.env`
   ```
   PYTHONPATH = "C:/path/to/BridgeHGH;$PYTHONPATH"
   ```
   or copy `houdini_gaea_bridge.py` into `<Documents>/houdiniXX.X/scripts/python/`.
2. Build the HDA from Houdini's Python Shell (run again after updates):
   ```python
   exec(open(r"C:/path/to/BridgeHGH/build_gaea_bridge_hda.py").read())
   ```
   Two nodes appear in the SOP Tab menu: **Gaea Erosion2 Bridge** (for a project
   with Gaea's Erosion2) and **Gaea Erosion Bridge** (for the older Erosion node).
   They work the same; each exposes its own node's parameters.

## Gaea project (once)

`File` (any path; the bridge sets it) → **Erosion2** (or **Erosion**) → Export nodes
named:

| Erosion2 output | Export node |
|-----------------|-------------|
| Out / Flow / Wear / Deposits | `output_height` / `output_flow` / `output_wear` / `output_sediment` |
| color map (Tint nodes → Combine), optional | `output_tint_combine` |
| Mask port ← second `File` node | loads `<Bridge Dir>/input_mask.exr` |

Export format EXR. The bridge builds a copy of the project and sets every path in it
(input File, mask File, Export locations), so the saved project is never modified and
never has to point at the bridge folder. Each bridge node works in its own folder
(`C:/Temp/gh_bridge/<node name>` by default).

## Usage

1. Connect a HeightField (and optionally a mask) to the node.
2. **Paths** tab, once: **Gaea Exe** (shared by every bridge node). Each node finds its
   project by itself (`erosion2.terrain` / `erosion.terrain`, else any project with that
   node) in the scene folder or next to `houdini_gaea_bridge.py`, subfolders included;
   set **Gaea Project** only to pick a specific file. **Check Paths** shows what is in
   use.
3. Press **Export > Build > Import**.

Tabs:

- **Bridge** — export resolution, build options, step-by-step buttons, Auto Import.
- **Erosion** — *Use Erosion Template* exposes all parameters of the project's erosion
  node (Erosion2 or Erosion, depending on the bridge node); *Auto Build on
  Change* rebuilds after every edit (the node is locked until the import).
- **Tint** — *Build Tint* (off by default) builds `output_tint_combine` with the
  project's own Tint settings; off, the tint nodes are left out of the build entirely. **Show / Hide Tint**
  shows the map as the Terrain's mask (luminance).
- **Gaea Layers** — Show Mask preview, per-mask Normalize / Range / Gamma / Blur,
  Keep Input Resolution.

## Tests

```powershell
& "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" tests/test_bridge_logic.py
```

`tools/houdini_compat_check.py` checks a Houdini version for everything the bridge uses.

## Limitations

- The Gaea graph itself is edited in Gaea; Houdini only changes the erosion node's values.
- The `.terrain` format isn't a public API; a future Gaea version may need an update.

---

Not affiliated with QuadSpinner or SideFX. Gaea and Houdini are trademarks of their
respective owners.
