"""Wrapping, removable UV-set tags using the existing Maya/Qt palette."""
import weakref
import json
from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid
import maya.cmds as cmds
from uv_straight_uv_merge import keep_names

OPTION = 'shellShifterKeepUVSets'
_editors = globals().get('_editors', weakref.WeakSet())


class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.arrange(QtCore.QRect(0, 0, width, 0), True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self.items:
            size = size.expandedTo(item.minimumSize())
        return size

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.arrange(rect)

    def arrange(self, rect, measure=False):
        x, y, line = rect.x(), rect.y(), 0
        for item in self.items:
            size = item.sizeHint()
            size.setWidth(min(size.width(), max(1, rect.width())))
            if x > rect.x() and x + size.width() > rect.right()+1:
                x = rect.x()
                y += line + self.spacing()
                line = 0
            if not measure:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), size))
            x += size.width() + self.spacing()
            line = max(line, size.height())
        return y + line - rect.y()


class KeepSetTags(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('keepUVSetTags')
        self.names = ('map1',)
        if cmds.optionVar(exists=OPTION):
            try:
                saved = json.loads(cmds.optionVar(query=OPTION))
                if isinstance(saved, list):
                    self.names = keep_names(saved) if saved else ()
            except (ValueError, TypeError):
                pass
        self.flow = FlowLayout(self)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)
        _editors.add(self)
        self.rebuild()

    def rebuild(self):
        while self.flow.count():
            item = self.flow.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        for index, name in enumerate(self.names):
            chip = QtWidgets.QFrame(self)
            chip.setObjectName('keepUVSetTag')
            chip.setStyleSheet('QFrame#keepUVSetTag { background: #505050; border-radius: 12px; }')
            row = QtWidgets.QHBoxLayout(chip)
            row.setContentsMargins(10, 2, 4, 2)
            row.setSpacing(3)
            label = QtWidgets.QLabel(name)
            label.setStyleSheet('color: '+('#9edbff' if index == 0 else '#ededed')+'; background: transparent;')
            row.addWidget(label)
            remove = QtWidgets.QToolButton(chip)
            remove.setText('×')
            remove.setObjectName('removeKeepUVSet_'+name)
            remove.setFixedSize(22, 22)
            remove.setToolTip('Remove '+name+' from sets to keep')
            remove.setStyleSheet('QToolButton { border: none; background: transparent; color: #bcbcbc; font-size: 17px; } QToolButton:hover { color: #ff8e83; background: #626262; border-radius: 9px; }')
            remove.clicked.connect(lambda checked=False, name=name: self.remove_name(name))
            row.addWidget(remove)
            chip.setToolTip(('Cleanup destination. ' if index == 0 else 'Protected set. Its UVs remain unchanged. ')+name)
            self.flow.addWidget(chip)
        add = QtWidgets.QToolButton(self)
        add.setObjectName('addKeepUVSet')
        add.setText('+')
        add.setFixedSize(28, 26)
        add.setToolTip('Add a UV set to keep')
        add.setStyleSheet('QToolButton { color: #9edbff; background: #505050; border: none; border-radius: 12px; font-size: 19px; } QToolButton:hover { background: #636363; }')
        add.clicked.connect(self.add_dialog)
        self.flow.addWidget(add)
        self.updateGeometry()

    def set_names(self, names):
        names = keep_names(names) if names else ()
        cmds.optionVar(stringValue=(OPTION, json.dumps(names)))
        for editor in tuple(_editors):
            if not isValid(editor):
                _editors.discard(editor)
                continue
            try:
                if editor.names != names:
                    editor.names = names
                    editor.rebuild()
                    editor.changed.emit()
            except RuntimeError:
                _editors.discard(editor)

    def add_name(self, name):
        self.set_names(self.names + (name.strip(),))

    def remove_name(self, name):
        self.set_names(tuple(entry for entry in self.names if entry != name))

    def add_dialog(self):
        text, accepted = QtWidgets.QInputDialog.getText(
            self, 'Keep UV Set', 'UV set name:')
        if accepted:
            try:
                self.add_name(text)
            except ValueError as error:
                QtWidgets.QMessageBox.information(self, 'UV Set Name', str(error))
