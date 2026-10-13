"""Native UV-editor annotation shape. Loaded on demand, never saved in scenes.

Uses a Viewport 2.0 locator draw override. The helper remains transient and
never changes mesh materials. UV-editor colors are handled by the Qt overlay.
"""
import maya.api.OpenMaya as om
import maya.api.OpenMayaUI as omui
import maya.api.OpenMayaRender as omr
import maya.OpenMayaRender as gl_api
import uv_straight_draw_state as state

TYPE_NAME = state.TYPE_NAME
# Local-use ID; this tool is not a globally registered commercial Maya plugin.
TYPE_ID = om.MTypeId(0x0007A5D1)
DRAW_CLASSIFICATION = 'drawdb/geometry/uvStraightAnnotationShape'
DRAW_REGISTRANT = 'UVStraightViewportDraw'


def maya_useNewAPI():
    pass


class AnnotationShape(omui.MPxLocatorNode):
    def excludeAsLocator(self):
        # The heatmap is a mesh overlay, not a user locator.
        return False

    def postConstructor(self):
        om.MFnDependencyNode(self.thisMObject()).setDoNotWrite(True)

    def isBounded(self):
        return False

    def boundingBox(self):
        return om.MBoundingBox()


class AnnotationUI(omui.MPxSurfaceShapeUI):
    def canDrawUV(self):
        state.CAN_DRAW_COUNT += 1
        return bool(state.PAYLOAD)

    def getDrawRequests(self, info, object_and_active_only, queue):
        # This helper intentionally draws nothing in 3D viewports.
        pass

    def select(self, *args):
        return False

    def drawUV(self, view, info):
        state.UV_CALL_COUNT += 1
        try:
            self._drawUV(view, info)
            state.DRAW_ERROR = ''
        except Exception as error:
            state.DRAW_ERROR = '{}: {}'.format(type(error).__name__, error)

    def _drawUV(self, view, info):
        if info.drawingFunction not in (omui.MTextureEditorDrawInfo.kDrawWireframe,
                                       omui.MTextureEditorDrawInfo.kDrawEverything):
            return
        if not state.PAYLOAD:
            return
        renderer = gl_api.MHardwareRenderer.theRenderer()
        if renderer is None:
            return
        gl = renderer.glFunctionTable()
        view.beginGL()
        gl.glPushAttrib(gl_api.MGL_CURRENT_BIT | gl_api.MGL_LINE_BIT |
                        gl_api.MGL_POINT_BIT | gl_api.MGL_ENABLE_BIT)
        try:
            gl.glDisable(gl_api.MGL_DEPTH_TEST)
            gl.glDisable(gl_api.MGL_LIGHTING)
            gl.glLineWidth(3.0)
            gl.glPointSize(7.0)
            for color, segments, points in state.PAYLOAD:
                gl.glColor3f(*color)
                gl.glBegin(gl_api.MGL_LINES)
                for a, b in segments:
                    gl.glVertex3f(a[0], a[1], 0.0)
                    gl.glVertex3f(b[0], b[1], 0.0)
                gl.glEnd()
                gl.glBegin(gl_api.MGL_POINTS)
                for point in points:
                    gl.glVertex3f(point[0], point[1], 0.0)
                gl.glEnd()
            state.DRAW_COUNT += 1
        finally:
            gl.glPopAttrib()
            view.endGL()


class ViewportData(om.MUserData):
    def __init__(self):
        super().__init__(False)
        self.points = om.MPointArray()
        self.colors = om.MColorArray()
        self.source = None


class ViewportDrawOverride(omr.MPxDrawOverride):
    """Draw the live distortion map as non-selectable world-space triangles."""
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

    def isTransparent(self):
        return True

    def prepareForDraw(self, obj_path, camera_path, frame_context, old_data):
        state.VIEWPORT_PREPARE_COUNT += 1
        data = old_data if isinstance(old_data, ViewportData) else ViewportData()
        if data.source is state.VIEWPORT_PAYLOAD:
            return data
        data.source = state.VIEWPORT_PAYLOAD
        data.points.clear()
        data.colors.clear()
        for rgba, triangles in state.VIEWPORT_PAYLOAD:
            color = om.MColor(rgba)
            for point in triangles:
                data.points.append(om.MPoint(*point))
                data.colors.append(color)
        return data

    def addUIDrawables(self, obj_path, draw_manager, frame_context, data):
        state.VIEWPORT_CALLBACK_COUNT += 1
        if not isinstance(data, ViewportData) or not len(data.points):
            return
        try:
            draw_manager.beginDrawable(omr.MUIDrawManager.kNonSelectable)
            try:
                draw_manager.setDepthPriority(omr.MRenderItem.sActiveWireDepthPriority)
                draw_manager.mesh(omr.MUIDrawManager.kTriangles, data.points, color=data.colors)
                state.VIEWPORT_DRAW_COUNT += 1
                state.VIEWPORT_DRAW_ERROR = ''
            finally:
                draw_manager.endDrawable()
        except Exception as error:
            state.VIEWPORT_DRAW_ERROR = '{}: {}'.format(type(error).__name__, error)


def initializePlugin(obj):
    plugin = om.MFnPlugin(obj, 'ShellShifter', '1.4.37', 'Any')
    plugin.registerNode(TYPE_NAME, TYPE_ID, AnnotationShape, lambda: None,
                        om.MPxNode.kLocatorNode, DRAW_CLASSIFICATION)
    omr.MDrawRegistry.registerDrawOverrideCreator(
        DRAW_CLASSIFICATION, DRAW_REGISTRANT, ViewportDrawOverride.creator)
    state.PLUGIN_LOADED = True


def uninitializePlugin(obj):
    omr.MDrawRegistry.deregisterDrawOverrideCreator(
        DRAW_CLASSIFICATION, DRAW_REGISTRANT)
    om.MFnPlugin(obj).deregisterNode(TYPE_ID)
    state.PLUGIN_LOADED = False
    state.VIEWPORT_PAYLOAD = []
