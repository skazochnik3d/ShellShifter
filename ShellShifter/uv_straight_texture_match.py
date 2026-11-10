"""Conservative filename matching for a batch of material textures (no Maya reads)."""
from collections import defaultdict
from pathlib import Path
import re

EXTENSIONS = {'.png', '.tif', '.tiff', '.tga', '.jpg', '.jpeg', '.bmp', '.exr',
              '.hdr', '.dds', '.tx', '.iff'}
MAP_ALIASES = {
    'normal': ('normal', 'normalmap', 'normalgl', 'normaldx', 'normalopengl',
               'normaldirectx', 'nm', 'n'),
    'base_color': ('basecolor', 'basecolour', 'albedo', 'diffuse', 'diff', 'color', 'colour', 'bc'),
    'ao': ('ambientocclusion', 'occlusion', 'ao'),
    'id_map': ('idmap', 'materialid', 'colorid', 'id'),
    'roughness': ('roughness', 'rough', 'rgh'),
    'glossiness': ('glossiness', 'gloss', 'smoothness'),
    'metallic': ('metallic', 'metalness', 'metal'),
    'specular': ('specular', 'spec'),
    'emissive': ('emissive', 'emission', 'emit'),
    'opacity': ('opacity', 'alpha', 'transparency'),
}
_ALIASES = {alias: key for key, aliases in MAP_ALIASES.items() for alias in aliases}
PACKED_CHANNELS = (('ao','r'), ('roughness','g'), ('metallic','b'))


def tokens(name):
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', str(name))
    name = re.sub(r'([a-z])([A-Z])', r'\1_\2', name)
    # Digits are separate tokens: Set1 and Set_1 agree; Set1 and Set10 do not.
    result = re.findall(r'[^\W\d_]+|\d+', name.casefold(), flags=re.UNICODE)
    # HDCab is an export prefix for Cab, used in the supplied WS_Bake example.
    result = ['cab' if part == 'hdcab' else part for part in result]
    return tuple(part for index, part in enumerate(result)
                 if not (part == 'hd' and index + 1 < len(result) and result[index + 1] == 'cab'))


def material_tokens(name):
    result = list(tokens(str(name).split(':')[-1]))
    while len(result) > 1 and result[-1] in ('mat', 'mtl', 'material', 'shader', 'set'):
        result.pop()
    if len(result) > 1 and result[0] in ('m', 'mat', 'mtl'):
        result.pop(0)
    return tuple(result)


def parse_name(path):
    """Map types must be explicit suffixes; unknown maps are left for the artist."""
    stem = Path(path).stem
    tile = re.search(r'[._-](1\d{3})$', stem)
    tile_number = int(tile.group(1)) if tile else None
    if tile:
        stem = stem[:tile.start()]
    tile_stem = stem
    while True:
        trimmed = re.sub(r'[._ -](?:srgb|linear|raw|directx|opengl|dx|gl|\d+k)$', '', stem, flags=re.I)
        if trimmed == stem:
            break
        stem = trimmed
    parts = list(tokens(stem))
    while parts and (parts[-1] in ('srgb', 'linear', 'raw', 'directx', 'opengl', 'dx', 'gl')
                     or (parts[-1] == 'k' and len(parts) > 1 and parts[-2].isdigit())):
        last = parts.pop()
        if last == 'k':
            parts.pop()
    if parts and parts[-1] in ('orm', 'aorm'):
        return dict(key=None, name=tuple(parts[:-1]), tile=tile_number,
                    tile_stem=tile_stem, packed=True)
    for count in range(min(3, len(parts)), 0, -1):
        alias = ''.join(parts[-count:])
        if alias in _ALIASES:
            return dict(key=_ALIASES[alias], name=tuple(parts[:-count]), tile=tile_number,
                        tile_stem=tile_stem)
    return dict(key=None, name=tuple(parts), tile=tile_number, tile_stem=tile_stem)


def collect_paths(paths, limit=1000):
    """Folders contribute their immediate image files, not alternate subfolders."""
    found = {}
    for value in paths:
        path = Path(value).expanduser()
        if not path.exists():
            raise ValueError('File or folder not found: ' + str(path))
        children = path.iterdir() if path.is_dir() else (path,)
        for child in children:
            if child.is_file() and child.suffix.lower() in EXTENSIONS:
                absolute = str(child.resolve())
                found[absolute.casefold()] = absolute
                if len(found) > limit:
                    raise ValueError('An import supports up to {} files.'.format(limit))
    if not found:
        raise ValueError('No supported images found. Folders are read without subfolders.')
    return sorted(found.values(), key=str.casefold)


def match_material(name, materials):
    """Choose the longest complete name suffix; equal names stay ambiguous."""
    scored = []
    for material in materials:
        wanted = material_tokens(material['name'])
        if wanted and len(name) >= len(wanted) and tuple(name[-len(wanted):]) == wanted:
            scored.append((len(wanted), material['uuid']))
    if not scored:
        return ()
    best = max(score for score, identity in scored)
    return tuple(identity for score, identity in scored if score == best)


def plan(paths, materials):
    """Group UDIM tiles; duplicates/formats retain separate, reviewable rows."""
    grouped = defaultdict(list)
    for path in collect_paths(paths):
        parsed = parse_name(path)
        file = Path(path)
        group = (str(file.parent).casefold(), parsed['tile_stem'].casefold(), file.suffix.lower()) if parsed['tile'] else (path,)
        grouped[group].append((path, parsed))
    rows = []
    for group in grouped.values():
        group.sort(key=lambda pair: pair[0].casefold())
        path, parsed = group[0]
        candidates = match_material(parsed['name'], materials)
        row = dict(path=path, paths=tuple(pair[0] for pair in group), key=parsed['key'],
                   material=candidates[0] if len(candidates) == 1 else None, candidates=candidates)
        if parsed.get('packed'):
            rows.extend(dict(row,key=key,channel=channel,packed=True) for key,channel in PACKED_CHANNELS)
        else:
            rows.append(row)
    return sorted(rows, key=lambda row: row['path'].casefold())
