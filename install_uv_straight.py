"""Compatibility installer for previous UV Straight download links."""
from pathlib import Path
import sys


def onMayaDroppedPythonFile(*args):
    folder = str(Path(__file__).resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    from install_shellshifter import onMayaDroppedPythonFile as install
    return install(*args)


if __name__ == '__main__':
    onMayaDroppedPythonFile()
