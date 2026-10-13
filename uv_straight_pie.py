"""Saved marking-menu layout and a drag-and-drop visual editor."""
import copy
import json
import math
import uuid
import weakref
import maya.cmds as cmds
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid, wrapInstance
from uv_straight_actions import CATALOG as ACTION_CATALOG, BY_ID, button_title

CATALOG = tuple((action, group, button_title(action))
                for action, group, _ in ACTION_CATALOG)

OPTION = 'shellShifterPieLayout'
MIME = 'application/x-shellshifter-pie'
DIRECTIONS = ('NW', 'N', 'NE', 'W', 'E', 'SW', 'S', 'SE')
CLOCKWISE = ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')
RADIAL_RADIUS = 70.
_clients = globals().get('_clients', weakref.WeakSet())
_editor = globals().get('_editor')


def entry(action=None, label=None, children=None):
    node = dict(id=uuid.uuid4().hex)
    if children is not None:
        node.update(kind='submenu', label=label or 'New Submenu', children=children)
    elif action:
        node.update(kind='action', action=action, label=label or button_title(action))
    else:
        node.update(kind='separator')
    return node


def default_layout():
    return dict(version=1, defaults_revision=227, slots={
        'N': entry('mark_v'), 'E': entry('mark_h'),
        'S': entry('orient'), 'W': entry('unpin'),
        'NW': entry('unfold'), 'NE': entry('relax'),
        'SW': entry(label='Edit Pins', children=[
            entry('pick_auto'), entry('pick_h'), entry('pick_v')]),
        'SE': entry(label='Unwrap', children=[
            entry('unwrap_mesh', 'Unwrap Mesh'),
            entry('straighten_quads')])}, items=[
        paired_entry('checker_apply', None, 'checker'),
        paired_entry('checker_remove', 'Remove Checker', 'checker'),
        paired_entry('tiles_down', '÷2', 'tiling'), paired_entry('tiles_up', '×2', 'tiling'),
        entry('distortion'),
        paired_entry('done', 'Mark Done', 'done'), paired_entry('done_clear', 'Clear Done', 'done')])


def paired_entry(action, label, pair):
    node = entry(action, label)
    node['pair'] = pair
    return node


def list_rows(nodes):
    """Pair adjacent tagged actions; each half remains independently editable."""
    rows = []
    for node in nodes:
        if (rows and len(rows[-1]) == 1 and node['kind'] == 'action' and node.get('pair') and
                rows[-1][0]['kind'] == 'action' and rows[-1][0].get('pair') == node['pair']):
            rows[-1].append(node)
        else:
            rows.append([node])
    return rows


def double_default_items(items):
    """Replace only the previous default groups with direct action pairs."""
    result = []
    patterns = {
        'Checker': [('checker_gray', 'Gray Squares'), ('checker_color', 'Color Labels'),
                    ('checker_remove', 'Remove Checker')],
        'Checker Tiling': [('tiles_up', 'Multiply by 2'), ('tiles_down', 'Divide by 2')],
        'Done': [('done', 'Mark Done'), ('done_clear', 'Clear Done')],
    }
    for node in items:
        expected = patterns.get(node.get('label'))
        if (node['kind'] != 'submenu' or expected is None or
                [(c.get('action'), c.get('label')) for c in node['children']] != expected):
            result.append(node)
            continue
        if node['label'] == 'Checker':
            first = paired_entry('checker_apply', 'Checker', 'checker')
            second = copy.deepcopy(node['children'][-1])
            second['pair'] = 'checker'
        elif node['label'] == 'Checker Tiling':
            first, second = copy.deepcopy(node['children'][1]), copy.deepcopy(node['children'][0])
            first.update(label='÷2', pair='tiling')
            second.update(label='×2', pair='tiling')
        else:
            first, second = copy.deepcopy(node['children'])
            first['pair'] = second['pair'] = 'done'
        first['id'] = node['id']
        result.extend([first, second])
    return result


def group_default_items(items):
    """Upgrade previous default rows without replacing the edited radial ring.

    Only rows with the old default names/contents are folded together. Renamed
    entries and user-created submenus keep their individual placement.
    """
    items = list(items)

    def action_row(action):
        return next((node for node in items if node['kind'] == 'action' and
                     node['action'] == action and node['label'] == BY_ID[action][1]), None)

    checker = next((node for node in items if node['kind'] == 'submenu' and
                    node['label'] == 'Apply Checker' and
                    [(child.get('action'), child.get('label')) for child in node['children']] ==
                    [('checker_gray', 'Gray Squares'), ('checker_color', 'Color Labels')]), None)
    remove = action_row('checker_remove')
    if checker is not None and remove is not None:
        checker['label'] = 'Checker'
        checker['children'].append(remove)
        items.remove(remove)

    for first_action, second_action, title, first_label, second_label in (
            ('tiles_up', 'tiles_down', 'Checker Tiling', 'Multiply by 2', 'Divide by 2'),
            ('done', 'done_clear', 'Done', 'Mark Done', 'Clear Done')):
        first, second = action_row(first_action), action_row(second_action)
        if first is not None and second is not None:
            index = items.index(first)
            # Keep the original row ID as the new group ID, avoiding broken
            # editor selections; the first action receives its own child ID.
            grouped = dict(id=first['id'], kind='submenu', label=title,
                           children=[entry(first_action, first_label), second])
            second['label'] = second_label
            items[index] = grouped
            items.remove(second)
    window = action_row('window')
    if window is not None:
        items.remove(window)
    return items


def validate(raw, normalize_labels=True):
    """Only known action IDs and bounded menu trees may reach Maya menuItem."""
    if not isinstance(raw, dict) or raw.get('version') != 1:
        raise ValueError('Unsupported pie-menu layout.')
    ids = set()
    count = [0]
    def clean(node, depth=0):
        if node is None:
            return None
        if not isinstance(node, dict) or depth > 4:
            raise ValueError('Invalid pie-menu entry.')
        count[0] += 1
        if count[0] > 200:
            raise ValueError('Too many pie-menu entries.')
        kind = node.get('kind')
        if kind == 'action' and node.get('action') not in BY_ID:
            return None
        if kind not in ('action', 'submenu', 'separator'):
            raise ValueError('Invalid pie-menu entry type.')
        identifier = node.get('id')
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            identifier = uuid.uuid4().hex
        ids.add(identifier)
        result = dict(id=identifier, kind=kind)
        if kind != 'separator':
            result['label'] = str(node.get('label', '')).strip()[:80] or 'New Submenu'
            if result['label'] in ('Add / Remove Segment by Click', 'Add / Remove Segment'):
                result['label'] = 'Toggle Segment'
        if kind == 'action':
            result['action'] = node['action']
            if (node['action'] == 'unwrap_mesh' and
                    result['label'] == 'Mesh: Hard Edges + Selected Seams'):
                result['label'] = 'Unwrap Mesh'
            if (node['action'] == 'orient' and
                    result['label'] in ('Orient Longest Side', 'Orient by Longest Side')):
                result['label'] = 'Orient Shells'
            if (node['action'] == 'overlap_check' and result['label'] == 'Check Overlaps'):
                result['label'] = 'Select Overlaps'
            if (node['action'] == 'bad_overlaps' and result['label'] == 'Check for Bad Overlaps'):
                result['label'] = 'Check Overlaps'
            if normalize_labels:
                # Update shipped captions in existing layouts while retaining
                # entry IDs, placement, action IDs and deliberate custom names.
                aliases = {
                    'unpin': ('Unpin Selected',), 'unfold': ('Unfold with Pins',),
                    'relax': ('Relax with Pins',),
                    'straighten_quads': ('Quad Strip', 'Straighten Quad Shells'),
                    'pick_toggle': ('Toggle Segment',),
                    'pick_auto': ('Automatic H/V', 'Automatic H V', 'Automatic HV',
                                  'Toggle Segment'),
                    'checker_apply': ('Checker',),
                    'checker_gray': ('Gray Squares',),
                    'checker_color': ('Color Labels',),
                    'tiles_up': ('Multiply by 2',), 'tiles_down': ('Divide by 2',),
                    'density_analyse': ('Analyse && Show',),
                }
                action = node['action']
                if result['label'] in (BY_ID[action][1], *aliases.get(action, ())):
                    result['label'] = button_title(action)
            pair = node.get('pair')
            if isinstance(pair, str) and (pair in ('checker', 'tiling', 'done') or
                    len(pair) == 36 and pair.startswith('row_') and
                    all(char in '0123456789abcdef' for char in pair[4:])):
                result['pair'] = pair
        if kind == 'submenu':
            result['children'] = [value for child in node.get('children', [])
                                  if (value := clean(child, depth+1)) is not None]
            if (normalize_labels and result['label'] == 'Toggle Segment' and
                    [child.get('action') for child in result['children']] ==
                    ['pick_auto', 'pick_h', 'pick_v']):
                result['label'] = 'Edit Pins'
        return result
    slots = {direction: clean(raw.get('slots', {}).get(direction)) for direction in DIRECTIONS}
    slots = {direction: node if node is None or node['kind'] != 'separator' else None
             for direction, node in slots.items()}
    items = [value for node in raw.get('items', []) if (value := clean(node)) is not None]
    revision = raw.get('defaults_revision', 0)
    return dict(version=1, defaults_revision=revision if isinstance(revision, int) else 0,
                slots=slots, items=items)


def load():
    try:
        if cmds.optionVar(exists=OPTION):
            value = validate(json.loads(cmds.optionVar(query=OPTION)), normalize_labels=False)
            if value['defaults_revision'] < 162:
                value['items'] = group_default_items(value['items'])
            if value['defaults_revision'] < 163:
                value['items'] = double_default_items(value['items'])
                value['defaults_revision'] = 163
            value = validate(value)
            if value['defaults_revision'] < 227:
                value['defaults_revision'] = 227
                cmds.optionVar(stringValue=(OPTION, json.dumps(value)))
            return value
    except (ValueError, TypeError, AttributeError, KeyError):
        pass
    return default_layout()


_layout = load()
_undo = []
_redo = []


def layout():
    return copy.deepcopy(_layout)


def radial_entries(value=None):
    """Stable clockwise order, with no vacant directions in the visible ring."""
    value = _layout if value is None else value
    return [(direction, value['slots'][direction]) for direction in CLOCKWISE
            if value['slots'][direction] is not None]


def radial_geometry(width, metrics, value=None):
    """Shared editor/popup geometry: equal angles for the occupied entries only."""
    return ring_geometry(width, metrics, radial_entries(value))


def ring_geometry(width, metrics, nodes, angles=None):
    """Fit wrapped labels around fixed gesture anchors, identically on both surfaces."""
    width = max(200, width)
    reference = 240.
    cap = 104.
    radius = RADIAL_RADIUS
    center = QtCore.QPointF(reference*.5, radius+24.)
    regions = []
    if not nodes:
        return QtCore.QPointF(width*.5, 30.), regions, 65.
    angles = angles if angles is not None else [-math.pi*.5+i*math.tau/len(nodes) for i in range(len(nodes))]
    anchors = [center+QtCore.QPointF(math.cos(angle)*radius, math.sin(angle)*radius) for angle in angles]
    for index, (direction, node) in enumerate(nodes):
        angle = angles[index]
        padding = 48 if node['kind'] == 'submenu' else 37
        requested = metrics.horizontalAdvance(node['label'])+padding
        anchor = anchors[index]
        item_width = min(cap, max(62., requested))
        text_bounds = metrics.boundingRect(QtCore.QRect(0, 0, int(item_width-padding), 1000),
                                          QtCore.Qt.TextWordWrap, node['label'])
        height = max(20., text_bounds.height()+6.)
        x = max(8., min(anchor.x()-item_width*.5, reference-8.-item_width))
        rect = QtCore.QRectF(x, anchor.y()-height*.5, item_width, height)
        # Leave space for the centre dot and the opposite column.
        if math.cos(angle) < -.1:
            rect.moveRight(min(rect.right(), reference*.5-8.))
        elif math.cos(angle) > .1:
            rect.moveLeft(max(rect.left(), reference*.5+8.))
        regions.append(dict(rect=rect, anchor=anchor, node=node, direction=direction,
                            parent=None, before=None, angle=angle, wrap=True))
    # Tall labels expand away from the centre instead of losing words.
    ordered = sorted(regions, key=lambda region: abs(region['rect'].center().y()-center.y()))
    placed = []
    for region in ordered:
        rect = region['rect']
        upward = region['anchor'].y() < center.y()
        for _ in range(len(regions)+1):
            collisions = [other['rect'] for other in placed
                          if rect.adjusted(-3, -3, 3, 3).intersects(other['rect'])]
            if not collisions:
                break
            if upward:
                rect.moveBottom(min(other.top() for other in collisions)-6.)
            else:
                rect.moveTop(max(other.bottom() for other in collisions)+6.)
        placed.append(region)
    offset = QtCore.QPointF((width-reference)*.5, max(0., 8.-min(r['rect'].top() for r in regions)))
    center += offset
    for region in regions:
        region['rect'].translate(offset)
        region['anchor'] += offset
    return center, regions, max(center.y()+radius+30., max(r['rect'].bottom() for r in regions)+14.)


def publish(value, remember=True):
    global _layout
    value = validate(value)
    # Reject edits that would silently discard an over-deep tree before saving.
    if remember:
        _undo.append(layout())
        del _undo[:-50]
        _redo.clear()
    cmds.optionVar(stringValue=(OPTION, json.dumps(value)))
    _layout = value
    for client in tuple(_clients):
        if isValid(client):
            client.refresh()
        else:
            _clients.discard(client)


def history(redo=False):
    source, destination = (_redo, _undo) if redo else (_undo, _redo)
    if source:
        value = source.pop()
        destination.append(layout())
        publish(value, remember=False)


def locate(value, identifier):
    """Return (container, key) for an entry, including nested submenu contents."""
    def search(container):
        keys = container.keys() if isinstance(container, dict) else range(len(container))
        for key in keys:
            node = container[key]
            if node is None:
                continue
            if node['id'] == identifier:
                return container, key
            if node['kind'] == 'submenu':
                found = search(node['children'])
                if found:
                    return found
        return None
    return search(value['slots']) or search(value['items'])


def node_by_id(identifier, value=None):
    found = locate(value or _layout, identifier)
    return found[0][found[1]] if found else None


def parent_of(identifier):
    def search(nodes, parent=None):
        for node in nodes:
            if node is None:
                continue
            if node['id'] == identifier:
                return parent
            if node['kind'] == 'submenu':
                found = search(node['children'], node['id'])
                if found:
                    return found
        return None
    return search(list(_layout['slots'].values()) + _layout['items'])


def detach(container, key):
    if isinstance(container, dict):
        container[key] = None
    else:
        container.pop(key)


def swap_list_entries(first, second, commit=True):
    value = layout()
    left, right = locate(value, first), locate(value, second)
    if left is None or right is None or left[0] is not right[0] or not isinstance(left[0], list):
        raise ValueError('Choose two entries in the same list.')
    nodes = left[0]
    nodes[left[1]], nodes[right[1]] = nodes[right[1]], nodes[left[1]]
    if commit:
        publish(value)
    return value


def clear_pair(value, identifier):
    found = locate(value, identifier)
    if not found:
        return
    container, index = found
    if isinstance(container, list):
        row = next(row for row in list_rows(container) if any(n['id'] == identifier for n in row))
        for node in row:
            node.pop('pair', None)
    else:
        container[index].pop('pair', None)


def pair_entries(source, target, side='right', commit=True):
    """Place two independent functions beside each other without losing old partners."""
    value = layout()
    if isinstance(source,dict):
        node,incoming=payload_node(source,value)
    else:
        incoming=locate(value,source)
        node=incoming[0][incoming[1]] if incoming else None
    destination=locate(value,target)
    if (node is None or destination is None or node['id'] == target or side not in ('left', 'right') or
            not isinstance(destination[0], list) or node['kind'] != 'action' or
            destination[0][destination[1]]['kind'] != 'action'):
        raise ValueError('Drag a function beside another function in the list.')
    clear_pair(value, node['id'])
    clear_pair(value, target)
    if incoming:detach(*incoming)
    container, index = locate(value, target)
    partner = container[index]
    node['pair'] = partner['pair'] = 'row_'+uuid.uuid4().hex
    container.insert(index if side == 'left' else index+1, node)
    value = validate(value)
    if commit:
        publish(value)
    return value


def payload_node(payload, value):
    if 'action' in payload:
        if payload['action'] not in BY_ID:
            raise ValueError('Choose an available function.')
        node=entry(payload['action'])
        # A catalog drag keeps one ID as it previews several destinations.
        if payload.get('id'):node['id']=payload['id']
        return node, None
    found = locate(value, payload.get('id'))
    if not found:
        raise ValueError('This menu entry is no longer available.')
    return found[0][found[1]], found


def put_slot(direction, payload, commit=True):
    value = layout()
    incoming, source = payload_node(payload, value)
    if incoming['kind'] == 'separator':
        raise ValueError('Separators belong in lists, not radial slots.')
    old = value['slots'][direction]
    if old and old['id'] == incoming['id']:
        return value
    if old and (locate({'slots': {}, 'items': [old]}, incoming['id']) or
                locate({'slots': {}, 'items': [incoming]}, old['id'])):
        raise ValueError('Move nested entries to an empty slot first.')
    if source:
        container, key = source
        if old:
            container[key] = old  # Dragging between occupied positions swaps them.
        else:
            detach(container, key)
    elif old:
        value['items'].append(old)  # Keep displaced entries available below the pie.
    value['slots'][direction] = incoming
    if commit:
        publish(value)
    return validate(value)


def insert(payload, parent=None, before=None, commit=True):
    value = layout()
    incoming, source = payload_node(payload, value)
    owner = node_by_id(parent, value) if parent else None
    if parent and (not owner or owner['kind'] != 'submenu'):
        raise ValueError('Select a submenu to add its contents.')
    if parent and locate({'slots': {}, 'items': [incoming]}, parent):
        raise ValueError('A submenu cannot contain itself.')
    if before == incoming['id']:
        return value
    if source:
        detach(*source)
    target = owner['children'] if owner else value['items']
    index = next((i for i, node in enumerate(target) if node['id'] == before), len(target))
    target.insert(index, incoming)
    if commit:
        publish(value)
    return validate(value)


def remove(identifier, commit=True):
    value = layout()
    found = locate(value, identifier)
    if found:
        detach(*found)
        if commit:
            publish(value)
    return validate(value)


def rename(identifier, title):
    value = layout()
    node = node_by_id(identifier, value)
    if node and node['kind'] != 'separator' and title.strip():
        node['label'] = title.strip()[:80]
        publish(value)


from uv_straight_icons import ACTION_ICONS as ICON_ACTIONS


def icon_key(node):
    if node['kind'] == 'action':
        return ICON_ACTIONS.get(node['action'], node['action'])
    children = node.get('children', [])
    return icon_key(children[0]) if children else None


def checked(action):
    import uv_straight_menu as menu
    if action.startswith('pick_'):
        active, axis = menu.picker_active(), menu.picker_axis()
        if action == 'pick_toggle':
            return active
        return active and axis == dict(pick_auto=None, pick_h='H', pick_v='V')[action]
    if action in ('wireframe', 'distortion', 'show_pins'):
        import uv_straight_native as native
        return native.display_state()[dict(wireframe='wireframe_enabled',
                                          distortion='distortion_enabled', show_pins='pins_enabled')[action]]
    if action in ('checker_gray', 'checker_color'):
        import uv_straight_checker as checker
        return checker.preferred_style() == action.removeprefix('checker_')
    if action == 'original_color':
        import uv_straight_move_overlaps as overlaps
        return overlaps.original_color_visible()
    return None


NO_SELECTION = {'window', 'popup', 'wireframe', 'distortion', 'show_pins', 'original_color',
                'pick_toggle', 'pick_auto', 'pick_h', 'pick_v', 'density_clear', 'scene_colors', 'original_clear'}


def populate(menu):
    import uv_straight_menu as native_menu
    from uv_straight_keymaps import enabled
    from uv_straight_icons import menu_image
    cmds.popupMenu(menu, edit=True, deleteAllItems=True)
    if not enabled('menu'):
        return
    selected = bool(cmds.ls(selection=True))
    def selectable(node):
        return (selected or node.get('action') in NO_SELECTION or
                any(selectable(child) for child in node.get('children', [])))
    def add(node, parent, direction=None):
        if node['kind'] == 'separator':
            cmds.menuItem(parent=parent, divider=True)
            return
        args = dict(parent=parent, label=node['label'], enable=selectable(node))
        if direction:
            args['radialPosition'] = direction
        status = checked(node['action']) if node['kind'] == 'action' else None
        active = bool(status) or (node['kind'] == 'submenu' and
                                  any(bool(checked(c['action'])) for c in node['children']
                                      if c['kind'] == 'action' and c['action'].startswith('pick_')))
        image = menu_image(icon_key(node), active=active)
        if image:
            args['image'] = image
        if node['kind'] == 'submenu':
            args.update(subMenu=True, boldFont=active)
            submenu = cmds.menuItem(**args)
            for child in node['children']:
                add(child, submenu)
        else:
            if status is not None:
                args.update(checkBox=status, boldFont=bool(status))
            action = node['action']
            def run(*unused, action=action):
                from uv_straight_actions import execute
                native_menu.after_menu_interaction(lambda: execute(action))
            args['command'] = run
            cmds.menuItem(**args)
    for direction in DIRECTIONS:
        if _layout['slots'][direction]:
            add(_layout['slots'][direction], menu, direction)
    if _layout['items']:
        cmds.menuItem(parent=menu, divider=True)
        for node in _layout['items']:
            add(node, menu)


def mime_data(payload):
    data = QtCore.QMimeData()
    data.setData(MIME, json.dumps(payload).encode('utf-8'))
    return data


def read_drop(event):
    try:
        return json.loads(bytes(event.mimeData().data(MIME)))
    except (ValueError, TypeError, AttributeError):
        return {}


def start_drag(widget, payload, title):
    drag = QtGui.QDrag(widget)
    drag.setMimeData(mime_data(payload))
    pixmap = QtGui.QPixmap(180, 32)
    pixmap.fill(QtGui.QColor('#486080'))
    painter = QtGui.QPainter(pixmap)
    painter.setPen(QtGui.QColor('#ffffff'))
    painter.drawText(pixmap.rect().adjusted(8, 0, -8, 0), QtCore.Qt.AlignVCenter,
                     QtGui.QFontMetrics(painter.font()).elidedText(title, QtCore.Qt.ElideRight, 164))
    painter.end()
    drag.setPixmap(pixmap)
    drag.exec(QtCore.Qt.MoveAction | QtCore.Qt.CopyAction)


class FunctionList(QtWidgets.QListWidget):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.setDragEnabled(True)
        self.setWordWrap(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        for action, group, title in CATALOG:
            item = QtWidgets.QListWidgetItem(group+' · '+title)
            item.setData(QtCore.Qt.UserRole, action)
            self.addItem(item)
        self.itemDoubleClicked.connect(lambda item: editor.add_function())

    def startDrag(self, actions):
        if self.currentItem():
            start_drag(self, dict(action=self.currentItem().data(QtCore.Qt.UserRole)), self.currentItem().text())

    def filter(self, text):
        for i in range(self.count()):
            item = self.item(i)
            item.setHidden(text.casefold() not in item.text().casefold())
        if self.currentItem() and self.currentItem().isHidden():
            self.clearSelection()


class RadialSlot(QtWidgets.QPushButton):
    def __init__(self, direction, editor):
        super().__init__()
        self.direction, self.editor = direction, editor
        self.setAcceptDrops(True)
        self.setCheckable(True)
        self.setMinimumSize(140, 60)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.clicked.connect(lambda: editor.select_slot(direction))
        self.origin = None

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.origin = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        node = _layout['slots'][self.direction]
        if node and self.origin is not None and event.buttons() & QtCore.Qt.LeftButton and (
                event.position().toPoint()-self.origin).manhattanLength() >= QtWidgets.QApplication.startDragDistance():
            self.origin = None
            self.setDown(False)
            start_drag(self, dict(id=node['id']), node['label'])
            return
        super().mouseMoveEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME):
            self.setProperty('dropTarget', True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.clear_drop_highlight()
        event.accept()

    def clear_drop_highlight(self):
        self.setProperty('dropTarget', False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event):
        self.clear_drop_highlight()
        self.editor.edit(lambda: put_slot(self.direction, read_drop(event)))
        self.editor.select_slot(self.direction)
        event.acceptProposedAction()


class MenuTree(QtWidgets.QTreeWidget):
    def __init__(self, editor, child_view=False):
        super().__init__()
        self.editor, self.child_view = editor, child_view
        self.drop_position = None
        self.setHeaderHidden(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDrop)
        self.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setMinimumHeight(155)
        self.itemSelectionChanged.connect(self.selected)

    def fill(self, nodes):
        self.blockSignals(True)
        self.clear()
        def add(nodes, parent):
            for node in nodes:
                title = node.get('label', '— Separator —')
                item = QtWidgets.QTreeWidgetItem([title])
                item.setData(0, QtCore.Qt.UserRole, node['id'])
                item.setToolTip(0, title)
                parent.addChild(item)
                if node['id'] == self.editor.identifier:
                    self.setCurrentItem(item)
                if node['kind'] == 'submenu':
                    add(node['children'], item)
        add(nodes, self.invisibleRootItem())
        self.expandAll()
        self.blockSignals(False)

    def selected(self):
        if self.currentItem():
            self.editor.select_node(self.currentItem().data(0, QtCore.Qt.UserRole))

    def startDrag(self, actions):
        if self.currentItem():
            start_drag(self, dict(id=self.currentItem().data(0, QtCore.Qt.UserRole)), self.currentItem().text(0))

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(MIME):
            self.drop_position = event.position().toPoint()
            self.viewport().update()
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.drop_position = None
        self.viewport().update()
        event.accept()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.drop_position is None:
            return
        target = self.itemAt(self.drop_position)
        painter = QtGui.QPainter(self.viewport())
        painter.setPen(QtGui.QPen(QtGui.QColor('#85c7f4'), 2))
        if target:
            rect = self.visualItemRect(target)
            relative = (self.drop_position.y()-rect.top())/max(1, rect.height())
            node = node_by_id(target.data(0, QtCore.Qt.UserRole))
            if node and node['kind'] == 'submenu' and .25 <= relative <= .75:
                painter.drawRect(rect.adjusted(1, 1, -2, -1))
            else:
                y = rect.bottom() if relative > .5 else rect.top()
                painter.drawLine(rect.left(), y, self.viewport().width()-4, y)
        else:
            painter.drawRect(self.viewport().rect().adjusted(2, 2, -3, -3))
        painter.end()

    def dropEvent(self, event):
        self.drop_position = None
        self.viewport().update()
        target = self.itemAt(event.position().toPoint())
        parent = self.editor.child_owner if self.child_view else None
        before = None
        if target:
            identifier = target.data(0, QtCore.Qt.UserRole)
            node = node_by_id(identifier)
            rect = self.visualItemRect(target)
            relative = (event.position().y()-rect.top())/max(1, rect.height())
            if node['kind'] == 'submenu' and .25 <= relative <= .75:
                parent = identifier
            else:
                parent = target.parent().data(0, QtCore.Qt.UserRole) if target.parent() else parent
                before = identifier
                if relative > .5:
                    container = node_by_id(parent)['children'] if parent else _layout['items']
                    index = next(i for i, child in enumerate(container) if child['id'] == identifier)
                    before = container[index+1]['id'] if index+1 < len(container) else None
        self.editor.edit(lambda: insert(read_drop(event), parent, before))
        event.acceptProposedAction()


class PieEditor(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Pie Menu Editor')
        self.resize(1020, 720)
        self.setMinimumSize(820, 600)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.direction = 'N'
        self.identifier = None
        self.child_owner = None
        self.setStyleSheet(
            'QDialog { background: #303030; color: #ededed; }'
            'QLabel, QCheckBox { color: #ededed; }'
            'QPushButton { background: #505050; color: #eeeeee; border: 1px solid #606060; border-radius: 7px; padding: 6px; }'
            'QPushButton:hover { background: #5a626b; border-color: #8fb4d4; }'
            'QPushButton:checked { background: #486080; border-color: #92bedb; }'
            'QPushButton[dropTarget="true"] { background: #45647b; border: 2px solid #85c7f4; }'
            'QPushButton:disabled { color: #888888; border-color: #414141; }'
            'QListWidget, QTreeWidget, QLineEdit { background: #252525; color: #ededed; border: 1px solid #555555; border-radius: 5px; }'
            'QListWidget::item, QTreeWidget::item { padding: 5px; }'
            'QListWidget::item:selected, QTreeWidget::item:selected { background: #486080; }')
        outer = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel('Drag functions into the pie. Drag menu entries to swap slots or move them into lists and submenus.')
        hint.setWordWrap(True)
        outer.addWidget(hint)
        body = QtWidgets.QHBoxLayout()
        outer.addLayout(body, 1)
        left = QtWidgets.QVBoxLayout()
        body.addLayout(left, 1)
        left.addWidget(QtWidgets.QLabel('Available Functions'))
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('Search functions...')
        left.addWidget(self.search)
        self.functions = FunctionList(self)
        self.functions.setMinimumWidth(215)
        left.addWidget(self.functions, 1)
        self.search.textChanged.connect(self.functions.filter)
        add = QtWidgets.QPushButton('Add Selected Function')
        add.clicked.connect(self.add_function)
        left.addWidget(add)
        right = QtWidgets.QVBoxLayout()
        body.addLayout(right, 3)
        canvas = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(canvas)
        grid.setSpacing(10)
        self.slots = {}
        for direction, row, column in (('NW', 0, 0), ('N', 0, 1), ('NE', 0, 2),
                                       ('W', 1, 0), ('E', 1, 2),
                                       ('SW', 2, 0), ('S', 2, 1), ('SE', 2, 2)):
            button = RadialSlot(direction, self)
            self.slots[direction] = button
            grid.addWidget(button, row, column)
        center = QtWidgets.QLabel('Pie Menu\n●')
        center.setAlignment(QtCore.Qt.AlignCenter)
        grid.addWidget(center, 1, 1)
        right.addWidget(canvas, 2)
        details = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLineEdit()
        self.title.setPlaceholderText('Entry label')
        self.title.editingFinished.connect(lambda: self.edit(lambda: rename(self.identifier, self.title.text()))
                                          if self.identifier else None)
        details.addWidget(self.title, 1)
        self.remove_button = QtWidgets.QPushButton('Remove')
        self.remove_button.clicked.connect(lambda: self.edit(lambda: remove(self.identifier)))
        details.addWidget(self.remove_button)
        right.addLayout(details)
        lists = QtWidgets.QHBoxLayout()
        right.addLayout(lists, 2)
        tail = QtWidgets.QVBoxLayout()
        lists.addLayout(tail, 1)
        self.tail_button = QtWidgets.QPushButton('More Actions ↓')
        self.tail_button.clicked.connect(self.select_tail)
        tail.addWidget(self.tail_button)
        self.tail = MenuTree(self)
        tail.addWidget(self.tail)
        children = QtWidgets.QVBoxLayout()
        lists.addLayout(children, 1)
        self.children_label = QtWidgets.QLabel('Submenu Contents')
        children.addWidget(self.children_label)
        self.children = MenuTree(self, True)
        children.addWidget(self.children)
        tools = QtWidgets.QHBoxLayout()
        new_group = QtWidgets.QPushButton('Add Submenu')
        new_group.clicked.connect(lambda: self.add_node(entry(label='New Submenu', children=[])))
        tools.addWidget(new_group)
        separator = QtWidgets.QPushButton('Add Separator')
        separator.clicked.connect(lambda: self.add_node(entry(), to_list=True))
        tools.addWidget(separator)
        right.addLayout(tools)
        self.status = QtWidgets.QLabel('Saved automatically. Changes appear the next time you open the pie menu.')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        footer = QtWidgets.QHBoxLayout()
        self.undo_button = QtWidgets.QPushButton('Undo Edit')
        self.undo_button.clicked.connect(lambda: history())
        self.redo_button = QtWidgets.QPushButton('Redo Edit')
        self.redo_button.clicked.connect(lambda: history(True))
        footer.addWidget(self.undo_button)
        footer.addWidget(self.redo_button)
        footer.addStretch()
        reset = QtWidgets.QPushButton('Restore Default Pie')
        reset.clicked.connect(lambda: self.edit(lambda: publish(default_layout())))
        footer.addWidget(reset)
        close = QtWidgets.QPushButton('Close')
        close.clicked.connect(self.close)
        footer.addWidget(close)
        outer.addLayout(footer)
        _clients.add(self)
        self.select_slot('N')

    def edit(self, operation):
        try:
            operation()
            self.status.setText('Saved automatically. Changes appear the next time you open the pie menu.')
        except (ValueError, RuntimeError) as error:
            self.status.setText(str(error))

    def select_slot(self, direction):
        self.direction = direction
        node = _layout['slots'][direction]
        self.identifier = node['id'] if node else None
        self.child_owner = self.identifier if node and node['kind'] == 'submenu' else None
        self.refresh()

    def select_node(self, identifier):
        self.direction = None
        self.identifier = identifier
        node = node_by_id(identifier)
        self.child_owner = identifier if node and node['kind'] == 'submenu' else parent_of(identifier)
        self.refresh()

    def select_tail(self):
        self.direction = self.identifier = self.child_owner = None
        self.refresh()

    def add_function(self):
        selected = self.functions.selectedItems()
        if selected:
            self.add_node(entry(selected[0].data(QtCore.Qt.UserRole)))
        else:
            self.status.setText('Select a function from the list first.')

    def add_node(self, node, to_list=False):
        def operation():
            if self.direction and not self.child_owner and not to_list:
                value = layout()
                old = value['slots'][self.direction]
                if old:
                    value['items'].append(old)
                value['slots'][self.direction] = node
                publish(value)
                self.select_slot(self.direction)
            else:
                value = layout()
                owner = node_by_id(self.child_owner, value) if self.child_owner else None
                (owner['children'] if owner else value['items']).append(node)
                publish(value)
        self.edit(operation)

    def refresh(self):
        for direction, button in self.slots.items():
            node = _layout['slots'][direction]
            title = node['label'] if node else 'Drop Function'
            metrics = QtGui.QFontMetrics(button.font())
            button.setText(direction+'\n'+metrics.elidedText(title, QtCore.Qt.ElideRight, 170)+
                           ('  >' if node and node['kind'] == 'submenu' else ''))
            button.setToolTip(title+'\nDrag to swap positions. Drop a function or menu entry here.')
            button.setChecked(direction == self.direction)
            if node:
                from uv_straight_icons import PATHS, icon
                key = icon_key(node)
                button.setIcon(icon(key) if key in PATHS else QtGui.QIcon())
            else:
                button.setIcon(QtGui.QIcon())
        if self.direction:
            node = _layout['slots'][self.direction]
            self.identifier = node['id'] if node else None
            self.child_owner = self.identifier if node and node['kind'] == 'submenu' else None
        selected = node_by_id(self.identifier) if self.identifier else None
        if self.identifier and selected is None:
            self.identifier = None
        self.title.setEnabled(bool(selected and selected['kind'] != 'separator'))
        self.title.setText(selected.get('label', '') if selected else '')
        self.remove_button.setEnabled(bool(selected))
        self.tail.fill(_layout['items'])
        owner = node_by_id(self.child_owner) if self.child_owner else None
        if not owner or owner['kind'] != 'submenu':
            self.child_owner = None
        self.children.setEnabled(owner is not None and owner['kind'] == 'submenu')
        self.children.fill(owner['children'] if self.child_owner else [])
        self.children_label.setText('Submenu: '+owner['label'] if self.child_owner else 'Select a submenu to edit its contents')
        self.undo_button.setEnabled(bool(_undo))
        self.redo_button.setEnabled(bool(_redo))


def show_editor(window):
    global _editor
    existing = _editor
    if existing is not None and isValid(existing):
        existing.show()
        existing.raise_()
        existing.activateWindow()
        return existing
    # Parent the editor to Maya, never a transient popup that closes on focus loss.
    import maya.OpenMayaUI as omui
    pointer = omui.MQtUtil.mainWindow()
    owner = wrapInstance(int(pointer), QtWidgets.QWidget) if pointer else None
    editor = PieEditor(owner)
    editor.setWindowModality(QtCore.Qt.ApplicationModal)
    window.pie_editor = editor
    _editor = editor
    editor.show()
    return editor
