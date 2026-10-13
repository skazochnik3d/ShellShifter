ShellShifter 1.4.232-beta for Maya 2025 and 2027 on Windows.

- Added Updates with a wrench icon and Check for Updates.
- Background checks on opening and every four hours; new-version notifications.
- Download & Install selects the correct Maya archive, checks SHA-256 and file
  manifests, backs up files and rolls back failed replacements.
- Restart Maya after installation. Scene data, hotkeys and Shelf paths remain.

Installation: extract the complete archive and drag install_shellshifter.py
into Maya's 3D viewport, then save the Shelf. Keep the extracted folder in place.

The two archives contain the same portable runtime, with a manifest for the
corresponding Maya version. Python/Qt/NumPy come from Maya.
