"""Drag into Maya's viewport to copy ShellShifter and install its Shelf button."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory

ANNOTATION = 'ShellShifter for Maya'
ENGLISH_ANNOTATION = 'ShellShifter for Maya'
LEGACY_ANNOTATION = 'UV Straight for Maya'
SHELF_ICON = str(Path(__file__).resolve().parent / 'assets' / 'shellshifter_shelf.png')
MANIFEST = 'ShellShifter_update_manifest.json'
RUNTIME_NAME = re.compile(
    r'(?:uv_straight(?:_[a-z0-9_]+)?\.py|shellshifter\.py|install_(?:shellshifter|uv_straight)\.py'
    r'|assets/[a-zA-Z0-9_.-]+\.(?:png|svg|whl))')


def _is_ours(annotation):
    annotations = (ANNOTATION, ENGLISH_ANNOTATION, LEGACY_ANNOTATION, 'ShellShifter для Maya')
    return annotation in annotations or annotation.startswith(tuple(value+' ' for value in annotations))


def rename_existing_buttons():
    """Update existing shelves without replacing their saved launch commands."""
    import maya.cmds as cmds
    if cmds.about(batch=True):
        return 0
    runtime = sys.modules.get('shellshifter')
    image = (str(Path(runtime.__file__).resolve().parent / 'assets' / 'shellshifter_shelf.png')
             if runtime else SHELF_ICON)
    command = _shelf_command(Path(image).parent.parent)
    count = 0
    for item in cmds.lsUI(type='shelfButton', long=True) or []:
        if _is_ours(cmds.shelfButton(item, query=True, annotation=True)):
            cmds.shelfButton(item, edit=True, label='ShellShifter',
                            annotation=ANNOTATION, imageOverlayLabel='', image1=image,
                            style='iconOnly', command=command, sourceType='python')
            count += 1
    return count


def _install(source, scripts, year):
    if (source == scripts or source.parent == scripts) and (source / MANIFEST).is_file():
        return source, False
    manifest = source / 'package_manifest.json'
    if not manifest.is_file():
        raise ValueError('Extract the entire ShellShifter archive before installing.')
    package = json.loads(manifest.read_bytes())
    version = package.get('version', '')
    if (package.get('product') != 'ShellShifter' or str(package.get('maya')) != year
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)[0-9.]*)?', version)):
        raise ValueError('Use the ShellShifter archive for Maya ' + year + '.')
    files = {name: digest for name, digest in package['files'].items()
             if RUNTIME_NAME.fullmatch(name)}
    if not {'shellshifter.py', 'uv_straight.py', 'install_shellshifter.py',
            'assets/shellshifter_shelf.png'} <= files.keys():
        raise ValueError('Extract the entire ShellShifter archive before installing.')
    # Older installations in scripts itself may be imported by a startup hook.
    # Update that copy in place so the hook and the Shelf use the same modules.
    legacy = (scripts / MANIFEST).is_file() and (scripts / 'shellshifter.py').is_file()
    target = scripts if legacy else scripts / 'ShellShifter'
    if target == source:
        target = scripts / 'ShellShifter_Installed'
    if target.is_symlink() or not target.resolve().is_relative_to(scripts):
        raise ValueError('Invalid ShellShifter installation folder.')
    target.mkdir(parents=True, exist_ok=True)
    lock = target / '.shellshifter_install'
    lock.mkdir()
    try:
        with TemporaryDirectory(dir=lock) as temporary:
            stage = Path(temporary)
            for name, digest in files.items():
                data = (source / name).read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('ShellShifter file checksum mismatch: ' + name)
                path = stage / 'new' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (stage / 'new' / MANIFEST).write_text(
                json.dumps(dict(version=version, maya=year, files=files), indent=2), encoding='utf-8')
            written = []
            try:
                for name in (*files, MANIFEST):
                    path = target / name
                    if not path.resolve().is_relative_to(target.resolve()) or path.is_symlink():
                        raise ValueError('Invalid ShellShifter installation path: ' + name)
                    backup = stage / 'previous' / name
                    if path.is_file():
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, backup)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(stage / 'new' / name, path)
                    written.append((path, backup))
            except Exception:
                for path, backup in reversed(written):
                    if backup.exists():
                        os.replace(backup, path)
                    else:
                        path.unlink(missing_ok=True)
                raise
    finally:
        lock.rmdir()
    return target, True


def _shelf_command(folder):
    return "import runpy\nrunpy.run_path({!r})['launch']()".format(str(folder / 'install_shellshifter.py'))


def launch():
    import maya.cmds as cmds

    folder = Path(__file__).resolve().parent
    for name, module in tuple(sys.modules.items()):
        if name != 'shellshifter' and name != 'uv_straight' and not name.startswith('uv_straight_'):
            continue
        if name.endswith('_plugin'):
            continue
        location = getattr(module, '__file__', None)
        if location and Path(location).resolve().parent != folder:
            cmds.warning('ShellShifter is already loaded from {}. Restart Maya and open the Shelf button for {}.'
                         .format(Path(location).resolve().parent, folder))
            return
    path = str(folder)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    import shellshifter
    return shellshifter.show()


def onMayaDroppedPythonFile(*args):
    import maya.cmds as cmds
    import maya.mel as mel
    shelf_top = mel.eval('$tmpShellShifterShelf = $gShelfTopLevel')
    shelf = cmds.tabLayout(shelf_top, query=True, selectTab=True)
    if not shelf:
        raise RuntimeError('Select a shelf before installing ShellShifter.')
    source = Path(__file__).resolve().parent
    scripts = Path(cmds.internalVar(userScriptDir=True)).resolve()
    folder, copied = _install(source, scripts, str(cmds.about(version=True))[:4])
    command = _shelf_command(folder)
    buttons = [item for item in cmds.lsUI(type='shelfButton', long=True) or []
               if _is_ours(cmds.shelfButton(item, query=True, annotation=True))]
    options = dict(label='ShellShifter', imageOverlayLabel='', annotation=ANNOTATION,
                   image1=str(folder / 'assets' / 'shellshifter_shelf.png'), style='iconOnly',
                   command=command, sourceType='python')
    if buttons:
        for button in buttons:
            cmds.shelfButton(button, edit=True, **options)
    else:
        cmds.shelfButton(parent=shelf, **options)
    mel.eval('saveAllShelves $gShelfTopLevel')
    if copied and ('uv_straight' in sys.modules or 'shellshifter' in sys.modules):
        cmds.warning('ShellShifter files copied. Restart Maya to load the new installation.')
        return
    exec(command, {})


if __name__ == '__main__':
    onMayaDroppedPythonFile()
