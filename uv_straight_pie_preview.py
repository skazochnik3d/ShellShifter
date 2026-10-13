"""Inline native-style marking-menu preview with direct manipulation."""
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid
import uv_straight_pie as pie
from uv_straight_pie_drag import PieDragOverlay
from uv_straight_pie_catalog import FunctionPicker


class PieFooterButton(QtWidgets.QToolButton):
    SIZE = 32

    def __init__(self, parent):
        super().__init__(parent)
        self.dragging = self.drop_active = False
        self.setObjectName('pieAddRemoveButton')
        self.setFixedSize(self.SIZE,self.SIZE)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.set_mode(False)
        self.hide()

    def set_mode(self, dragging, drop_active=False):
        self.dragging,self.drop_active=bool(dragging),bool(drop_active)
        title='Drop Here to Remove Function' if dragging else 'Add Function to Pie Menu'
        self.setToolTip(title)
        self.setAccessibleName(title)
        self.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        painter=QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect=QtCore.QRectF(self.rect()).adjusted(1,1,-1,-1)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor('#424242' if self.isDown() else
                                     '#626262' if self.underMouse() or self.drop_active else '#525252'))
        painter.drawEllipse(rect)
        pen=QtGui.QPen(QtGui.QColor('#eeeeee'),1.6)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        painter.setPen(pen);painter.setBrush(QtCore.Qt.NoBrush)
        center=rect.center();x,y=center.x(),center.y()
        if self.dragging:
            painter.drawLine(QtCore.QLineF(x-6,y-5,x+6,y-5))
            painter.drawRoundedRect(QtCore.QRectF(x-2,y-8,4,3),1,1)
            painter.drawRoundedRect(QtCore.QRectF(x-4.5,y-3,9,10),1,1)
            for offset in (-1.5,1.5):
                painter.drawLine(QtCore.QLineF(x+offset,y-1,x+offset,y+4))
        else:
            painter.drawLine(QtCore.QLineF(x-6,y,x+6,y))
            painter.drawLine(QtCore.QLineF(x,y-6,x,y+6))
        if self.hasFocus():
            painter.setPen(QtGui.QPen(QtGui.QColor('#a0a0a0'),1,QtCore.Qt.DashLine))
            painter.drawEllipse(rect.adjusted(2,2,-2,-2))
        painter.end()


class NativePieCanvas(QtWidgets.QWidget):
    """Draw/hit-test menu entries in Maya's staggered radial arrangement."""
    ROW = 24
    BUTTON_RADIUS = 12.
    INSERT_SNAP = 8.
    INSERT_SIDE = 10.

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setObjectName('nativePieCanvas')
        self.setMinimumWidth(240)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(QtCore.Qt.PreventContextMenu)
        self.regions = []
        self.panels = []
        self.origin = None
        self.pressed = None
        self.hover = None
        self.drop_target = None
        self.expanded = []
        self.states = {}
        self.background = '#383838'
        self.footer = PieFooterButton(self)
        self.footer.clicked.connect(self.add_from_footer)
        self.drag = PieDragOverlay(self)
        self.refresh()

    def menu_font(self):
        font = QtGui.QFont(self.font())
        font.setPixelSize(11)
        return font

    def refresh(self):
        self.expanded = [identifier for identifier in self.expanded if pie.node_by_id(identifier)]
        self.states = {}
        def scan(nodes):
            for node in nodes:
                if not node:
                    continue
                if node['kind'] == 'action':
                    self.states[node['id']] = pie.checked(node['action'])
                elif node['kind'] == 'submenu':
                    scan(node['children'])
        scan(list(pie._layout['slots'].values()) + pie._layout['items'])
        self.reflow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def hideEvent(self, event):
        self.drag.cancel()
        super().hideEvent(event)

    def reflow(self):
        width = max(240, self.width())
        metrics = QtGui.QFontMetrics(self.menu_font())
        self.regions, self.panels = [], []
        value = self.drag.proposal if hasattr(self, 'drag') and self.drag.proposal is not None else pie._layout
        self.pie_center, self.regions, y = pie.radial_geometry(width, metrics, value)
        if not self.regions and self.editor.editing:
            # One add target for an empty ring, never holes among active entries.
            self.pie_center = QtCore.QPointF(width*.5, 30)
            self.regions.append(dict(rect=QtCore.QRectF(width*.5-17, 20, 34, 20),
                                     node=None, direction='N', parent=None, before=None))
            y = 65
        self.radial_bottom = y
        # Open submenu panels are part of this same draggable canvas.
        for identifier in self.expanded:
            node = pie.node_by_id(identifier, value)
            if not node or node['kind'] != 'submenu':
                continue
            y = self.add_panel(node['children'], y, identifier, node['label'], metrics)
            y += 12
        y = self.add_panel(value['items'], y, None, None, metrics)
        if self.editor.editing:
            # Keep the trash target still while the existing live preview moves.
            if self.drag.source and not self.drag.footer_rect.isEmpty():
                rect=self.drag.footer_rect
                height=int(max(y+20,self.drag.original_height,rect.bottom()+12))
            else:
                rect=QtCore.QRectF((self.width()-self.footer.SIZE)*.5,y+20,self.footer.SIZE,self.footer.SIZE)
                height=int(rect.bottom()+12)
            self.footer.setGeometry(rect.toRect())
            self.footer.show()
        else:
            height = int(y+20)
            self.footer.hide()
        if height != self.height():
            self.setFixedHeight(height)
            self.updateGeometry()
        if hasattr(self, 'drag'):
            self.drag.setGeometry(self.rect())
        self.update_footer()
        self.update()

    def add_from_footer(self):
        if self.editor.editing and not self.drag.source:
            self.editor.add_to_list()

    def update_footer(self, drop_active=False):
        self.footer.set_mode(bool(self.drag.source),drop_active)
        if self.editor.editing:
            self.footer.raise_()

    def trash_hit(self, point):
        if not self.editor.editing or self.footer.isHidden():
            return False
        rect=self.drag.footer_rect if self.drag.source else QtCore.QRectF(self.footer.geometry())
        delta=point-rect.center()
        return delta.x()**2+delta.y()**2<=(rect.width()*.5+6)**2

    def add_panel(self, nodes, y, parent, title, metrics):
        from uv_straight_icons import PATHS
        rows = pie.list_rows(nodes)
        widths = [metrics.horizontalAdvance(row[0].get('label', ''))+46 if len(row) == 1 else
                  2*(max(metrics.horizontalAdvance(n['label']) for n in row)+34)+10 for row in rows]
        width = min(self.width()-24, max([155]+widths))
        x = (self.width()-width)*.5
        header = 21 if title else 0
        heights = []
        for row in rows:
            if len(row) == 1:
                heights.append(self.ROW)
                continue
            cell_width = (width-10)*.5
            text_heights = []
            for node in row:
                padding = 27 if self.states.get(node['id']) is not None else 34 if pie.icon_key(node) in PATHS else 12
                bounds = metrics.boundingRect(QtCore.QRect(0, 0, int(cell_width-padding), 1000),
                                              QtCore.Qt.TextWordWrap, node['label'])
                text_heights.append(bounds.height()+6)
            heights.append(max([20]+text_heights)+4)
        height = header+(sum(heights) if rows else self.ROW)+8
        rect = QtCore.QRectF(x, y, width, height)
        self.panels.append(dict(rect=rect, parent=parent, title=title))
        indices = {node['id']: index for index, node in enumerate(nodes)}
        row_y = y+header+4
        for i, row in enumerate(rows):
            cell_width = (width-6-(4 if len(row) == 2 else 0))/len(row)
            for column, node in enumerate(row):
                index = indices[node['id']]
                self.regions.append(dict(rect=QtCore.QRectF(x+3+column*(cell_width+4),
                                                         row_y+2,
                                                         cell_width, heights[i]-4),
                                         node=node, direction=None, parent=parent, paired=len(row) == 2,
                                         wrap=len(row) == 2,
                                         before=nodes[index+1]['id'] if index+1 < len(nodes) else None))
            row_y += heights[i]
        return y+height

    def hit(self, point):
        return next((region for region in self.regions if region['rect'].contains(point)), None)

    def panel_at(self, point):
        return next((panel for panel in self.panels if panel['rect'].contains(point)), None)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        self.paint_contents(painter)
        painter.end()

    def paint_contents(self, painter):
        """Shared renderer also supports runtime page opacity without effects."""
        if self.background:
            painter.fillRect(self.rect(), QtGui.QColor(self.background))
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setFont(self.menu_font())
        self.paint_underlay(painter)
        painter.setPen(QtGui.QPen(QtGui.QColor('#151515'), 1))
        painter.setBrush(QtGui.QColor('#777777'))
        painter.drawEllipse(self.pie_center, 5, 5)
        for panel in self.panels:
            if panel['title']:
                painter.setPen(QtGui.QColor('#94bbd6'))
                painter.drawText(panel['rect'].adjusted(8, 0, -8, -panel['rect'].height()+20),
                                 QtCore.Qt.AlignVCenter,
                                 QtGui.QFontMetrics(painter.font()).elidedText(panel['title'], QtCore.Qt.ElideRight,
                                                                           int(panel['rect'].width()-16)))
        for region in self.regions:
            node, rect = region['node'], region['rect']
            if node is not None and self.drag.source:
                continue
            selected = (region['direction'] == self.editor.direction if region['direction'] else
                        node is not None and node['id'] == self.editor.identifier)
            hovered = region is self.hover
            if node is None:
                if self.editor.editing or self.drop_target:
                    painter.setPen(QtGui.QPen(QtGui.QColor('#61727f'), 1, QtCore.Qt.DashLine))
                    painter.setBrush(QtGui.QColor('#363b40'))
                    painter.drawRoundedRect(rect,self.entry_radius(rect),self.entry_radius(rect))
                    painter.setPen(QtGui.QColor('#91b8d4'))
                    painter.drawText(rect, QtCore.Qt.AlignCenter, '+')
                continue
            self.paint_entry(painter, region, selected, hovered)
        if self.drop_target:
            rect, inside, below = self.drop_target
            painter.setPen(QtGui.QPen(QtGui.QColor('#727272'), 1, QtCore.Qt.DashLine))
            painter.setBrush(QtCore.Qt.NoBrush)
            if inside:
                painter.drawRoundedRect(rect,self.entry_radius(rect),self.entry_radius(rect))
            else:
                y = rect.bottom() if below else rect.top()
                painter.drawLine(QtCore.QLineF(rect.left(), y, rect.right(), y))

    def entry_radius(self, rect):
        return min(self.BUTTON_RADIUS,rect.width()*.5,rect.height()*.5)

    def paint_entry(self, painter, region, selected=False, hovered=False):
        """Draw labels and icons at their native size, including during a drag."""
        node, rect = region['node'], region['rect']
        painter.save()
        painter.setClipRect(rect.adjusted(-1, -1, 1, 1), QtCore.Qt.IntersectClip)
        painter.setFont(self.menu_font())
        if node['kind'] == 'separator':
            painter.setPen(QtGui.QColor('#6a6a6a'))
            painter.drawLine(QtCore.QLineF(rect.left()+7, rect.center().y(), rect.right()-7, rect.center().y()))
            painter.restore()
            return
        painter.setPen(QtGui.QPen(QtGui.QColor('#85bbdd' if selected else '#111111'), 1))
        painter.setBrush(QtGui.QColor('#486080' if selected else '#5a6066' if hovered else '#494949'))
        painter.drawRoundedRect(rect,self.entry_radius(rect),self.entry_radius(rect))
        start = rect.left()+6
        from uv_straight_icons import PATHS, PIE_SIZE, paint_icon
        key = pie.icon_key(node)
        state = self.states.get(node['id'])
        if state is not None:
            box = QtCore.QRectF(start, rect.center().y()-5, 10, 10)
            painter.setPen(QtGui.QPen(QtGui.QColor('#b8b8b8'), 1))
            painter.setBrush(QtGui.QColor('#373737'))
            painter.drawRect(box)
            if state:
                painter.setPen(QtGui.QPen(QtGui.QColor('#eef2f6'), 1.5))
                painter.drawPolyline(QtGui.QPolygonF([box.topLeft()+QtCore.QPointF(1, 5),
                                                     box.topLeft()+QtCore.QPointF(4, 8),
                                                     box.topLeft()+QtCore.QPointF(9, 2)]))
            start += 15
        elif key in PATHS:
            paint_icon(painter,QtCore.QRectF(start,rect.center().y()-PIE_SIZE*.5,PIE_SIZE,PIE_SIZE),key)
            start += PIE_SIZE+4
        right = rect.right()-6
        if node['kind'] == 'submenu':
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor('#d6d6d6'))
            painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(right-4, rect.center().y()-3),
                                                 QtCore.QPointF(right, rect.center().y()),
                                                 QtCore.QPointF(right-4, rect.center().y()+3)]))
            right -= 10
        painter.setPen(QtGui.QColor('#e9edf1' if self.entry_enabled(node) else '#858585'))
        text_rect = QtCore.QRectF(start, rect.top(), max(1, right-start), rect.height())
        if region.get('wrap'):
            painter.drawText(text_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft |
                             QtCore.Qt.TextWordWrap, node['label'])
        else:
            title = QtGui.QFontMetrics(painter.font()).elidedText(node['label'], QtCore.Qt.ElideRight, int(text_rect.width()))
            painter.drawText(text_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, title)
        painter.restore()

    def entry_enabled(self, node):
        return True

    def paint_underlay(self, painter):
        pass

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.origin = event.position().toPoint()
            self.pressed = self.hit(event.position())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag.source and not self.drag.settling:
            self.drag.move_entry(event.position())
            event.accept()
            return
        target = self.hit(event.position())
        if (self.editor.editing and self.pressed and self.pressed['node'] and self.origin is not None and
                event.buttons() & QtCore.Qt.LeftButton and
                (event.position().toPoint()-self.origin).manhattanLength() >= QtWidgets.QApplication.startDragDistance()):
            self.drag.begin(self.pressed, QtCore.QPointF(self.origin))
            self.origin = None
            self.pressed = None
            self.drag.move_entry(event.position())
            return
        self.hover = target
        self.setCursor(QtCore.Qt.OpenHandCursor if target and self.editor.editing and target['node'] else QtCore.Qt.ArrowCursor)
        self.setToolTip((target['node'].get('label', 'Separator') if target and target['node'] else 'Empty position')+
                        '\nDrag to move. Drop on the trash to remove.')
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.drag.source and not self.drag.settling:
            self.drag.move_entry(event.position())
            self.editor.edit(self.drag.finish)
            event.accept()
            return
        target = self.hit(event.position())
        if event.button() == QtCore.Qt.LeftButton and self.origin is not None:
            if target:
                self.editor.select(target)
                node = target['node']
                if node and node['kind'] == 'submenu':
                    identifier = node['id']
                    if identifier in self.expanded:
                        self.expanded.remove(identifier)
                    else:
                        self.expanded.append(identifier)
                    self.reflow()
                elif node is None and self.editor.editing:
                    self.editor.add_function()
            elif (panel := self.panel_at(event.position())):
                self.editor.direction = None
                self.editor.identifier = panel['parent']
                self.editor.container = panel['parent']
                self.editor.refresh()
        self.origin = self.pressed = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        target = self.hit(event.position())
        if target and target['node'] and target['node']['kind'] == 'action':
            self.editor.select(target)
            self.editor.rename_selected()

    def contextMenuEvent(self, event):
        event.accept()

    def insertion_boundary(self, point):
        """Larger invisible row targets; drawing and animation stay unchanged."""
        dragging = self.drag.source and not self.drag.settling
        regions = self.drag.hit_regions if dragging else self.regions
        panels = self.drag.hit_panels if dragging else self.panels
        candidates=[]
        for panel in panels:
            rect=panel['rect']
            if not rect.left()-self.INSERT_SIDE<=point.x()<=rect.right()+self.INSERT_SIDE:
                continue
            rows={}
            for region in regions:
                if (region['node'] is not None and not region['direction'] and
                        region['parent']==panel['parent'] and rect.contains(region['rect'].center())):
                    rows.setdefault(region['rect'].top(),[]).append(region)
            rows=[sorted(row,key=lambda item:item['rect'].left()) for _,row in sorted(rows.items())]
            for index in range(len(rows)+1) if rows else ():
                top=rows[index][0]['rect'].top() if index<len(rows) else None
                bottom=max(r['rect'].bottom() for r in rows[index-1]) if index else None
                y=(top+bottom)*.5 if index and index<len(rows) else top-2 if not index else bottom+2
                if abs(point.y()-y)<=self.INSERT_SNAP:
                    candidates.append(dict(parent=panel['parent'],
                                           before=rows[index][0]['node']['id'] if index<len(rows) else None,
                                           rect=QtCore.QRectF(rect.left()+3,y,rect.width()-6,0)))
        return min(candidates,key=lambda item:abs(point.y()-item['rect'].top()),default=None)

    def drop_spec(self, point):
        dragging = self.drag.source and not self.drag.settling
        if dragging and self.trash_hit(point):
            return ('remove',self.drag.source)
        boundary=self.insertion_boundary(point)
        if boundary:
            return ('list',boundary['parent'],boundary['before'])
        target = self.drag.hit(point) if dragging else self.hit(point)
        if target:
            if target['direction']:
                return ('slot', target['direction'])
            node, rect = target['node'], target['rect']
            source = self.drag.external or (pie.node_by_id(self.drag.source) if self.drag.source else None)
            if target.get('paired') and source and source.get('pair') == node.get('pair') and source['id'] != node['id']:
                source_location = pie.locate(pie._layout, source['id'])
                target_location = pie.locate(pie._layout, node['id'])
                if source_location[0] is target_location[0] and isinstance(source_location[0], list):
                    return ('swap', node['id'])
            relative = (point.y()-rect.top())/rect.height()
            horizontal = (point.x()-rect.left())/rect.width()
            if (source and source['kind'] == 'action' and node['kind'] == 'action' and
                    source['id'] != node['id'] and .25 <= relative <= .75):
                return ('pair', node['id'], 'left' if horizontal < .5 else 'right')
            if node['kind'] == 'submenu' and .25 <= relative <= .75:
                return ('list', node['id'], None)
            return ('list', target['parent'], target['before'] if relative > .5 else node['id'])
        empty = self.ring_drop_direction(point)
        if empty:
            return ('slot', empty)
        panel = self.drag.panel_at(point) if dragging else self.panel_at(point)
        return ('list', panel['parent'], None) if panel else None

    def ring_drop_direction(self, point):
        if point.y() > self.radial_bottom-12 or point.y() < 8:
            return None
        delta = point-self.pie_center
        distance = (delta.x()**2+delta.y()**2)**.5
        radius = max(58., self.pie_center.y()-24.)
        if 18 < distance < radius+25:
            return next((direction for direction in pie.CLOCKWISE if pie._layout['slots'][direction] is None), None)
        return None

    def propose_drop(self, payload, spec):
        if spec[0] == 'remove':
            return pie.layout() if 'action' in payload else pie.remove(payload['id'],commit=False)
        if spec[0] == 'slot':
            return pie.put_slot(spec[1], payload, commit=False)
        if spec[0] == 'swap':
            return pie.swap_list_entries(payload['id'], spec[1], commit=False)
        if spec[0] == 'pair':
            return pie.pair_entries(payload if 'action' in payload else payload['id'], spec[1], spec[2], commit=False)
        return pie.insert(payload, spec[1], spec[2], commit=False)

    def leaveEvent(self, event):
        self.hover = None
        self.update()
        super().leaveEvent(event)

    def dragEnterEvent(self, event):
        if self.editor.editing and event.mimeData().hasFormat(pie.MIME):
            payload=pie.read_drop(event)
            if (not isinstance(payload,dict) or ('action' in payload and
                    (not isinstance(payload['action'],str) or payload['action'] not in pie.BY_ID))):
                event.ignore();return
            if payload.get('action') in pie.BY_ID:
                self.drag.begin_external(payload['action'],event.position())
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if not self.editor.editing or not event.mimeData().hasFormat(pie.MIME):
            return
        if self.drag.external:
            self.drag.move_entry(event.position())
            if self.drag.spec:
                event.acceptProposedAction()
            else:
                event.ignore()
            return
        target = self.hit(event.position())
        panel = self.panel_at(event.position())
        self.drop_target = None
        boundary=self.insertion_boundary(event.position())
        if boundary:
            self.drop_target=boundary['rect'],False,False
        elif target:
            rect, node = target['rect'], target['node']
            relative = (event.position().y()-rect.top())/rect.height()
            inside = bool(target['direction'] or node and node['kind'] == 'submenu' and .25 <= relative <= .75)
            self.drop_target = rect, inside, relative > .5
        elif panel:
            self.drop_target = panel['rect'], True, False
        elif self.ring_drop_direction(event.position()):
            self.drop_target = QtCore.QRectF(event.position().x()-17, event.position().y()-10, 34, 20), True, False
        if self.drop_target:
            event.acceptProposedAction()
        self.update()

    def dragLeaveEvent(self, event):
        if self.drag.external and not self.drag.settling:
            self.drag.cancel()
        self.drop_target = None
        self.update()
        event.accept()

    def dropEvent(self, event):
        if not self.editor.editing or not event.mimeData().hasFormat(pie.MIME):
            event.ignore()
            return
        if self.drag.external and not self.drag.settling:
            self.drag.move_entry(event.position())
            valid=self.drag.proposal is not None and self.drag.proposal!=self.drag.original
            succeeded=self.editor.edit(self.drag.finish)
            if valid and succeeded:event.acceptProposedAction()
            else:event.ignore()
            return
        payload = pie.read_drop(event)
        target = self.hit(event.position())
        panel = self.panel_at(event.position())
        self.drop_target = None
        boundary=self.insertion_boundary(event.position())
        if boundary:
            self.editor.edit(lambda:pie.insert(payload,boundary['parent'],boundary['before']))
        elif target and target['direction']:
            self.editor.edit(lambda: pie.put_slot(target['direction'], payload))
            self.editor.direction = target['direction']
            self.editor.container = None
        elif target:
            node, rect = target['node'], target['rect']
            relative = (event.position().y()-rect.top())/rect.height()
            inside = node['kind'] == 'submenu' and .25 <= relative <= .75
            parent = node['id'] if inside else target['parent']
            before = None if inside else target['before'] if relative > .5 else node['id']
            self.editor.edit(lambda: pie.insert(payload, parent, before))
        elif panel:
            self.editor.edit(lambda: pie.insert(payload, panel['parent']))
        elif (direction := self.ring_drop_direction(event.position())):
            self.editor.edit(lambda: pie.put_slot(direction, payload))
            self.editor.direction, self.editor.container = direction, None
        else:
            event.ignore()
            self.update()
            return
        self.editor.refresh()
        event.acceptProposedAction()


class InlinePieEditor(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.editing = False
        self.direction = self.identifier = self.container = None
        self.force_list = False
        self.last_error = ''
        self.function_picker = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.canvas = NativePieCanvas(self)
        layout.addWidget(self.canvas)
        pie._clients.add(self)
        self.refresh()
        self.hide()

    def set_editing(self, enabled):
        self.editing = bool(enabled)
        if not enabled:
            self.canvas.drag.cancel()
        self.setVisible(self.editing)
        self.refresh()

    def hideEvent(self, event):
        self.close_functions()
        super().hideEvent(event)

    def close_functions(self):
        if self.function_picker is not None and isValid(self.function_picker):
            self.function_picker.close()

    def open_functions(self, callback):
        if not self.editing:return
        if self.function_picker is not None and isValid(self.function_picker):
            self.function_picker.close()
        picker=FunctionPicker(self,callback)
        self.function_picker=picker
        anchor=QtCore.QRect(self.canvas.mapToGlobal(QtCore.QPoint()),self.canvas.size())
        picker.open_at(anchor)
        return picker

    def refresh(self):
        if self.direction:
            node = pie._layout['slots'][self.direction]
            self.identifier = node['id'] if node else None
        if self.identifier and pie.node_by_id(self.identifier) is None:
            self.identifier = self.container = None
        self.canvas.refresh()
        self.updateGeometry()
        if hasattr(self.window, 'adjust_popup_size') and not self.canvas.drag.source:
            self.window.adjust_popup_size()

    def edit(self, operation):
        try:
            operation()
            self.last_error = ''
            return True
        except (ValueError, RuntimeError) as error:
            self.last_error = str(error)
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), self.last_error, self.canvas)
            return False

    def context_menu(self, point):
        region = self.canvas.hit(QtCore.QPointF(point))
        panel = self.canvas.panel_at(QtCore.QPointF(point))
        if region:
            self.select(region)
        else:
            self.direction = None
            self.identifier = self.container = panel['parent'] if panel else None
            self.force_list = panel is not None
            self.refresh()
        node = pie.node_by_id(self.identifier) if self.identifier else None
        menu = QtWidgets.QMenu(self.canvas)
        menu.setObjectName('pieEntryContextMenu')
        menu.setStyleSheet('QMenu { background: #353535; color: #ededed; border: 1px solid #686868; }'
                          'QMenu::item { padding: 6px 18px; } QMenu::item:selected { background: #486080; }'
                          'QMenu::item:disabled { color: #888888; }')
        def item(title, callback, enabled=True, parent=menu):
            action = parent.addAction(title)
            action.setEnabled(enabled)
            action.setData(callback)
            return action
        item('Add Function to Submenu...' if node and node['kind'] == 'submenu' else 'Add Function...',
             self.add_function if (node and node['kind'] == 'submenu') or panel else self.add_radial_function)
        item('Add to Lower List...', self.add_to_list)
        item('Add Submenu...', self.add_submenu)
        if (region and not region['direction']) or panel:
            item('Add Separator', self.add_separator)
        if node:
            if node['kind'] == 'action':
                item('Replace Function...', self.replace_function)
            item('Rename...', self.rename_selected, node['kind'] != 'separator')
            item('Remove', self.remove_selected)
            positions = menu.addMenu('Move to Position')
            for direction in pie.DIRECTIONS:
                item(direction, lambda direction=direction, identifier=node['id']:
                     self.edit(lambda: pie.put_slot(direction, dict(id=identifier))), parent=positions,
                     enabled=node['kind'] != 'separator')
            item('Move to Lower List', lambda identifier=node['id']: self.edit(lambda: pie.insert(dict(id=identifier))))
        menu.addSeparator()
        item('Undo Edit', lambda: pie.history(), bool(pie._undo))
        item('Redo Edit', lambda: pie.history(True), bool(pie._redo))
        item('Restore Default Pie', lambda: self.edit(lambda: pie.publish(pie.default_layout())))
        return menu

    def replace_function(self):
        identifier = self.identifier
        def replace(action):
            def operation():
                value = pie.layout()
                found = pie.locate(value, identifier)
                if found:
                    replacement = pie.entry(action)
                    if found[0][found[1]].get('pair'):
                        replacement['pair'] = found[0][found[1]]['pair']
                    found[0][found[1]] = replacement
                    pie.publish(value)
            self.edit(operation)
        return self.open_functions(replace)

    def add_separator(self):
        value = pie.layout()
        owner = pie.node_by_id(self.container, value) if self.container else None
        (owner['children'] if owner else value['items']).append(pie.entry())
        self.edit(lambda: pie.publish(value))

    def select(self, region):
        self.direction = region['direction']
        node = region['node']
        self.identifier = node['id'] if node else None
        self.container = self.identifier if node and node['kind'] == 'submenu' else region['parent']
        self.force_list = region['direction'] is None
        self.refresh()

    def add_node(self, node):
        def operation():
            value = pie.layout()
            if self.direction and not self.container:
                old = value['slots'][self.direction]
                if old:
                    value['items'].append(old)
                value['slots'][self.direction] = node
            elif self.container:
                owner = pie.node_by_id(self.container, value)
                if owner and owner['kind'] == 'submenu':
                    owner['children'].append(node)
            elif self.force_list:
                value['items'].append(node)
            else:
                empty = next((direction for direction in pie.DIRECTIONS if value['slots'][direction] is None), None)
                if empty:
                    value['slots'][empty] = node
                else:
                    value['items'].append(node)
            pie.publish(value)
        self.edit(operation)

    def add_function(self):
        return self.open_functions(lambda action:self.add_node(pie.entry(action)))

    def add_radial_function(self):
        def add(action):
            value = pie.layout()
            empty = next((direction for direction in pie.CLOCKWISE if value['slots'][direction] is None), None)
            node = pie.entry(action)
            if empty:
                value['slots'][empty] = node
                self.direction, self.container = empty, None
            else:
                value['items'].append(node)
            self.edit(lambda: pie.publish(value))
        return self.open_functions(add)

    def add_to_list(self):
        return self.open_functions(lambda action:self.edit(lambda:pie.insert(dict(action=action))))

    def add_submenu(self):
        title, accepted = QtWidgets.QInputDialog.getText(self, 'Add Submenu', 'Submenu name:')
        if accepted and title.strip():
            self.add_node(pie.entry(label=title.strip(), children=[]))

    def remove_selected(self):
        if self.identifier:
            self.edit(lambda: pie.remove(self.identifier))

    def rename_selected(self):
        node = pie.node_by_id(self.identifier) if self.identifier else None
        if node and node['kind'] != 'separator':
            title, accepted = QtWidgets.QInputDialog.getText(self, 'Rename Menu Entry', 'Label:', text=node['label'])
            if accepted and title.strip():
                self.edit(lambda: pie.rename(node['id'], title))

