"""Shared artist-painted raster icons and vector icons for every UI surface.

The edited atlas owns the Unwrap/Tile designs; other actions use SVG sources.
Both paths render at the target device resolution and feed Qt/native menus.
"""
from pathlib import Path
from functools import lru_cache
import hashlib
import math
import tempfile
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtSvg import QSvgRenderer


PANEL_SIZE = 22
PIE_SIZE = 18
RASTER_SIZE = 96
RASTER_ATLAS=Path(__file__).resolve().parent/'assets'/'shellshifter_icons.png'
REVISION='v13_20261108_aa_'+(hashlib.sha256(RASTER_ATLAS.read_bytes()).hexdigest()[:12] if RASTER_ATLAS.is_file() else 'svg')
RASTER_KEYS=('mesh','quad','unfold','orient','relax','stack','flip_u','flip_v','rotate_ccw','rotate_cw','pick')
_raster_atlas=None
_raster_images={}

ACTION_ICONS = {
    'unwrap_mesh': 'mesh', 'unfold': 'unfold', 'relax': 'relax',
    'relax_h':'H', 'relax_v':'V',
    'flip_shells':'flip_u','rotate_shells':'rotate_cw','rotate_angle':'rotate_cw',
    'rotate_ccw':'rotate_ccw','rotate_cw':'rotate_cw',
    'pack_uvs': 'pack', 'pack_to_other': 'pack_other', 'pack_target': 'pick',
    'uv_to_square': 'to_square', 'uv_to_rectangle': 'to_rectangle',
    'mark_h': 'H', 'mark_v': 'V', 'auto_hv': 'auto', 'unpin': 'unpin',
    'pick_toggle': 'pick', 'pick_auto': 'auto', 'pick_h': 'H', 'pick_v': 'V', 'pick_axes_auto': 'auto',
    'straighten_quads': 'quad', 'orient': 'orient',
    'checker_apply': 'checker', 'checker_gray': 'checker',
    'checker_color': 'checker_color', 'checker_remove': 'checker_remove',
    'tiles_down': 'tiles_down', 'tiles_up': 'tiles_up',
    'wireframe': 'wireframe', 'show_pins': 'pins', 'distortion': 'distortion',
    'density_get': 'density_get', 'density_set': 'density_set',
    'density_analyse': 'density_analyse', 'density_clear': 'clear',
    'small_shells': 'small_shells', 'zero_area_shells': 'zero_area_shells', 'hard_edges': 'hard_edges',
    'overlap_check': 'overlaps', 'fix_butterflies': 'butterfly',
    'bad_overlaps': 'bad_overlaps', 'uv_cleanup': 'cleanup',
    'done': 'done', 'done_clear': 'done_clear', 'scene_colors': 'scene_colors',
    'move_up': 'move_up', 'move_down': 'move_down',
    'move_left': 'move_left', 'move_right': 'move_right',
    'flip_u': 'flip_u', 'flip_v': 'flip_v',
    'original_set': 'original_set', 'original_clear': 'clear',
    'original_color': 'eye', 'overlap_analyse': 'analyse', 'stack_shells': 'stack',
    'window': 'window', 'popup': 'popup', 'check_updates': 'updates','match_uvs':'stack',
    'pin_axis_h':'H', 'pin_axis_v':'V', 'texel_groups':'density_analyse', 'shell_groups':'pack',
    'texture_transfer':'texture_transfer', 'transfer_connect':'texture_transfer',
    'display_texel':'density_analyse', 'display_checker':'checker', 'select_in_tile':'pick',
    'material_assign':'material_folder', 'material_clear':'clear', 'material_convert':'material_convert',
    'material_view':'material_eye', 'material_refresh':'material_refresh',
}

from uv_straight_actions import CATALOG
# Small text controls and the compact Originals row retain their existing size.
BUTTONS = {title: ACTION_ICONS[action] for action, _, title in CATALOG
           if action not in ('original_set', 'original_clear')}
BUTTONS.update({'Orient by Longest Side': 'orient',
                'Remove Segment by Click': 'pick', 'Add / Remove Segment by Click': 'pick',
                'Add / Remove Segment': 'pick'})


# Rounded outlines, shaded panels and the same gold/cyan/mint accents as the rail.
BODIES = {
    'texture_transfer': '<g stroke="#282d31" stroke-width="1.7" stroke-linejoin="round">'
                        '<rect x="7.55" y="0.85" width="15.6" height="15.6" rx="5" fill="#806a3e"/>'
                        '<rect x="0.85" y="7.55" width="15.6" height="15.6" rx="5" fill="#bba370"/></g>',
    'wrench': '<path d="M14 3.5C17 1.5 20 2.5 21.5 4L17 8.5L15.5 7L11 11.5L12.5 13L8 17.5L6.5 16L3 19.5L4.5 21L8 17.5L9.5 19L16 12.5C20 13 23 9.5 21.5 6L18 9.5L14.5 6Z" class="panel"/>'
              '<circle cx="5" cy="19" r="1" class="node"/>',
    'updates': '<path d="M20 13.6A8.2 8.2 0 1 1 18.1 6.6M17.4 2.8L18.1 6.6L14.3 7.3" '
               'class="refresh"/>',
    'wireframe': '<path d="M12 2.5L21 7.5V17L12 22L3 17V7.5Z" class="accent"/>'
                 '<path d="M3 7.5L12 12.5L21 7.5M12 12.5V22M7.5 5L16.5 10V19.5M16.5 5L7.5 10V19.5M3 12L12 17L21 12" class="line"/>',
    # A flat carton net: four side panels, two lids and tapered glue flaps.
    'mesh': '<path d="M2 8H7V3H9V1.5H11V3H13V8H22V16H13V21H11V22.5H9V21H7V16H2Z" '
            'fill="#c29a63" stroke="#e4ad60" stroke-width="1.1"/>'
            '<path d="M2 8L3 5.5H6L7 8M13 8L14 5.5H16.5L17.5 8M17.5 8L18.5 5.5H21L22 8" '
            'fill="#a77c47" stroke="#e4ad60" stroke-width="1.1"/>'
            '<path d="M2 16L3 18.5H6L7 16M13 16L14 18.5H16.5L17.5 16M17.5 16L18.5 18.5H21L22 16" '
            'fill="#a77c47" stroke="#e4ad60" stroke-width="1.1"/>'
            '<path d="M7 8V16M13 8V16M17.5 8V16M2 8H22M2 16H22" '
            'stroke="#f1ddb7" stroke-width=".9" stroke-dasharray="1.5 1.3"/>',
    'unfold': '<path d="M2.5 7L8.5 3.5L15 7L21.5 3.5V17L15 20.5L8.5 17L2.5 20.5Z" class="panel"/>'
              '<path d="M8.5 3.5V17M15 7V20.5M2.5 13.5L8.5 10L15 13.5L21.5 10" class="line"/>'
              '<circle cx="8.5" cy="3.5" r="1.5" class="pin"/>'
              '<circle cx="15" cy="20.5" r="1.5" class="pin"/>',
    'relax': '<rect x="3" y="4" width="18" height="16" rx="3" class="panel"/>'
             '<path d="M3 9C7 6 9 12 12 10S17 7 21 9M3 15C7 12 9 18 12 16S17 13 21 15" class="line"/>'
             '<path d="M9 4C6 9 12 15 9 20M15 4C12 9 18 15 15 20" class="line"/>'
             '<circle cx="3" cy="4" r="1.4" class="pin"/><circle cx="21" cy="20" r="1.4" class="pin"/>',
    'pack': '<path d="M12 2.5L21 7.5V17L12 22L3 17V7.5Z" class="box"/>'
            '<path d="M12 2.5L21 7.5L12 12.5L3 7.5Z" fill="#bf925c" stroke="none"/>'
            '<path d="M3 7.5L12 12.5V22L3 17Z" fill="#a47743" stroke="none"/>'
            '<path d="M12 12.5L21 7.5V17L12 22Z" fill="#835b34" stroke="none"/>'
            '<path d="M3 7.5L12 12.5L21 7.5M12 12.5V22" class="accent"/>'
            '<path d="M6.4 5.6L8.6 4.4L17.6 9.4L15.4 10.6Z" fill="#f1d39b" stroke="none"/>'
            '<path d="M15.4 10.6L17.6 9.4V13.6L15.4 14.8Z" fill="#d8b476" stroke="none"/>'
            '<path d="M4.4 11.7L9.4 14.5V17.5L4.4 14.7Z" fill="#f1e4c5" stroke="none"/>'
            '<path d="M12 2.5L21 7.5V17L12 22L3 17V7.5Z" class="accent"/>',
    'pack_other': '<rect x="2.5" y="3" width="7" height="7" rx="1.5" class="panel"/>'
                  '<rect x="14.5" y="14" width="7" height="7" rx="1.5" class="panel"/>'
                  '<path d="M3 14V21H10M14 3H21V10" class="accent"/>'
                  '<path d="M10 14L15 9M10 9H15V14" class="line"/>',
    'to_square': '<rect x="2.5" y="3" width="13" height="18" rx="1.5" class="panel"/>'
                 '<path d="M2.5 12H15.5" class="accent" stroke-dasharray="2 2"/>'
                 '<path d="M19.5 20V4M17 6.5L19.5 4L22 6.5M17 17.5L19.5 20L22 17.5" class="line"/>',
    'to_rectangle': '<rect x="2.5" y="3" width="13" height="18" rx="1.5" class="accent"/>'
                    '<path d="M2.5 12H15.5V19.5Q15.5 21 14 21H4Q2.5 21 2.5 19.5Z" class="panel"/>'
                    '<path d="M19.5 4V12M17 9.5L19.5 12L22 9.5" class="line"/>',
    'H': '<path d="M3 12H21M3 8V16M21 8V16" class="accent"/>'
         '<circle cx="3" cy="12" r="1.8" class="node"/><circle cx="21" cy="12" r="1.8" class="node"/>',
    'V': '<path d="M12 3V21M8 3H16M8 21H16" class="accent"/>'
         '<circle cx="12" cy="3" r="1.8" class="node"/><circle cx="12" cy="21" r="1.8" class="node"/>',
    'auto': '<rect x="3" y="3" width="18" height="18" rx="3" class="panel"/>'
            '<path d="M3 12H21" stroke="#ff963b"/><path d="M12 3V21" stroke="#38c9ff"/>'
            '<circle cx="12" cy="12" r="2.2" class="node"/><path d="M19 1V5M17 3H21" class="spark"/>',
    'unpin': '<path d="M8 3H16L15 8L19 12H13L12 21L11 12H5L9 8Z" class="panel"/>'
             '<path d="M4 3L20 21" class="warning"/>',
    'pick': '<path d="M3 5H11M3 2V8M11 2V8" class="accent"/>'
            '<path d="M10 9L21 15L16 17L14 22Z" class="panel"/>'
            '<path d="M3 15V21M0 18H6" class="accent"/>',
    'quad': '<rect x="3.5" y="3.5" width="17" height="17" rx="2" class="panel"/>'
            '<path d="M9 3.5V20.5M15 3.5V20.5M3.5 9H20.5M3.5 15H20.5" class="line"/>',
    'orient': '<path d="M6 15L14 7L19 12L11 20Z" class="panel"/>'
              '<path d="M3 21H21M3 11C2 5 7 2 13 3M10 1L13 3L11 6" class="accent"/>',
    'checker': '<rect x="3" y="3" width="18" height="18" rx="2" class="panel"/>'
               '<path d="M3 3H9V9H3ZM15 3H21V9H15ZM9 9H15V15H9ZM3 15H9V21H3ZM15 15H21V21H15Z" class="node" stroke="none"/>',
    'checker_color': '<rect x="3" y="3" width="18" height="18" rx="2" class="panel"/>'
                     '<path d="M4 4H11V11H4Z" fill="#e89962" stroke="none"/>'
                     '<path d="M13 4H20V11H13Z" fill="#73bde9" stroke="none"/>'
                     '<path d="M4 13H11V20H4Z" fill="#74cc9d" stroke="none"/>'
                     '<path d="M13 13H20V20H13Z" fill="#d28cce" stroke="none"/>',
    'checker_remove': '<rect x="3" y="3" width="15" height="15" rx="2" class="panel"/>'
                      '<path d="M3 3H8V8H3ZM13 3H18V8H13ZM8 8H13V13H8ZM3 13H8V18H3Z" class="node" stroke="none"/>'
                      '<path d="M15 15L22 22M22 15L15 22" class="warning"/>',
    'tiles_down': '<rect x="3" y="3" width="18" height="18" rx="3" class="panel"/><path d="M7 12H17" class="line"/>',
    'tiles_up': '<rect x="3" y="3" width="18" height="18" rx="3" class="panel"/><path d="M7 12H17M12 7V17" class="line"/>',
    'pins': '<rect x="4" y="4" width="16" height="16" rx="2" class="panel"/>'
            '<path d="M4 4H20M4 20H20" stroke="#ff963b"/><path d="M4 4V20M20 4V20" stroke="#38c9ff"/>'
            '<circle cx="4" cy="4" r="1.5" class="node"/><circle cx="20" cy="20" r="1.5" class="node"/>',
    'distortion': '<path d="M3 5L10 3L15 7L21 5L20 19L13 21L8 17L3 19Z" class="panel"/>'
                  '<path d="M10 3L8 17M15 7L13 21M3 12L9 10L14 14L20.5 12" class="line"/>',
    'density_get': '<rect x="3" y="3" width="12" height="12" rx="2" class="panel"/>'
                   '<path d="M7 3V15M11 3V15M3 7H15M3 11H15M18 12V21M15 18L18 21L21 18" class="line"/>',
    'density_set': '<rect x="3" y="3" width="12" height="12" rx="2" class="panel"/>'
                   '<path d="M7 3V15M11 3V15M3 7H15M3 11H15M18 21V12M15 15L18 12L21 15" class="line"/>',
    'density_analyse': '<path d="M3 17V7M8 17V3M13 17V11" class="accent"/>'
                       '<circle cx="17" cy="14" r="4" class="panel"/><path d="M20 17L23 21" class="line"/>',
    'clear': '<path d="M3 15L13 4L21 11L12 21H8Z" class="panel"/>'
             '<path d="M9 8L17 15M12 21H22" class="line"/>',
    'small_shells': '<path d="M3 9V3H9M15 3H21V9M3 15V21H9M15 21H21V15" class="accent"/>'
                    '<rect x="7" y="10" width="10" height="4" rx="1" class="panel"/>',
    'zero_area_shells': '<path d="M4 19L20 5" class="accent"/>'
                        '<circle cx="4" cy="19" r="2" class="node"/>'
                        '<circle cx="12" cy="12" r="2" class="node"/>'
                        '<circle cx="20" cy="5" r="2" class="node"/>',
    'hard_edges': '<path d="M3 6L11 3L21 7L13 11V21L3 17Z" class="panel"/>'
                  '<path d="M13 11L21 7V17L13 21" class="line"/>'
                  '<path d="M13 11V21" class="warning"/>',
    'overlaps': '<rect x="3" y="3" width="12" height="12" rx="2" class="panel"/>'
                '<rect x="9" y="9" width="12" height="12" rx="2" class="shade"/>'
                '<path d="M9 9H15V15H9Z" class="node" stroke="none"/>',
    'stack': '<path d="M3 6L8 2H15L19 6V13L15 17H8L3 13Z" class="panel"/>'
             '<path d="M6 9L11 5H18L22 9V16L18 20H11L6 16Z" class="shade"/>'
             '<path d="M10 13H18M14 9V17" class="line"/>'
             '<circle cx="14" cy="13" r="1.7" class="node"/>',
    'butterfly': '<path d="M3 5L10 8V18L3 21ZM21 5L14 8V18L21 21Z" class="panel"/>'
                 '<path d="M12 2V7M12 10V13M12 16V22" class="warning"/>'
                 '<path d="M6 8L10 11M18 8L14 11" class="line"/>',
    'bad_overlaps': '<rect x="2.5" y="3" width="11" height="11" rx="2" class="panel"/>'
                    '<rect x="9.5" y="10" width="11" height="11" rx="2" class="shade"/>'
                    '<path d="M17 3L22 8M22 3L17 8" class="warning"/>',
    'cleanup': '<rect x="3" y="3" width="11" height="11" rx="2" class="panel"/>'
               '<rect x="7" y="7" width="11" height="11" rx="2" class="shade"/>'
               '<path d="M14 18L17 21L22 15" class="success"/>',
    'done': '<rect x="3" y="3" width="18" height="18" rx="3" class="panel"/>'
            '<path d="M6 12L10 16L18 7" class="line"/>',
    'done_clear': '<rect x="3" y="3" width="18" height="18" rx="3" class="panel"/>'
                  '<path d="M8 12H16" class="line"/>',
    'scene_colors': '<circle cx="12" cy="12" r="9" class="panel"/>'
                    '<circle cx="8" cy="7" r="2" fill="#e89962" stroke="none"/>'
                    '<circle cx="16" cy="8" r="2" fill="#73bde9" stroke="none"/>'
                    '<circle cx="7" cy="14" r="2" fill="#74cc9d" stroke="none"/>'
                    '<circle cx="14" cy="16" r="2" fill="#d28cce" stroke="none"/>',
    'move_right': '<path d="M3 12H21M14 5L21 12L14 19" class="accent"/>',
    'flip_u': '<path d="M9 5L3 12L9 19ZM15 5L21 12L15 19Z" class="panel"/>'
              '<path d="M12 2V5M12 9V12M12 16V19M12 22V23" class="line"/>',
    'original_set': '<rect x="3" y="3" width="14" height="14" rx="2" class="panel"/>'
                    '<path d="M14 18L17 21L22 15" class="success"/>',
    'eye': '<path d="M2 12C7 4 17 4 22 12C17 20 7 20 2 12Z" class="panel"/>'
           '<circle cx="12" cy="12" r="3" class="line"/>',
    'eye_closed': '<path d="M2 12C7 4 17 4 22 12C17 20 7 20 2 12Z" class="line"/>'
                  '<path d="M3 3L21 21" class="line"/>',
    'analyse': '<circle cx="10" cy="10" r="6.5" class="panel"/>'
               '<path d="M15 15L21 21M6 10H14M10 6V14" class="line"/>',
    'window': '<rect x="3" y="3" width="18" height="18" rx="3" class="panel"/>'
              '<path d="M3 8H21M8 8V21" class="line"/>',
    'popup': '<rect x="3" y="7" width="14" height="14" rx="2" class="panel"/>'
             '<path d="M12 3H21V12M21 3L11 13" class="line"/>',
    'cylinder': '<ellipse cx="7" cy="6" rx="4" ry="2.5" class="panel"/>'
                '<path d="M3 6V18C3 22 11 22 11 18V6M3 18C3 15 11 15 11 18M15 5H21V19H15Z" class="accent"/>',
}
for action, degrees in (('move_down',90),('move_left',180),('move_up',270)):
    BODIES[action] = '<g transform="rotate({} 12 12)">{}</g>'.format(degrees,BODIES['move_right'])
BODIES['flip_v'] = '<g transform="rotate(90 12 12)">{}</g>'.format(BODIES['flip_u'])
BODIES['rotate_cw'] = '<path d="M17 5A8 8 0 1 0 20 13" class="accent"/><path d="M13 3L18 5L16 10" class="line"/>'
BODIES['rotate_ccw'] = '<g transform="translate(24 0) scale(-1 1)">{}</g>'.format(BODIES['rotate_cw'])
BODIES['straight_h'] = BODIES['H']
BODIES['straight_v'] = BODIES['V']
# Existing menu/preview code uses PATHS as the public icon-key registry.
PATHS = BODIES

ACCENTS = dict(mesh='#e4ad60',unfold='#73bde9',relax='#8cd1c1',pack='#deb577',
               rotate_cw='#73bde9',rotate_ccw='#73bde9',
               pack_other='#deb577',to_square='#deb577',to_rectangle='#deb577',
               H='#ff963b',V='#38c9ff',auto='#73bde9',
               unpin='#e18b9a',pick='#91b6ef',quad='#91b6ef',orient='#e4ad60',
               checker='#b8b8b8',checker_remove='#b8b8b8',checker_color='#8cd1c1',
               wireframe='#91b6ef',pins='#41c8ee',distortion='#cf91df',density_get='#37d6a3',
               density_set='#37d6a3',density_analyse='#37d6a3',clear='#b8b8b8',
               small_shells='#efbd55',zero_area_shells='#efbd55',hard_edges='#efbd55',overlaps='#91b6ef',
               butterfly='#efbd55',bad_overlaps='#efbd55',cleanup='#efbd55',
               done='#65cf89',done_clear='#df8d8f',scene_colors='#8cd1c1',
               analyse='#dba775',stack='#8cd1c1',window='#e4ad60',popup='#e4ad60',updates='#65cf89',
               straight_h='#ff963b',straight_v='#38c9ff',cylinder='#e4ad60')
_renderers, _pixmaps, _icons = {}, {}, {}


TASK_PATHS={
    'unfold':'<path d="M3 5L9 3L15 5L21 3V19L15 21L9 19L3 21ZM9 3V19M15 5V21"/>',
    'quad':'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 12H21M12 3V21"/>',
    'relax':'<path d="M2 6C5 2 7 10 10 6S15 10 18 6S21 7 22 5M2 12C5 8 7 16 10 12S15 16 18 12S21 13 22 11M2 18C5 14 7 22 10 18S15 22 18 18S21 19 22 17"/>',
    'orient':'<ellipse cx="12" cy="12" rx="10" ry="4"/><path d="M7 6C9 0 15 0 17 6M17 18C15 24 9 24 7 18M7 6L4 7L5 3M17 18L20 17L19 21M6 10C5 13 6 16 7 18M18 14C19 11 18 8 17 6"/>',
    'stack':'<path d="M12 3L22 8L12 13L2 8ZM2 12L12 17L22 12M2 16L12 21L22 16"/>',
    'transfer':'<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5A2 2 0 0 0 14 3H5A2 2 0 0 0 3 5V14A2 2 0 0 0 5 16H8"/>',
    'unpin':'<path d="M2 2L22 22M9 9L7 12L4 14H14M12 16V22M15 11L17 8V5H19V2H8M17 8L20 13V14H18"/>',
    'flip_u':'<path d="M12 3V5M12 9V11M12 15V17M12 21V22M3 7L8 12L3 17ZM21 7L16 12L21 17Z"/>',
    'flip_v':'<path d="M2 12H4M8 12H10M14 12H16M20 12H22M7 3L12 8L17 3ZM7 21L12 16L17 21Z"/>',
    'rotate_ccw':'<path d="M3 10A9 9 0 1 1 3 15M3 3V10H10"/>',
    'rotate_cw':'<path d="M21 10A9 9 0 1 0 21 15M21 3V10H14"/>',
    'pin':'<path d="M8 3H16L15 8L18 13V15H6V13L9 8ZM12 15V22"/>'}
for _name,_path in TASK_PATHS.items():
    _color='#de8090' if _name=='unpin' else '#e4ae60' if _name in ('orient','flip_u','flip_v','rotate_ccw','rotate_cw') else '#b9b9b9' if _name=='pin' else '#a6d7d5'
    BODIES['task_'+_name]='<g fill="none" stroke="'+_color+'" stroke-width="1.5">'+_path+'</g>'
BODIES['spin_up']='<path d="M4 16L12 8L20 16" stroke="#d0d0d0" stroke-width="2.4"/>'
BODIES['spin_down']='<path d="M4 8L12 16L20 8" stroke="#d0d0d0" stroke-width="2.4"/>'

BODIES.update({
    'material_eye': '<path d="M2 12C7 3 17 3 22 12C17 21 7 21 2 12Z" class="line"/><circle cx="12" cy="12" r="3" class="line"/>',
    'material_eye_off': '<path d="M2 12C7 3 17 3 22 12C17 21 7 21 2 12ZM3 3l18 18" class="line" opacity=".68"/><circle cx="12" cy="12" r="3" class="line" opacity=".68"/>',
    'material_convert': '<path d="m2 9 3-3 3 3M5 6v10a2 2 0 0 0 2 2h6m9-3-3 3-3-3m3 3V8a2 2 0 0 0-2-2h-6" class="line"/>',
    'material_folder': '<path d="M2 17V6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v2M4 20h14a2 2 0 0 0 1.9-1.4l2-6a2 2 0 0 0-1.9-2.6H7a2 2 0 0 0-1.9 1.4l-2.1 6A2 2 0 0 0 4 20Z" class="line"/>',
    'material_close': '<path d="m5 5 14 14M19 5 5 19" class="line"/>',
    'material_chevron_down': '<path d="m4 8 8 8 8-8" stroke="#f0f0f0" stroke-width="2.6"/>',
    'material_chevron_right': '<path d="m9 5 7 7-7 7" class="line"/>',
    'material_chevron_up': '<path d="m5 15 7-7 7 7" class="line"/>',
    'material_chevron': '<path d="m5 9 7 7 7-7" class="line"/>',
    'material_refresh': '<path d="M20 4v5h-5m5 0a9 9 0 1 0 1 6" stroke="#bdbdbd"/>',
    'material_pin': '<path d="M12 17v5m-5-20h10v3h-2v5l4 4v3H5v-3l4-4V5H7Z" stroke="#bdbdbd"/>',
    'material_pinned': '<path d="M12 17v5m-5-20h10v3h-2v5l4 4v3H5v-3l4-4V5H7Z" stroke="#e5edf3" fill="#e5edf3"/>',
})


def svg(action, dark=False, highlight=False):
    accent = '#202830' if dark else '#ffad66' if highlight else ACCENTS.get(action,'#dba775')
    line = '#202830' if dark else '#ffcf9d' if highlight else '#e5edf3'
    warning = '#202830' if dark else '#ef7180'
    success = '#202830' if dark else '#65cf89'
    body = BODIES[action]
    if dark:
        # Colored fills on the H/V buttons need a dark readable pictogram.
        for color in ('#ff963b','#38c9ff','#ffd36c'):
            body = body.replace(color,line)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
            '<defs><linearGradient id="panel" x1="0" y1="0" x2="1" y2="1">'
            '<stop stop-color="{accent}" stop-opacity=".55"/>'
            '<stop offset="1" stop-color="{accent}" stop-opacity=".14"/>'
            '</linearGradient></defs><style>'
            '.panel{{fill:url(#panel);stroke:{accent}}}'
            '.shade{{fill:{accent};fill-opacity:.28;stroke:{accent}}}'
            '.accent{{fill:none;stroke:{accent}}}.line{{fill:none;stroke:{line};stroke-width:1.4}}'
            '.refresh{{fill:none;stroke:{accent};stroke-width:2.5}}'
            '.node{{fill:{line};stroke:none}}.pin{{fill:#efbd55;stroke:{line};stroke-width:.7}}'
            '.spark{{fill:none;stroke:#ffd36c}}.warning{{fill:none;stroke:{warning};stroke-width:2}}'
            '.success{{fill:none;stroke:{success};stroke-width:2}}'
            '.box{{fill:#a47743;stroke:{accent}}}'
            '</style><g fill="none" stroke-width="1.7" stroke-linejoin="round" '
            'stroke-linecap="round">{body}</g></svg>').format(
                accent=accent,line=line,warning=warning,success=success,body=body)


def renderer(action, dark=False, highlight=False):
    key = (action,bool(dark),bool(highlight))
    if key not in _renderers:
        result = QSvgRenderer(QtCore.QByteArray(svg(*key).encode('utf8')))
        if not result.isValid():raise ValueError('Invalid ShellShifter vector icon: '+action)
        _renderers[key] = result
    return _renderers[key]


def paint_icon(painter, rect, action, dark=False, highlight=False):
    """Draw at the target's device resolution, including high-DPI screens."""
    painter.save()
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    raster=raster_image(action,dark)
    if raster is not None:
        # QPainter's smooth transform only samples a few source pixels. A
        # 544 -> 18px draw aliases thin lines despite the smooth flag. Prefilter
        # the full source footprint before drawing at the actual device size.
        target=QtCore.QRectF(rect);transform=painter.deviceTransform()
        width=max(1,math.ceil(target.width()*math.hypot(transform.m11(),transform.m12())))
        height=max(1,math.ceil(target.height()*math.hypot(transform.m21(),transform.m22())))
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        painter.drawImage(target,scaled_raster(action,bool(dark),width,height))
    else:renderer(action,dark,highlight).render(painter,QtCore.QRectF(rect))
    painter.restore()


def raster_image(action,dark=False):
    """Use the edited sheet verbatim, including its outline outside old bounds."""
    global _raster_atlas
    if action not in RASTER_KEYS:return None
    key=(action,bool(dark))
    if key not in _raster_images:
        if _raster_atlas is None:_raster_atlas=QtGui.QImage(str(RASTER_ATLAS))
        if _raster_atlas.isNull() or _raster_atlas.size()!=QtCore.QSize(2560,1920):return None
        index=RASTER_KEYS.index(action)
        # 640px cells, with a common 544px frame. The old 512px frame clips
        # the newly painted outline (e.g. above the Orient arrow).
        result=_raster_atlas.copy((index%4)*640+48,(index//4)*640+48,544,544)
        if dark:
            painter=QtGui.QPainter(result);painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
            painter.fillRect(result.rect(),QtGui.QColor('#202830'));painter.end()
        _raster_images[key]=result
    return _raster_images[key]


@lru_cache(maxsize=256)
def scaled_raster(action,dark,width,height):
    """Area-filter in premultiplied alpha, cached at physical pixel dimensions."""
    source=raster_image(action,dark).convertToFormat(QtGui.QImage.Format_ARGB32_Premultiplied)
    return source.scaled(width,height,QtCore.Qt.IgnoreAspectRatio,QtCore.Qt.SmoothTransformation)


def pixmap(action, dark=False, highlight=False, size=RASTER_SIZE):
    key = (action,bool(dark),bool(highlight),int(size))
    if key not in _pixmaps:
        result = QtGui.QPixmap(key[3],key[3]);result.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(result)
        paint_icon(painter,result.rect(),action,dark,highlight);painter.end()
        _pixmaps[key] = result
    return _pixmaps[key]


def icon(action, dark=False, highlight=False):
    key = (action,bool(dark),bool(highlight))
    if key not in _icons:
        result = QtGui.QIcon()
        for size in (18,22,24,32,36,44,48,64,96,192):result.addPixmap(pixmap(action,dark,highlight,size))
        _icons[key] = result
    return _icons[key]


def menu_image(action, active=False):
    if action not in BODIES:return None
    folder = Path(tempfile.gettempdir()) / ('uv_straight_action_icons_'+REVISION+'_native48')
    folder.mkdir(exist_ok=True)
    path = folder / (action + ('_active' if active else '') + '.png')
    if not path.exists() and not pixmap(action,highlight=active,size=48).save(str(path)):
        raise RuntimeError('Could not create menu icon: '+str(path))
    return str(path).replace('\\','/')


class ActionButton(QtWidgets.QPushButton):
    """Keep the shared pictogram at the left edge of an action button."""
    def __init__(self, text, action, dark=False):
        super().__init__(text)
        self.icon_action,self.icon_dark = action,bool(dark)
        self.centered_icon=False
        self.action_pixmap = pixmap(action,dark)
        self.setStyleSheet('QPushButton { padding-left: 34px; padding-right: 8px; }')

    def paintEvent(self, event):
        if self.centered_icon:
            option=QtWidgets.QStyleOptionButton();self.initStyleOption(option)
            option.text='';option.icon=QtGui.QIcon()
            painter=QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            background='#414141' if not self.isEnabled() else '#424242' if self.isDown() else '#606060' if self.underMouse() else '#505050'
            painter.setBrush(QtGui.QColor(background))
            painter.setPen(QtGui.QPen(QtGui.QColor('#8098b4'),1) if self.hasFocus() else QtCore.Qt.NoPen)
            painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),8,8)
            if not self.isEnabled():painter.setOpacity(.45)
            size=16;gap=6;metrics=QtGui.QFontMetrics(self.font())
            text=metrics.elidedText(self.text(),QtCore.Qt.ElideRight,max(0,self.width()-size-gap-12))
            width=metrics.horizontalAdvance(text);left=(self.width()-size-gap-width)*.5
            paint_icon(painter,QtCore.QRectF(left,(self.height()-size)*.5,size,size),self.icon_action,self.icon_dark)
            painter.setFont(self.font());painter.setPen(option.palette.buttonText().color())
            painter.drawText(QtCore.QRectF(left+size+gap,0,width,self.height()),QtCore.Qt.AlignVCenter|QtCore.Qt.AlignLeft,text)
            painter.end();return
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        if not self.isEnabled():painter.setOpacity(.45)
        size = min(PANEL_SIZE,self.height()-4)
        paint_icon(painter,QtCore.QRectF(7,(self.height()-size)*.5,size,size),
                   self.icon_action,self.icon_dark)
        painter.end()
