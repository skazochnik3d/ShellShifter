"""Choose an integer UV tile in a native UV Editor, with a hover outline."""
import math
from PySide2 import QtCore,QtGui,QtWidgets
from shiboken2 import isValid,wrapInstance
import maya.cmds as cmds
import maya.OpenMaya as om
import maya.OpenMayaUI as omui

active=globals().get('active')
contexts=globals().get('contexts',[])
commands=globals().get('commands',[])
native_context=None


def tile_at(point,bounds,width,height):
    left,right,bottom,top=bounds
    if not all(math.isfinite(v) for v in bounds) or right<=left or top<=bottom or width<=0 or height<=0:
        raise ValueError('UV Editor projection is not ready.')
    return (math.floor(left+point.x()/width*(right-left)),math.floor(top-point.y()/height*(top-bottom)))


class TileOverlay(QtWidgets.QWidget):
    def __init__(self,viewport):
        # An ordinary child widget clears QmayaGLWidget to black. Windows
        # composites this owned transparent tool window over its framebuffer.
        super().__init__(viewport,QtCore.Qt.Tool|QtCore.Qt.FramelessWindowHint|QtCore.Qt.WindowTransparentForInput|QtCore.Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground);self.setAttribute(QtCore.Qt.WA_NoSystemBackground)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents);self.setAttribute(QtCore.Qt.WA_QuitOnClose,False)
        self.setFocusPolicy(QtCore.Qt.NoFocus);self.tile=None;self.bounds=None
    def paintEvent(self,event):
        if self.tile is None or self.bounds is None:return
        from uv_straight_overlay import project_uv
        u,v=self.tile
        a=project_uv((u,v+1),self.bounds,self.width(),self.height())
        b=project_uv((u+1,v),self.bounds,self.width(),self.height())
        rect=QtCore.QRectF(a,b).normalized();painter=QtGui.QPainter(self)
        painter.fillRect(rect,QtGui.QColor(225,183,92,42))
        painter.setPen(QtGui.QPen(QtGui.QColor('#efc56e'),2));painter.drawRect(rect)
        text=str(1001+u+10*v) if 0<=u<10 and v>=0 else '({}, {})'.format(u,v)
        painter.drawText(rect,QtCore.Qt.AlignCenter,text+' · click')


class TilePicker(QtCore.QObject):
    def __init__(self,callback):
        super().__init__(QtWidgets.QApplication.instance());self.callback=callback
        self.overlay=None;self.viewport=None;self.root=None;self.context=None;self.context_name=None
        self.previous_tool=cmds.currentCtx();self.previous_focus=cmds.getPanel(withFocus=True)
        self.selection=cmds.ls(selection=True,long=True) or [];self.hilite=cmds.ls(hilite=True,long=True) or []
        self.modes={key:cmds.selectMode(query=True,**{key:True}) for key in ('object','component','hierarchical')}
        flags=('meshComponents','polymeshVertex','polymeshEdge','polymeshFace','polymeshUV','polymeshVtxFace','polymeshFreeEdge','meshUVShell')
        self.masks=[(obj,key,cmds.selectType(query=True,**dict({key:True},**({'objectComponent':True} if obj else {})))) for obj in (False,True) for key in flags]
        self.finished=False;self.finishing=False;self.pressed_tile=None
        self.timer=QtCore.QTimer(self);self.timer.setInterval(33);self.timer.timeout.connect(self.update)

    def start(self):
        from uv_straight_native import _find_editors
        editors=_find_editors()
        if not editors:
            import maya.mel as mel
            mel.eval('texturePanelShow()');editors=_find_editors()
        if not editors:raise ValueError('Open the UV Editor to select a tile.')
        visible=[editor for editor in editors if cmds.control(cmds.textureWindow(editor,query=True,control=True),query=True,isObscured=True) is False]
        focus=cmds.getPanel(withFocus=True);editor=focus if focus in editors else (visible or editors)[0]
        control=cmds.textureWindow(editor,query=True,control=True);pointer=omui.MQtUtil.findControl(control)
        if not pointer:raise ValueError('The UV Editor is not ready. Try selecting the tile again.')
        self.root=wrapInstance(int(pointer),QtWidgets.QWidget)
        ports=[w for w in [self.root]+self.root.findChildren(QtWidgets.QWidget) if w.metaObject().className()=='QmayaGLWidget']
        if len(ports)!=1:raise ValueError('Could not find the UV Editor coordinate view.')
        self.viewport=ports[0]
        plugin='uv_straight_pack_target_plugin'
        from uv_straight_plugin_loader import load_plugin
        load_plugin(plugin)
        self.context_name=cmds.shellShifterPickUVTileContext()
        self.context=native_context
        self.overlay=TileOverlay(self.viewport)
        from uv_straight_native import _no_undo
        with _no_undo():
            cmds.setFocus(editor);cmds.setToolTo(self.context_name)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.timer.start();self.update()

    def eventFilter(self,watched,event):
        if self.finished or self.finishing:return False
        if event.type()==QtCore.QEvent.KeyPress and event.key()==QtCore.Qt.Key_Escape:
            self.finish();return True
        if event.type() in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseButtonRelease):
            # MPxTexContext.doPress is not delivered consistently by Maya's
            # Qt UV viewport. Consume the real widget click before Maya's
            # selection handler, including its release, then restore the tool.
            viewport=self.viewport
            inside=(viewport is not None and isValid(viewport) and isinstance(watched,QtWidgets.QWidget)
                    and (watched is viewport or viewport.isAncestorOf(watched)))
            if event.type()==QtCore.QEvent.MouseButtonRelease and event.button()==QtCore.Qt.LeftButton and self.pressed_tile is not None:
                self.finish(self.pressed_tile);return True
            if inside and event.type()==QtCore.QEvent.MouseButtonPress:
                point=viewport.mapFromGlobal(event.globalPos())
                if not viewport.rect().contains(point):return False
                if event.button()==QtCore.Qt.RightButton:self.finish();return True
                if event.button()==QtCore.Qt.LeftButton and not event.modifiers()&QtCore.Qt.AltModifier:
                    self.pressed_tile=tile_at(point,self.bounds(),viewport.width(),viewport.height())
                    return True
        return False

    def bounds(self):
        values=[om.MScriptUtil() for _ in range(4)]
        for value in values:value.createFromDouble(0.)
        pointers=[value.asDoublePtr() for value in values];self.context.viewRect(*pointers)
        result=tuple(om.MScriptUtil.getDouble(p) for p in pointers)
        tile_at(QtCore.QPointF(),result,1,1)
        return result

    def current_tile(self):
        point=self.viewport.mapFromGlobal(QtGui.QCursor.pos())
        if not self.viewport.rect().contains(point):return None
        return tile_at(point,self.bounds(),self.viewport.width(),self.viewport.height())

    def update(self):
        if self.finishing or self.finished:return
        if self.viewport is None or not isValid(self.viewport):self.finish(restore_tool=True);return
        try:
            bounds=self.bounds();tile=self.current_tile()
            self.overlay.setGeometry(QtCore.QRect(self.viewport.mapToGlobal(QtCore.QPoint()),self.viewport.size()))
            self.overlay.bounds=bounds;self.overlay.tile=tile
            self.overlay.setVisible(tile is not None and self.viewport.isVisible() and not self.viewport.window().isMinimized())
            self.overlay.update()
        except (RuntimeError,ValueError):self.overlay.hide()

    def pick(self):
        if self.finishing or self.finished:return
        tile=self.current_tile()
        if tile is not None:self.finish(tile)

    def finish(self,tile=None,restore_tool=True):
        if self.finishing or self.finished:return
        self.finishing=True;self.timer.stop()
        if self.overlay is not None and isValid(self.overlay):self.overlay.hide()
        # Never change/destroy a Maya context while its doPress is on the stack.
        QtCore.QTimer.singleShot(0,lambda:self.cleanup(tile,restore_tool))

    def cleanup(self,tile,restore_tool):
        global active
        if self.finished:return
        if active is self:active=None
        self.finished=True;self.finishing=True;self.timer.stop()
        QtWidgets.QApplication.instance().removeEventFilter(self)
        from uv_straight_native import _no_undo
        try:
            self.restore(restore_tool,_no_undo)
        finally:
            if self.overlay is not None and isValid(self.overlay):self.overlay.close();self.overlay.deleteLater()
            if self.context_name:
                try:cmds.deleteUI(self.context_name,toolContext=True)
                except RuntimeError:pass
            try:
                if tile is not None:self.callback(*tile)
            finally:self.deleteLater()

    def restore(self,restore_tool,no_undo):
        with no_undo():
            if restore_tool and cmds.currentCtx()==self.context_name:cmds.setToolTo(self.previous_tool)
            for key,on in self.modes.items():
                if on and not cmds.selectMode(query=True,**{key:True}):cmds.selectMode(**{key:True});break
            for obj,key,on in self.masks:
                options=dict({key:bool(on)},**({'objectComponent':True} if obj else {}))
                if on is not None and cmds.selectType(query=True,**dict({key:True},**({'objectComponent':True} if obj else {})))!=on:cmds.selectType(**options)
            current=cmds.ls(hilite=True,long=True) or []
            if current!=self.hilite:
                if current:cmds.hilite(current,unHilite=True)
                if self.hilite:cmds.hilite(self.hilite,replace=True)
            if (cmds.ls(selection=True,long=True) or [])!=self.selection:
                cmds.select(self.selection,replace=True) if self.selection else cmds.select(clear=True)
            if restore_tool and self.previous_focus:
                try:cmds.setFocus(self.previous_focus)
                except RuntimeError:pass


def choose_tile(callback):
    global active
    if active is not None:active.finish();raise ValueError('UV tile selection is already active.')
    active=TilePicker(callback)
    try:active.start()
    except Exception:
        active.finish();raise
