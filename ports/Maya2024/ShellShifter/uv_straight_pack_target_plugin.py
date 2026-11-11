"""Native UV-only click context; never delegates clicks to Maya selection."""
import maya.OpenMayaMPx as mpx
import maya.OpenMayaUI as omui

class TileContext(mpx.MPxTexContext):
    def toolOnSetup(self,*args):self.setHelpString('ShellShifter: click a UV tile. Esc/right click cancels.')
    def doPress(self,event):
        import uv_straight_pack_target as target
        if target.active is None:return
        from PySide2 import QtCore,QtWidgets
        if event.mouseButton()==omui.MEvent.kLeftMouse and not QtWidgets.QApplication.keyboardModifiers()&QtCore.Qt.AltModifier:
            target.active.pick()
    def abortAction(self,*args):
        import uv_straight_pack_target as target
        if target.active is not None:target.active.finish()
    def toolOffCleanup(self,*args):
        import uv_straight_pack_target as target
        if target.active is not None:target.active.finish(restore_tool=False)


class TileCommand(mpx.MPxContextCommand):
    def makeObj(self):
        import uv_straight_pack_target as target
        target.native_context=TileContext();target.contexts.append(target.native_context)
        return mpx.asMPxPtr(target.native_context)


def creator():
    import uv_straight_pack_target as target
    command=TileCommand();target.commands.append(command);return mpx.asMPxPtr(command)


def initializePlugin(obj):
    mpx.MFnPlugin(obj,'ShellShifter','1.4.240','Any').registerContextCommand('shellShifterPickUVTileContext',creator)


def uninitializePlugin(obj):mpx.MFnPlugin(obj).deregisterContextCommand('shellShifterPickUVTileContext')
