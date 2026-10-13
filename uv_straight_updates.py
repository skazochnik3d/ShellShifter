"""Public GitHub releases, background checks and verified, reversible updates.

Network and archive work never touches Maya/Qt widgets. Files are replaced on
the GUI thread only after verification; the running modules stay loaded until
the artist restarts Maya. No scene edits, credentials or external dependencies.
"""
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from PySide6 import QtCore, QtGui, QtWidgets

REPOSITORY = 'skazochnik3d/ShellShifter'
REPOSITORY_URL = 'https://github.com/' + REPOSITORY
RELEASES_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases?per_page=20'
CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
_manager = globals().get('_manager')


def version_key(version):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)(?:[.]?(\d+))?)?', str(version))
    if not match:
        raise ValueError('Invalid ShellShifter version.')
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch),
            {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}[stage], int(number or 0))


def asset_name(version, maya):
    version_key(version)
    if str(maya) not in ('2025', '2027'):
        raise ValueError('Updates support Maya 2025 and 2027.')
    return 'ShellShifter_Maya{}_v{}_Install.zip'.format(maya, version.replace('-', '_'))


def _asset_url(asset, tag):
    url = asset.get('browser_download_url', '')
    expected = REPOSITORY_URL + '/releases/download/' + tag + '/' + asset.get('name', '')
    if url != expected or urllib.parse.urlparse(url).scheme != 'https':
        raise ValueError('Unexpected release download address.')
    return url


def choose_release(releases, current, maya):
    """Include beta releases; GitHub's /latest endpoint omits prereleases."""
    current_key = version_key(current)
    candidates = []
    if not isinstance(releases, list):
        raise ValueError('Unexpected GitHub response.')
    for release in releases:
        if release.get('draft'):
            continue
        tag = release.get('tag_name', '')
        try:
            key = version_key(tag)
        except ValueError:
            continue
        version = tag.removeprefix('v')
        name = asset_name(version, maya)
        assets = [a for a in release.get('assets', []) if a.get('name') == name]
        if key <= current_key or len(assets) != 1:
            continue
        asset = assets[0]
        size = asset.get('size', 0)
        if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
            continue
        url = _asset_url(asset, tag)
        manifest = next((a for a in release.get('assets', [])
                         if a.get('name') == 'ShellShifter_updates.json'), None)
        manifest_url = _asset_url(manifest, tag) if manifest else None
        candidates.append((key, dict(version=version, maya=str(maya), name=name,
                                     url=url, size=size, digest=asset.get('digest'),
                                     manifest_url=manifest_url,
                                     notes=str(release.get('body') or '')[:6000])))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        address = urllib.parse.urlparse(newurl)
        if address.scheme != 'https' or address.hostname not in (
                'github.com', 'api.github.com', 'release-assets.githubusercontent.com',
                'objects.githubusercontent.com'):
            raise ValueError('Unexpected download redirect.')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _read_url(url, limit):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'ShellShifter-updater', 'Accept': 'application/vnd.github+json'
        if url.startswith('https://api.github.com/') else 'application/octet-stream'})
    opener = urllib.request.build_opener(_HTTPSRedirect())
    with opener.open(request, timeout=10) as response:
        content = bytearray()
        deadline = time.monotonic() + 90
        while True:
            chunk = response.read(min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limit or time.monotonic() > deadline:
                raise ValueError('Download exceeds the size or time limit.')
    return bytes(content)


def fetch_release(current, maya):
    return choose_release(json.loads(_read_url(RELEASES_URL, 2 * 1024 * 1024)), current, maya)


def _runtime_path(name):
    return (bool(re.fullmatch(r'uv_straight(?:_[a-z0-9_]+)?\.py', name)) or
            name in ('shellshifter.py', 'install_shellshifter.py', 'install_uv_straight.py',
                     'assets/shellshifter_checker_color.png'))


def verify_archive(data, release, digest):
    if len(data) != release['size'] or len(data) > MAX_ARCHIVE:
        raise ValueError('Release archive size does not match.')
    if not re.fullmatch(r'[a-fA-F0-9]{64}', digest or ''):
        raise ValueError('Release SHA-256 is missing.')
    if hashlib.sha256(data).hexdigest() != digest.lower():
        raise ValueError('Release SHA-256 does not match.')
    prefix = 'ShellShifter_' + release['version'] + '/'
    files, expanded, seen = {}, 0, set()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 200:
            raise ValueError('Too many archive entries.')
        for info in archive.infolist():
            raw = info.filename
            if '\\' in raw or not raw.startswith(prefix):
                raise ValueError('Unsafe archive path.')
            name = raw[len(prefix):]
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or '..' in path.parts or
                    str(path) != name.rstrip('/') or ':' in name or
                    stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError('Unsafe archive entry.')
            if info.is_dir():
                continue
            if name.casefold() in seen:
                raise ValueError('Duplicate archive entry.')
            seen.add(name.casefold())
            if not (_runtime_path(name) or name in (
                    'package_manifest.json', 'README.md', 'INSTALL_RU.txt', 'INSTALL_EN.txt', 'LICENSE')):
                raise ValueError('Unexpected file in release: ' + name)
            expanded += info.file_size
            if expanded > MAX_EXPANDED or info.file_size > 8 * 1024 * 1024:
                raise ValueError('Archive expands beyond the size limit.')
            files[name] = archive.read(info)
    manifest = json.loads(files.pop('package_manifest.json'))
    if (manifest.get('product') != 'ShellShifter' or
            manifest.get('version') != release['version'] or
            str(manifest.get('maya')) != release['maya'] or
            set(manifest.get('files', {})) != set(files)):
        raise ValueError('Release manifest does not match the package.')
    for name, content in files.items():
        if hashlib.sha256(content).hexdigest() != manifest['files'][name]:
            raise ValueError('Package file checksum does not match: ' + name)
        if name.endswith('.py'):
            compile(content, name, 'exec')
    runtime = {name: content for name, content in files.items() if _runtime_path(name)}
    if not {'uv_straight.py', 'uv_straight_updates.py', 'uv_straight_preview.py',
            'shellshifter.py', 'install_shellshifter.py'} <= set(runtime):
        raise ValueError('Incomplete ShellShifter package.')
    for name, content in runtime.items():
        if not name.endswith('.py'):
            continue
        for node in ast.walk(ast.parse(content)):
            imports = ([part.name for part in node.names] if isinstance(node, ast.Import) else
                       [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for module in imports:
                if module.startswith('uv_straight') and module + '.py' not in runtime:
                    raise ValueError('Missing runtime dependency: ' + module)
    # The core version must agree with the advertised release before any writes.
    source = runtime['uv_straight.py'].decode('utf-8-sig')
    if not re.search(r"^VERSION\s*=\s*['\"]" + re.escape(release['version']) + r"['\"]", source, re.M):
        raise ValueError('Core version does not match the release.')
    return runtime


def download_release(release):
    digest = release.get('digest') or ''
    if digest.startswith('sha256:'):
        digest = digest[7:]
    else:
        if not release.get('manifest_url'):
            raise ValueError('This release does not publish archive checksums.')
        manifest = json.loads(_read_url(release['manifest_url'], 65536))
        if manifest.get('version') != release['version']:
            raise ValueError('Release checksum manifest version does not match.')
        entry = manifest.get('assets', {}).get(release['name'], {})
        if str(entry.get('maya')) != release['maya'] or entry.get('size') != release['size']:
            raise ValueError('Release checksum manifest does not match.')
        digest = entry.get('sha256')
    return verify_archive(_read_url(release['url'], MAX_ARCHIVE), release, digest)


def install_files(runtime, root, release):
    """Atomic file replacements plus rollback; never overwrite user preferences."""
    root = Path(root).resolve(strict=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    backup = Path(tempfile.mkdtemp(prefix='ShellShifter_backup_' + stamp + '_', dir=root))
    replacements = dict(runtime)
    replacements['ShellShifter_update_manifest.json'] = json.dumps(
        dict(version=release['version'], maya=release['maya'],
             files={name: hashlib.sha256(data).hexdigest() for name, data in runtime.items()}),
        indent=2).encode('utf8')
    targets, originals, committed = {}, {}, []
    try:
        for name, content in replacements.items():
            if name != 'ShellShifter_update_manifest.json' and not _runtime_path(name):
                raise ValueError('Unexpected installation path.')
            target = root/name
            if not target.resolve().is_relative_to(root) or target.is_symlink():
                raise ValueError('Installation path leaves the plugin folder.')
            targets[name] = target
            originals[name] = target.exists()
            if target.exists():
                saved = backup/name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            staged = backup/'new'/name
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
        (backup/'rollback.json').write_text(json.dumps(originals, indent=2), encoding='utf8')
        for name, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup/'new'/name, target)
            committed.append(name)
    except Exception as error:
        failures = []
        for name in reversed(committed):
            try:
                if originals[name]:
                    shutil.copy2(backup/name, targets[name])
                else:
                    targets[name].unlink()
            except OSError as rollback_error:
                failures.append(str(rollback_error))
        if failures:
            raise RuntimeError('Update failed; restore files from {}: {}'.format(backup, '; '.join(failures))) from error
        raise
    return backup


def _maya_parent():
    import maya.OpenMayaUI as omui
    from shiboken6 import wrapInstance
    pointer = omui.MQtUtil.mainWindow()
    return wrapInstance(int(pointer), QtWidgets.QWidget) if pointer else None


def _operations_running():
    progress = sys.modules.get('uv_straight_pack_progress')
    if progress is not None and progress.is_running():
        return True
    for widget in QtWidgets.QApplication.allWidgets():
        if widget.objectName() not in ('uvStraightQtWindow', 'uvStraightQtPopup'):
            continue
        for name in ('stack_shells', 'error_checks', 'uv_sets'):
            controller = getattr(widget, name, None)
            if controller and (getattr(controller, 'running', False) or
                               getattr(controller, 'ticket', None) is not None or
                               getattr(controller, 'steps', None) is not None):
                return True
    return False


class UpdateManager(QtCore.QObject):
    changed = QtCore.Signal()
    outcome = QtCore.Signal(object)

    def __init__(self):
        super().__init__(QtWidgets.QApplication.instance())
        import uv_straight as uv
        import maya.cmds as cmds
        self.current, self.maya = uv.VERSION, cmds.about(version=True)[:4]
        self.root = Path(uv.__file__).resolve().parent
        self.latest, self.dialog, self.staged = None, None, None
        self.busy = self.started = self.closed = self.pending_restart = False
        self.notified = set()
        self.status = 'Automatic check: on startup and every 4 hours.'
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(CHECK_INTERVAL_MS)
        self.timer.timeout.connect(self.check)
        self.outcome.connect(self._completed, QtCore.Qt.QueuedConnection)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.stop)
        metadata = self.root/'ShellShifter_update_manifest.json'
        try:
            installed = json.loads(metadata.read_text(encoding='utf8'))['version']
            self.pending_restart = version_key(installed) > version_key(self.current)
            if self.pending_restart:
                self.status = 'Update installed. Restart Maya to finish.'
        except (OSError, ValueError, KeyError):
            pass

    def start(self):
        import maya.cmds as cmds
        if self.started or cmds.about(batch=True):
            return
        self.started = True
        self.timer.start()
        QtCore.QTimer.singleShot(1500, self.check)

    def stop(self):
        self.closed = True
        self.timer.stop()

    def _work(self, kind, function, manual=False):
        if self.busy or self.closed:
            return False
        self.busy = True
        self.status = 'Checking for updates…' if kind == 'check' else 'Downloading and verifying…'
        self.changed.emit()
        def run():
            try:
                result = dict(kind=kind, manual=manual, value=function())
            except Exception as error:
                result = dict(kind=kind, manual=manual, error=str(error))
            if not self.closed:
                try:
                    self.outcome.emit(result)
                except RuntimeError:
                    pass  # Maya exited while a request was waiting for its timeout.
        threading.Thread(target=run, name='ShellShifter updates', daemon=True).start()
        return True

    def check(self, manual=False):
        if self.pending_restart:
            return
        if str(self.maya) not in ('2025', '2027'):
            self.status = 'Updates support Maya 2025 and 2027.'
            self.changed.emit()
            return
        current, maya = self.current, self.maya
        self._work('check', lambda: fetch_release(current, maya), manual)

    def manual_check(self):
        self.check(manual=True)

    @QtCore.Slot(object)
    def _completed(self, result):
        self.busy = False
        if self.closed:
            return
        if 'error' in result:
            self.status = 'Update unavailable: ' + result['error'][:220]
        elif result['kind'] == 'check':
            if self.latest != result['value']:
                self.staged = None
            self.latest = result['value']
            if self.latest:
                self.status = 'Version {} is available.'.format(self.latest['version'])
                if result['manual'] or self.latest['version'] not in self.notified:
                    self.notified.add(self.latest['version'])
                    self.show_dialog()
            else:
                self.status = 'You have the latest version: ' + self.current
        else:
            self.latest, self.staged = result['value']
            self._install_staged()
        self.changed.emit()

    def install(self):
        if self.busy or self.pending_restart or not self.latest:
            return
        if _operations_running():
            self.status = 'Finish the current UV operation, then try again.'
            self.changed.emit()
            return
        if self.staged is not None:
            self._install_staged()
            self.changed.emit()
            return
        release = dict(self.latest)
        self._work('download', lambda: (release, download_release(release)), manual=True)

    def _install_staged(self):
        if _operations_running():
            self.status = 'Download ready. Finish the current UV operation, then press Install.'
            return
        try:
            self.backup = install_files(self.staged, self.root, self.latest)
            self.pending_restart = True
            self.staged = None
            self.status = 'Update installed. Restart Maya to finish.'
        except Exception as error:
            self.status = 'Installation failed: ' + str(error)[:220]

    def show_dialog(self):
        from shiboken6 import isValid
        if self.dialog is not None and isValid(self.dialog):
            self.dialog.close()
        dialog = QtWidgets.QDialog(_maya_parent())
        self.dialog = dialog
        dialog.setObjectName('ShellShifterUpdateDialog')
        dialog.setWindowTitle('ShellShifter Update')
        dialog.setWindowFlags(dialog.windowFlags() | QtCore.Qt.Tool)
        dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        dialog.setMinimumWidth(390)
        dialog.setStyleSheet('QDialog {background:#303030;} QLabel {color:#dedede;} '
                            'QPushButton {background:#505050;color:#eeeeee;padding:8px;border-radius:8px;}')
        layout = QtWidgets.QVBoxLayout(dialog)
        label = QtWidgets.QLabel('ShellShifter {} → {}\nMaya {}'.format(
            self.current, self.latest['version'], self.maya))
        label.setTextFormat(QtCore.Qt.PlainText)
        layout.addWidget(label)
        notes = QtWidgets.QPlainTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(self.latest['notes'])
        notes.setMaximumHeight(170)
        layout.addWidget(notes)
        layout.addWidget(QtWidgets.QLabel('Restart Maya after installation. Your scene stays open.'))
        status = QtWidgets.QLabel(self.status)
        status.setTextFormat(QtCore.Qt.PlainText)
        status.setWordWrap(True)
        layout.addWidget(status)
        row = QtWidgets.QHBoxLayout()
        install = QtWidgets.QPushButton('Download & Install')
        install.clicked.connect(self.install)
        later = QtWidgets.QPushButton('Later')
        later.clicked.connect(dialog.close)
        row.addWidget(install)
        row.addWidget(later)
        layout.addLayout(row)
        def sync():
            if not isValid(dialog):
                return
            status.setText(self.status)
            install.setEnabled(not self.busy and not self.pending_restart)
            if self.staged is not None:
                install.setText('Install')
            if self.pending_restart:
                later.setText('Close')
        self.changed.connect(sync)
        def disconnected(*unused):
            try:
                self.changed.disconnect(sync)
            except (RuntimeError, TypeError):
                pass
        dialog.destroyed.connect(disconnected)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()


def manager():
    global _manager
    if _manager is None or _manager.closed:
        _manager = UpdateManager()
    return _manager


class UpdateController:
    def __init__(self, window, layout):
        from uv_straight_icons import ActionButton
        self.manager = manager()
        self.connected = True
        label = QtWidgets.QLabel('ShellShifter ' + self.manager.current)
        layout.addWidget(label)
        self.button = ActionButton('Check for Updates', 'updates')
        self.button.setObjectName('checkForUpdates')
        self.button.clicked.connect(self.manager.manual_check)
        layout.addWidget(self.button)
        window.shortcut_actions['check_updates'] = self.manager.manual_check
        window.destroyed.connect(self.stop)
        self.manager.changed.connect(self.sync)
        self.sync()
        self.manager.start()

    def sync(self):
        self.button.setToolTip(self.manager.status)
        self.button.setEnabled(not self.manager.busy and not self.manager.pending_restart)

    def stop(self, *unused):
        if self.connected:
            self.connected = False
            try:
                self.manager.changed.disconnect(self.sync)
            except (RuntimeError, TypeError):
                pass
