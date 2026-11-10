"""Background UV overlap quality scan and selectable shell result rows."""
import time
import threading
import math
from dataclasses import replace
from types import SimpleNamespace
from PySide2 import QtCore, QtWidgets
from uv_straight_qt5 import signals_blocked
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


class HardEdgeNavigator(QtWidgets.QWidget):
    """Step through one scan result at a time in the 3D viewport."""
    def __init__(self):
        super().__init__()
        self.setObjectName('hardEdgeNavigation')
        self.edges = []
        self.index = -1
        self.status = QtWidgets.QLabel(alignment=QtCore.Qt.AlignCenter)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.setStyleSheet(
            'QToolButton { background:#525252; color:#eeeeee; border:0; border-radius:8px; }'
            'QToolButton:hover { background:#626262; }'
            'QToolButton:pressed { background:#424242; }'
            'QToolButton:disabled { background:#414141; color:#888888; }')
        self.arrows = []
        for direction, arrow, hint in (
                (-1, QtCore.Qt.LeftArrow, 'Focus the previous hard edge without a UV cut.'),
                (1, QtCore.Qt.RightArrow, 'Focus the next hard edge without a UV cut.')):
            button = QtWidgets.QToolButton()
            button.setFixedSize(28, 26)
            button.setArrowType(arrow)
            button.setAccessibleName(hint)
            button.setToolTip(hint)
            button.clicked.connect(lambda checked=False, step=direction: self.step(step))
            self.arrows.append(button)
        row.addWidget(self.arrows[0])
        row.addWidget(self.status, 1)
        row.addWidget(self.arrows[1])
        self.hide()

    def clear(self):
        self.edges = []
        self.index = -1
        self.hide()

    def load(self, issues):
        self.clear()
        meshes = {}
        for issue in issues:
            mesh = issue.mesh
            if mesh.name not in meshes:
                meshes[mesh.name] = (mesh.fn.getPath(), mesh.fn.numEdges)
            self.edges.append((*meshes[mesh.name], issue.edge_id))
        self.step(1)

    def step(self, direction):
        while self.edges:
            index = (self.index + direction) % len(self.edges)
            path, edge_count, edge = self.edges[index]
            if not path.isValid() or not path.isVisible() or om.MFnMesh(path).numEdges != edge_count:
                del self.edges[index]
                self.index = index - 1 if direction > 0 else index
                continue
            shape = path.fullPathName()
            component = '{}.e[{}]'.format(shape, edge)
            cmds.selectMode(component=True)
            cmds.selectType(allComponents=False)
            cmds.selectType(polymeshEdge=True)
            cmds.hilite(shape, replace=True)
            cmds.select(component, replace=True)
            self.index = index
            self.status.setText('Edge {} / {}'.format(index + 1, len(self.edges)))
            for arrow in self.arrows:
                arrow.setEnabled(len(self.edges) > 1)
            self.show()
            panel = cmds.getPanel(withFocus=True)
            if not panel or cmds.getPanel(typeOf=panel) != 'modelPanel':
                panel = next((p for p in cmds.getPanel(visiblePanels=True) or []
                              if cmds.getPanel(typeOf=p) == 'modelPanel'), None)
            if panel:
                cmds.viewFit(cmds.modelPanel(panel, query=True, camera=True),
                             animate=False, fitFactor=.6)
            return
        self.clear()


class ScanSignals(QtCore.QObject):
    progress = QtCore.Signal(object,object)
    finished = QtCore.Signal(object)


class ScanTicket:
    def __init__(self,snapshot,signals,focus=None,revision=0):
        self.snapshot,self.signals = snapshot,signals
        self.focus,self.revision = focus,revision
        self.cancelled = threading.Event()
        self.report,self.error = None,''


class ScanTask(QtCore.QRunnable):
    def __init__(self,ticket):
        super().__init__()
        self.ticket = ticket

    def run(self):
        ticket = self.ticket
        steps = checks.analyse_steps(ticket.snapshot,focus=ticket.focus)
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
        self.result_uv_sets = {}
        self.active_findings = ()
        self.change_tracker = None
        self.review_revision = 0
        self.validated_findings = {}
        self.recheck_target = None
        self.pending_transition = None
        self.ticket = None
        self.signals = ScanSignals(self)
        self.signals.progress.connect(self.worker_progress,QtCore.Qt.QueuedConnection)
        self.signals.finished.connect(self.worker_finished,QtCore.Qt.QueuedConnection)
        self.button = ActionButton('Bad Overlaps','bad_overlaps')
        self.button.setObjectName('uvErrorCheckButton')
        self.button.setToolTip(
            f'Find folds within shells and overlapping shells with less than {checks.MATCH_THRESHOLD:.0%} '
            'agreement of their combined UV area.\nMatching stacks and touching borders are ignored. '
            'Click again to stop.')
        self.button.clicked.connect(self.start_or_cancel)
        layout.addWidget(self.button)

        self.analysis_toggle = QtWidgets.QToolButton()
        self.analysis_toggle.setObjectName('uvErrorAnalysisToggle')
        self.analysis_toggle.setText('Overlap Results')
        self.analysis_toggle.setToolTip('Show or hide the overlap results. The report and selection are kept.')
        self.analysis_toggle.setCheckable(True)
        self.analysis_toggle.setChecked(True)
        self.analysis_toggle.setArrowType(QtCore.Qt.DownArrow)
        self.analysis_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.analysis_toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.analysis_toggle.setFixedHeight(22)
        self.analysis_toggle.setStyleSheet(
            'QToolButton { color: #bdbdbd; background: transparent; border: 0; '
            'border-radius: 5px; padding: 0 4px; text-align: left; }'
            'QToolButton:hover { background: #454545; }')
        self.analysis_toggle.hide()
        layout.addWidget(self.analysis_toggle)
        self.analysis = QtWidgets.QWidget()
        self.analysis.setObjectName('uvErrorAnalysis')
        analysis_layout = QtWidgets.QVBoxLayout(self.analysis)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        analysis_layout.setSpacing(4)
        self.analysis.hide()
        layout.addWidget(self.analysis)
        self.analysis_toggle.toggled.connect(self._set_analysis_expanded)

        self.status = ResultLabel()
        analysis_layout.addWidget(self.status)
        self.results = QtWidgets.QListWidget()
        self.results.setObjectName('uvErrorResults')
        self.results.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.results.setStyleSheet(
            'QListWidget { background: #303030; border: 1px solid #505050; border-radius: 8px; }'
            'QListWidget::item { color: #ededed; padding: 6px; }'
            'QListWidget::item:selected { background: #486080; }')
        self.results.setToolTip('Click a result to select and frame its UV shells.\nCtrl / Shift selects multiple groups.')
        self.selection_timer = QtCore.QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.timeout.connect(self._select)
        self.results.itemSelectionChanged.connect(self.result_selection_changed)
        self.results.itemClicked.connect(self.result_clicked)
        self.results.hide()
        analysis_layout.addWidget(self.results)
        self.detail = QtWidgets.QLabel()
        self.detail.setWordWrap(True)
        self.detail.hide()
        analysis_layout.addWidget(self.detail)
        self.navigation = QtWidgets.QWidget()
        self.navigation.setObjectName('uvErrorNavigation')
        self.navigation.setFixedHeight(26)
        self.navigation.setStyleSheet(
            'QToolButton { background: #525252; color: #eeeeee; border: 0; border-radius: 8px; }'
            'QToolButton:hover { background: #626262; }'
            'QToolButton:pressed { background: #424242; }')
        navigation_row = QtWidgets.QHBoxLayout(self.navigation)
        navigation_row.setContentsMargins(0,0,0,0)
        navigation_row.setSpacing(6)
        self.previous_shell = QtWidgets.QToolButton()
        self.previous_shell.setObjectName('uvErrorPreviousShell')
        self.previous_shell.setArrowType(QtCore.Qt.LeftArrow)
        self.previous_shell.setAccessibleName('Previous conflicting pair')
        self.next_shell = QtWidgets.QToolButton()
        self.next_shell.setObjectName('uvErrorNextShell')
        self.next_shell.setArrowType(QtCore.Qt.RightArrow)
        self.next_shell.setAccessibleName('Next conflicting pair')
        self.shell_status = QtWidgets.QLabel()
        self.shell_status.setAlignment(QtCore.Qt.AlignCenter)
        navigation_row.addWidget(self.previous_shell)
        navigation_row.addWidget(self.shell_status,1)
        navigation_row.addWidget(self.next_shell)
        for button,direction in ((self.previous_shell,-1),(self.next_shell,1)):
            button.setFixedSize(28,26)
            button.setToolTip(('Previous' if direction<0 else 'Next') + ' conflicting pair in this group.')
            button.clicked.connect(lambda checked=False,direction=direction:self.navigate_shell(direction))
        self.navigation.hide()
        analysis_layout.addWidget(self.navigation)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        self.window = window
        window.installEventFilter(self)
        window.destroyed.connect(self.stop)

    def _set_analysis_expanded(self, expanded):
        self.analysis_toggle.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.analysis.setVisible(expanded)
        adjust = getattr(self.window, 'adjust_popup_size', None)
        if adjust is not None:
            QtCore.QTimer.singleShot(0, adjust)

    def _show_analysis(self):
        self.analysis_toggle.show()
        self._set_analysis_expanded(self.analysis_toggle.isChecked())

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
            self.navigation.setEnabled(True)
        except RuntimeError:
            pass
        if self.steps is not None:
            self.steps.close()
            self.steps = None
        if self.ticket is not None:
            self.ticket.cancelled.set()
            self.ticket = None
        self._release_callbacks()
        if self.change_tracker is not None:
            self.change_tracker.stop()
            self.change_tracker = None
        self.recheck_target = None
        self.pending_transition = None

    def scene_changed(self, *unused):
        self.stop()
        self.report = None
        self.result_uv_sets.clear()
        self.active_findings = ()
        self.validated_findings.clear()
        with signals_blocked(self.results):
            self.results.clear()
        self.results.hide()
        self.navigation.hide()
        self.detail.hide()
        self.status.setText('Scene changed. Run the check again.')

    def start_or_cancel(self, *unused):
        if self.running:
            self.stop()
            self.status.setText('Check stopped. Previous results kept.')
            return
        self.recheck_target = None
        self.pending_transition = None
        self.steps = checks.read_steps()
        self.phase = 'read'
        self.callbacks = [om.MSceneMessage.addCallback(event,self.scene_changed)
                          for event in (om.MSceneMessage.kBeforeNew,om.MSceneMessage.kBeforeOpen)]
        self.status.setStyleSheet('color: #dedede;')
        self.status.setText('Preparing check…')
        self._show_analysis()
        self.navigation.setEnabled(False)
        self.timer.start(0)

    def advance(self):
        deadline = time.perf_counter()+.008
        last = None
        try:
            while self.steps is not None and time.perf_counter() < deadline:
                last = next(self.steps)
        except StopIteration as done:
            self.steps = None
            if self.phase in ('read','recheck_read'):
                self.ticket = ScanTicket(done.value,self.signals,self.recheck_target,
                                         self.review_revision)
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
        if ticket.focus is not None:
            self.finish_recheck(ticket)
            return
        if ticket.cancelled.is_set():
            self._release_callbacks()
            return
        if ticket.error:
            self.navigation.setEnabled(True)
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
        self.result_uv_sets.clear()
        self.active_findings = ()
        self.review_revision = 0
        self.validated_findings.clear()
        self.recheck_target = None
        self.pending_transition = None
        self.selection_timer.stop()
        self.detail.hide()
        self._watch_results()
        self.populate_results()
        try:
            self.resume_selection((), 0)
        except (RuntimeError, ValueError) as error:
            self.status.setText(str(error))
            cmds.warning(str(error))

    @staticmethod
    def _result_groups(findings):
        """Group folded shells by stack and overlap pairs by shared shell."""
        folds = [finding for finding in findings if finding.kind == 'fold']
        pairs = sorted((finding for finding in findings if finding.kind != 'fold'),
                       key=lambda finding: -1.0 if finding.agreement is None else finding.agreement)
        parents = {}

        def root(key):
            parents.setdefault(key, key)
            while parents[key] != key:
                parents[key] = parents[parents[key]]
                key = parents[key]
            return key

        folded_shells = {finding.shells[0] for finding in folds}
        for pair in pairs:
            first, second = pair.shells
            if pair.kind == 'fold_overlap' and first in folded_shells and second in folded_shells:
                parents[root(second)] = root(first)
        folded_groups = {}
        for finding in folds:
            folded_groups.setdefault(root(finding.shells[0]), []).append(finding)

        parents.clear()
        for pair in pairs:
            first, second = pair.shells
            parents[root(second)] = root(first)
        groups = {}
        for pair in pairs:
            groups.setdefault(root(pair.shells[0]), []).append(pair)
        return [tuple(group) for group in (*folded_groups.values(), *groups.values())]

    @staticmethod
    def _finding_label(finding):
        if finding.kind == 'fold':
            return 'Folded shell'
        if finding.kind == 'fold_overlap':
            return 'Overlap with a folded shell'
        percentage = format(finding.agreement, '.2%')
        if round(finding.agreement * 100, 2) >= checks.MATCH_THRESHOLD * 100:
            percentage = '<{:.0%}'.format(checks.MATCH_THRESHOLD)
        return 'Stack mismatch · {} match'.format(percentage)

    @staticmethod
    def _finding_names(finding):
        names = [(shell.shape.rsplit('|', 1)[0] or shell.shape).rsplit('|', 1)[-1]
                 for shell in finding.shells]
        if len(names) == 2 and names[0] == names[1]:
            first, second = finding.shells
            return '{} · f[{}] ↔ f[{}]'.format(names[0], first.faces[0], second.faces[0])
        return ' ↔ '.join(names)

    def populate_results(self):
        groups = self._result_groups(self.report.findings)
        row_height = self.results.fontMetrics().lineSpacing() * 2 + 16
        with signals_blocked(self.results):
            self.results.clear()
            for group in groups:
                first = group[0]
                label = self._finding_label(first)
                names = self._finding_names(first)
                if first.kind == 'fold' and len(group) > 1:
                    label = 'Folded stack · {} shells'.format(len(group))
                    members = list(dict.fromkeys(self._finding_names(finding) for finding in group))
                    names = ', '.join(members[:2]) + (' …' if len(members) > 2 else '')
                elif len(group) > 1:
                    label = '{} · {} pairs'.format(
                        'Stack mismatch' if first.kind == 'mismatch' else 'Overlap group', len(group))
                item = QtWidgets.QListWidgetItem(label + '\n' + names)
                item.setData(QtCore.Qt.UserRole, group)
                item.setSizeHint(QtCore.QSize(0, row_height))
                item.setToolTip(label + '\n' + '\n'.join(dict.fromkeys(
                    shell.shape for finding in (group if first.kind == 'fold' else (first,))
                    for shell in finding.shells)))
                self.results.addItem(item)
        self.results.setFixedHeight(min(5, max(1, len(groups))) * row_height + 8)
        self.results.setVisible(bool(groups))
        self.navigation.setEnabled(True)
        folds = sum(finding.kind == 'fold' for finding in self.report.findings)
        pairs = len(self.report.findings) - folds
        text = 'Checked: {}\nFolds: {} · Conflicting pairs: {}'.format(self.report.checked, folds, pairs)
        if self.report.skipped:
            text += '\nSkipped: {}'.format(len(self.report.skipped))
        self.status.setText(text)
        self.status.setStyleSheet('color: {};'.format(
            '#ef8181' if groups or self.report.skipped else '#70ce8b'))
        self.status.setToolTip('\n'.join('{}: {}'.format(*entry) for entry in self.report.skipped))
        self._show_analysis()

    @staticmethod
    def _group_selection(group, preferred=()):
        return group if group[0].kind == 'fold' else preferred or group[:1]

    def _selected_findings(self):
        groups = [item.data(QtCore.Qt.UserRole) for item in self.results.selectedItems()]
        if len(groups) == 1:
            group = groups[0]
            active = tuple(finding for finding in self.active_findings if finding in group)
            return self._group_selection(group, active)
        return tuple(finding for group in groups for finding in group)

    def _select(self):
        findings = self._selected_findings()
        if not findings:
            self.detail.hide()
            self.navigation.hide()
            return
        transition = (tuple(finding.key for finding in findings), self.results.currentRow())
        if self.running:
            if self.recheck_target is not None:
                self.pending_transition = transition
            return
        try:
            if not self.queue_recheck(transition):
                self._select_findings(findings)
        except (RuntimeError, ValueError) as error:
            self.status.setText(str(error))
            cmds.warning(str(error))

    def result_selection_changed(self):
        # Both mouse signals share one deferred Maya selection update.
        self.selection_timer.start(0)
        if self.recheck_target is not None:
            self.pending_transition = (tuple(finding.key for finding in self._selected_findings()),
                                       self.results.currentRow())

    def result_clicked(self, *unused):
        self.result_selection_changed()

    def _select_findings(self, findings):
        shells = tuple(dict.fromkeys(shell for finding in findings for shell in finding.shells))
        for shell in shells:
            self._validate_shell(shell)
        checks.select_issues(shells)
        item = self.results.currentItem()
        group = item.data(QtCore.Qt.UserRole) if item is not None else ()
        self.active_findings = findings if findings and all(finding in group for finding in findings) else ()
        active = self.active_findings[0] if len(self.active_findings) == 1 else None
        has_pair_navigation = active is not None and active.kind != 'fold' and len(group) > 1
        self.navigation.setVisible(has_pair_navigation)
        if has_pair_navigation:
            self.shell_status.setText('Pair {} / {}'.format(group.index(active) + 1, len(group)))
            self.detail.setText(self._finding_label(active) + '\n' + self._finding_names(active))
        multiple_rows = len(findings) > 1 and not self.active_findings
        if multiple_rows:
            self.detail.setText('Selected {} findings.'.format(len(findings)))
        self.detail.setVisible(has_pair_navigation or multiple_rows)
        if self.active_findings:
            self._frame_shells(shells)
        if hasattr(self.window, 'adjust_popup_size'):
            self.window.adjust_popup_size()

    def resume_selection(self, keys, row):
        wanted = set(keys)
        rows, findings = [], []
        for index in range(self.results.count()):
            group = self.results.item(index).data(QtCore.Qt.UserRole)
            selected = [finding for finding in group if finding.key in wanted]
            if selected:
                rows.append(index)
                findings.extend(self._group_selection(group, selected))
        if not rows and self.results.count():
            row = min(max(0, row), self.results.count() - 1)
            rows = [row]
            findings = self._group_selection(self.results.item(row).data(QtCore.Qt.UserRole))
        with signals_blocked(self.results):
            self.results.clearSelection()
            if rows:
                self.results.setCurrentRow(rows[0])
                for index in rows:
                    self.results.item(index).setSelected(True)
        self._select_findings(tuple(findings))

    def _watch_results(self,dirty=False):
        from uv_straight_changes import ChangeTracker
        if self.change_tracker is not None:self.change_tracker.stop()
        self.change_tracker = ChangeTracker()
        meshes = []
        for entry in self.report.scope or self.report.issues:
            try:
                paths = cmds.ls(entry.uuid,long=True,allPaths=True) or []
                if not paths:continue
                path = entry.shape if entry.shape in paths else paths[0]
                fn = om.MFnMesh(om.MSelectionList().add(path).getDagPath(0))
                self.result_uv_sets.setdefault(entry.uuid,fn.currentUVSetName())
                meshes.append(SimpleNamespace(name=path,path=fn.getPath()))
            except RuntimeError:continue
        self.change_tracker.watch(meshes)
        self.change_tracker.clean()
        if dirty:self.review_revision += 1

    def _consume_edits(self):
        if self.change_tracker is None:self._watch_results(dirty=True)
        if self.change_tracker.all_dirty or self.change_tracker.dirty_shapes:
            self.review_revision += 1
            self.change_tracker.clean()

    def queue_recheck(self, transition):
        self._consume_edits()
        active = self.active_findings
        if not active or all(self.validated_findings.get(finding.key, 0) >= self.review_revision
                             for finding in active):
            return False
        shells = tuple(dict.fromkeys(shell for finding in active for shell in finding.shells))
        for shell in shells:
            self._validate_shell(shell)
        self.pending_transition = transition
        self.recheck_target = shells
        try:
            self._begin_recheck()
        except (RuntimeError, ValueError):
            self.recheck_target = None
            self.pending_transition = None
            raise
        return True

    def _begin_recheck(self):
        shapes = []
        for entry in self.report.scope or self.report.issues:
            paths = cmds.ls(entry.uuid, long=True, allPaths=True) or []
            if not paths:
                continue
            path = entry.shape if entry.shape in paths else paths[0]
            current = om.MFnMesh(om.MSelectionList().add(path).getDagPath(0)).currentUVSetName()
            if current != self.result_uv_sets.get(entry.uuid):
                raise ValueError('UV set changed. Results kept; return to the checked UV set.')
            if path not in shapes:
                shapes.append(path)
        self._release_callbacks()
        self.callbacks = [om.MSceneMessage.addCallback(event, self.scene_changed)
                          for event in (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kBeforeOpen)]
        self.steps = checks.read_steps(shapes)
        self.phase = 'recheck_read'
        self.navigation.setEnabled(False)
        self.status.setText('Rechecking edited UVs…')
        self.timer.start(0)

    def finish_recheck(self, ticket):
        self._release_callbacks()
        self.navigation.setEnabled(True)
        if ticket.cancelled.is_set():
            return
        try:
            if ticket.error:
                raise ValueError(ticket.error)
            if ticket.report is None or ticket.report.skipped:
                raise ValueError('Recheck incomplete. Previous results kept.')
            for shell in ticket.focus:
                self._validate_shell(shell)
            self._consume_edits()
            if ticket.revision != self.review_revision:
                self._begin_recheck()
                return
            transition = self.pending_transition
            self.apply_recheck(ticket.focus, ticket.report)
            self.recheck_target = None
            self.pending_transition = None
            self.active_findings = ()
            self.resume_selection(*transition)
        except (RuntimeError, ValueError) as error:
            self.recheck_target = None
            self.pending_transition = None
            self.status.setText(str(error))
            cmds.warning(str(error))

    def apply_recheck(self, active, report):
        reviewed = {}
        for entry in (*active, *report.reviewed):
            reviewed.setdefault(entry.uuid, set()).update(entry.faces)
        remaining = {
            finding.key: finding for finding in self.report.findings
            if not any(shell.uuid in reviewed and reviewed[shell.uuid].intersection(shell.faces)
                       for shell in finding.shells)
        }
        for finding in report.findings:
            remaining[finding.key] = finding
            self.validated_findings[finding.key] = self.review_revision
        meshes = {}
        for finding in remaining.values():
            for shell in finding.shells:
                meshes.setdefault((shell.shape, shell.uuid, shell.face_count), set()).update(shell.faces)
        issues = tuple(checks.MeshErrors(*mesh, tuple(sorted(faces))) for mesh, faces in meshes.items())
        self.report = replace(self.report, issues=issues, findings=tuple(remaining.values()))
        self.populate_results()

    def _validate_shell(self, shell):
        fn = self._issue_mesh(shell)
        if fn.currentUVSetName() != self.result_uv_sets.get(shell.uuid):
            raise ValueError('UV set changed. Run the check again.')
        return fn

    @staticmethod
    def _issue_mesh(issue):
        paths = cmds.ls(issue.uuid,long=True,allPaths=True) or []
        if not paths:raise ValueError('Mesh no longer exists. Run the check again.')
        shape = issue.shape if issue.shape in paths else paths[0]
        fn = om.MFnMesh(om.MSelectionList().add(shape).getDagPath(0))
        if fn.numPolygons != issue.face_count:
            raise ValueError('Topology changed. Run the check again.')
        return fn

    def _shell_frame_bounds(self,issue):
        """Frame this shell with a 5% minimum linear span of its whole UV set."""
        fn = self._issue_mesh(issue)
        uv_set = fn.currentUVSetName()
        counts,ids = fn.getAssignedUVs(uv_set)
        u,v = fn.getUVs(uv_set)
        used = set(ids)
        selected = set()
        faces = set(issue.faces)
        offset = 0
        for face,count in enumerate(counts):
            if face in faces:selected.update(ids[offset:offset+count])
            offset += count
        if not selected:raise ValueError('Shell has no UVs. Run the check again.')
        left,right = min(u[i] for i in selected),max(u[i] for i in selected)
        bottom,top = min(v[i] for i in selected),max(v[i] for i in selected)
        set_span = max(max(u[i] for i in used)-min(u[i] for i in used),
                       max(v[i] for i in used)-min(v[i] for i in used))
        if set_span<=1.e-8:set_span=1.
        span = max((right-left)*1.2,(top-bottom)*1.2,set_span*.05,1.e-6)
        center_u,center_v = (left+right)*.5,(bottom+top)*.5
        bounds = (center_u-span*.5,center_u+span*.5,center_v-span*.5,center_v+span*.5)
        if not all(math.isfinite(value) for value in bounds):
            raise ValueError('Shell contains invalid UV coordinates.')
        return bounds

    @staticmethod
    def _frame_uv_bounds(editors,bounds):
        """Native Frame Selected on an ephemeral rectangle, with exact cleanup.

        Maya exposes no absolute UV view rectangle setter. A private selection
        connection prevents shell masks/locked editor lists from fitting the
        entire source mesh instead. Source UV coordinates are never edited.
        """
        from uv_straight_native import _no_undo,uv_edit
        selection = om.MGlobal.getActiveSelectionList()
        hilite = om.MGlobal.getHiliteList()
        mode = om.MGlobal.selectionMode()
        mask = om.MGlobal.componentSelectionMask()
        focus = cmds.getPanel(withFocus=True)
        parent = None
        connections = []
        with uv_edit(),_no_undo():
            try:
                root = om.MFnTransform()
                parent = root.create()
                root.setName('SS_UVFrameTmp#');root.setDoNotWrite(True)
                root.findPlug('hiddenInOutliner',False).setBool(True)
                fn = om.MFnMesh()
                fn.create([om.MPoint(0,0,0),om.MPoint(1.e-6,0,0),
                           om.MPoint(1.e-6,1.e-6,0),om.MPoint(0,1.e-6,0)],
                          [4],[0,1,2,3],parent=parent)
                fn.setDoNotWrite(True)
                left,right,bottom,top = bounds
                fn.setUVs([left,right,right,left],[bottom,bottom,top,top])
                fn.assignUVs([4],[0,1,2,3])
                components = fn.fullPathName()+'.map[0:3]'
                om.MGlobal.setSelectionMode(om.MGlobal.kSelectComponentMode)
                om.MGlobal.setComponentSelectionMask(om.MSelectionMask(om.MSelectionMask.kSelectMeshUVs))
                om.MGlobal.setHiliteList(om.MSelectionList().add(root.fullPathName()))
                om.MGlobal.setActiveSelectionList(om.MSelectionList().add(components))
                content = cmds.selectionConnection();connections.append(content)
                picked = cmds.selectionConnection();connections.append(picked)
                cmds.selectionConnection(content,edit=True,select=fn.fullPathName())
                cmds.selectionConnection(picked,edit=True,select=components)
                for editor in editors:
                    previous_main = cmds.textureWindow(editor,query=True,mainListConnection=True)
                    previous_selection = cmds.textureWindow(editor,query=True,selectionConnection=True)
                    try:
                        cmds.textureWindow(editor,edit=True,forceMainConnection=content,selectionConnection=picked)
                        cmds.setFocus(editor)
                        cmds.textureWindow(editor,edit=True,frameSelected=True)
                    finally:
                        cmds.textureWindow(editor,edit=True,forceMainConnection=previous_main,
                                           selectionConnection=previous_selection)
            finally:
                try:
                    om.MGlobal.setSelectionMode(mode)
                    om.MGlobal.setComponentSelectionMask(mask)
                    om.MGlobal.setHiliteList(hilite)
                    om.MGlobal.setActiveSelectionList(selection)
                finally:
                    try:
                        if parent is not None:
                            modifier = om.MDagModifier();modifier.deleteNode(parent);modifier.doIt()
                    finally:
                        for connection in connections:
                            if connection:cmds.deleteUI(connection)
                        if focus:
                            try:cmds.setFocus(focus)
                            except RuntimeError:pass

    def _frame_shells(self, shells):
        from uv_straight_native import _find_editors
        editors = _find_editors()
        if editors:
            bounds = [self._shell_frame_bounds(shell) for shell in shells]
            self._frame_uv_bounds(editors, (min(b[0] for b in bounds), max(b[1] for b in bounds),
                                            min(b[2] for b in bounds), max(b[3] for b in bounds)))
        else:
            panel = cmds.getPanel(withFocus=True)
            if not panel or cmds.getPanel(typeOf=panel) != 'modelPanel':
                panel = next((p for p in cmds.getPanel(visiblePanels=True) or []
                              if cmds.getPanel(typeOf=p) == 'modelPanel'),None)
            if panel:
                selection = cmds.ls(selection=True,long=True) or []
                faces = cmds.polyListComponentConversion(selection,toFace=True) or []
                try:
                    if faces:cmds.select(faces,replace=True)
                    cmds.viewFit(cmds.modelPanel(panel,query=True,camera=True),animate=False)
                finally:cmds.select(selection,replace=True)

    def navigate_shell(self, direction):
        if self.running:
            return
        item = self.results.currentItem()
        group = item.data(QtCore.Qt.UserRole) if item is not None else ()
        if not group or group[0].kind == 'fold':
            return
        self.selection_timer.stop()
        try:
            active = self.active_findings[0] if len(self.active_findings) == 1 else None
            if active in group:
                index = (group.index(active) + direction) % len(group)
            else:
                index = 0 if direction > 0 else len(group) - 1
            finding = group[index]
            transition = ((finding.key,), self.results.currentRow())
            if not self.queue_recheck(transition):
                self.resume_selection(*transition)
        except (RuntimeError, ValueError) as error:
            self.status.setText(str(error))
            cmds.warning(str(error))
