"""One-click cooperative UV-set cleanup with bulk Undo."""
import time
import json
import traceback
from pathlib import Path
from PySide6 import QtCore, QtWidgets
import maya.api.OpenMaya as om
import maya.cmds as cmds
import uv_straight_uv_merge as sets
from uv_straight_uv_set_tags import KeepSetTags
from uv_straight_check_ui import ResultLabel
from uv_straight_icons import ActionButton


class UVSetController(QtCore.QObject):
    def __init__(self, window, layout, button_text='UV Set Cleanup'):
        super().__init__(window)
        self.window = window
        self.steps = None
        self.callbacks = []
        self.report = ()
        self.tags = KeepSetTags(window)
        self.tags.setToolTip('The first tag is the cleanup destination. Additional tags protect existing UV sets without changing their UVs. The distortion limit is fixed at 5%.')
        self.tags.changed.connect(self.settings_changed)
        self.button_text = button_text
        self.button = ActionButton(button_text, 'cleanup')
        self.button.setAccessibleName('UV Set Cleanup')
        self.button.setObjectName('cleanupExtraUVSets')
        self.button.setToolTip('Check selected meshes or groups, merge qualifying shells into the first Keep sets tag, and delete unprotected UV sets. The whole operation supports Undo.')
        self.button.clicked.connect(self.start_or_cancel)
        layout.addWidget(self.button)
        layout.addWidget(self.tags)
        self.status = ResultLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        window.installEventFilter(self)
        window.destroyed.connect(self.stop)
        self.settings_changed()

    def settings_changed(self, *unused):
        self.stop()
        self.report = ()
        if self.tags.names:
            self.status.setText('')
        else:
            self.status.setText('Add a UV set to keep. The first tag is the cleanup destination.')
        self.adjust_popup_size()

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() in (QtCore.QEvent.Hide, QtCore.QEvent.Close, QtCore.QEvent.Destroy):
            if event.type() == QtCore.QEvent.Hide and getattr(self.window, 'popup_flag_change', False):
                return False
            self.stop()
        return False

    def stop(self, *unused):
        try:
            self.timer.stop()
            self.button.setText(self.button_text)
            self.button.setEnabled(bool(self.tags.names))
            self.tags.setEnabled(True)
        except RuntimeError:
            pass
        if self.steps is not None:
            self.steps.close()
            self.steps = None
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.callbacks = []

    def scene_changed(self, *unused):
        self.stop()
        self.report = ()
        self.status.setText('Scene changed. Run UV Set Cleanup on selected meshes or groups.')

    def start_or_cancel(self):
        if self.steps is not None:
            self.stop()
            self.status.setText('UV Set Cleanup stopped. No UV sets were changed.')
            return
        if not self.tags.names:
            return
        self.report = ()
        self.status.setText('Checking UV sets...')
        self.steps = sets.analyse_steps(limit=sets.DEFAULT_LIMIT, keep=self.tags.names)
        self.callbacks = [om.MSceneMessage.addCallback(event, self.scene_changed)
                          for event in (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kBeforeOpen)]
        self.button.setText('Stop UV Set Cleanup')
        self.tags.setEnabled(False)
        self.timer.start(0)

    def advance(self):
        deadline = time.perf_counter() + .008
        try:
            while self.steps is not None and time.perf_counter() < deadline:
                done, total = next(self.steps)
                self.status.setText('Checking UV sets: {} / {}'.format(done, total))
        except StopIteration as finished:
            report = finished.value
            self.stop()
            self.promote(report)
            return
        except Exception as error:
            self.stop()
            self.status.setText(str(error))
            return
        self.timer.start(1)

    def promote(self, report):
        self.report = report
        chosen = [result for result in report if result.actionable]
        if not chosen:
            self.status.setText('No extra UV sets found on the selected meshes.' if not report else
                                'Checked {} meshes. No eligible UV sets to clean.'.format(len(report)))
            self.adjust_popup_size()
            return
        self.button.setText('Cleaning UV Sets...')
        self.button.setEnabled(False)
        self.tags.setEnabled(False)
        self.status.setText('Checked {} meshes. Cleaning {} meshes...'.format(len(report), len(chosen)))
        unmapped = sum(result.unmapped_faces for result in chosen)
        merged = sum(result.good_shells for result in chosen if result.actionable)
        created = sum(result.create_map1 for result in chosen if result.actionable)
        deleted = sum(len(result.delete_sets) for result in chosen if result.actionable)
        try:
            count = sets.promote(chosen, limit=sets.DEFAULT_LIMIT, keep=self.tags.names)
        except Exception as error:
            self.status.setText(str(error))
            report = dict(context=getattr(sets, '_last_error_context', {}),
                          error=str(error), traceback=traceback.format_exc(),
                          keep_sets=list(self.tags.names), limit=sets.DEFAULT_LIMIT,
                          meshes=[dict(shape=result.shape, uuid=result.uuid, sources=result.source,
                                       remove=list(result.delete_sets)) for result in chosen])
            try:
                Path(__file__).with_name('shellshifter_uv_cleanup_error.json').write_text(
                    json.dumps(report, indent=2), encoding='utf-8')
            except OSError:
                pass
            cmds.warning(str(error))
            return
        finally:
            self.stop()
            self.adjust_popup_size()
        self.report = ()
        message = 'Cleaned {} meshes · Created {}: {} · Merged shells: {} · Removed sets: {}. Undo is available.'.format(
            count, self.tags.names[0], created, merged, deleted)
        if unmapped:
            message += ' Unmapped faces: {}.'.format(unmapped)
        self.status.setText(message)
        print('ShellShifter: '+message)
        if not cmds.about(batch=True):
            try:
                cmds.inViewMessage(amg='ShellShifter: cleaned {} meshes, removed {} extra UV sets.'.format(count, deleted),
                                   pos='topCenter', fade=True)
            except RuntimeError:
                pass
        self.adjust_popup_size()

    def adjust_popup_size(self):
        if hasattr(self.window, 'adjust_popup_size'):
            self.window.adjust_popup_size()
