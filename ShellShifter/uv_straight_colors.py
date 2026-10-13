"""Shared surface colors for completion, checker and distortion composition."""
DONE_COLOR = (.18, .85, .3, 1.)
DONE_TINT_WEIGHT = .28


def completion_tint(rgba):
    """Blend a green completion wash without losing red/blue distortion contrast."""
    return tuple(value*(1.-DONE_TINT_WEIGHT) + green*DONE_TINT_WEIGHT
                 for value, green in zip(rgba[:3], DONE_COLOR[:3])) + (rgba[3],)


def done_surface(checker):
    # Modulate2X multiplies the checker by twice the CPV color. Start from
    # neutral .5 so the green wash preserves its dark/light square contrast.
    return completion_tint((.5, .5, .5, 1.)) if checker else DONE_COLOR
