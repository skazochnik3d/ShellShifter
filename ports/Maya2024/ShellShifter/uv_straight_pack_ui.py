"""Compact standalone packing controls using the shared palette styling."""
import time
from PySide2 import QtCore, QtWidgets
from uv_straight_qt5 import signals_blocked
import maya.cmds as cmds

PREFIX = 'uvStraightPacking'


class PowerOfTwoSpinBox(QtWidgets.QSpinBox):
    """Typed values and arrows both stay on the texture power-of-two ladder."""
    @staticmethod
    def nearest(value):
        value=max(2,min(8192,int(value)));lower=1 << (value.bit_length()-1)
        return lower if value-lower<lower*2-value else min(8192,lower*2)
    def setValue(self,value):super().setValue(self.nearest(value))
    def valueFromText(self,text):return self.nearest(super().valueFromText(text))
    def stepBy(self,steps):self.setValue(1 << max(1,min(13,self.value().bit_length()-1+steps)))


def automatic_precision(resolution):
    """At least 1024 cells, up to 4096; never coarser than a 4K texture."""
    return max(1024,min(4096,1 << (int(resolution)-1).bit_length()))


def build_packing_controls(window, layout, button):
    from uv_straight_preview import VisibleCheckBox, CheckerPatternComboBox, CONTROL_SPACING
    import uv_straight as uv
    layout.setAlignment(QtCore.Qt.AlignTop)
    controls = {}
    window.packing_controls = controls

    import json
    from pathlib import Path
    keys=('Resolution','Area','Spacing','Margin','SearchSeconds','Iterations',
          'Normalize','Rotate','Keep_Stacks','Cpu_Assist','Spread_Small','Transfer','TransferMode','TileU','TileV')
    settings_path=Path(cmds.internalVar(userPrefDir=True))/'ShellShifterPacking.json'
    try:
        persisted=json.loads(settings_path.read_text(encoding='utf8'))
        if not isinstance(persisted,dict):persisted={}
    except (OSError,ValueError):
        persisted={}
    for key,value in (persisted.items() if not getattr(build_packing_controls,'settings_loaded',False) else ()):
        if key not in keys:continue
        if isinstance(value,str):cmds.optionVar(stringValue=(PREFIX+key,value))
        elif isinstance(value,int):cmds.optionVar(intValue=(PREFIX+key,value))
        elif isinstance(value,float):cmds.optionVar(floatValue=(PREFIX+key,value))

    build_packing_controls.settings_loaded=True

    def saved(key, default):
        if cmds.optionVar(exists=PREFIX+key):return cmds.optionVar(query=PREFIX+key)
        if key=='SearchSeconds' and cmds.optionVar(exists=PREFIX+'Seconds'):
            return cmds.optionVar(query=PREFIX+'Seconds')
        return default

    def save_settings():
        values={key:saved(key,None) for key in keys}
        temporary=settings_path.with_suffix('.tmp')
        try:
            settings_path.parent.mkdir(parents=True,exist_ok=True)
            temporary.write_text(json.dumps({key:value for key,value in values.items() if value is not None}),encoding='utf8')
            temporary.replace(settings_path)
        except OSError as error:
            cmds.warning('ShellShifter could not save packing settings: '+str(error))

    def saved_area():
        value = saved('Area', 'square')
        if value == 'vertical':
            value = 'horizontal'
            cmds.optionVar(stringValue=(PREFIX+'Area', value))
        return value

    fields=QtWidgets.QGridLayout();fields.setSpacing(CONTROL_SPACING)
    fields.setColumnStretch(1,1);fields.setColumnStretch(3,1);layout.addLayout(fields)
    positions={'Size':(0,0),'UV Area':(0,2),'Gap':(1,0),'Margin':(1,2),'Time':(2,0),'Iters':(2,2)}
    def row(title,widget):
        r,c=positions[title];widget.setMinimumWidth(0)
        widget.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
        fields.addWidget(QtWidgets.QLabel(title),r,c);fields.addWidget(widget,r,c+1)

    resolution = PowerOfTwoSpinBox()
    resolution.setRange(2, 8192);resolution.setKeyboardTracking(False)
    resolution.setValue(int(saved('Resolution', 4096)))
    resolution.setToolTip('Square texture size: 2, 4, 8, …, 4096, 8192. Arrows double or halve the size.')
    resolution.valueChanged.connect(lambda value: cmds.optionVar(intValue=(PREFIX+'Resolution', value)))
    row('Size', resolution)
    controls['resolution'] = resolution
    from uv_straight_pack_area import AREA_CHOICES
    area = CheckerPatternComboBox()
    area.setObjectName('packingUVArea')
    area.setStyleSheet(
        'QComboBox#packingUVArea { padding-right: 4px; }'
        'QComboBox#packingUVArea::drop-down { width: 18px; }'
        'QComboBox#packingUVArea::down-arrow { image: none; }')
    for key,title,size in AREA_CHOICES:area.addItem(title,key)
    area.setCurrentIndex(max(0,area.findData(saved_area())))
    area.setToolTip('Square: U 0–1, V 0–1. Half (lower half): U 0–1, V 0–0.5.\nShell proportions are preserved.\nThe area is relative to the selected tile. Gap and Margin are calculated in pixels using Size.')
    area.currentIndexChanged.connect(lambda index: cmds.optionVar(stringValue=(PREFIX+'Area',area.currentData())))
    row('UV Area',area)
    controls['area'] = area
    seconds = QtWidgets.QSpinBox()
    seconds.setRange(0,3600)
    seconds.setSingleStep(10)
    seconds.setSuffix(' s')
    seconds.setSpecialValueText('∞')
    seconds.setToolTip('Unlimited searches until Stop and Keep Best is clicked. Set a time in seconds to limit the search. The best complete layout is always retained.')
    seconds.setValue(int(saved('SearchSeconds',0)))
    seconds.valueChanged.connect(lambda value: cmds.optionVar(intValue=(PREFIX+'SearchSeconds',value)))
    row('Time',seconds)
    controls['seconds'] = seconds
    iterations=QtWidgets.QSpinBox();iterations.setRange(0,1000000000);iterations.setSpecialValueText('∞')
    iterations.setValue(int(saved('Iterations',0)))
    iterations.setToolTip('Completed iteration limit; 0 is unlimited. One batch of GPU candidates counts as one iteration. The first time or iteration limit reached stops the search and retains the best complete layout.')
    iterations.valueChanged.connect(lambda value:cmds.optionVar(intValue=(PREFIX+'Iterations',value)))
    row('Iters',iterations);controls['iterations']=iterations
    for key, title in (('spacing', 'Gap'), ('margin', 'Margin')):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(0., 4095.)
        spin.setDecimals(1)
        spin.setSuffix(' px')
        spin.setValue(float(saved(key.title(), 10. if key == 'spacing' else 5.)))
        spin.valueChanged.connect(lambda value, key=key: cmds.optionVar(floatValue=(PREFIX+key.title(), value)))
        row(title, spin)
        # Keep the complete value and suffix visible beside the step buttons.
        spin.setMinimumWidth(spin.fontMetrics().horizontalAdvance('4095.0 px') + 28)
        controls[key] = spin

    def clamp_spacing(value):
        for key in ('spacing', 'margin'):
            controls[key].setMaximum(value*min(AREA_CHOICES[area.currentIndex()][2])*.5-.1)
    clamp_spacing(resolution.value())
    resolution.valueChanged.connect(clamp_spacing)
    area.currentIndexChanged.connect(lambda index:clamp_spacing(resolution.value()))
    flags=QtWidgets.QGridLayout();flags.setSpacing(CONTROL_SPACING);layout.addLayout(flags)
    for number,(key, title, hint) in enumerate((
            ('normalize', 'Density', "On: normalize ungrouped shells to the base density. Off: retain their original relative sizes. Texel Group targets apply in both modes. The packer scales the complete layout together to fit."),
            ('rotate', 'Rotation', 'Allow 90-degree rotations for tighter packing. Packing favors horizontal elongated shells when Rotation is enabled. UV Stacks rotate as units. Shell shapes and mirrored orientation are preserved.'),
            ('keep_stacks', 'UV Stacks', 'Preserve all UV overlaps: full, partial and nested. Shells connected by overlapping area move, rotate and scale as a group. Partial group selection includes overlapping visible shells on selected meshes. Spacing is measured between groups. Density normalization uses one shared factor.'),
            ('cpu_assist', 'CPU Assist', 'Run up to 4 CPU searches alongside the GPU.\nRequires Time ≥ 5 s and unlimited Iters.'),
            ('spread_small', 'Fill Gaps', 'After packing, fill free pockets: redistribute compact small shells, then fit narrow strips and small L-shaped shells into remaining gaps. Placement uses actual outlines and preserves minimum Gap/Margin. Large shells, scale and rotations remain unchanged. With UV Stacks, stacks move as units. This pass takes up to 10 seconds and changes distribution, not total occupied area.'))):
        check = VisibleCheckBox(title)
        check.setToolTip(hint)
        check.setChecked(bool(saved(key.title(), key != 'spread_small')))
        check.toggled.connect(lambda checked, key=key: cmds.optionVar(intValue=(PREFIX+key.title(), int(checked))))
        flags.addWidget(check,number//2,number%2)
        controls[key] = check

    def transfer_separator(name):
        line=QtWidgets.QFrame();line.setObjectName(name);line.setFixedHeight(1)
        line.setStyleSheet('background: #4b4b4b; border: 0;');layout.addWidget(line)
    transfer_separator('packingTransferSeparatorTop')
    source=VisibleCheckBox('Texture Transfer')
    source.setToolTip(
        'Rebuild textures for a new UV layout.\n'
        'Same Mesh: enable before Pack UVs to save the original UVs.\n'
        'Duplicate Mesh: pick Source with old UVs and textures, then Target with new UVs. No packing needed.\n'
        'Choose a resolution and output folder, then click Transfer & Connect Textures.')
    source.setChecked(bool(saved('Transfer',True)))
    source.toggled.connect(lambda checked:cmds.optionVar(intValue=(PREFIX+'Transfer',int(checked))))
    layout.addWidget(source);controls['transfer']=source
    transfer_mode=CheckerPatternComboBox()
    transfer_mode.setObjectName('textureTransferMode')
    transfer_mode.addItem('Transfer to Same Mesh','same')
    transfer_mode.addItem('Transfer to Duplicate Mesh','duplicate')
    transfer_mode.setToolTip('Same Mesh saves the original UVs when you pack. Duplicate Mesh uses the Source and Target you pick.')
    def restore_transfer_mode():
        index=transfer_mode.findData(saved('TransferMode','same'))
        transfer_mode.setCurrentIndex(max(0,index))
        transfer_mode.setVisible(source.isChecked())
    restore_transfer_mode()
    layout.addWidget(transfer_mode)
    source.toggled.connect(transfer_mode.setVisible)
    transfer_separator('packingTransferSeparatorBottom')

    for key,widget in controls.items():
        signal=widget.toggled if isinstance(widget,QtWidgets.QCheckBox) else (
            widget.currentIndexChanged if key=='area' else widget.valueChanged)
        signal.connect(lambda *_:save_settings())

    from uv_straight_transfer_ui import WrappingLabel
    status = WrappingLabel()
    status.hide()
    controls['status'] = status
    def target_tile():return (int(saved('TileU',0)),int(saved('TileV',0)))
    def perform(to_other=False):
        from shiboken2 import isValid
        def refresh_transfer():
            if isValid(status):
                window.texture_transfer.refresh()

        started = time.perf_counter()
        try:
            save_settings()
            result = uv.pack_selected_uvs(
                resolution.value(), controls['spacing'].value(), controls['margin'].value(),
                controls['normalize'].isChecked(), controls['rotate'].isChecked(),
                'contours', True,
                seconds=seconds.value(), precision=automatic_precision(resolution.value()), interactive=True,
                keep_stacks=controls['keep_stacks'].isChecked(),gpu=True,cpu_assist=controls['cpu_assist'].isChecked(),
                target_size=AREA_CHOICES[area.currentIndex()][2],
                shape_proxies=False,target_origin=target_tile(),iteration_limit=iterations.value(),
                spread_small=controls['spread_small'].isChecked(),
                preserve_source=source.isChecked() and transfer_mode.currentData()=='same',
                base_density=window.texel_density_value.value() if hasattr(window,'texel_density_value') else 500.,
                refresh_transfer=refresh_transfer if source.isChecked() else None,
                keep_texel_groups=keep_texel_enabled(),
                to_other=to_other)
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
            if result.get('fixed_shell_groups'):
                status.setText(status.text()+' · {} Shell Groups fixed'.format(result['fixed_shell_groups']))
            if result.get('spread_small_moved'):
                status.setText(status.text()+' · {} small groups redistributed'.format(result['spread_small_moved']))
            if result.get('pack_groups_moved'):
                status.setText(status.text()+' · {} shells moved closer to their groups'.format(result['pack_groups_moved']))
            if result.get('precision'):
                status.setText(status.text()+' · grid {}'.format(result['precision']))
            if result.get('engine'):
                status.setText(status.text()+' · '+result['engine'])
        status.show()
        return result
    actions=QtWidgets.QHBoxLayout();actions.setSpacing(CONTROL_SPACING);layout.addLayout(actions)
    controls['button'] = button('Pack UVs', lambda: perform(), actions)
    controls['button'].setToolTip('Pack selected whole shells into the target tile, respecting visible fixed shells on selected meshes and UV Area. Unselected shells stay fixed. Selecting an object includes all its visible shells.\nRunning again with the same shells and settings continues from the best result. UV, geometry, visibility or material changes reset preparation.')
    def tile_text():
        u,v=target_tile()
        return 'Tile '+(str(1001+u+v*10) if 0<=u<10 and v>=0 else '({}, {})'.format(u,v))
    def choose_target():
        from uv_straight_pack_target import choose_tile
        def picked(u,v):
            from shiboken2 import isValid
            cmds.optionVar(intValue=(PREFIX+'TileU',u));cmds.optionVar(intValue=(PREFIX+'TileV',v))
            save_settings()
            if isValid(window):window.packing_refresh()
        choose_tile(picked)
    controls['tile_button']=button('Choose UV Tile',choose_target,actions)
    controls['tile_button'].setText(tile_text())
    controls['tile_button'].setToolTip('Choose the packing target: point at a whole tile in the UV Editor and left-click. Esc or right-click cancels. Shell selection is preserved.')
    actions.setStretch(0,1);actions.setStretch(1,1)
    other_actions = QtWidgets.QHBoxLayout()
    layout.addLayout(other_actions)
    controls['other_button'] = button('Pack to Other', lambda: perform(to_other=True), other_actions)
    controls['other_button'].setToolTip('Pack selected shells around visible shells already in the target tile.')
    conversion = QtWidgets.QHBoxLayout()
    conversion.setSpacing(CONTROL_SPACING)
    layout.addLayout(conversion)
    controls['square_button'] = button('To Square', lambda:uv.rectangle_to_square(preserve_source=False), conversion)
    controls['square_button'].setToolTip('Stretch selected shells from the lower half to the full square: V ×2 about V=0; U is unchanged. Selecting any part includes the whole shell. One Undo restores the layout.')
    controls['rectangle_button'] = button('To Rectangle', lambda:uv.square_to_rectangle(preserve_source=False), conversion)
    controls['rectangle_button'].setToolTip('Compress selected shells into the lower half: V ×0.5 about V=0; U is unchanged. Selecting any part includes the whole shell. One Undo restores the layout.')
    conversion.setStretch(0,1);conversion.setStretch(1,1)
    from uv_straight_groups_ui import build as build_groups, keep_texel_enabled
    build_groups(window,layout)
    from uv_straight_transfer_direct_ui import DirectTransferControls
    window.texture_transfer=DirectTransferControls(window,layout,resolution,source,controls['spacing'],
                                                    transfer_mode.currentData())
    def change_transfer_mode():
        mode=transfer_mode.currentData()
        cmds.optionVar(stringValue=(PREFIX+'TransferMode',mode))
        save_settings()
        window.texture_transfer.set_mode(mode)
        if hasattr(window,'adjust_popup_size'):
            QtCore.QTimer.singleShot(0,window.adjust_popup_size)
    transfer_mode.currentIndexChanged.connect(change_transfer_mode)
    layout.addWidget(status)
    # Sharing persisted settings also keeps a dock and a popup in agreement.
    def refresh():
        for key, widget in controls.items():
            if key in ('status', 'button', 'tile_button', 'other_button', 'square_button', 'rectangle_button'):
                continue
            blocker = QtCore.QSignalBlocker(widget)
            if key == 'area':
                widget.setCurrentIndex(max(0,widget.findData(saved_area())))
            elif key in ('normalize', 'rotate', 'keep_stacks','cpu_assist','spread_small','transfer'):
                widget.setChecked(bool(saved(key.title(), key != 'spread_small')))
            else:
                widget.setValue(saved('SearchSeconds' if key == 'seconds' else key.title(),
                                      4096 if key == 'resolution' else 0 if key in ('seconds','iterations') else 10. if key == 'spacing' else 5.))
        clamp_spacing(resolution.value())
        controls['tile_button'].setText(tile_text())
        with signals_blocked(transfer_mode):
            restore_transfer_mode()
        window.texture_transfer.set_mode(transfer_mode.currentData())
        window.texture_transfer.set_enabled(source.isChecked())
    window.packing_refresh = refresh
