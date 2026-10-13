"""Rounded function palette with nonblocking pointer-driven copy dragging."""
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import isValid
import uv_straight_pie as pie


class FunctionButton(QtWidgets.QToolButton):
    def __init__(self, palette, action, group, title):
        super().__init__(palette.contents)
        self.palette,self.action,self.group=palette,action,group
        self.node=pie.entry(action)
        self.origin=None
        self.setObjectName('pieCatalogFunction')
        self.setText(title)
        self.setAccessibleName(title)
        self.setToolTip(group+' · '+title+ ('\nDrag into the pie menu. Click to add.'
            if palette.draggable else '\nClick to add a shortcut.'))
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Fixed)
        self.setFixedHeight(28)
        self.setCursor(QtCore.Qt.OpenHandCursor if palette.draggable else QtCore.Qt.PointingHandCursor)
        self.clicked.connect(lambda:palette.choose(action))

    def paintEvent(self, event):
        painter=QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        if not self.isEnabled():painter.setOpacity(.45)
        self.palette.editor.canvas.paint_entry(painter,dict(node=self.node,
            rect=QtCore.QRectF(self.rect()).adjusted(1,1,-1,-1)),hovered=self.underMouse() or self.isDown())
        painter.end()

    def enterEvent(self, event):
        super().enterEvent(event);self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event);self.update()

    def mousePressEvent(self, event):
        if event.button()==QtCore.Qt.LeftButton:
            self.origin=event.position().toPoint();self.setDown(True)
            self.setFocus(QtCore.Qt.MouseFocusReason)
            event.accept();return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self.palette.draggable and self.origin is not None and event.buttons() & QtCore.Qt.LeftButton and
                (event.position().toPoint()-self.origin).manhattanLength()>=QtWidgets.QApplication.startDragDistance()):
            self.origin=None;self.setDown(False)
            self.palette.start_drag(self,event.globalPosition().toPoint())
            event.accept();return
        if self.origin is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.setDown(self.rect().contains(event.position().toPoint()))
            event.accept();return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button()==QtCore.Qt.LeftButton:
            clicked=self.origin is not None and self.rect().contains(event.position().toPoint())
            self.origin=None;self.setDown(False)
            event.accept()
            if clicked:self.click()
            return
        self.origin=None
        super().mouseReleaseEvent(event)


class FunctionDragGhost(QtWidgets.QWidget):
    """Input-transparent pill follows the pointer without a native drag loop."""
    def __init__(self, palette, node, width):
        flags=(QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint | QtCore.Qt.NoDropShadowWindowHint |
               QtCore.Qt.WindowTransparentForInput | QtCore.Qt.WindowDoesNotAcceptFocus)
        super().__init__(palette,flags)
        self.canvas=palette.editor.canvas
        self.node=node
        self.setObjectName('pieCatalogDragGhost')
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setFixedSize(width,28)

    def follow(self, point):
        self.move(point-QtCore.QPoint(self.width()//2,14))
        if self.isHidden():self.show()
        self.raise_()

    def paintEvent(self, event):
        painter=QtGui.QPainter(self)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(),QtCore.Qt.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        painter.setRenderHint(QtGui.QPainter.Antialiasing);painter.setOpacity(.92)
        rect=QtCore.QRectF(self.rect()).adjusted(1,1,-1,-1)
        self.canvas.paint_entry(painter,dict(node=self.node,rect=rect),hovered=True)
        painter.setOpacity(1.)
        painter.setPen(QtGui.QPen(QtGui.QColor('#a0a0a0'),1));painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(rect,self.canvas.entry_radius(rect),self.canvas.entry_radius(rect))
        painter.end()


class FunctionPicker(QtWidgets.QDialog):
    """Use Popup rather than exec: the editor remains available as a drop target."""
    def __init__(self, editor, callback, *, draggable=True, title='Pie Menu Functions', catalog=None, parent=None):
        super().__init__(parent if parent is not None else editor,
            QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint | QtCore.Qt.NoDropShadowWindowHint)
        self.editor,self.callback=editor,callback
        self.draggable=bool(draggable)
        self.catalog=tuple(pie.CATALOG if catalog is None else catalog)
        self.dragging=False
        self.ghost=self.source_button=None
        self.cursor_override=False
        if self.draggable:editor.canvas.drag.cleared.connect(self.overlay_cleared)
        self.setObjectName('pieFunctionPalette')
        self.setWindowTitle(title)
        self.setModal(False)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_NoMouseReplay)
        self.setAutoFillBackground(False)
        self.setFixedWidth(326)
        self.setStyleSheet(
            'QLabel { color: #eeeeee; background: transparent; }'
            'QLabel[heading="true"] { color: #aaaaaa; padding-top: 9px; padding-bottom: 2px; }'
            'QLineEdit { background: #474747; color: #eeeeee; border: 1px solid #555555; '
            'border-radius: 7px; padding: 5px 7px; min-height: 18px; }'
            'QScrollArea, QWidget#pieCatalogContents { background: transparent; border: 0; }'
            'QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }'
            'QScrollBar::handle:vertical { background: #666666; border-radius: 4px; min-height: 24px; }'
            'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }'
            'QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }'
            'QToolButton#pieCatalogClose { background: #494949; color: #eeeeee; border: 0; border-radius: 11px; }'
            'QToolButton#pieCatalogClose:hover { background: #626262; }')
        layout=QtWidgets.QVBoxLayout(self);layout.setContentsMargins(12,12,12,12);layout.setSpacing(8)
        header=QtWidgets.QHBoxLayout()
        self.title=QtWidgets.QLabel(title);self.title.setAlignment(QtCore.Qt.AlignCenter)
        header.addWidget(self.title,1)
        close=QtWidgets.QToolButton();close.setObjectName('pieCatalogClose');close.setText('×')
        close.setFixedSize(22,22);close.setToolTip('Close');close.clicked.connect(self.close)
        header.addWidget(close);layout.addLayout(header)
        self.search=QtWidgets.QLineEdit();self.search.setPlaceholderText('Search functions...')
        self.search.setClearButtonEnabled(True);layout.addWidget(self.search)
        self.scroll=QtWidgets.QScrollArea();self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.contents=QtWidgets.QWidget();self.contents.setObjectName('pieCatalogContents')
        rows=QtWidgets.QVBoxLayout(self.contents);rows.setContentsMargins(0,0,4,0);rows.setSpacing(4)
        self.buttons=[];self.groups={}
        for action,group,title in self.catalog:
            if group not in self.groups:
                heading=QtWidgets.QLabel(group);heading.setProperty('heading',True)
                rows.addWidget(heading);self.groups[group]=[heading,[]]
            button=FunctionButton(self,action,group,title)
            self.buttons.append(button);self.groups[group][1].append(button);rows.addWidget(button)
        self.empty=QtWidgets.QLabel('No matching functions');self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.hide();rows.addWidget(self.empty);rows.addStretch()
        self.scroll.setWidget(self.contents);layout.addWidget(self.scroll,1)
        self.search.textChanged.connect(self.filter_functions)
        self.resize(self.width(),480)

    def filter_functions(self, text):
        text=text.casefold().strip()
        for button in self.buttons:
            button.setVisible(text in (button.group+' '+button.text()).casefold())
        for heading,buttons in self.groups.values():
            heading.setVisible(any(not button.isHidden() for button in buttons))
        self.empty.setVisible(all(button.isHidden() for button in self.buttons))
        self.scroll.verticalScrollBar().setValue(0)

    def open_at(self, anchor):
        screen=QtGui.QGuiApplication.screenAt(anchor.center()) or QtGui.QGuiApplication.primaryScreen()
        available=screen.availableGeometry()
        self.resize(self.width(),min(480,available.height()-24))
        x=anchor.right()+8
        if x+self.width()>available.right():x=anchor.left()-self.width()-8
        x=max(available.left()+4,min(x,available.right()-self.width()-4))
        y=max(available.top()+4,min(anchor.top(),available.bottom()-self.height()-4))
        self.move(x,y);self.show();self.raise_();self.search.setFocus()

    def choose(self, action):
        button=next((button for button in self.buttons if button.action==action),None)
        if (button is not None and button.isEnabled() and not self.dragging and
                (not self.draggable or self.editor.editing)):
            self.callback(action)
            self.close()

    def start_drag(self, button, global_point=None):
        if not self.draggable or self.dragging or not self.editor.editing:return
        canvas=self.editor.canvas
        if canvas.drag.source:canvas.drag.cancel()
        global_point=global_point if global_point is not None else button.mapToGlobal(button.rect().center())
        canvas.drag.begin_external(button.action,QtCore.QPointF(canvas.mapFromGlobal(global_point)),pointer_owner=self)
        self.dragging=True
        self.source_button=button;button.setDown(False);button.origin=None
        try:
            self.ghost=FunctionDragGhost(self,canvas.drag.external,int(canvas.drag.floating.width()))
            QtWidgets.QApplication.instance().installEventFilter(self)
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.ClosedHandCursor);self.cursor_override=True
            self.move_pointer(global_point)
        except Exception:
            canvas.drag.cancel();self.cleanup_pointer()
            raise

    def over_canvas(self, global_point):
        canvas=self.editor.canvas
        if not isValid(canvas) or not canvas.isVisible() or not self.editor.editing:return False
        # The destination must be visible, including when the editor is scrolled.
        point=canvas.mapFromGlobal(global_point)
        return canvas.visibleRegion().contains(point) and not self.frameGeometry().contains(global_point)

    def move_pointer(self, global_point):
        if not self.dragging:return
        canvas=self.editor.canvas
        if not isValid(canvas) or not canvas.drag.external or canvas.drag.settling:
            self.cleanup_pointer();return
        if self.ghost is not None and isValid(self.ghost):self.ghost.follow(global_point)
        point=QtCore.QPointF(canvas.mapFromGlobal(global_point)) if self.over_canvas(global_point) else QtCore.QPointF(-10000,-10000)
        canvas.drag.move_entry(point)

    def finish_pointer(self, global_point=None, cancel=False):
        if not self.dragging:return
        canvas=self.editor.canvas
        try:
            if isValid(canvas) and canvas.drag.external and not canvas.drag.settling:
                if cancel:
                    canvas.drag.cancel()
                else:
                    self.move_pointer(global_point)
                    if not self.editor.edit(canvas.drag.finish):canvas.drag.cancel()
        finally:
            self.cleanup_pointer()

    def cleanup_pointer(self):
        self.dragging=False
        QtWidgets.QApplication.instance().removeEventFilter(self)
        if self.cursor_override:
            QtWidgets.QApplication.restoreOverrideCursor();self.cursor_override=False
        if self.source_button is not None and isValid(self.source_button):
            self.source_button.setDown(False);self.source_button.origin=None
            if QtWidgets.QWidget.mouseGrabber() is self.source_button:self.source_button.releaseMouse()
        self.source_button=None
        if self.ghost is not None and isValid(self.ghost):
            self.ghost.hide();self.ghost.deleteLater()
        self.ghost=None

    def overlay_cleared(self):
        if self.dragging:self.cleanup_pointer()

    def eventFilter(self, watched, event):
        if not self.dragging:return False
        kind=event.type()
        if kind in (QtCore.QEvent.ShortcutOverride,QtCore.QEvent.KeyPress) and event.key()==QtCore.Qt.Key_Escape:
            if kind==QtCore.QEvent.KeyPress:self.finish_pointer(cancel=True)
            event.accept();return True
        # Let QWidgetWindow update Qt's implicit mouse grab before handling the
        # forwarded mouse event. Escape must be consumed at either event level
        # so the popup's native window cannot also close it after cancellation.
        if kind!=QtCore.QEvent.ApplicationDeactivate and not isinstance(watched,QtWidgets.QWidget):
            return False
        if kind==QtCore.QEvent.MouseMove:
            if event.buttons() & QtCore.Qt.LeftButton:self.move_pointer(event.globalPosition().toPoint())
            else:self.finish_pointer(cancel=True)
            event.accept();return True
        if kind==QtCore.QEvent.MouseButtonRelease and event.button()==QtCore.Qt.LeftButton:
            self.finish_pointer(event.globalPosition().toPoint());event.accept();return True
        if kind==QtCore.QEvent.MouseButtonPress:
            if event.button()==QtCore.Qt.RightButton:self.finish_pointer(cancel=True)
            event.accept();return True
        if kind==QtCore.QEvent.Wheel:
            event.accept();return True
        if kind==QtCore.QEvent.ApplicationDeactivate:
            self.finish_pointer(cancel=True)
        return False

    def hideEvent(self, event):
        if self.dragging:self.finish_pointer(cancel=True)
        super().hideEvent(event)
        self.deleteLater()

    def closeEvent(self, event):
        if self.dragging:self.finish_pointer(cancel=True)
        super().closeEvent(event)
        self.deleteLater()

    def paintEvent(self, event):
        painter=QtGui.QPainter(self);painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(),QtCore.Qt.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        painter.setRenderHint(QtGui.QPainter.Antialiasing);painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor('#303030'));painter.drawRoundedRect(QtCore.QRectF(self.rect()),14,14)
        painter.end()
