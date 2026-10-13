"""Cancellable background matching; only snapshot reads and final UV edits use Maya."""
import threading
import time
from PySide6 import QtCore
import uv_straight_stack as stack
from uv_straight_icons import ActionButton
from uv_straight_check_ui import ResultLabel


class Signals(QtCore.QObject):
    progress=QtCore.Signal(object,object)
    finished=QtCore.Signal(object)


class Ticket:
    def __init__(self,records,signals):
        self.records,self.signals=records,signals
        self.cancelled=threading.Event()
        self.plan,self.error=None,''


class MatchTask(QtCore.QRunnable):
    def __init__(self,ticket):
        super().__init__();self.ticket=ticket

    def run(self):
        ticket=self.ticket;steps=stack.analyse_steps(ticket.records);last=0.
        try:
            while not ticket.cancelled.is_set():
                progress=next(steps);now=time.perf_counter()
                if now-last>=.05:
                    ticket.signals.progress.emit(ticket,progress);last=now
        except StopIteration as done:
            ticket.plan=done.value
        except Exception as error:
            ticket.error=str(error)
        finally:
            steps.close();ticket.records=None
            try:ticket.signals.finished.emit(ticket)
            except RuntimeError:pass


class StackController(QtCore.QObject):
    def __init__(self,window,layout):
        super().__init__(window)
        self.window,self.prepared,self.ticket=window,None,None
        self.signals=Signals(self)
        self.signals.progress.connect(self.progress,QtCore.Qt.QueuedConnection)
        self.signals.finished.connect(self.finished,QtCore.Qt.QueuedConnection)
        self.button=ActionButton('Stack Similar Shells','stack')
        self.button.setObjectName('stackSimilarShells')
        self.button.setToolTip('Stack selected shells with matching surface geometry and topology.\n'
                               'The least distorted shell is the template; all corresponding UV points are aligned.\n'
                               'Only current UV sets change. Click again to stop matching.')
        self.button.setFixedHeight(28)
        self.button.clicked.connect(self.start_or_cancel)
        layout.addWidget(self.button)
        self.status=ResultLabel();self.status.setObjectName('stackShellsResult')
        layout.addWidget(self.status)
        window.installEventFilter(self)
        window.destroyed.connect(self.stop)

    def eventFilter(self,watched,event):
        if watched is self.window and event.type() in (QtCore.QEvent.Hide,QtCore.QEvent.Close,QtCore.QEvent.Destroy):
            if event.type()==QtCore.QEvent.Hide and getattr(self.window,'popup_flag_change',False):
                return False
            running=self.ticket is not None
            self.stop()
            if running and event.type()!=QtCore.QEvent.Destroy:
                self.status.setText('Stacking stopped when the window closed.')
        return False

    def stop(self,*unused):
        if self.ticket is not None:
            self.ticket.cancelled.set()
        self.ticket,self.prepared=None,None
        try:self.button.setText('Stack Similar Shells')
        except RuntimeError:pass

    def start_or_cancel(self):
        if self.ticket is not None:
            self.stop();self.status.setText('Stacking stopped. UVs were not changed.')
            return
        try:
            self.prepared=stack.prepare()
            self.ticket=Ticket(self.prepared['records'],self.signals)
            self.button.setText('Stop Stacking')
            self.status.setText('Matching {} selected shells…'.format(len(self.ticket.records)))
            QtCore.QThreadPool.globalInstance().start(MatchTask(self.ticket))
        except Exception as error:
            self.stop();self.status.setText(str(error))

    def progress(self,ticket,progress):
        if ticket is self.ticket:
            self.status.setText('Matching shells: {} / {}'.format(*progress))

    def finished(self,ticket):
        if ticket is not self.ticket or ticket.cancelled.is_set():
            return
        prepared=self.prepared
        self.ticket,self.prepared=None,None
        self.button.setText('Stack Similar Shells')
        try:
            if ticket.error:raise ValueError(ticket.error)
            if ticket.plan is None:raise ValueError('Stacking did not finish.')
            result=stack.apply(prepared,ticket.plan)
            if not result['groups']:
                text='No matching shells found in the selection.'
            else:
                text='Stacked {} shell{} in {} group{}.'.format(result['stacked'],'' if result['stacked']==1 else 's',
                       result['groups'],'' if result['groups']==1 else 's')
            if result['unmatched']:
                text+=' Unmatched: {}.'.format(result['unmatched'])
            self.status.setText(text)
        except Exception as error:
            self.status.setText(str(error))
        if hasattr(self.window,'adjust_popup_size'):
            QtCore.QTimer.singleShot(0,self.window.adjust_popup_size)
