"""Shared surface colors for completion, checker and distortion composition."""
DONE_COLOR = (.18, .85, .3, 1.)
DONE_TINT_WEIGHT = .28
DISTORTION_NEUTRAL = (.5, .5, .5, 1.)
FINISHED_DISPLAY_NODE = ':shellShifterUnwrapDisplay'
FINISHED_DISPLAY_ATTR = 'finishedVisible'
FINISHED_DISPLAY_PLUG = FINISHED_DISPLAY_NODE + '.' + FINISHED_DISPLAY_ATTR


def finished_visible():
    """Older scenes display their saved completion marks by default."""
    import maya.cmds as cmds
    return not cmds.objExists(FINISHED_DISPLAY_PLUG) or bool(cmds.getAttr(FINISHED_DISPLAY_PLUG))


def completion_tint(rgba):
    """Blend a green completion wash without losing red/blue distortion contrast."""
    return tuple(value*(1.-DONE_TINT_WEIGHT) + green*DONE_TINT_WEIGHT
                 for value, green in zip(rgba[:3], DONE_COLOR[:3])) + (rgba[3],)


def done_surface(checker):
    # Modulate2X multiplies the checker by twice the CPV color. Start from
    # neutral .5 so the green wash preserves its dark/light square contrast.
    return completion_tint((.5, .5, .5, 1.)) if checker else DONE_COLOR
