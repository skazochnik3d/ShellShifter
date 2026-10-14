"""Explicit bug reports: Qt owns images/widgets, a worker owns HTTPS only.

No credentials, scene data, file paths, automatic capture or background polling.
The public endpoint is configured by the maintainer after deploying the service.
"""
import base64
import json
from pathlib import Path
import platform
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid
import uv_straight_report_config as config

REPOSITORY = 'skazochnik3d/ShellShifter'
MAX_IMAGE = 2 * 1024 * 1024
MAX_INPUT = 8 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
MAX_SIDE = 1600
_manager = globals().get('_manager')


def configured_endpoint():
    try:
        value = config.ENDPOINT
        return validate_endpoint(value) if value else ''
    except (ValueError, TypeError, AttributeError):
        return ''


def validate_endpoint(value):
    # Owner-controlled configuration, never a URL supplied by a bug report.
    if not isinstance(value, str) or not re.fullmatch(
            r'https://shellshifter-reports\.[a-z0-9-]+\.workers\.dev/v1/reports', value):
        raise ValueError('Use the deployed ShellShifter HTTPS service address.')
    return value


def environment_info():
    import maya.cmds as cmds
    import uv_straight as uv
    return dict(shellshifter=uv.VERSION, maya=str(cmds.about(version=True)),
                maya_api=str(cmds.about(apiVersion=True)),
                os=platform.system() + ' ' + platform.release(),
                python=platform.python_version(), qt=QtCore.qVersion())


def prepare_image(image):
    """Re-encode pixels only; remove original filename, EXIF and text metadata."""
    if image.isNull() or image.width() * image.height() > MAX_PIXELS:
        raise ValueError('Choose an image of at most 16 megapixels.')
    if max(image.width(), image.height()) > MAX_SIDE:
        image = image.scaled(MAX_SIDE, MAX_SIDE, QtCore.Qt.KeepAspectRatio,
                             QtCore.Qt.SmoothTransformation)
    for attempt in range(5):
        # A new pixel image does not carry the source image's text/EXIF metadata.
        clean = QtGui.QImage(image.size(), QtGui.QImage.Format_RGBA8888)
        clean.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(clean)
        painter.drawImage(0, 0, image)
        painter.end()
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.WriteOnly)
        if not clean.save(buffer, 'PNG'):
            raise ValueError('Could not prepare the screenshot.')
        data = bytes(buffer.data())
        if len(data) <= MAX_IMAGE:
            return data, clean
        image = image.scaled(max(1, int(image.width() * .75)),
                             max(1, int(image.height() * .75)),
                             QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
    raise ValueError('Screenshot is too large; choose a smaller image.')


def read_image(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_INPUT:
        raise ValueError('Choose a PNG or JPEG file smaller than 8 MB.')
    reader = QtGui.QImageReader(str(path))
    reader.setDecideFormatFromContent(True)
    if bytes(reader.format()).lower() not in (b'png', b'jpeg', b'jpg'):
        raise ValueError('Only PNG and JPEG screenshots are supported.')
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > MAX_PIXELS:
        raise ValueError('Choose an image of at most 16 megapixels.')
    reader.setAutoTransform(True)
    return prepare_image(reader.read())


def make_payload(title, description, environment, image=None, request_id=None):
    title, description = title.strip(), description.strip()
    if not title or len(title) > 120:
        raise ValueError('Enter a title (up to 120 characters).')
    if not 10 <= len(description) <= 12000:
        raise ValueError('Describe the problem using 10 to 12,000 characters.')
    if image is not None and len(image) > MAX_IMAGE:
        raise ValueError('Screenshot exceeds 2 MB.')
    return dict(schema=1, request_id=request_id or str(uuid.uuid4()), title=title,
                description=description, environment=dict(environment),
                screenshot=base64.b64encode(image).decode('ascii') if image else None,
                consent_public=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


ERRORS = {
    'rate_limit': 'Too many reports. Please try again tomorrow.',
    'disabled': 'Bug reporting is temporarily unavailable.',
    'invalid_report': 'The report or screenshot was rejected. Check the fields.',
    'in_progress': 'This report is still being sent. Try again shortly.',
    'unconfirmed': 'Delivery could not be confirmed. Check GitHub Issues before sending a new report.',
    'upstream': 'GitHub could not accept the report. Please try again later.',
    'conflict': 'This report ID was already used. Reopen the window for a new report.',
}


def send_report(endpoint, payload):
    """Pure Python HTTPS; no Maya calls, widgets or automatic retries."""
    validate_endpoint(endpoint)
    data = json.dumps(payload, ensure_ascii=False).encode('utf8')
    if len(data) > 3 * 1024 * 1024:
        raise ValueError('The report is too large.')
    request = urllib.request.Request(endpoint, data=data, method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'User-Agent': 'ShellShifter-BugReport/1'})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=25)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, urllib.error.URLError):
        raise ValueError('Connection failed. Your report remains here; check GitHub before retrying.') from None
    with response:
        raw = response.read(16385)
        if len(raw) > 16384:
            raise ValueError('Unexpected response from the reporting service.')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError('Unexpected response from the reporting service.') from None
        if not isinstance(result, dict):
            raise ValueError('Unexpected response from the reporting service.')
        if response.status not in (200, 201):
            raise ValueError(ERRORS.get(result.get('error'), 'The reporting service is unavailable.'))
    url = result.get('issue_url', '')
    if not isinstance(url, str) or not re.fullmatch(
            r'https://github\.com/skazochnik3d/ShellShifter/issues/[1-9][0-9]*', url):
        raise ValueError('The service did not return a valid ShellShifter issue link.')
    return url


class _ClipboardFilter(QtCore.QObject):
    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress and event.matches(QtGui.QKeySequence.Paste):
            if QtWidgets.QApplication.clipboard().mimeData().hasImage():
                self.parent().paste_image()
                return True
        return False


class ReportDialog(QtWidgets.QDialog):
    def __init__(self, manager, parent):
        super().__init__(parent)
        self.manager = manager
        self.image_data = None
        self.request_id = str(uuid.uuid4())
        self.issue_url = ''
        self.environment = environment_info()
        self.setObjectName('ShellShifterReportDialog')
        self.setWindowTitle('ShellShifter — Report a Bug')
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)
        self.setMinimumWidth(430)
        self.setStyleSheet(
            'QDialog {background:#303030;} QLabel,QCheckBox {color:#dedede;}'
            'QLineEdit,QPlainTextEdit {background:#383838;color:#eee;border:1px solid #555;'
            'border-radius:6px;padding:6px;}'
            'QPushButton {background:#505050;color:#eee;padding:7px;border-radius:8px;}'
            'QPushButton:disabled {color:#888;} QPushButton:hover {background:#606060;}')
        layout = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLineEdit()
        self.title.setObjectName('reportTitle')
        self.title.setMaxLength(120)
        self.title.setPlaceholderText('Short title')
        layout.addWidget(self.title)
        self.description = QtWidgets.QPlainTextEdit()
        self.description.setObjectName('reportDescription')
        self.description.setPlaceholderText('What happened?\nHow can we reproduce it?\nWhat did you expect?')
        self.description.setMinimumHeight(150)
        layout.addWidget(self.description)
        self.clipboard_filter = _ClipboardFilter(self)
        for control in (self.title, self.description):
            control.installEventFilter(self.clipboard_filter)
            control.textChanged.connect(self.edited)
        row = QtWidgets.QHBoxLayout()
        self.attach = QtWidgets.QPushButton('Attach Screenshot')
        self.paste = QtWidgets.QPushButton('Paste Screenshot')
        self.remove = QtWidgets.QPushButton('Remove')
        self.attach.clicked.connect(self.choose_image)
        self.paste.clicked.connect(self.paste_image)
        self.remove.clicked.connect(self.remove_image)
        for control in (self.attach, self.paste, self.remove):
            row.addWidget(control)
        layout.addLayout(row)
        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMaximumHeight(170)
        layout.addWidget(self.preview)
        self.image_label = QtWidgets.QLabel('No screenshot attached')
        layout.addWidget(self.image_label)
        info = QtWidgets.QLabel('ShellShifter {shellshifter} · Maya {maya} · {os}'.format(**self.environment))
        info.setTextFormat(QtCore.Qt.PlainText)
        info.setWordWrap(True)
        info.setToolTip('\n'.join(k + ': ' + v for k, v in self.environment.items()))
        layout.addWidget(info)
        self.consent = QtWidgets.QCheckBox('Publish this description and screenshot on GitHub')
        self.consent.stateChanged.connect(self.sync)
        layout.addWidget(self.consent)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QtWidgets.QHBoxLayout()
        self.send = QtWidgets.QPushButton('Send')
        self.send.setObjectName('sendBugReport')
        self.send.clicked.connect(self.submit)
        self.open_issue = QtWidgets.QPushButton('Open Report')
        self.open_issue.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.issue_url)))
        close = QtWidgets.QPushButton('Close')
        close.clicked.connect(self.close)
        for control in (self.send, self.open_issue, close):
            row.addWidget(control)
        layout.addLayout(row)
        self.sync()

    def edited(self):
        if not self.manager.busy:
            self.request_id = str(uuid.uuid4())
            self.issue_url = ''
            self.status.clear()
            self.sync()

    def sync(self, *unused):
        busy = self.manager.busy
        for control in (self.title, self.description, self.attach, self.paste, self.consent):
            control.setEnabled(not busy)
        self.remove.setEnabled(not busy and self.image_data is not None)
        self.send.setEnabled(bool(self.manager.endpoint) and not busy and not self.issue_url
                             and self.consent.isChecked())
        self.send.setText('Sending…' if busy else 'Send')
        self.open_issue.setVisible(bool(self.issue_url))
        self.preview.setVisible(self.image_data is not None)
        if not self.manager.endpoint:
            self.status.setText('The maintainer has not connected the reporting service yet.')

    def set_image(self, prepared):
        data, image = prepared
        self.image_data = data
        self.preview.setPixmap(QtGui.QPixmap.fromImage(image).scaled(
            390, 160, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        self.image_label.setText('Screenshot · {} × {} · {:.0f} KB'.format(
            image.width(), image.height(), len(data) / 1024))
        self.edited()

    def choose_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Attach Screenshot', '', 'Screenshots (*.png *.jpg *.jpeg)')
        if path:
            try:
                self.set_image(read_image(path))
            except (ValueError, OSError):
                self.status.setText('Could not attach this image. Use PNG/JPEG, under 8 MB and 16 megapixels.')

    def paste_image(self):
        if self.manager.busy:
            return
        try:
            self.set_image(prepare_image(QtWidgets.QApplication.clipboard().image()))
        except ValueError as error:
            self.status.setText(str(error))

    def remove_image(self):
        self.image_data = None
        self.preview.clear()
        self.image_label.setText('No screenshot attached')
        self.edited()

    def submit(self):
        if self.manager.busy or not self.manager.endpoint or not self.consent.isChecked() or self.issue_url:
            return
        try:
            payload = make_payload(self.title.text(), self.description.toPlainText(),
                                   self.environment, self.image_data, self.request_id)
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.status.setText('Sending your report…')
        self.manager.submit(payload)
        self.sync()


class ReportManager(QtCore.QObject):
    outcome = QtCore.Signal(object)

    def __init__(self):
        super().__init__(QtWidgets.QApplication.instance())
        self.endpoint = configured_endpoint()
        self.busy = False
        self.dialog = None
        self.outcome.connect(self.completed, QtCore.Qt.QueuedConnection)

    def show(self):
        from uv_straight_updates import _maya_parent
        self.endpoint = configured_endpoint()
        if self.dialog is None or not isValid(self.dialog):
            self.dialog = ReportDialog(self, _maya_parent())
        elif self.dialog.issue_url and not self.busy:
            self.dialog.deleteLater()
            self.dialog = ReportDialog(self, _maya_parent())
        self.dialog.sync()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def submit(self, payload):
        if self.busy:
            return
        self.busy = True
        endpoint = self.endpoint
        def work():
            try:
                result = dict(url=send_report(endpoint, payload))
            except Exception as error:
                # No request/response bodies, filesystem paths or tokens in logs.
                result = dict(error=str(error) if isinstance(error, ValueError)
                              else 'The reporting service is unavailable.')
            try:
                self.outcome.emit(result)
            except RuntimeError:
                pass  # Maya exited while HTTPS was in progress.
        threading.Thread(target=work, name='ShellShifterReport', daemon=True).start()

    @QtCore.Slot(object)
    def completed(self, result):
        self.busy = False
        if self.dialog is not None and isValid(self.dialog):
            if 'url' in result:
                self.dialog.issue_url = result['url']
                self.dialog.status.setText('Report sent. Thank you!')
            else:
                self.dialog.status.setText(result['error'])
            self.dialog.sync()


def show_report():
    global _manager
    if _manager is None or not isValid(_manager):
        _manager = ReportManager()
    return _manager.show()
