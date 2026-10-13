# Publish an update

1. Update `uv_straight.VERSION` using `X.Y.Z` or `X.Y.Z-beta`.
2. Verify the changed tools in both Maya 2025 and 2027. If runtime files were
   added, update `runtime_files.json`.
3. Run `python tools/build_release.py` with Python 3.11 or newer. This creates
   separate Maya 2025/2027 ZIP files, internal file manifests and a shared
   `ShellShifter_updates.json` with archive SHA-256 values.
4. Commit and push the source. With authenticated GitHub CLI, publish the
   corresponding tag and all three assets, for example:

```powershell
gh release create v1.4.233-beta build/ShellShifter_Maya2025_v1.4.233_beta_Install.zip build/ShellShifter_Maya2027_v1.4.233_beta_Install.zip build/ShellShifter_updates.json --prerelease --title "ShellShifter 1.4.233-beta" --notes-file release_notes.md
```

The tag version, core version, ZIP names and manifest versions must match.
The updater selects the newest release with the correct Maya asset and ignores
draft releases. Beta releases are included. Publish archives as GitHub Release
assets; source-code ZIPs are not installation packages.
