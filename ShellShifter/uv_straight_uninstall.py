"""Remove ShellShifter installations belonging to the current Maya profile."""
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

from install_shellshifter import MANIFEST, RUNTIME_NAME, _is_ours

FOLDER = re.compile(r'ShellShifter(?:_Installed|_[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)[0-9.]*)?)?')
STARTUP = {
    'uv_straight_startup.mel': b'import uv_straight_menu; uv_straight_menu.install()',
    'shellshifter_startup.mel': b'import shellshifter; shellshifter.install_menu()',
}
STARTUP_LINE = re.compile(rb'[ \t]*source "(?:uv_straight|shellshifter)_startup\.mel";[ \t]*(?:\r?\n)?')


def _owned_files(folder, scripts, year):
    markers = [folder / MANIFEST]
    if folder != scripts:
        markers.append(folder / 'package_manifest.json')
    paths = set()
    for marker in markers:
        if not marker.is_file() or marker.is_symlink():
            continue
        try:
            data = json.loads(marker.read_bytes())
            names = data['files']
            if (str(data['maya']) != year or not isinstance(names, dict)
                    or not {'shellshifter.py', 'uv_straight.py'} <= names.keys()):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        paths.add(marker)
        for name in names:
            if RUNTIME_NAME.fullmatch(name) or (folder != scripts and name in (
                    'INSTALL_EN.txt', 'INSTALL_RU.txt', 'README.md', 'LICENSE')):
                path = folder / name
                paths.add(path)
                if path.suffix == '.py':
                    paths.update((folder / '__pycache__').glob(path.stem + '.cpython-*.pyc'))
    # The updater puts previous plugin files in folders with a rollback manifest.
    if paths:
        for backup in folder.glob('ShellShifter_backup_*'):
            marker = backup / 'rollback.json'
            if not marker.is_file() or backup.is_symlink() or marker.is_symlink():
                continue
            try:
                rollback = json.loads(marker.read_bytes())
            except (ValueError, OSError):
                continue
            if not isinstance(rollback, dict) or MANIFEST not in rollback:
                continue
            paths.add(marker)
            for name, existed in rollback.items():
                if type(existed) is bool and (name == MANIFEST or RUNTIME_NAME.fullmatch(name)):
                    paths.update((backup / name, backup / 'new' / name))
    return paths


def removal_plan(scripts, year):
    scripts = scripts.resolve()
    folders = [scripts] + [p for p in scripts.iterdir() if FOLDER.fullmatch(p.name)
                           and p.is_dir() and not p.is_symlink()]
    paths = set().union(*(_owned_files(folder, scripts, year) for folder in folders))
    if not paths:
        raise ValueError('Install ShellShifter with the current drag-and-drop installer first.')
    startup = set()
    for name, command in STARTUP.items():
        path = scripts / name
        if path.is_file() and not path.is_symlink():
            lines = [line.strip() for line in path.read_bytes().splitlines()
                     if line.strip() and not line.lstrip().startswith(b'//')]
            if lines == [b'evalDeferred -lowestPriority "python(\\"' + command + b'\\")";']:
                startup.add(path)
    user_setup = scripts / 'userSetup.mel'
    setup_bytes = None
    if startup and user_setup.is_file() and not user_setup.is_symlink():
        old = user_setup.read_bytes()
        new = b''.join(line for line in old.splitlines(keepends=True)
                       if not (STARTUP_LINE.fullmatch(line) and
                               any(b'"' + p.name.encode('ascii') + b'"' in line for p in startup)))
        if new != old:
            setup_bytes = new
            paths.add(user_setup)
    paths.update(startup)
    files = sorted(path for path in paths if path.is_file() and not path.is_symlink()
                   and path.resolve().is_relative_to(scripts))
    return files, user_setup, setup_bytes


def remove_files(scripts, plan):
    files, user_setup, setup_bytes = plan
    moved = []
    directories = set()
    with TemporaryDirectory(prefix='.shellshifter-uninstall-', dir=scripts) as temporary:
        staging = Path(temporary)
        try:
            for index, path in enumerate(files):
                backup = staging / str(index)
                os.replace(path, backup)
                moved.append((path, backup))
                parent = path.parent
                while parent != scripts:
                    directories.add(parent)
                    parent = parent.parent
            if setup_bytes is not None:
                new_setup = staging / 'userSetup.mel'
                new_setup.write_bytes(setup_bytes)
                os.replace(new_setup, user_setup)
        except OSError:
            for path, backup in reversed(moved):
                os.replace(backup, path)
            raise
    for directory in sorted(directories, key=lambda p: -len(p.parts)):
        try:
            directory.rmdir()  # Only remove directories emptied by this uninstall.
        except OSError:
            pass


def request_uninstall(manager):
    import maya.cmds as cmds
    import maya.mel as mel
    from uv_straight_updates import QtWidgets, _maya_parent, _operations_running

    if manager.busy or _operations_running():
        cmds.warning('Finish active ShellShifter operations before uninstalling.')
        return
    scripts = Path(cmds.internalVar(userScriptDir=True)).resolve()
    if not manager.root.is_relative_to(scripts):
        cmds.warning('Install ShellShifter with the current drag-and-drop installer before uninstalling.')
        return
    try:
        plan = removal_plan(scripts, manager.maya)
    except (OSError, ValueError) as error:
        cmds.warning(str(error))
        return
    choice = cmds.confirmDialog(
        title='Uninstall ShellShifter',
        message='Remove ShellShifter files and Shelf buttons for Maya {}?\n'
                'Save your scene and restart Maya afterward.'.format(manager.maya),
        button=['Uninstall', 'Cancel'], defaultButton='Cancel',
        cancelButton='Cancel', dismissString='Cancel')
    if choice != 'Uninstall':
        return
    windows = [w for w in QtWidgets.QApplication.allWidgets()
               if w.objectName() in ('uvStraightQtWindow', 'uvStraightQtPopup')]
    if _operations_running() or any(
            getattr(getattr(w, 'match_uvs', None), 'ticket', None) is not None or
            getattr(getattr(getattr(w, 'materials', None), 'textures', None),
                    'conversion_task', None) is not None for w in windows):
        cmds.warning('Finish active ShellShifter operations before uninstalling.')
        return
    manager.stop()
    for window in windows:
        window.close()
    for name, method in (('uv_straight_menu', 'uninstall'), ('uv_straight_native', 'disable'),
                         ('uv_straight_texture_reload', 'stop'), ('uv_straight_group_visibility', 'stop')):
        module = sys.modules.get(name)
        if module:
            getattr(module, method)()
    custom = getattr(sys.modules.get('uv_straight_custom_keymaps'), '_manager', None)
    if custom:
        custom.stop()
    try:
        remove_files(scripts, plan)
        for item in cmds.lsUI(type='shelfButton', long=True) or []:
            if _is_ours(cmds.shelfButton(item, query=True, annotation=True)):
                cmds.deleteUI(item)
        mel.eval('saveAllShelves $gShelfTopLevel')
    except (OSError, RuntimeError) as error:
        cmds.warning('ShellShifter uninstall: ' + str(error))
        return
    QtWidgets.QMessageBox.information(_maya_parent(), 'ShellShifter',
                                      'ShellShifter removed. Restart Maya to finish.')
