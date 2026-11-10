"""Run Maya's unchanged harmonic Relax on bounded, disposable island batches.

untangleUV spends time on the containing mesh even for a small UV selection.
Copy only complete selected UV islands, keeping geometry, UV topology, pins
and world transform. Read all results before committing once to the source.
Scratch work has no undo entries; the source commit remains fully undoable.
"""
from collections import defaultdict
from types import SimpleNamespace
import time
import uuid
import maya.cmds as cmds
import maya.api.OpenMaya as om


def batches(mesh,ids,limit=384):
    islands=defaultdict(set)
    for uv in sorted(ids):islands[mesh.shell_ids[uv]].add(uv)
    batch=set()
    for island in islands.values():
        if batch and len(batch)+len(island)>limit:yield batch;batch=set()
        batch.update(island)
    if batch:yield batch


def relax(mesh,ids,iterations,fixed=None):
    import uv_straight as uv
    from uv_straight_solver import write_batch
    ids=set(ids);started=time.perf_counter();coordinates=mesh.fn.getUVs(mesh.uv_set)
    points=mesh.fn.getPoints();faces=defaultdict(list)
    for uvs,vertices in zip(mesh.faces,mesh.face_vertices):
        if uvs and uvs[0] in ids:
            if not set(uvs)<=ids:raise ValueError('Unfold bridge requires complete UV islands.')
            faces[mesh.shell_ids[uvs[0]]].append((uvs,vertices))
    weights=cmds.polyPinUV(mesh.components(ids),query=True,value=True) or []
    pins={i:w for i,w in zip(sorted(ids),weights) if w>0}
    fixed={} if fixed is None else {i:tuple(p) for i,p in fixed.items() if i in ids}
    # Temporary pins live on scratch copies only; source pin weights stay intact.
    pins.update({i:1. for i in fixed})
    result={};count=0
    # Restore recording before the source UV commit, including on exceptions.
    recording=cmds.undoInfo(query=True,state=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        for batch in batches(mesh,ids):
            selected=[face for owner in sorted({mesh.shell_ids[i] for i in batch}) for face in faces[owner]]
            if not selected:continue
            uv_ids=sorted({i for uvs,_ in selected for i in uvs});uv_index={i:n for n,i in enumerate(uv_ids)}
            vertices=sorted({i for _,face in selected for i in face});vertex_index={i:n for n,i in enumerate(vertices)}
            counts=[len(uvs) for uvs,_ in selected]
            parent=None
            try:
                parent=cmds.createNode('transform',name='SS_UnfoldScratch_'+uuid.uuid4().hex[:12],skipSelect=True)
                cmds.setAttr(parent+'.visibility',False)
                cmds.xform(parent,worldSpace=True,matrix=list(mesh.path.inclusiveMatrix()))
                selection=om.MSelectionList();selection.add(parent);parent_object=selection.getDependNode(0)
                fn=om.MFnMesh();fn.create([points[i] for i in vertices],counts,
                    [vertex_index[i] for _,face in selected for i in face],parent=parent_object)
                uv_set=fn.currentUVSetName()
                # The scratch set's name is independent; copy coordinates from
                # the captured source set without switching the source's set.
                fn.setUVs([fixed[i][0] if i in fixed else coordinates[0][i] for i in uv_ids],
                          [fixed[i][1] if i in fixed else coordinates[1][i] for i in uv_ids],uv_set)
                fn.assignUVs(counts,[uv_index[i] for uvs,_ in selected for i in uvs],uv_set)
                name=fn.fullPathName()
                components=lambda values:['{}.map[{}]'.format(name,i) for i in sorted(values)]
                by_weight=defaultdict(list)
                for i in uv_ids:
                    if i in pins:by_weight[pins[i]].append(uv_index[i])
                for weight,pinned in by_weight.items():cmds.polyPinUV(components(pinned),operation=0,value=weight,uvSetName=uv_set)
                scratch=SimpleNamespace(name=name,path=fn.getPath(),fn=fn,uv_set=uv_set,components=components)
                scratch_fixed={uv_index[i] for i in uv_ids if i in fixed}
                if scratch_fixed:
                    uv._maya_relax_bridge(scratch,set(range(len(uv_ids)))-scratch_fixed,{},iterations,
                                          pin_unselected=True)
                else:uv._maya_relax_bridge(scratch,set(range(len(uv_ids))),{},iterations)
                u,v=fn.getUVs(uv_set)
                result.update({i:fixed[i] if i in fixed else (u[n],v[n]) for n,i in enumerate(uv_ids)})
                count+=1
            finally:
                if parent is not None and cmds.objExists(parent):cmds.delete(parent)
    finally:cmds.undoInfo(stateWithoutFlush=recording)
    # If a batch failed, no coordinates from previous batches reached the mesh.
    write_batch([(mesh,result)])
    return dict(batches=count,uvs=len(result),seconds=time.perf_counter()-started)
