"""PySide2 helpers for Maya 2024."""
from contextlib import contextmanager
from shiboken2 import VoidPtr, isValid


def image_bytes(image):
    return VoidPtr(image.constBits(), image.sizeInBytes()).toBytes()


@contextmanager
def signals_blocked(widget):
    previous = widget.blockSignals(True)
    try:
        yield
    finally:
        if isValid(widget):
            widget.blockSignals(previous)
