"""Background UV overlap quality scan and selectable shell result rows."""
import time
import threading
from PySide6 import QtCore, QtWidgets
import maya.api.OpenMaya as om
import maya.cmds as cmds
import uv_straight_overlap_quality as checks
from uv_straight_icons import ActionButton


class ResultLabel(QtWidgets.QLabel):
    """Reserve space only when a check has progress, results or an error."""
    def __init__(self):
        super().__init__()
        self.setWordWrap(True)
        self.hide()

    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))


class ScanSignals(QtCore.QObject):
    progress = QtCore.Signal(object,object)
    finished = QtCore.Signal(object)


class ScanTicket:
    def __init__(self,snapshot,signals):
        self.snapshot,self.signals = snapshot,signals
        self.cancelled = threading.Event()
        self.report,self.error = None,''


class ScanTask(QtCore.QRunnable):
    def __init__(self,ticket):
        super().__init__()
        self.ticket = ticket

    def run(self):
        ticket = self.ticket
        steps = checks.analyse_steps(ticket.snapshot)
        last_emit = 0.
        try:
            while not ticket.cancelled.is_set():
                progress = next(steps)
                now = time.perf_counter()
                if now-last_emit >= .05:
                    ticket.signals.progress.emit(ticket,progress)
                    last_emit = now
        except StopIteration as done:
            ticket.report = done.value
        except Exception as error:
            ticket.error = str(error)
        finally:
            steps.close()
            ticket.snapshot = None
            try:
                ticket.signals.finished.emit(ticket)
            except RuntimeError:
                pass  # Its owning window was destroyed after cancellation.


class ErrorCheckController(QtCore.QObject):
    def __init__(self, window, layout):
        super().__init__(window)
        self.window = window
        self.steps = None
        self.phase = 'read'
        self.callbacks = []
        self.report = None
        self.ticket = None
        self.signals = ScanSignals(self)
        self.signals.progress.connect(self.worker_progress,QtCore.Qt.QueuedConnection)
        self.signals.finished.connect(self.worker_finished,QtCore.Qt.QueuedConnection)
        self.button = ActionButton('Check Overlaps','bad_overlaps')
        self.button.setObjectName('uvErrorCheckButton')
        self.button.setToolTip('Select overlapping UV shells that match by less than 99% of their combined UV area.\nMatching stacks and touching borders are excluded. Click again to stop.')
        self.button.clicked.connect(self.start_or_cancel)
        layout.addWidget(self.button)
        self.status = ResultLabel()
        layout.addWidget(self.status)
        self.results = QtWidgets.QListWidget()
        self.results.setObjectName('uvErrorResults')
        self.results.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.results.setStyleSheet(
            'QListWidget { background: #303030; border: 1px solid #505050; border-radius: 8px; }'
            'QListWidget::item { color: #ededed; padding: 6px; }'
            'QListWidget::item:selected { background: #486080; }')
        self.results.setToolTip('Click to select UV shells with invalid overlaps.\nCtrl / Shift selects multiple rows.')
        self.selection_timer = QtCore.QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.timeout.connect(self._select)
        self.results.itemSelectionChanged.connect(self.result_selection_changed)
        self.results.itemClicked.connect(self.result_clicked)
        self.results.hide()
        layout.addWidget(self.results)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        self.window = window
        window.installEventFilter(self)
        window.destroyed.connect(self.stop)

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() in (QtCore.QEvent.Hide,QtCore.QEvent.Close,QtCore.QEvent.Destroy):
            if event.type() == QtCore.QEvent.Hide and getattr(self.window, 'popup_flag_change', False):
                return False
            running = self.running
            self.stop()
            if running and event.type() != QtCore.QEvent.Destroy:
                self.status.setText('Check stopped when the window closed.')
        return False

    @property
    def running(self):
        return self.steps is not None or self.ticket is not None

    def _release_callbacks(self):
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.callbacks = []

    def stop(self, *unused):
        try:
            self.timer.stop()
            self.selection_timer.stop()
        except RuntimeError:
            pass
        if self.steps is not None:
            self.steps.close()
            self.steps = None
        if self.ticket is not None:
            self.ticket.cancelled.set()
            self.ticket = None
        self._release_callbacks()

    def scene_changed(self, *unused):
        self.stop()
        self.report = None
        with QtCore.QSignalBlocker(self.results):
            self.results.clear()
        self.results.hide()
        self.status.setText('Scene changed. Run the check again.')

    def start_or_cancel(self, *unused):
        if self.running:
            self.stop()
            self.status.setText('Check stopped. Previous results kept.')
            return
        self.steps = checks.read_steps()
        self.phase = 'read'
        self.callbacks = [om.MSceneMessage.addCallback(event,self.scene_changed)
                          for event in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen)]
        self.status.setStyleSheet('color: #dedede;')
        self.status.setText('Preparing check…')
        self.timer.start(0)

    def advance(self):
        deadline = time.perf_counter()+.008
        last = None
        try:
            while self.steps is not None and time.perf_counter() < deadline:
                last = next(self.steps)
        except StopIteration as done:
            self.steps = None
            if self.phase == 'read':
                self.ticket = ScanTicket(done.value,self.signals)
                self.status.setText('Checking overlaps…')
                QtCore.QThreadPool.globalInstance().start(ScanTask(self.ticket))
            else:
                self._release_callbacks()
                self.show_report(done.value)
            return
        except Exception as error:
            self.stop()
            self.status.setStyleSheet('color: #ef8181;')
            self.status.setText(str(error))
            cmds.warning(str(error))
            return
        if last is not None:
            self.status.setText('{}: {} / {}'.format(last.stage,last.done,last.total))
        if self.steps is not None:
            self.timer.start(1)

    def worker_progress(self,ticket,progress):
        if ticket is self.ticket and not ticket.cancelled.is_set():
            self.status.setText('{}: {} / {}'.format(progress.stage,progress.done,progress.total))

    def worker_finished(self,ticket):
        if ticket is not self.ticket:
            return
        self.ticket = None
        if ticket.cancelled.is_set():
            self._release_callbacks()
            return
        if ticket.error:
            self._release_callbacks()
            self.status.setStyleSheet('color: #ef8181;')
            self.status.setText(ticket.error)
            cmds.warning(ticket.error)
        elif ticket.report is not None:
            self.steps = checks.native_steps(ticket.report)
            self.phase = 'native'
            self.timer.start(0)

    def show_report(self, report):
        self.report = report
        self.selection_timer.stop()
        with QtCore.QSignalBlocker(self.results):
            self.results.clear()
            for issue in report.issues:
                parent = issue.shape.rsplit('|',1)[0] or issue.shape
                name = parent.rsplit('|',1)[-1]
                item = QtWidgets.QListWidgetItem('{} · faces: {}'.format(name,len(issue.faces)))
                item.setData(QtCore.Qt.UserRole,issue)
                item.setToolTip(issue.shape+'\nClick to select the problem UV shells.')
                self.results.addItem(item)
        self.results.setFixedHeight(min(6,max(1,len(report.issues)))*32+8)
        self.results.setVisible(bool(report.issues))
        text = 'Checked: {} · Errors: {}'.format(report.checked,len(report.issues))
        text += '\nMatching pairs: {}'.format(report.matching_pairs)
        if report.skipped:
            text += '\nSkipped: {}'.format(len(report.skipped))
        self.status.setText(text)
        self.status.setStyleSheet('color: {};'.format('#ef8181' if report.issues or report.skipped else '#70ce8b'))
        self.status.setToolTip('\n'.join('{}: {}'.format(*entry) for entry in report.skipped))
        try:
            checks.select_issues(report.issues)
        except (RuntimeError,ValueError) as error:
            self.status.setText(str(error))
            cmds.warning(str(error))
        if hasattr(self.window,'adjust_popup_size'):
            self.window.adjust_popup_size()

    def _select(self):
        issues = [item.data(QtCore.Qt.UserRole) for item in self.results.selectedItems()]
        if not issues:
            return
        try:
            checks.select_issues(issues)
        except (RuntimeError,ValueError) as error:
            self.status.setText(str(error))
            cmds.warning(str(error))

    def result_selection_changed(self):
        # Apply Maya selection after Qt finishes handling the mouse event.
        # SelectionChanged + ItemClicked share one queued update.
        self.selection_timer.start(0)

    def result_clicked(self, *unused):
        # A repeated click also restores the UV selection after external edits.
        self.selection_timer.start(0)
