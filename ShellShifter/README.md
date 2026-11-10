# ShellShifter

UV tools for **Autodesk Maya 2025 and 2027 on Windows**.

Unwrap and Unfold with H/V guides, GPU packing, texel density, distortion
display, UV checks, material-based UV selection, stacking and a custom pie menu.

## Install

1. Extract the ZIP to a temporary folder.
2. Drag **install_shellshifter.py** into Maya’s 3D viewport.
3. After installation, click the Shelf logo. The extracted folder and ZIP can be removed.

Maya's bundled Python, Qt and NumPy are sufficient. GPU packing needs an
OpenCL 1.2 driver with FP64 support; Unwrap uses Maya's Unfold3D.

[Русская инструкция](INSTALL_RU.txt) · [English instructions](INSTALL_EN.txt)

## Updates

Open **Updates** using the green circular-arrow icon. **Check for Updates** checks GitHub
releases for your Maya version, including beta releases. ShellShifter also
checks in the background on opening and every four hours while Maya runs.
A new version opens a notification with **Download & Install** and **Later**.

Installation verifies archive and file checksums, backs up the previous files
and restores them if a replacement fails. Finish active UV operations before
installing. After installation, save your scene and **restart Maya**. The
updater keeps scene data, settings, hotkeys and Shelf paths. Connection failures
appear in the Check for Updates tooltip without opening automatic error dialogs.

## Tools

- **Unwrap:** cuts selected/hard edges, unfolds, orients and packs silently.
- **Pins:** H/V guides, segment editing, constrained Unfold and Relax.
- **Straighten Quads:** quad grids and suitable ribbons with triangles/n-gons;
  ribbons receive guided Unfold, while quad grids retain their direct result.
- **Packing:** GPU packing, packing around existing shells, lower half of a
  UV tile and conversions between the lower half and the full square.
- **Materials:** compact, two-column selection of UVs by material.
- **Display:** pins, distortion and wireframe controls.
- **UV Checks:** small shells, hard edges, overlap selection, 99% overlap
  agreement, butterfly seam repair and UV-set cleanup.
- **Move Overlaps:** move/flip, originals and stacking using the least distorted
  matching shell. UV operations support Undo.
- **Keymaps:** custom shortcuts and editable pie menu.

Default gestures: **Shift + Space** opens the popup; **Alt + Shift + right click**
opens the pie menu; **Shift + wheel** adjusts the H/V angle with a fading display.


