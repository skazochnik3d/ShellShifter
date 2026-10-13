"""Responsive packing dialog: numerical search in a worker, Maya edits in GUI."""
import threading
import time
from PySide6 import QtCore, QtWidgets

_active = None


class PackWorker(QtCore.QThread):
    progress = QtCore.Signal(dict)
    outcome = QtCore.Signal(object, object)

    def __init__(self, data, cancel, parent):
        super().__init__(parent)
        self.data, self.cancel = data, cancel

    def run(self):
        from uv_straight_pack import plan
        try:
            result = plan(self.data, self.progress.emit, self.cancel)
            self.outcome.emit(result, None)
        except Exception as error:
            self.outcome.emit(None, error)


class PackingProgress(QtWidgets.QDialog):
    def __init__(self, prepare, commit, parent=None):
        super().__init__(parent)
        self.prepare, self.commit = prepare, commit
        self.cancel = threading.Event()
        self.worker = None
        self.result, self.error = None, None
        self.started = time.monotonic()
        self.setWindowTitle('Packing UVs')
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        self.setMinimumWidth(380)
        self.setStyleSheet('QDialog { background: #343434; color: #ededed; }'
                          'QLabel { color: #ededed; }'
                          'QPushButton { background: #565656; color: #ededed; border: 0;'
                          'border-radius: 12px; padding: 7px 14px; }'
                          'QPushButton:hover { background: #657681; }'
                          'QProgressBar { background: #262626; border: 0; border-radius: 4px;'
                          'text-align: center; color: white; min-height: 16px; }'
                          'QProgressBar::chunk { background: #5389a5; border-radius: 4px; }')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)
        self.phase = QtWidgets.QLabel('Reading UV shells…')
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0,0)
        self.free = QtWidgets.QLabel('Free space: —')
        self.free.setToolTip('Estimate from UV triangle area. Existing overlapping faces can affect this estimate.')
        self.iterations = QtWidgets.QLabel('Iterations: 0')
        self.iterations.setToolTip('Completed search cycles. One GPU batch counts as one iteration.')
        self.layouts = QtWidgets.QLabel('Layouts: 0 · Improvements: 0')
        stats = QtWidgets.QHBoxLayout()
        stats.addWidget(self.free,1); stats.addWidget(self.iterations)
        self.elapsed = QtWidgets.QLabel('Elapsed: 0.0 s')
        self.engine = QtWidgets.QLabel('Preparing…')
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        self.hint.hide()
        self.stop = QtWidgets.QPushButton('Stop and Keep Best')
        self.stop.setToolTip('Stop the search and apply the best complete layout found so far.')
        self.stop.clicked.connect(self.request_stop)
        layout.addWidget(self.phase); layout.addWidget(self.bar)
        layout.addLayout(stats);layout.addWidget(self.layouts);layout.addWidget(self.elapsed);layout.addWidget(self.engine)
        layout.addWidget(self.hint); layout.addWidget(self.stop)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(lambda: self.elapsed.setText('Elapsed: {:.1f} s'.format(time.monotonic()-self.started)))
        self.timer.start()

    def start(self):
        try:
            self.prepared = self.prepare()
            self.worker = PackWorker(self.prepared['data'],self.cancel,self)
            self.worker.progress.connect(self.update_progress)
            self.worker.outcome.connect(self.completed)
            self.worker.start()
        except Exception as error:
            self.error = error
            self.finish()

    @QtCore.Slot(dict)
    def update_progress(self, state):
        if self.cancel.is_set(): return
        # Search uses a busy bar and the user-facing statistics below. Keep
        # technical candidate phases and per-batch details out of the palette.
        if not self.phase.isHidden():
            self.phase.hide()
            self.adjustSize()
        self.iterations.setText('Iterations: {}'.format(state['iteration']))
        self.layouts.setText('Layouts: {} · Improvements: {}'.format(state.get('full_layouts',0),state.get('improvements',0)))
        if state.get('engine'):self.engine.setText(state['engine'])
        if state['free_percent'] is not None:
            self.free.setText('Free space: ≈ {:.1f}%'.format(state['free_percent']))

    def request_stop(self):
        if not self.stop.isEnabled(): return
        self.cancel.set()
        self.stop.setEnabled(False)
        self.phase.setText('Stopping search…')
        self.phase.show()
        self.adjustSize()

    @QtCore.Slot(object, object)
    def completed(self, result, error):
        # The worker has no scene access. Its result is applied on this thread.
        self.stop.setEnabled(False)
        if self.worker: self.worker.wait()
        self.error = error
        if error is None:
            try:
                self.result = result
                if not result.get('cancelled'):
                    self.phase.setText('Applying best layout…')
                    self.phase.show()
                    self.adjustSize()
                    self.stop.setEnabled(False)
                    self.stop.setText('Applying…')
                    self.bar.setRange(0,result['shells'])
                    self.free.setText('Free space: ≈ {:.1f}%'.format(result['free_percent']))
                    self.iterations.setText('Iterations: {}'.format(result['iterations']))
                    self.layouts.setText('Layouts: {} · Improvements: {}'.format(result.get('full_layouts',0),result.get('improvements',0)))
                    self.commit(self.prepared,result,self.apply_progress)
                self.result['elapsed'] = time.monotonic()-self.started
            except Exception as error:
                self.error = error
        self.finish()

    def apply_progress(self, done, total):
        self.bar.setValue(done)
        self.hint.setText('Applying {} / {} shells'.format(done,total))
        if self.hint.isHidden():
            self.hint.show()
            self.adjustSize()
        # Paint/timers remain responsive while applying one atomic undo chunk;
        # user input is excluded so scene commands cannot interrupt the commit.
        now=time.monotonic()
        if done==total or now-getattr(self,'last_apply_paint',0.)>=.06:
            self.last_apply_paint=now
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)

    def finish(self):
        self.timer.stop()
        self.accept()

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            self.request_stop()
        elif self.result is None and self.error is None:
            self.cancel.set()  # Closing before the preparation timer fires.
        else:
            super().reject()

    def closeEvent(self, event):
        if ((self.worker is not None and self.worker.isRunning()) or
                (self.worker is None and self.result is None and self.error is None)):
            self.request_stop(); event.ignore()
        else:
            super().closeEvent(event)


def is_running():
    return _active is not None


def run(prepare, commit, parent=None):
    global _active
    if _active is not None:
        raise ValueError('A packing search is already running.')
    app = QtWidgets.QApplication.instance()
    if app is None:
        raise ValueError('Interactive packing requires a Maya UI session.')
    # A popup palette can destroy itself on focus loss; never parent a worker
    # dialog to that transient window. Application modality keeps it in front.
    dialog = PackingProgress(prepare,commit,parent)
    _active = dialog
    try:
        QtCore.QTimer.singleShot(0,dialog.start)
        dialog.exec()
        if dialog.error is not None: raise dialog.error
        return dialog.result
    finally:
        if dialog.worker is not None and dialog.worker.isRunning():
            dialog.cancel.set(); dialog.worker.wait()
        _active = None
        dialog.deleteLater()
