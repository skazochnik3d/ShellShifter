"""Runtime-only VP2 provider; never evaluates or edits the source meshes."""
import maya.api.OpenMaya as om
import maya.api.OpenMayaUI as omui
import maya.api.OpenMayaRender as omr
import uv_straight_live_draw_state as state

TYPE_NAME = state.TYPE_NAME
TYPE_ID = om.MTypeId(0x0007A5D2)
DRAW_CLASSIFICATION = 'drawdb/geometry/' + TYPE_NAME
DRAW_REGISTRANT = 'ShellShifterLivePreviewDraw'


def maya_useNewAPI():
    pass


class LivePreviewShape(omui.MPxLocatorNode):
    def excludeAsLocator(self):
        return False

    def postConstructor(self):
        om.MFnDependencyNode(self.thisMObject()).setDoNotWrite(True)

    def isBounded(self):
        return False


class ViewportData(om.MUserData):
    def __init__(self):
        super().__init__(False)
        self.source = None
        self.points = om.MPointArray()
        self.colors = om.MColorArray()


class ViewportDrawOverride(omr.MPxDrawOverride):
    def __init__(self, obj):
        super().__init__(obj, None, True)

    @staticmethod
    def creator(obj):
        return ViewportDrawOverride(obj)

    def supportedDrawAPIs(self):
        return omr.MRenderer.kAllDevices

    def isBounded(self, obj_path, camera_path):
        return False

    def hasUIDrawables(self):
        return True

    def prepareForDraw(self, obj_path, camera_path, frame_context, old_data):
        state.VIEWPORT_PREPARE_COUNT += 1
        data = old_data if isinstance(old_data, ViewportData) else ViewportData()
        source = state.VIEWPORT_PAYLOAD
        if source is not None:
            kind, panel = frame_context.renderingDestination()
            source = source.by_panel.get(panel, source) if kind == omr.MFrameContext.k3dViewport else None
        if data.source is source:
            return data
        data.source = source
        if source is None:
            data.points, data.colors = om.MPointArray(), om.MColorArray()
        else:
            data.points, data.colors = source.points, source.colors
        return data

    def addUIDrawables(self, obj_path, draw_manager, frame_context, data):
        state.VIEWPORT_CALLBACK_COUNT += 1
        if not isinstance(data, ViewportData) or not len(data.points):
            return
        try:
            draw_manager.beginDrawable(omr.MUIDrawManager.kNonSelectable)
            try:
                draw_manager.setDepthPriority(omr.MRenderItem.sDormantFilledDepthPriority)
                draw_manager.mesh(omr.MUIDrawManager.kTriangles, data.points, color=data.colors)
                state.VIEWPORT_DRAW_COUNT += 1
                state.VIEWPORT_DRAW_ERROR = ''
            finally:
                draw_manager.endDrawable()
        except Exception as error:
            state.VIEWPORT_DRAW_ERROR = '{}: {}'.format(type(error).__name__, error)


def initializePlugin(obj):
    plugin = om.MFnPlugin(obj, 'ShellShifter', '1.4.114', 'Any')
    plugin.registerNode(TYPE_NAME, TYPE_ID, LivePreviewShape, lambda: None,
                        om.MPxNode.kLocatorNode, DRAW_CLASSIFICATION)
    omr.MDrawRegistry.registerDrawOverrideCreator(DRAW_CLASSIFICATION, DRAW_REGISTRANT,
                                                 ViewportDrawOverride.creator)
    state.PLUGIN_LOADED = True


def uninitializePlugin(obj):
    omr.MDrawRegistry.deregisterDrawOverrideCreator(DRAW_CLASSIFICATION, DRAW_REGISTRANT)
    om.MFnPlugin(obj).deregisterNode(TYPE_ID)
    state.VIEWPORT_PAYLOAD = None
    state.PLUGIN_LOADED = False
