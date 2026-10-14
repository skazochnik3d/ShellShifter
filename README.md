# ShellShifter

UV tools for **Autodesk Maya 2025 and 2027 on Windows**.

Unwrap and Unfold with H/V guides, GPU packing, texel density, distortion
display, UV checks, material-based UV selection, stacking and a custom pie menu.

## Install

1. Download the archive for your Maya version from [Releases](https://github.com/skazochnik3d/ShellShifter/releases).
2. Extract the entire archive to a permanent folder that you can write to.
3. Drag **install_shellshifter.py** from that folder into Maya's 3D viewport.
4. Save All Shelves. Click **SS** to open ShellShifter next time.

Keep the extracted folder in place: the Shelf button uses its path.
Maya's bundled Python, Qt and NumPy are sufficient. GPU packing needs an
OpenCL 1.2 driver with FP64 support; Unwrap uses Maya's Unfold3D.

[Русская инструкция](docs/INSTALL_RU.txt) · [English instructions](docs/INSTALL_EN.txt)

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

**Report a Bug** opens a modeless report window with a description, screenshot
file/clipboard attachment and preview. Versions are included automatically;
the user must approve public publication. Send runs in the background and
returns a GitHub Issue link. The reporting service must first be connected by
the maintainer; an empty endpoint keeps Send disabled. No scene files, paths,
automatic screenshots or credentials are collected by the plugin.
See [service setup](https://github.com/skazochnik3d/ShellShifter/blob/main/docs/BUG_REPORTS_SETUP_RU.md).

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

## Development

The repository is organized into four folders:

- [`ShellShifter`](ShellShifter/) — plugin modules, installer and checker asset.
- [`docs`](docs/) — installation, publishing instructions and release notes.
- [`tools`](tools/) — release builder and runtime file list.
- [`services`](services/) — optional bug-report service; excluded from plugin archives.

Only files listed in `tools/runtime_files.json` are included in install archives.
Archives keep all modules beside `install_shellshifter.py` in one plugin folder.
If installing from a source-code archive, open the `ShellShifter` folder first.
See [publishing instructions](docs/PUBLISH.md) for building future updates.

The current runtime was verified in Maya 2025 and 2027. Release checks cover
the updater, rollback, GUI responsiveness, palette/pie/shortcut integration,
native plugins and representative UV operations. No external Python packages
are required.
