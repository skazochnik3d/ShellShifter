"""Build Maya-specific install archives and checksums using the standard library."""
import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT/'ShellShifter'
DOCS = ROOT/'docs'


def build():
    core = ast.parse((RUNTIME/'uv_straight.py').read_bytes())
    version = next(ast.literal_eval(node.value) for node in core.body
                   if isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == 'VERSION' for target in node.targets))
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)(?:[.]?\d+)?)?', version):
        raise ValueError('Invalid release version')
    runtime = json.loads((ROOT/'tools/runtime_files.json').read_text())
    if len(runtime) != len(set(runtime)):
        raise ValueError('Duplicate runtime paths')
    package = {}
    inputs = {name: RUNTIME/name for name in runtime}
    inputs.update({'README.md': ROOT/'README.md',
                   'INSTALL_RU.txt': DOCS/'INSTALL_RU.txt', 'INSTALL_EN.txt': DOCS/'INSTALL_EN.txt'})
    for name, source in inputs.items():
        path = source.resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError('Missing/unsafe release path: ' + name)
        data = path.read_bytes()
        if name.endswith('.py'):
            compile(data, name, 'exec')
        if name == 'README.md':
            # Installed packages keep the existing flat layout expected by
            # Maya and previous updater versions; repository docs live separately.
            text = data.decode('utf8').split('## Development', 1)[0]
            text = text.replace('(docs/INSTALL_RU.txt)', '(INSTALL_RU.txt)').replace(
                '(docs/INSTALL_EN.txt)', '(INSTALL_EN.txt)')
            data = text.encode('utf8')
        package[name] = data
    folder = 'ShellShifter_' + version
    output = ROOT/'build'
    output.mkdir(exist_ok=True)
    releases = dict(version=version, assets={})
    for year in ('2025', '2027'):
        manifest = dict(product='ShellShifter', version=version, maya=year,
                        tested_platform='Windows / Autodesk Maya '+year,
                        entry_point='install_shellshifter.py',
                        files={name:hashlib.sha256(data).hexdigest() for name,data in sorted(package.items())})
        entries = dict(package, **{'package_manifest.json': (json.dumps(manifest, indent=2)+'\n').encode('utf8')})
        name = 'ShellShifter_Maya{}_v{}_Install.zip'.format(year, version.split('-', 1)[0])
        destination = output/name
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for entry, data in sorted(entries.items()):
                archive.writestr(folder+'/'+entry, data)
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise ValueError('Corrupt release archive')
        releases['assets'][name] = dict(maya=year, size=destination.stat().st_size,
                                       sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
    (output/'ShellShifter_updates.json').write_text(json.dumps(releases, indent=2)+'\n', encoding='utf8')
    return releases


if __name__ == '__main__':
    print(json.dumps(build(), indent=2))
