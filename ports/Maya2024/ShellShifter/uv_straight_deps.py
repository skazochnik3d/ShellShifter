"""Load the NumPy dependency bundled with the Windows Maya 2024 build."""
from importlib.util import find_spec
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import time
import zipfile


def _prepare_numpy():
    root = Path(__file__).resolve().parent
    cache = Path(os.environ.get('LOCALAPPDATA', str(root))) / 'ShellShifter' / 'Maya2024'
    target = cache / 'numpy-2.2.6'
    if not target.is_dir():
        cache.mkdir(parents=True, exist_ok=True)
        wheel = root / 'assets/numpy-2.2.6-cp310-cp310-win_amd64.whl'
        with TemporaryDirectory(prefix='.numpy_', dir=cache) as temporary:
            staging = Path(temporary) / 'numpy'
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(staging)
            for attempt in range(10):
                try:
                    staging.rename(target)
                    break
                except OSError as error:
                    if target.is_dir():
                        break  # Another process installed the same wheel.
                    if getattr(error, 'winerror', None) not in (5, 32) or attempt == 9:
                        raise
                    time.sleep(.1)
    sys.path.insert(0, str(target))


if find_spec('numpy') is None:
    _prepare_numpy()

import numpy as np
