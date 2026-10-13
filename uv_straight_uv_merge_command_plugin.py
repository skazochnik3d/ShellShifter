"""Stable undo command loader; implementation updates without unloading plugins."""
import maya.api.OpenMaya as om


def maya_useNewAPI():
    pass


class MergeCommand(om.MPxCommand):
    def __init__(self):
        super().__init__()
        self.operation = None

    @staticmethod
    def creator():
        return MergeCommand()

    def isUndoable(self):
        return True

    def doIt(self, args):
        import uv_straight_uv_merge as merge
        import uv_straight_uv_merge_command as worker
        self.operation = worker.MergeOperation()
        self.operation.prepare(merge._pending.pop(args.asString(0)))
        self.operation.redoIt()

    def redoIt(self):
        self.operation.redoIt()

    def undoIt(self):
        self.operation.undoIt()


def initializePlugin(obj):
    om.MFnPlugin(obj, 'ShellShifter', '1.4.132-beta', 'Any').registerCommand(
        'shellShifterCollectUVShells', MergeCommand.creator)


def uninitializePlugin(obj):
    om.MFnPlugin(obj).deregisterCommand('shellShifterCollectUVShells')
