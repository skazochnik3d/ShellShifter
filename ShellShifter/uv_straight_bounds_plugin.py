"""API 1.0 bridge exposing UV viewport bounds; no scene or drawing nodes."""
import maya.OpenMayaMPx as mpx
import uv_straight_bounds_state as state


class BoundsContext(mpx.MPxTexContext):
    pass


class BoundsCommand(mpx.MPxContextCommand):
    def makeObj(self):
        state.CONTEXT = BoundsContext()
        state.CONTEXTS.append(state.CONTEXT)
        return mpx.asMPxPtr(state.CONTEXT)


def create_command():
    command = BoundsCommand()
    state.COMMANDS.append(command)
    return mpx.asMPxPtr(command)


def initializePlugin(obj):
    mpx.MFnPlugin(obj, 'ShellShifter', '1.4.0', 'Any').registerContextCommand(
        'uvStraightBoundsContext', create_command)


def uninitializePlugin(obj):
    mpx.MFnPlugin(obj).deregisterContextCommand('uvStraightBoundsContext')
    state.CONTEXT = None
