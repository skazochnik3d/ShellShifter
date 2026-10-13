# Publish an update

1. Update `ShellShifter/uv_straight.VERSION` using `X.Y.Z`.
2. Verify the changed tools in both Maya 2025 and 2027. If runtime files were
   added, update `tools/runtime_files.json`.
3. Run `python tools/build_release.py` with Python 3.11 or newer. This creates
   separate Maya 2025/2027 ZIP files, internal file manifests and a shared
   `ShellShifter_updates.json` with archive SHA-256 values.
4. Commit and push the source. With authenticated GitHub CLI, publish the
   corresponding tag and all three assets, for example:

```powershell
gh release create v1.4.235 build/ShellShifter_Maya2025_v1.4.235_Install.zip build/ShellShifter_Maya2027_v1.4.235_Install.zip build/ShellShifter_updates.json --latest --title "ShellShifter 1.4.235" --notes-file docs/releases/1.4.235.md
```

Use the short release title `ShellShifter X.Y.Z`. Supported Maya versions are
listed in the description and the individual archive names. Installation
archives use `ShellShifter_Maya2025_vX.Y.Z_Install.zip` and the Maya 2027 equivalent.
Do not add `beta` or Maya versions to the release title.

The tag and core version must match. Archive manifests contain that exact
version. Historical prerelease tags keep their original names to preserve
links; their display titles and archive names omit the beta suffix.
The updater selects the newest release with the correct Maya asset and ignores
draft releases. Beta releases are included. Publish archives as GitHub Release
assets; source-code ZIPs are not installation packages.
