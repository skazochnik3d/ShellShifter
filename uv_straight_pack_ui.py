"""Compact standalone packing controls using the shared palette styling."""
import time
from PySide6 import QtCore, QtWidgets
import maya.cmds as cmds

PREFIX = 'uvStraightPacking'


def automatic_precision(resolution):
    """At least 1024 cells, up to 4096; never coarser than a 4K texture."""
    return max(1024,min(4096,1 << (int(resolution)-1).bit_length()))


def build_packing_controls(window, layout, button):
    from uv_straight_preview import VisibleCheckBox, CheckerPatternComboBox, CONTROL_SPACING
    import uv_straight as uv
    controls = {}
    window.packing_controls = controls

    def saved(key, default):
        return cmds.optionVar(query=PREFIX+key) if cmds.optionVar(exists=PREFIX+key) else default

    def saved_area():
        value = saved('Area', 'square')
        if value == 'vertical':
            value = 'horizontal'
            cmds.optionVar(stringValue=(PREFIX+'Area', value))
        return value

    def row(title, widget):
        line = QtWidgets.QHBoxLayout()
        line.setSpacing(CONTROL_SPACING)
        line.addWidget(QtWidgets.QLabel(title))
        line.addWidget(widget, 1)
        layout.addLayout(line)

    resolution = QtWidgets.QSpinBox()
    resolution.setRange(256, 8192)
    resolution.setSingleStep(256)
    resolution.setValue(int(saved('Resolution', 2048)))
    resolution.valueChanged.connect(lambda value: cmds.optionVar(intValue=(PREFIX+'Resolution', value)))
    row('Texture Size', resolution)
    controls['resolution'] = resolution
    from uv_straight_pack_area import AREA_CHOICES
    area = CheckerPatternComboBox()
    area.setObjectName('packingUVArea')
    area.setStyleSheet(
        'QComboBox#packingUVArea { padding-right: 28px; }'
        'QComboBox#packingUVArea::drop-down { width: 24px; }'
        'QComboBox#packingUVArea::down-arrow { image: none; }')
    for key,title,size in AREA_CHOICES:area.addItem(title,key)
    area.setCurrentIndex(max(0,area.findData(saved_area())))
    area.setToolTip('Square: U 0–1, V 0–1. Bottom Half: U 0–1, V 0–0.5.\n'
                    'Shell proportions stay unchanged.\n'
                    'Spacing and margin use the square Texture Size in pixels.')
    area.currentIndexChanged.connect(lambda index: cmds.optionVar(stringValue=(PREFIX+'Area',area.currentData())))
    row('UV Area',area)
    controls['area'] = area
    seconds = QtWidgets.QSpinBox()
    seconds.setRange(0,3600)
    seconds.setSingleStep(10)
    seconds.setSuffix(' s')
    seconds.setSpecialValueText('Unlimited')
    seconds.setToolTip('Unlimited searches until you press Stop and Keep Best. Set a number of seconds for a timed search. The best complete layout is always retained.')
    seconds.setValue(int(saved('SearchSeconds',0)))
    seconds.valueChanged.connect(lambda value: cmds.optionVar(intValue=(PREFIX+'SearchSeconds',value)))
    row('Search Limit',seconds)
    controls['seconds'] = seconds
    for key, title in (('spacing', 'Shell Spacing'), ('margin', 'Border Margin')):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(0., 4095.)
        spin.setDecimals(1)
        spin.setSuffix(' px')
        spin.setValue(float(saved(key.title(), 10. if key == 'spacing' else 5.)))
        spin.valueChanged.connect(lambda value, key=key: cmds.optionVar(floatValue=(PREFIX+key.title(), value)))
        row(title, spin)
        controls[key] = spin

    def clamp_spacing(value):
        for key in ('spacing', 'margin'):
            controls[key].setMaximum(value*min(AREA_CHOICES[area.currentIndex()][2])*.5-.1)
    clamp_spacing(resolution.value())
    resolution.valueChanged.connect(clamp_spacing)
    area.currentIndexChanged.connect(lambda index:clamp_spacing(resolution.value()))
    for key, title, hint in (
            ('normalize', 'Normalize Density', 'Match world-space texel density. Disable to preserve relative shell scales.'),
            ('rotate', 'Allow Rotation', 'Allow rigid 90-degree turns. Shell shapes and mirroring are preserved.'),
            ('keep_stacks', 'Keep UV Stacks', 'Move existing coincident UV outlines as one group. Selecting part of a stack includes matching shells on the selected meshes. Spacing is measured between groups. Density normalization uses one shared factor per stack.')):
        check = VisibleCheckBox(title)
        check.setToolTip(hint)
        check.setChecked(bool(saved(key.title(), True)))
        check.toggled.connect(lambda checked, key=key: cmds.optionVar(intValue=(PREFIX+key.title(), int(checked))))
        layout.addWidget(check)
        controls[key] = check

    status = QtWidgets.QLabel()
    status.setWordWrap(True)
    status.hide()
    controls['status'] = status
    def perform(around_others=False):
        from shiboken6 import isValid
        started = time.perf_counter()
        try:
            result = uv.pack_selected_uvs(
                resolution.value(), controls['spacing'].value(), controls['margin'].value(),
                controls['normalize'].isChecked(), controls['rotate'].isChecked(),
                'contours', around_others,
                seconds=seconds.value(), precision=automatic_precision(resolution.value()), interactive=True,
                keep_stacks=controls['keep_stacks'].isChecked(),gpu=True,
                target_size=AREA_CHOICES[area.currentIndex()][2])
        except Exception as error:
            if isValid(status):
                status.setText(str(error))
                status.show()
            raise
        if not isValid(status): return result
        if result.get('cancelled'):
            status.setText('Stopped before a complete layout. UVs unchanged.')
        else:
            status.setText('{} {} shells in {:.2f} s · free ≈ {:.1f}% · {} iterations'.format(
                'Kept best for' if result.get('stopped') else 'Packed', result['shells'],
                time.perf_counter()-started,result.get('free_percent',0.),result.get('iterations',0)))
            status.setText(status.text()+' · {} attempts · {} layouts · {} improvements'.format(
                result.get('attempts',0),result.get('full_layouts',0),result.get('improvements',0)))
            if result.get('stack_groups'):
                status.setText(status.text()+' · {} stacks kept'.format(result['stack_groups']))
            if result.get('precision'):
                status.setText(status.text()+' · grid {}'.format(result['precision']))
            if result.get('engine'):
                status.setText(status.text()+' · '+result['engine'])
        status.show()
        return result
    controls['button'] = button('Pack UVs', perform, layout)
    controls['button'].setToolTip('Pack existing UVs into the selected UV Area. Selecting any part of a shell packs that whole shell; object selection packs all its shells.')
    controls['other_button'] = button('Pack to Other', lambda:perform(True), layout)
    controls['other_button'].setToolTip('Pack selected shells into free space around all other visible meshes and unselected shells. Uses current UV sets within the selected UV Area; other shells stay in place. Selecting part of a shell packs the whole shell.')
    conversion = QtWidgets.QHBoxLayout()
    conversion.setSpacing(CONTROL_SPACING)
    layout.addLayout(conversion)
    controls['square_button'] = button('To Square', uv.rectangle_to_square, conversion)
    controls['square_button'].setToolTip('Stretch selected shells from the bottom half to the full square: V ×2 around V=0; U stays unchanged. Selecting any part includes the whole shell. One Undo restores the layout.')
    controls['rectangle_button'] = button('To Rectangle', uv.square_to_rectangle, conversion)
    controls['rectangle_button'].setToolTip('Compress selected shells to the bottom half: V ×0.5 around V=0; U stays unchanged. Selecting any part includes the whole shell. One Undo restores the layout.')
    layout.addWidget(status)
    # Sharing persisted settings also keeps a dock and a popup in agreement.
    def refresh():
        for key, widget in controls.items():
            if key in ('status', 'button', 'other_button', 'square_button', 'rectangle_button'):
                continue
            blocker = QtCore.QSignalBlocker(widget)
            if key == 'area':
                widget.setCurrentIndex(max(0,widget.findData(saved_area())))
            elif key in ('normalize', 'rotate', 'keep_stacks'):
                widget.setChecked(bool(saved(key.title(), True)))
            else:
                widget.setValue(saved('SearchSeconds' if key == 'seconds' else key.title(),
                                      2048 if key == 'resolution' else 0 if key == 'seconds' else 10. if key == 'spacing' else 5.))
        clamp_spacing(resolution.value())
    window.packing_refresh = refresh
