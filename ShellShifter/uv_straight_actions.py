"""User-facing shortcut actions bound to the actual palette commands/settings."""
from PySide6 import QtWidgets
from shiboken6 import isValid

# Stable IDs remain independent of labels and palette positions.
CATALOG = (
    ('unwrap_mesh', 'Unwrap', 'Unwrap Mesh'),
    ('unfold', 'Unwrap', 'Unfold'),
    ('relax', 'Unwrap', 'Relax'),
    ('pack_uvs', 'Packing', 'Pack UVs'),
    ('pack_to_other', 'Packing', 'Pack to Other'),
    ('uv_to_square', 'Packing', 'To Square'),
    ('uv_to_rectangle', 'Packing', 'To Rectangle'),
    ('mark_h', 'H / V Pins', 'Mark H'),
    ('mark_v', 'H / V Pins', 'Mark V'),
    ('auto_hv', 'H / V Pins', 'Auto H/V'),
    ('unpin', 'H / V Pins', 'Unpin'),
    ('pick_toggle', 'H / V Pins', 'Edit Pins'),
    ('pick_auto', 'H / V Pins', 'Segment Picker: Automatic H/V'),
    ('pick_h', 'H / V Pins', 'Segment Picker: H'),
    ('pick_v', 'H / V Pins', 'Segment Picker: V'),
    ('straighten_quads', 'Orient & Straighten', 'Straighten Quads'),
    ('orient', 'Orient & Straighten', 'Orient Shells'),
    ('checker_apply', 'Checker', 'Apply Checker'),
    ('checker_gray', 'Checker', 'Apply Gray Squares'),
    ('checker_color', 'Checker', 'Apply Color Labels'),
    ('checker_remove', 'Checker', 'Remove Checker'),
    ('tiles_down', 'Checker', 'Checker Tiles /2'),
    ('tiles_up', 'Checker', 'Checker Tiles x2'),
    ('wireframe', 'Display', 'Wireframe'),
    ('show_pins', 'Display', 'Toggle H/V Pins'),
    ('distortion', 'Display', 'Toggle Distortion'),
    ('density_get', 'Texel Density', 'Get Texel Density'),
    ('density_set', 'Texel Density', 'Set Texel Density'),
    ('density_analyse', 'Texel Density', 'Analyse & Show Texel Density'),
    ('density_clear', 'Texel Density', 'Clear Texel Density'),
    ('small_shells', 'UV Checks', 'Find Small / Thin Shells'),
    ('hard_edges', 'UV Checks', 'Check Hard Edges'),
    ('overlap_check', 'UV Checks', 'Select Overlaps'),
    ('fix_butterflies', 'UV Checks', 'Fix Butterfly Overlaps'),
    ('bad_overlaps', 'UV Checks', 'Check Overlaps'),
    ('uv_cleanup', 'UV Checks', 'UV Set Cleanup'),
    ('done', 'Finished Meshes', 'Mark Done'),
    ('done_clear', 'Finished Meshes', 'Clear Done'),
    ('scene_colors', 'Finished Meshes', 'Restore Scene Colors'),
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
    ('stack_shells', 'Move Overlaps', 'Stack Similar Shells'),
    ('check_updates', 'Updates', 'Check for Updates'),
    ('report_bug', 'Updates', 'Report a Bug'),
    ('window', 'ShellShifter', 'Open ShellShifter'),
    ('popup', 'ShellShifter', 'Toggle Popup'),
)
BY_ID = {action: (group, title) for action, group, title in CATALOG}
BUTTON_IDS = {title: action for action, group, title in CATALOG}
BUTTON_IDS.update({'÷2': 'tiles_down', '×2': 'tiles_up', 'Get': 'density_get',
                   'Set': 'density_set', 'Analyse && Show': 'density_analyse',
                   'Clear': 'density_clear', 'U': 'flip_u', 'V': 'flip_v',
                   'Analyse': 'overlap_analyse'})

# The palette uses short captions where the surrounding section supplies context.
# Keep pie entries and its function picker consistent with those visible controls;
# the descriptive catalog above remains useful for shortcut registration.
BUTTON_TITLES = dict(
    pick_auto='Edit Pins', pick_h='H', pick_v='V',
    checker_gray='Gray Squares', checker_color='Color Labels',
    tiles_down='÷2', tiles_up='×2', show_pins='Show H/V Pins',
    distortion='Show Distortion', density_get='Get', density_set='Set',
    density_analyse='Analyse & Show', density_clear='Clear',
    overlap_check='Select', bad_overlaps='Check 99%',
    fix_butterflies='Fix Butterfly', uv_cleanup='Cleanup',
    flip_u='U', flip_v='V', original_set='Set Selected',
    original_clear='Clear', original_color='Show Originals Color',
    overlap_analyse='Analyse')


def button_title(action):
    return BUTTON_TITLES.get(action, BY_ID[action][1])


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
