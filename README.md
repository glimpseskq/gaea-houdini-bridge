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
- **Erosion2 and Tint parameters in Houdini**: change them and rebuild; the Gaea project
  itself is never modified (a copy is built).
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
   A **Gaea Erosion2 Bridge** node appears in the SOP Tab menu.

## Gaea project (once)

`File` (`<Bridge Dir>/input_height.exr`) → **Erosion2** → Export nodes named:

| Erosion2 output | Export node |
|-----------------|-------------|
| Out / Flow / Wear / Deposits | `output_height` / `output_flow` / `output_wear` / `output_sediment` |
| same, through a **Tint** node | `output_tint_height` / `_flow` / `_wear` / `_sediment` |
| Mask port ← second `File` node | loads `<Bridge Dir>/input_mask.exr` |

Exports: Format EXR, Location Custom, Output Path `<Bridge Dir>/output`. With Use Erosion
Template or Use Tint Parameters on, the bridge sets these paths itself. Save the project.

## Usage

1. Connect a HeightField (and optionally a mask) to the node.
2. **Paths** tab: Bridge Dir, Gaea Project (`.terrain`), Gaea Exe.
3. Press **Export > Build > Import**.

Tabs:

- **Bridge** — export resolution, build options, step-by-step buttons, Auto Import.
- **Erosion** — *Use Erosion Template* exposes all Erosion2 parameters; *Auto Build on
  Change* rebuilds after every edit (the node is locked until the import).
- **Tint** — *Use Tint Parameters* exposes each layer's Tint. To view a tint, pick it in
  the **Tint** menu and press **Load Tint** (press again to reload; None or
  **Reset** hides it). The tint is shown as the Terrain's mask (luminance of the map).
- **Gaea Layers** — Show Mask preview, per-mask Normalize / Range / Gamma / Blur,
  Keep Input Resolution.

## Tests

```powershell
& "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" tests/test_bridge_logic.py
```

`tools/houdini_compat_check.py` checks a Houdini version for everything the bridge uses.

## Limitations

- The Gaea graph itself is edited in Gaea; Houdini only changes Erosion2/Tint values.
- The `.terrain` format isn't a public API; a future Gaea version may need an update.

---

Not affiliated with QuadSpinner or SideFX. Gaea and Houdini are trademarks of their
respective owners.
