"""Shared state between Maya's isolated Python plug-in loader and ShellShifter."""

TYPE_NAME = 'uvStraightAnnotationShape'
PLUGIN_LOADED = False
PAYLOAD = []
VIEWPORT_PAYLOAD = []
DRAW_COUNT = 0
VIEWPORT_DRAW_COUNT = 0
VIEWPORT_PREPARE_COUNT = 0
VIEWPORT_CALLBACK_COUNT = 0
VIEWPORT_DRAW_ERROR = ''
CAN_DRAW_COUNT = 0
UV_CALL_COUNT = 0
DRAW_ERROR = ''
