"""User-facing shortcut actions bound to the actual palette commands/settings."""
from PySide6 import QtWidgets
from shiboken6 import isValid

# Stable IDs remain independent of labels and palette positions.
CATALOG = (
    ('unwrap_mesh', 'Unwrap', 'Unwrap Mesh'),
    ('unfold', 'Unwrap', 'Unfold'),
    ('relax', 'Unwrap', 'Relax'),
    ('relax_h', 'Unwrap', 'Relax H'),
    ('relax_v', 'Unwrap', 'Relax V'),
    ('straighten_quads', 'Unwrap', 'Straighten Quads'),
    ('orient', 'Unwrap', 'Orient Shells'),
    ('flip_shells','Unwrap','Flip'),
    ('rotate_shells','Unwrap','Rotate'),
    ('rotate_angle','Unwrap','Rotation Angle'),
    ('rotate_ccw','Unwrap','Rotate Counterclockwise'),
    ('rotate_cw','Unwrap','Rotate Clockwise'),
    ('stack_shells', 'Unwrap', 'Stack Similar Shells'),
    ('match_uvs', 'Unwrap', 'Transfer UVs'),
    ('unpin', 'Unwrap', 'Unpin'),
    ('pin_axis_h', 'Unwrap', 'Toggle H Pin Axis'),
    ('pin_axis_v', 'Unwrap', 'Toggle V Pin Axis'),
    ('pack_uvs', 'Packing', 'Pack UVs'),
    ('pack_to_other', 'Packing', 'Pack to Other'),
    ('pack_target', 'Packing', 'Choose UV Tile'),
    ('uv_to_square', 'Packing', 'To Square'),
    ('uv_to_rectangle', 'Packing', 'To Rectangle'),
    ('texel_groups', 'Packing', 'Texel Groups'),
    ('shell_groups', 'Packing', 'Shell Groups'),
    ('texture_transfer', 'Packing', 'Texture Transfer'),
    ('transfer_connect', 'Packing', 'Transfer & Connect Textures'),
    ('mark_h', 'Unwrap', 'Mark H'),
    ('mark_v', 'Unwrap', 'Mark V'),
    ('auto_hv', 'Unwrap', 'Mark Auto H/V'),
    ('pick_toggle', 'Unwrap', 'Edit Pins'),
    ('pick_auto', 'Unwrap', 'Segment Picker: Automatic H/V'),
    ('pick_h', 'Unwrap', 'Segment Picker: H'),
    ('pick_v', 'Unwrap', 'Segment Picker: V'),
    ('pick_axes_auto', 'Unwrap', 'Automatic Pin Axis Selection'),
    ('wireframe', 'Display', 'Wireframe'),
    ('show_pins', 'Display', 'Toggle H/V Pins'),
    ('distortion', 'Display', 'Toggle Distortion'),
    ('scene_colors', 'Display', 'Finished Unwrap Meshes'),
    ('done', 'Display', 'Mark Done'),
    ('done_clear', 'Display', 'Clear Done'),
    ('display_texel', 'Display', 'Texel'),
    ('display_checker', 'Display', 'Checker'),
    ('checker_apply', 'Checker', 'Apply Checker'),
    ('checker_gray', 'Checker', 'Apply Gray Squares'),
    ('checker_color', 'Checker', 'Apply Color Labels'),
    ('checker_remove', 'Checker', 'Remove Checker'),
    ('tiles_down', 'Checker', 'Checker Tiles /2'),
    ('tiles_up', 'Checker', 'Checker Tiles x2'),
    ('density_get', 'Texel Density', 'Get Texel Density'),
    ('density_set', 'Texel Density', 'Set Texel Density'),
    ('density_analyse', 'Texel Density', 'Analyse & Show Texel Density'),
    ('density_clear', 'Texel Density', 'Clear Texel Density'),
    ('small_shells', 'UV Checks', 'Find Small / Thin Shells'),
    ('zero_area_shells', 'UV Checks', 'Zero-Area Shells'),
    ('hard_edges', 'UV Checks', 'Check Hard Edges'),
    ('overlap_check', 'UV Checks', 'Select Overlaps'),
    ('fix_butterflies', 'UV Checks', 'Fix Butterfly Overlaps'),
    ('bad_overlaps', 'UV Checks', 'Bad Overlaps'),
    ('uv_cleanup', 'UV Checks', 'UV Set Cleanup'),
    ('move_up', 'Move Overlaps', 'Move Shells Up'),
    ('move_down', 'Move Overlaps', 'Move Shells Down'),
    ('move_left', 'Move Overlaps', 'Move Shells Left'),
    ('move_right', 'Move Overlaps', 'Move Shells Right'),
    ('flip_u', 'Move Overlaps', 'Flip U'),
    ('flip_v', 'Move Overlaps', 'Flip V'),
    ('original_set', 'Move Overlaps', 'Set Selection as Original'),
    ('original_clear', 'Move Overlaps', 'Clear Originals'),
    ('original_color', 'Move Overlaps', 'Toggle Originals Color'),
    ('overlap_analyse', 'Move Overlaps', 'Analyse Copies'),
    ('select_in_tile', 'Move Overlaps', 'Select in Tile'),
    ('material_assign', 'Materials', 'Assign…'),
    ('material_clear', 'Materials', 'Clear Material Textures'),
    ('material_convert', 'Materials', 'Convert'),
    ('material_view', 'Materials', 'View'),
    ('material_refresh', 'Materials', 'Refresh Assigned Materials'),
    ('check_updates', 'Updates', 'Check for Updates'),
    ('window', 'ShellShifter', 'Open ShellShifter'),
    ('popup', 'ShellShifter', 'Toggle Popup'),
)
BY_ID = {action: (group, title) for action, group, title in CATALOG}
_LEGACY_ONLY = frozenset((
    'pack_to_other', 'mark_h', 'mark_v', 'auto_hv', 'pick_toggle', 'pick_auto',
    'pick_h', 'pick_v', 'pick_axes_auto', 'checker_gray', 'checker_color', 'overlap_check',
))
# Old IDs still resolve for saved menus and shortcuts, but are not offered
# for new assignments.
ASSIGNABLE_CATALOG = tuple(item for item in CATALOG if item[0] not in _LEGACY_ONLY)
BUTTON_IDS = {title: action for action, group, title in CATALOG}
BUTTON_IDS.update({'÷2': 'tiles_down', '×2': 'tiles_up', 'Get': 'density_get',
                   'Set': 'density_set', 'Analyse && Show': 'density_analyse',
                   'Show Color':'density_analyse','Clear Color':'density_clear',
                   'Clear': 'density_clear', 'U': 'flip_u', 'V': 'flip_v',
                   'Analyse': 'overlap_analyse','Analyse Select':'overlap_analyse'})

# The palette uses short captions where the surrounding section supplies context.
# Keep pie entries and its function picker consistent with those visible controls;
# the descriptive catalog above remains useful for shortcut registration.
BUTTON_TITLES = dict(
    pick_auto='Edit Pins', pick_h='H', pick_v='V',pick_axes_auto='Auto H/V',auto_hv='Mark Auto H/V',
    checker_gray='Gray Squares', checker_color='Color Labels',
    tiles_down='÷2', tiles_up='×2', show_pins='Pins', scene_colors='Show Done',
    pin_axis_h='H', pin_axis_v='V', material_clear='Clear',
    distortion='Distortion', density_get='Get', density_set='Set',
    density_analyse='Show Color', density_clear='Clear Color',
    overlap_check='Select',
    fix_butterflies='Fix Butterfly', uv_cleanup='Cleanup',
    flip_u='U', flip_v='V', original_set='Set Selected',
    original_clear='Clear', original_color='Show Originals Color',
    overlap_analyse='Analyse Select', stack_shells='Stack Shells')


def button_title(action):
    return BUTTON_TITLES.get(action, BY_ID[action][1])


# Tooltip captions are independent of action IDs and saved menu labels.
TOOLTIP_LABELS = {
    'Unwrap': 'Unwrap',
    'Unwrap Mesh': 'Unwrap Mesh',
    'Unfold': 'Unfold',
    'Relax': 'Relax',
    'Relax H': 'Relax H',
    'Relax V': 'Relax V',
    'Flip':'Flip',
    'Rotate':'Rotate',
    'Rotation Angle':'Rotation Angle',
    'Rotate Counterclockwise':'Rotate Counterclockwise',
    'Rotate Clockwise':'Rotate Clockwise',
    'Packing': 'Packing',
    'Pack UVs': 'Pack UVs',
    'Pack to Other': 'Pack to Other',
    'Choose UV Tile': 'Choose UV Tile',
    'To Square': 'To Square',
    'To Rectangle': 'To Rectangle',
    'H / V Pins': 'H / V Pins',
    'Mark H': 'Mark H',
    'Mark V': 'Mark V',
    'Auto H/V': 'Auto H/V',
    'Mark Auto H/V': 'Mark Auto H/V',
    'Unpin': 'Unpin',
    'Edit Pins': 'Edit Pins',
    'Segment Picker: Automatic H/V': 'Segment Picker: Automatic H/V',
    'Segment Picker: H': 'Segment Picker: H',
    'Segment Picker: V': 'Segment Picker: V',
    'Automatic Pin Axis Selection': 'Automatic Pin Axis Selection',
    'Orient & Straighten': 'Orient & Straighten',
    'Straighten Quads': 'Straighten Quads',
    'Orient Shells': 'Orient Shells',
    'Checker': 'Checker',
    'Apply Checker': 'Apply Checker',
    'Apply Gray Squares': 'Apply Gray Squares',
    'Apply Color Labels': 'Apply Color Labels',
    'Gray Squares': 'Gray Squares',
    'Color Labels': 'Color Labels',
    'Remove Checker': 'Remove Checker',
    'Checker Tiles /2': 'Checker Tiles /2',
    'Checker Tiles x2': 'Checker Tiles x2',
    'Checker Tiling': 'Checker Tiling',
    'Multiply by 2': 'Multiply by 2',
    'Divide by 2': 'Divide by 2',
    'Display': 'Display',
    'Wireframe': 'Wireframe',
    'Toggle H/V Pins': 'Toggle H/V Pins',
    'Show H/V Pins': 'Show Pins',
    'Show Pins':'Show Pins',
    'Distortion':'Show Distortion',
    'Intensity':'Intensity',
    'Show Color':'Show Color',
    'Clear Color':'Clear Color',
    'Toggle Distortion': 'Toggle Distortion',
    'Show Distortion': 'Show Distortion',
    'Texel Density': 'Texel Density',
    'Get Texel Density': 'Get Texel Density',
    'Set Texel Density': 'Set Texel Density',
    'Analyse & Show Texel Density': 'Analyse & Show Texel Density',
    'Clear Texel Density': 'Clear Texel Density',
    'Get': 'Get',
    'Set': 'Set',
    'Analyse & Show': 'Analyse & Show',
    'Clear': 'Clear',
    'Materials': 'Materials',
    'UV Checks': 'UV Checks',
    'Find Small / Thin Shells': 'Find Small / Thin Shells',
    'Check Hard Edges': 'Check Hard Edges',
    'Select Overlaps': 'Select Overlaps',
    'Select': 'Select',
    'Fix Butterfly Overlaps': 'Fix Butterfly Overlaps',
    'Fix Butterfly': 'Fix Butterfly',
    'Check Overlaps': 'Check Overlaps',
    'Bad Overlaps': 'Bad Overlaps',
    'Check Shells': 'Bad Overlaps',
    'Check 99%': 'Bad Overlaps',
    'UV Set Cleanup': 'Cleanup',
    'Cleanup': 'Cleanup',
    'Finished Unwrap Meshes': 'Finished Unwrap Meshes',
    'Done': 'Done',
    'Mark Done': 'Mark Done',
    'Clear Done': 'Clear Done',
    'Move Overlaps': 'Move Overlaps',
    'Move Shells Up': 'Move Shells Up',
    'Move Shells Down': 'Move Shells Down',
    'Move Shells Left': 'Move Shells Left',
    'Move Shells Right': 'Move Shells Right',
    'Flip U': 'Flip U',
    'Flip V': 'Flip V',
    'Set Selection as Original': 'Set Selection as Original',
    'Set Selected': 'Set Selected',
    'Clear Originals': 'Clear Originals',
    'Toggle Originals Color': 'Toggle Originals Color',
    'Show Originals Color': 'Show Originals Color',
    'Analyse Copies': 'Analyse Copies',
    'Analyse': 'Analyse',
    'Stack Similar Shells': 'Stack Shells',
    'Stack Shells': 'Stack Shells',
    'Analyse Select': 'Analyse Select',
    'Updates': 'Updates',
    'Check for Updates': 'Check for Updates',
    'Open ShellShifter': 'Open ShellShifter',
    'Toggle Popup': 'Toggle Popup',
    'Keymaps': 'Keymaps',
    'Drop Function': 'Drop Function',
    'Separator': 'Separator',
    '— Separator —': '— Separator —',
    'Empty position': 'Empty position',
    'New Submenu': 'New Submenu',
}


def tooltip_label(title):
    """Translate built-in captions while retaining user-defined labels."""
    title = title.replace('&&', '&')
    return TOOLTIP_LABELS.get(title, title)


def register_button(window, title, callback):
    action = BUTTON_IDS.get(title)
    if action:
        window.shortcut_actions[action] = callback


def tool_window():
    import uv_straight as uv
    import uv_straight_preview as preview
    app = QtWidgets.QApplication.instance()
    candidates = [preview._popup, uv._window]
    candidates += [widget for widget in app.allWidgets() if hasattr(widget, 'shortcut_actions')]
    valid = [window for window in candidates if window is not None and isValid(window)
             and hasattr(window, 'shortcut_actions')]
    # A visible popup owns the settings used while it is open.
    return next((window for window in valid if window.isVisible()), valid[0] if valid else None)


def execute(action):
    if action not in BY_ID:
        raise ValueError('This ShellShifter function is no longer available.')
    import uv_straight as uv
    import uv_straight_menu as menu
    if action == 'window':
        menu.after_menu_interaction(lambda: uv._run(uv.show))
        return
    if action == 'popup':
        import uv_straight_preview as preview
        menu.after_menu_interaction(lambda: uv._run(preview.toggle_popup))
        return
    window = tool_window()
    if window is None:
        uv.show()
        window = tool_window()
    uv._run(window.shortcut_actions[action])
