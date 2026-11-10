"""Stable Maya command loader; undo objects survive ShellShifter reloads."""
import maya.api.OpenMaya as om

def maya_useNewAPI():pass

class PackedUVCommand(om.MPxCommand):
    def __init__(self):
        super().__init__()
        self.operation=None

    @staticmethod
    def creator():return PackedUVCommand()

    def isUndoable(self):return True

    def doIt(self,args):
        import uv_straight_pack_commit as commit
        entries,callback=commit._pending.pop(args.asString(0))
        self.operation=commit.UVCommit(entries,callback)
        self.operation.redoIt()

    def redoIt(self):self.operation.redoIt()

    def undoIt(self):self.operation.undoIt()

def initializePlugin(obj):
    om.MFnPlugin(obj,'ShellShifter','1.4.193-beta','Any').registerCommand(
        'shellShifterApplyPackedUVs',PackedUVCommand.creator)

def uninitializePlugin(obj):
    om.MFnPlugin(obj).deregisterCommand('shellShifterApplyPackedUVs')
