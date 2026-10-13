"""Drag into Maya's viewport to install/update the ShellShifter shelf button.

Keep shellshifter.py and the compatible uv_straight*.py modules beside this file.
"""
from pathlib import Path
import sys

ANNOTATION = 'ShellShifter for Maya'
LEGACY_ANNOTATION = 'UV Straight for Maya'


def _is_ours(annotation):
    return annotation in (ANNOTATION, LEGACY_ANNOTATION) or annotation.startswith(
        (ANNOTATION+' ', LEGACY_ANNOTATION+' '))


def rename_existing_buttons():
    """Update existing shelves without replacing their saved launch commands."""
    import maya.cmds as cmds
    if cmds.about(batch=True):
        return 0
    count = 0
    for item in cmds.lsUI(type='shelfButton', long=True) or []:
        if _is_ours(cmds.shelfButton(item, query=True, annotation=True)):
            cmds.shelfButton(item, edit=True, label='ShellShifter',
                            annotation=ANNOTATION, imageOverlayLabel='SS')
            count += 1
    return count


def onMayaDroppedPythonFile(*args):
    import maya.cmds as cmds
    import maya.mel as mel
    import importlib
    folder = str(Path(__file__).resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    import uv_straight
    importlib.reload(uv_straight)
    import shellshifter
    shelf_top = mel.eval('$tmpShellShifterShelf = $gShelfTopLevel')
    shelf = cmds.tabLayout(shelf_top, query=True, selectTab=True)
    if not shelf:
        raise RuntimeError('Select a shelf before installing ShellShifter.')
    command = ('import sys\n'
               'p = {!r}\n'
               'if p not in sys.path: sys.path.insert(0, p)\n'
               'import shellshifter\nshellshifter.show()').format(folder)
    existing = cmds.shelfLayout(shelf, query=True, childArray=True) or []
    button = next((item for item in existing if cmds.objectTypeUI(item) == 'shelfButton'
                   and _is_ours(cmds.shelfButton(item, query=True, annotation=True))), None)
    options = dict(label='ShellShifter', imageOverlayLabel='SS', annotation=ANNOTATION,
                   command=command, sourceType='python')
    if button:
        cmds.shelfButton(button, edit=True, **options)
    else:
        cmds.shelfButton(parent=shelf, image='commandButton.png', **options)
    shellshifter.show()


if __name__ == '__main__':
    onMayaDroppedPythonFile()
