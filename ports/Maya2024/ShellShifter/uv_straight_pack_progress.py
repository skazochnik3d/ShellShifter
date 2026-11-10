"""Responsive packing dialog: numerical search in a worker, Maya edits in GUI."""
import threading
import time
from PySide2 import QtCore, QtWidgets

_active = None


class PreparationCancelled(Exception):
    pass


def preparation_progress(phase, done=None, total=None):
    if _active is not None and _active.worker is None:
        _active.prepare_progress(phase, done, total)


class PackWorker(QtCore.QThread):
    progress = QtCore.Signal(dict)
    outcome = QtCore.Signal(object, object)

    def __init__(self, data, cancel, parent):
        super().__init__(parent)
        self.data, self.cancel = data, cancel
        self.finish_cancel = parent.finish_cancel

    def run(self):
        from uv_straight_pack import plan
        try:
            result = plan(self.data, self.progress.emit, self.cancel,self.finish_cancel)
            self.outcome.emit(result, None)
        except Exception as error:
            self.outcome.emit(None, error)


class PackingProgress(QtWidgets.QDialog):
    def __init__(self, prepare, commit, parent=None, refresh_transfer=None):
        super().__init__(parent)
        self.prepare, self.commit = prepare, commit
        self.refresh_transfer = refresh_transfer
        self.committing = False
        self.cancel = threading.Event()
        self.finish_cancel = threading.Event()
        self.finalizing = False
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
        self.free.setToolTip('A preliminary estimate is shown during the search. The final result uses the UV union area: overlapping regions count once. Padding is included in free space.')
        self.iterations = QtWidgets.QLabel('Iterations: 0')
        self.iterations.setToolTip('GPU: completed search cycles; one GPU batch counts once. CPU: completed independent trials. The two counters are shown separately.')
        self.layouts = QtWidgets.QLabel('Layouts: 0 · Improvements: 0')
        self.layouts.setToolTip('Total complete layouts from GPU and CPU. Improvements count changes to their shared best result, including accepted CPU improvements once.')
        stats = QtWidgets.QHBoxLayout()
        stats.addWidget(self.free,1); stats.addWidget(self.iterations)
        self.elapsed = QtWidgets.QLabel('Elapsed: 0.0 s')
        self.engine = QtWidgets.QLabel('Preparing…')
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        self.hint.hide()
        self.stop = QtWidgets.QPushButton('Stop')
        self.stop.setToolTip('Stop searching and apply the best complete layout. If Fill Gaps is enabled, a final pass redistributes small shells; that pass can be stopped separately.')
        self.stop.clicked.connect(self.request_stop)
        layout.addWidget(self.phase); layout.addWidget(self.bar)
        layout.addLayout(stats);layout.addWidget(self.layouts);layout.addWidget(self.elapsed);layout.addWidget(self.engine)
        layout.addWidget(self.hint); layout.addWidget(self.stop)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(lambda: self.elapsed.setText('Elapsed: {:.1f} s'.format(time.monotonic()-self.started)))
        from uv_straight_language import apply
        apply(self)
        self.timer.start()

    def start(self):
        if self.cancel.is_set():
            self.completed(dict(cancelled=True,shells=0),None)
            return
        # Show the dialog before synchronous Maya scene reads begin.
        self.repaint()
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)
        try:
            from uv_straight_native import uv_edit
            with uv_edit():
                self.prepared = self.prepare()
                if self.cancel.is_set():
                    raise PreparationCancelled()
            self.bar.setRange(0, 0)
            self.worker = PackWorker(self.prepared['data'],self.cancel,self)
            self.worker.progress.connect(self.update_progress)
            self.worker.outcome.connect(self.completed)
            self.worker.start()
        except PreparationCancelled:
            self.completed(dict(cancelled=True, shells=0), None)
        except Exception as error:
            self.error = error
            self.finish()

    def prepare_progress(self, phase, done=None, total=None):
        # Maya scene access stays on its main thread. Yield only between reads,
        # with ShellShifter overlay timers paused by uv_edit().
        now = time.monotonic()
        if now - getattr(self, 'last_prepare_paint', 0.) >= .05:
            self.last_prepare_paint = now
            self.phase.setText(phase)
            if total:
                self.bar.setRange(0, total)
                self.bar.setValue(done or 0)
            else:
                self.bar.setRange(0, 0)
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.AllEvents, 5)
        if self.cancel.is_set():
            raise PreparationCancelled()

    def update_statistics(self, state):
        limit=state.get('iteration_limit',0)
        engine=state.get('engine') or 'CPU'
        device='GPU' if engine.startswith('GPU') else 'CPU'
        iterations='Iterations · {}: {}{}'.format(
            device,state.get('iteration',state.get('iterations',0)),' / '+str(limit) if limit else '')
        if state.get('cpu_workers'):
            iterations+=' · CPU: {}'.format(state.get('cpu_iterations',0))
        self.iterations.setText(iterations)
        self.layouts.setText('Layouts: {} · Improvements: {}'.format(
            state.get('full_layouts',0)+state.get('cpu_layouts',0),state.get('improvements',0)))
        self.engine.setText(engine)

    @QtCore.Slot(dict)
    def update_progress(self, state):
        if state.get('postprocess'):
            self.finalizing=True
            self.phase.setText('Distributing small shells…')
            self.phase.show()
            self.stop.setText('Stop Distribution and Apply')
            self.stop.setToolTip('Stop redistribution and apply the complete layout with the moves already made.')
            self.stop.setEnabled(not self.finish_cancel.is_set())
            self.adjustSize()
            return
        if self.cancel.is_set(): return
        # Search uses a busy bar and the user-facing statistics below. Keep
        # technical candidate phases and per-batch details out of the palette.
        if not self.phase.isHidden():
            self.phase.hide()
            self.adjustSize()
        self.update_statistics(state)
        if state['free_percent'] is not None:
            self.free.setText('Free space: ≈ {:.1f}%'.format(state['free_percent']))
            self.stop.setText('Stop and Keep Best')
            self.hint.hide()
        else:
            self.free.setText('Free space: calculating…')
            self.stop.setText('Stop')
            self.hint.setText('No complete layout yet. Stopping now leaves UVs unchanged.')
            self.hint.show()

    def request_stop(self):
        if not self.stop.isEnabled(): return
        (self.finish_cancel if self.finalizing else self.cancel).set()
        self.stop.setEnabled(False)
        self.phase.setText('Finishing distribution…' if self.finalizing else 'Stopping search…')
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
                    self.committing = True
                    self.stop.setText('Applying…')
                    self.commit_progress('Preparing to apply layout…')
                    self.free.setText('Free space: ≈ {:.1f}%'.format(result['free_percent']))
                    if result.get('area_method')=='scanline_union':
                        self.free.setToolTip('Free area estimated from the UV union. Overlapping regions count once; padding is included in free space.')
                    else:
                        self.free.setToolTip('Preliminary estimate by UV groups. Fixed overlaps count once; overlaps within moving groups may affect the result.')
                    self.update_statistics(result)
                    applied=self.commit(self.prepared,result,self.apply_progress,self.source_progress)
                    if isinstance(applied,dict):self.result=applied
                    self.free.setText('Free space: ≈ {:.1f}%'.format(self.result['free_percent']))
                self.result['elapsed'] = time.monotonic()-self.started
                if self.refresh_transfer is not None:
                    self.committing = True
                    self.commit_progress('Refreshing Texture Transfer…')
                    QtCore.QTimer.singleShot(0, self.finish_after_transfer_refresh)
                    return
            except Exception as error:
                self.error = error
        self.finish()

    def finish_after_transfer_refresh(self):
        try:
            self.refresh_transfer()
        except Exception as error:
            self.error = error
        self.finish()

    def commit_progress(self, phase, done=0, total=0, hint=''):
        self.phase.setText(phase)
        self.phase.show()
        self.bar.setRange(0, total)
        if total:
            self.bar.setValue(done)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.elapsed.setText('Elapsed: {:.1f} s'.format(time.monotonic()-self.started))
        self.adjustSize()
        # Paint synchronously without dispatching Maya timers during scene writes.
        self.repaint()

    def source_progress(self, done, total):
        now = time.monotonic()
        if done != total and now-getattr(self, 'last_source_paint', 0.) < .1:
            return
        self.last_source_paint = now
        hint = 'Source meshes {} / {}'.format(done, total) if total else ''
        self.commit_progress('Saving texture-transfer source...', done, total, hint)

    def apply_progress(self, done, total):
        now = time.monotonic()
        if done == total:
            self.commit_progress('Finalizing packed UVs…')
        elif not done or now-getattr(self, 'last_apply_paint', 0.) >= .15:
            self.last_apply_paint = now
            self.commit_progress('Applying best layout...', done, total,
                                 'Applying {} / {} shells'.format(done, total))

    def finish(self):
        self.timer.stop()
        self.accept()

    def reject(self):
        if self.committing:
            return
        if self.worker is not None and self.worker.isRunning():
            self.request_stop()
        elif self.result is None and self.error is None:
            self.cancel.set()  # Closing before the preparation timer fires.
        else:
            super().reject()

    def closeEvent(self, event):
        if self.committing:
            event.ignore()
            return
        if ((self.worker is not None and self.worker.isRunning()) or
                (self.worker is None and self.result is None and self.error is None)):
            self.request_stop(); event.ignore()
        else:
            super().closeEvent(event)


def is_running():
    return _active is not None


def run(prepare, commit, parent=None, refresh_transfer=None):
    global _active
    if _active is not None:
        raise ValueError('A packing search is already running.')
    app = QtWidgets.QApplication.instance()
    if app is None:
        raise ValueError('Interactive packing requires a Maya UI session.')
    # A popup palette can destroy itself on focus loss; never parent a worker
    # dialog to that transient window. Application modality keeps it in front.
    dialog = PackingProgress(prepare,commit,parent,refresh_transfer)
    _active = dialog
    try:
        QtCore.QTimer.singleShot(0,dialog.start)
        dialog.exec_()
        if dialog.error is not None: raise dialog.error
        return dialog.result
    finally:
        if dialog.worker is not None and dialog.worker.isRunning():
            dialog.cancel.set(); dialog.worker.wait()
        _active = None
        dialog.deleteLater()
