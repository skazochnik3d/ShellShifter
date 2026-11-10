"""Validated topology snapshots for pin drawing and component picking.

Fresh function sets/UV arrays are read on access. Bulk face/UV assignments
detect equal-count cuts/sews, history replacement and set switches. Selection
changes reuse topology instead of constructing Mesh and flattening UV strings.
"""
from array import array
from collections import OrderedDict,defaultdict
from copy import copy
import numpy as np
import maya.api.OpenMaya as om


class PinMeshCache:
    def __init__(self,limit=24,face_limit=250000):
        self.entries=OrderedDict();self.limit=limit;self.face_limit=face_limit
        self.builds=0;self.hits=0
    def clear(self):self.entries.clear()
    def get(self,path):
        import uv_straight as uv
        name=path.fullPathName();fn=om.MFnMesh(path);uv_set=fn.currentUVSetName()
        counts,vertices=fn.getVertices();uv_counts,uv_ids=fn.getAssignedUVs(uv_set);u,v=fn.getUVs(uv_set)
        signature=(uv_set,fn.numVertices,fn.numEdges,len(u),
                   *(array('i',a).tobytes() for a in (counts,vertices,uv_counts,uv_ids)))
        entry=self.entries.get(name)
        if (entry is None or not entry[0].isAlive() or not entry[0].isValid() or
                entry[0].object()!=path.node() or entry[1]!=signature):
            mesh=uv.Mesh(path)
            mesh._pin_topology_hash=uv._topology(mesh)
            pairs=sorted({tuple(sorted(pair)) for values in mesh.edge_uvs.values() for pair in values})
            mesh._pin_uv_pairs=np.asarray(pairs,dtype=np.int32).reshape(-1,2)
            mesh._pin_border_pairs=uv._border_pairs(mesh,mesh.all_shells())
            border_neighbors=defaultdict(set)
            for a,b in mesh._pin_border_pairs:
                border_neighbors[a].add(b);border_neighbors[b].add(a)
            mesh._pin_border_neighbors=dict(border_neighbors)
            mesh._pin_shell_array=np.asarray(mesh.shell_ids,dtype=np.int32)
            mesh._pin_all_ids=frozenset(mesh.points)
            mesh._pin_face_uvs={};vertex_uvs=defaultdict(set);offset=voffset=0
            for face,(count,vc) in enumerate(zip(uv_counts,counts)):
                ids=tuple(uv_ids[offset:offset+count]);mesh._pin_face_uvs[face]=ids
                if count==vc:
                    for vertex,i in zip(vertices[voffset:voffset+vc],ids):vertex_uvs[vertex].add(i)
                offset+=count;voffset+=vc
            mesh._pin_vertex_uvs=dict(vertex_uvs)
            entry=(om.MObjectHandle(path.node()),signature,mesh);self.entries[name]=entry;self.builds+=1
        else:self.hits+=1
        self.entries.move_to_end(name)
        total=sum(len(e[2].faces) for e in self.entries.values())
        while len(self.entries)>1 and (len(self.entries)>self.limit or total>self.face_limit):
            _,removed=self.entries.popitem(last=False);total-=len(removed[2].faces)
        mesh=copy(entry[2]);mesh.fn=fn;mesh.path=om.MDagPath(path)
        mesh.points=dict(enumerate(zip(u,v)));mesh._pin_coordinates=np.column_stack((u,v))
        mesh.selected=set();mesh.explicit_selected=set()
        return mesh


def explicit_uv_ids(meshes,selection=None):
    selection=selection if selection is not None else om.MGlobal.getActiveSelectionList()
    result={m.name:set() for m in meshes};by_path={m.name:m for m in meshes}
    for n in range(selection.length()):
        try:
            path,component=selection.getComponent(n)
            if component.isNull() or not component.hasFn(om.MFn.kMeshMapComponent):continue
            if path.node().hasFn(om.MFn.kTransform):path.extendToShape()
            mesh=by_path.get(path.fullPathName())
            if mesh is None:continue
            indices=om.MFnSingleIndexedComponent(component)
            ids=mesh.points if indices.isComplete else indices.getElements()
            result[mesh.name].update(i for i in ids if i in mesh.points)
        except (RuntimeError,TypeError):continue
    return result


def selected_meshes(cache,shapes=None):
    from uv_straight_qt_native import selected_mesh_shapes
    if shapes is None:
        _,paths=selected_mesh_shapes(with_paths=True)
        shapes=tuple(dict.fromkeys(path.fullPathName() for path,_ in paths))
    meshes=[cache.get(om.MSelectionList().add(shape).getDagPath(0)) for shape in shapes]
    by_path={m.name:m for m in meshes};selection=om.MGlobal.getActiveSelectionList()
    for n in range(selection.length()):
        try:path,component=selection.getComponent(n)
        except (RuntimeError,TypeError):
            try:path=selection.getDagPath(n);component=om.MObject()
            except (RuntimeError,TypeError):continue
        name=path.fullPathName()
        if component.isNull():
            for mesh in meshes:
                if mesh.name==name or mesh.name.startswith(name+'|'):mesh.selected.update(mesh.points)
            continue
        if path.node().hasFn(om.MFn.kTransform):path.extendToShape();name=path.fullPathName()
        mesh=by_path.get(name)
        if mesh is None:continue
        if not component.hasFn(om.MFn.kSingleIndexedComponent):
            # Preserve Maya's conversion semantics for uncommon component kinds.
            import uv_straight as uv
            return uv._selection()[1]
        indices=om.MFnSingleIndexedComponent(component)
        if component.hasFn(om.MFn.kMeshMapComponent):
            ids=mesh.points if indices.isComplete else indices.getElements()
            mesh.selected.update(i for i in ids if i in mesh.points);mesh.explicit_selected.update(mesh.selected)
        elif component.hasFn(om.MFn.kMeshEdgeComponent):
            ids=range(mesh.fn.numEdges) if indices.isComplete else indices.getElements()
            for edge in ids:
                for pair in mesh.edge_uvs.get(edge,()):mesh.selected.update(pair)
        elif component.hasFn(om.MFn.kMeshPolygonComponent):
            ids=range(mesh.fn.numPolygons) if indices.isComplete else indices.getElements()
            for face in ids:mesh.selected.update(mesh._pin_face_uvs.get(face,()))
        elif component.hasFn(om.MFn.kMeshVertComponent):
            ids=range(mesh.fn.numVertices) if indices.isComplete else indices.getElements()
            for vertex in ids:mesh.selected.update(mesh._pin_vertex_uvs.get(vertex,()))
        else:
            import uv_straight as uv
            return uv._selection()[1]
    return [m for m in meshes if m.selected]


_action_cache=PinMeshCache()


def pin_selection():
    import maya.cmds as cmds
    import sys
    selection=cmds.ls(selection=True,long=True,flatten=True) or []
    if not selection:raise ValueError('Select UVs, polygon edges, faces or a mesh.')
    native=sys.modules.get('uv_straight_native');manager=getattr(native,'_manager',None)
    cache=getattr(manager,'pin_mesh_cache',_action_cache)
    meshes=selected_meshes(cache)
    if not meshes:raise ValueError('The selection has no UVs in the current UV set.')
    return selection,meshes
