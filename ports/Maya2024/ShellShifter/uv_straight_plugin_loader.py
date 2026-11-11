"""Load Maya Python plug-ins from the selected ShellShifter installation."""
from pathlib import Path
import tempfile

import maya.cmds as cmds

_ROOT = Path(__file__).resolve().parent
_staging = globals().get('_staging')


def _plugin_path(source):
    global _staging
    if str(source).isascii():
        return source
    if _staging is None:
        for candidate in (Path(tempfile.gettempdir()), Path.home() / 'AppData' / 'Local' / 'Temp'):
            if candidate.is_dir() and str(candidate.resolve()).isascii():
                _staging = tempfile.TemporaryDirectory(prefix='ShellShifter_plugins_', dir=candidate.resolve())
                break
        if _staging is None:
            raise RuntimeError('Maya requires a temporary folder with an ASCII path for Python plug-ins.')
    target = Path(_staging.name) / source.name
    if not target.exists():
        target.write_bytes(source.read_bytes())
    return target


def load_plugin(name):
    source = _ROOT / (name + '.py')
    try:
        path = _plugin_path(source)
        if not cmds.pluginInfo(name, query=True, loaded=True):
            if not cmds.loadPlugin(str(path), quiet=True):
                raise RuntimeError('Maya did not register the plug-in.')
        if not cmds.pluginInfo(name, query=True, loaded=True):
            raise RuntimeError('Maya did not load the plug-in.')
        registered = Path(cmds.pluginInfo(name, query=True, path=True)).resolve()
        if registered != path.resolve():
            raise RuntimeError('Maya already loaded {}. Restart Maya and use the current Shelf button.'.format(registered))
    except (OSError, RuntimeError) as error:
        raise RuntimeError('ShellShifter could not load {} from {}: {}'.format(name, source, error)) from error
