"""Remap immutable data per image, then safely connect successful materials."""
import json
from pathlib import Path
import re
import time
import uuid
import numpy as np
import maya.cmds as cmds
from uv_straight_transfer_models import CornerMap,ImageSet
from uv_straight_transfer_errors import CancelledError
from uv_straight_transfer_snapshot import metadata,set_metadata,mesh_fn,capture,capture_uv,source,restrict,combine,digest,compatible_geometry,encode,transfer_parts,frame_error
from uv_straight_transfer_graph import assignments,describe,validate_files,file_errors,reconnect,graph_change,identity
from uv_straight_transfer_raster import rasterize_transfer,rasterize_color_transfer
from uv_straight_transfer_codec import encode_normals
from uv_straight_transfer_images import MImageBackend,image_bit_depth
from uv_straight_transfer_udim import output_path


class RawImageBackend(MImageBackend):
    """Straight RGBA image IO also preserves RGB where alpha is zero."""
    def read_tile(self,path,udim=1001):
        from PySide6 import QtGui
        from uv_straight_transfer_models import ImageTile
        depth=image_bit_depth(path);image=QtGui.QImage(str(path))
        if image.isNull():
            if depth==16:raise ValueError('Could not read the 16-bit texture without precision loss: '+str(path))
            return super().read_tile(path,udim)
        if depth==16 and image.format() not in (QtGui.QImage.Format_Grayscale16,QtGui.QImage.Format_RGBX64,
                                                QtGui.QImage.Format_RGBA64,QtGui.QImage.Format_RGBA64_Premultiplied):
            raise ValueError('The codec returned 8-bit data for a 16-bit texture: '+str(path))
        image=image.convertToFormat(QtGui.QImage.Format_RGBA64 if depth==16 else QtGui.QImage.Format_RGBA8888)
        dtype=np.uint16 if depth==16 else np.uint8;size=np.dtype(dtype).itemsize
        values=np.frombuffer(image.constBits(),dtype=dtype,count=image.sizeInBytes()//size)
        pixels=values.reshape(image.height(),image.bytesPerLine()//size)[:,:image.width()*4].reshape(image.height(),image.width(),4)
        return ImageTile(udim,pixels.astype(np.float32)/(65535. if depth==16 else 255.),str(path),depth)

    def write(self,path,pixels,*,bit_depth=8):
        if bit_depth not in (8,16):raise ValueError('Output depth must be 8 or 16 bits.')
        if Path(path).suffix.lower()!='.png':
            if bit_depth==16:raise ValueError('Use PNG for 16-bit output.')
            return super().write(path,pixels)
        from PySide6 import QtGui
        import os
        import tempfile
        destination=Path(path);destination.parent.mkdir(parents=True,exist_ok=True)
        values=np.asarray(pixels,dtype=np.float32);rgba=np.ones(values.shape[:2]+(4,),dtype=np.float32)
        rgba[...,:values.shape[2]]=values
        raw=np.ascontiguousarray(np.rint(np.clip(rgba,0,1)*(65535 if bit_depth==16 else 255)),dtype=np.uint16 if bit_depth==16 else np.uint8)
        image=QtGui.QImage(raw.data,raw.shape[1],raw.shape[0],raw.strides[0],
                          QtGui.QImage.Format_RGBA64 if bit_depth==16 else QtGui.QImage.Format_RGBA8888)
        fd,temporary=tempfile.mkstemp(prefix='.ss_transfer_',suffix='.png',dir=str(destination.parent));os.close(fd)
        try:
            if not image.save(temporary,'PNG'):raise ValueError('Could not write texture: '+str(destination))
            os.replace(temporary,destination)
        finally:
            if Path(temporary).exists():Path(temporary).unlink()
        return str(destination)


def _material_rows(selected=None,progress=None):
    grouped={}
    paths=sorted(set(cmds.ls(dag=True,type='mesh',long=True,allPaths=True,noIntermediate=True) or []))
    for index,path in enumerate(paths):
        if progress:progress('Finding selected material assignments…',index,len(paths))
        assigned=assignments(path)
        if selected is not None:
            assigned=[material for material in assigned if material['uuid'] in selected]
        if not assigned:continue
        uv_set=mesh_fn(path).currentUVSetName();data=metadata(path);state=data['sets'].get(uv_set,{})
        for material in assigned:
            key=material['uuid'];entry=state.get('materials',{}).get(key)
            row=grouped.setdefault(key,dict(uuid=key,name=material['name'],bindings=[],has_source=False))
            row['bindings'].append(dict(path=path,uv_set=uv_set,faces=material['faces'],entry=entry))
            row['has_source']|=entry is not None
    return grouped


def _check_material_row(row):
    reasons=[];warnings=[];entries=[binding['entry'] for binding in row['bindings']]
    row['state']='ready'
    if not row['has_source']:
        reasons.append('Source UVs have not been saved. Pack with Texture Transfer enabled in Same Mesh mode.')
        row['state']='no_source'
    elif any(entry is None for entry in entries):
        reasons.append('Source data is missing for some meshes');row['state']='incomplete'
    pending=[entry for entry in entries if entry and entry['pending']]
    row['completed']=bool(row['has_source'] and all(entries) and not pending)
    if row['completed']:
        reasons.append('Transfer completed; a new source will be saved on the next pack');row['state']='completed'
    elif pending and len(pending)!=len(entries):
        reasons.append('Inconsistent source state across material meshes');row['state']='incomplete'
    row['textures']=0
    graph=None
    try:
        graph=describe(row['name'],tolerant=True);row['textures']=len(graph['files'])
        if not graph['files']:row['state']='no_textures'
        errors=file_errors(graph)
        for entry in pending:errors.update(entry.get('source_errors',{}))
        done=_completed_files(row['bindings'])
        remaining=[texture for texture in graph['files'] if texture['uuid'] not in done]
        warnings=list(dict.fromkeys([*errors.values(),*(item['reason'] for item in graph.get('warnings',()))]))
        if not remaining or all(texture['uuid'] in errors for texture in remaining):
            if not row['completed']:reasons.extend(warnings or ['The material has no available File textures to transfer'])
        ignored={texture['uuid'] for entry in pending for texture in entry['graph']['files']
                 if not texture.get('stats') or texture.get('error')}
        ignored.update(errors)
        checked={}
        for entry in pending:
            saved_graph=entry['graph']
            signature=saved_graph.get('signature')
            if checked.get(signature)==saved_graph:continue
            change=graph_change(saved_graph,graph,ignored)
            if change:
                reasons.append('Textures or the material network changed after saving the source: '+change)
                row['state']='changed'
                break
            checked[signature]=saved_graph
    except Exception as error:
        reasons.insert(0,str(error))
        if row['state']!='no_textures':row['state']='material_error'
    row['ready']=not reasons
    row['reason']='; '.join(dict.fromkeys(reasons+warnings))
    if row['ready'] and warnings:row['state']='partial'
    return graph


def materials():
    """Show assigned materials before packing as well as saved transfer sources."""
    rows=_material_rows().values()
    for row in rows:_check_material_row(row)
    return sorted(rows,key=lambda row:row['name'].casefold())


def _roster(material_uuid):
    result=[]
    for path in sorted(set(cmds.ls(dag=True,type='mesh',long=True,allPaths=True,noIntermediate=True) or [])):
        for material in assignments(path):
            if material['uuid']==material_uuid:result.append((path,material['faces']))
    return result


def _signature(snapshot):
    uv=snapshot.uv_set
    return digest(snapshot.points,snapshot.face_counts,snapshot.face_vertices,snapshot.normals,
                  snapshot.triangle_faces,snapshot.triangle_corners,uv.uvs,uv.corner_uv_ids)


def _completed_files(bindings):
    """Skip completed maps only while every recipient still uses those UVs."""
    entries=[binding['entry'] for binding in bindings if binding.get('entry')]
    if not entries:return set()
    done=set.intersection(*(set(entry.get('transferred_files',())) for entry in entries))
    if not done:return done
    for binding in bindings:
        if not binding.get('entry'):return set()
        if digest(*capture_uv(binding['path'],binding['uv_set']))!=binding['entry'].get('transferred_uv'):
            return set()
    return done


def _prepare_material(row,key,graph,targets,sources,progress=None):
    validation=[]
    src_parts=[];dst_parts=[];bindings=[];sampled_bindings=[];skipped_warnings=[]
    for index,binding in enumerate(row['bindings']):
        if progress:progress('Reading saved and current UVs: '+row['name'],index,len(row['bindings']))
        path,uv_set=binding['path'],binding['uv_set'];data=metadata(path);state=data['sets'][uv_set]
        entry=state['materials'][key];source_key=(path,entry['source'])
        if source_key not in sources:sources[source_key]=source(path,state,entry)
        original=sources[source_key]
        if (path,uv_set) not in targets:targets[path,uv_set]=capture(path,uv_set)
        target=targets[path,uv_set]
        if entry['faces']!=binding['faces']:raise ValueError('Material assignment changed: '+row['name'])
        if not compatible_geometry(original,target):
            raise ValueError('Geometry, normals or triangulation changed after the source was saved: '+path)
        first,last,skipped=transfer_parts(original,target,binding['faces'])
        if skipped['triangles']:
            skipped_warnings.append(dict(stage='prepare',material=row['name'],code='ZERO_AREA_TARGET_UV',
                reason='Skipped {} zero-area Target UV triangles on {} (first faces {}).'.format(skipped['triangles'],path,', '.join(map(str,skipped['faces'][:12]))),
                context=dict(mesh=path,triangle_count=skipped['triangles'],faces=skipped['faces'])))
        bindings.append(dict(path=path,uv_set=uv_set,source=entry['source']))
        validation.append(dict(path=path,uv_set=uv_set,signature=_signature(target)))
        if first is None:continue
        src_parts.append(first);dst_parts.append(last);sampled_bindings.append(binding)
    errors=file_errors(graph)
    done=_completed_files(row['bindings'])
    for binding in row['bindings']:
        errors.update(binding['entry'].get('source_errors',{}))
    if any(texture['is_normal'] for texture in graph['files']):
        for role,parts in (('Source',src_parts),('Target',dst_parts)):
            for part in parts:
                reason=frame_error(part,role)
                if reason:
                    for texture in graph['files']:
                        if texture['is_normal']:errors[texture['uuid']]=reason
    source_by_file={}
    for texture in graph['files']:
        if texture['uuid'] in done:continue
        parts=[];different=False
        for binding,default in zip(sampled_bindings,src_parts):
            entry=binding['entry'];epoch=entry.get('file_sources',{}).get(texture['uuid'],entry['source'])
            if epoch==entry['source']:parts.append(default);continue
            different=True;cache_key=(binding['path'],epoch)
            if cache_key not in sources:
                state=metadata(binding['path'])['sets'][binding['uv_set']]
                sources[cache_key]=source(binding['path'],state,dict(entry,source=epoch))
            first,_,_=transfer_parts(sources[cache_key],targets[binding['path'],binding['uv_set']],binding['faces'])
            if first is not None:parts.append(first)
        if different:
            if texture['is_normal']:
                for part in parts:
                    reason=frame_error(part,'Source')
                    if reason:errors[texture['uuid']]=reason
            source_by_file[texture['uuid']]=combine(parts)
    if not src_parts:raise ValueError('All selected Target UV triangles have zero area for '+row['name']+'. Repair the Target UVs first.')
    return dict(uuid=key,name=row['name'],source=combine(src_parts),target=combine(dst_parts),
                graph=graph,bindings=bindings,
                roster=[(binding['path'],binding['faces']) for binding in row['bindings']],validation=validation,
                textures=[texture for texture in graph['files'] if texture['uuid'] not in done],
                texture_errors=errors,transferred_files=sorted(done),source_by_file=source_by_file,prepare_warnings=skipped_warnings)


def prepare(material_uuids,settings,output_directory,progress=None):
    from uv_straight_material_textures import end_preview
    end_preview()
    if not 1<=settings.width<=8192 or not 1<=settings.height<=8192:raise ValueError('Resolution must be between 1 and 8192.')
    if settings.supersampling not in (1,2,4):raise ValueError('Supersampling: 1×, 2× or 4×.')
    if settings.output_bit_depth not in (0,8,16):raise ValueError('Output depth must be automatic, 8 or 16 bits.')
    if not 0<=settings.padding<=4096:raise ValueError('Padding must be between 0 and 4096 px.')
    if settings.input_green_sign not in (-1,1) or settings.output_green_sign not in (-1,1):raise ValueError('Unknown normal coordinate system.')
    if not str(output_directory).strip():raise ValueError('Choose an output folder.')
    selected=list(dict.fromkeys(material_uuids))
    if not selected:raise ValueError('Select materials to transfer.')
    rows=_material_rows(set(selected),progress)
    jobs=[];targets={};sources={};validation=[];warnings=[]
    for index,key in enumerate(selected):
        if progress:progress('Checking selected materials…',index,len(selected))
        try:
            if key not in rows:raise ValueError('Saved material source not found. Pack in Same Mesh mode first.')
            row=rows[key]
            graph=_check_material_row(row)
            if not row['ready']:raise ValueError(row['name']+': '+row['reason'])
            job=_prepare_material(row,key,graph,targets,sources,progress)
            jobs.append(job);validation.extend(job['validation'])
        except CancelledError:
            raise
        except (ValueError,RuntimeError,OSError,KeyError) as error:
            if len(selected)==1:raise
            warnings.append(dict(stage='prepare',material=rows.get(key,{}).get('name',key),reason=str(error)))
    destination=Path(output_directory).expanduser().resolve()
    # Results live directly in the chosen folder. A unique suffix per run
    # protects source images and previous exports from being overwritten.
    run_id=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    return dict(jobs=jobs,settings=settings,directory=str(destination),run_id=run_id,validation=validation,warnings=warnings)


def plan(prepared,progress=None,cancel=None,backend=None,*,gpu=True):
    """Default to OpenCL; the CPU reference also supports machines without it."""
    from uv_straight_pack_gpu import GPUUnavailable
    engine=None;compute=dict(backend='CPU',device='CPU')
    if cancel is not None and cancel.is_set():raise CancelledError('Transfer cancelled.')
    try:
        if gpu:
            try:
                if progress:progress(dict(phase='Starting GPU texture transfer…'))
                from uv_straight_transfer_gpu import GPUTransfer
                engine=GPUTransfer();compute.update(backend='OpenCL',device=engine.name)
            except GPUUnavailable as error:
                compute['fallback_reason']=str(error)
                if progress:progress(dict(phase='GPU unavailable; transferring on CPU: '+str(error)))
        return _plan(prepared,progress,cancel,backend,engine,compute)
    finally:
        if engine is not None:engine.close()


def _plan_texture(prepared,job,texture,file_index,backend,engine,compute,progress,check,cancelled,written,bound):
    from uv_straight_pack_gpu import GPUUnavailable
    settings=prepared['settings'];src=job.get('source_by_file',{}).get(texture['uuid'],job['source']);dst=job['target']
    mapping=CornerMap(np.arange(src.corner_count,dtype=np.int32),'saved_corners','exact',
                     np.arange(len(src.face_counts),dtype=np.int32),np.arange(len(src.points),dtype=np.int32))
    check()
    if progress:progress(dict(phase='Reading '+texture['node'],material=job['name'],done=file_index,total=len(job.get('textures',job['graph']['files']))))
    started=time.perf_counter();images=backend.read(texture['path']);read_seconds=time.perf_counter()-started
    output_depth=settings.output_bit_depth or max(tile.bit_depth for tile in images.tiles.values())
    if texture['tiling']==0:
        images=ImageSet(images.tiles, repeat=True)
    last_progress=[0.]
    def update(phase,done,total):
        now=time.monotonic()
        if progress and (done==total or now-last_progress[0]>=.05):
            progress(dict(phase=phase+' · '+texture['node'],material=job['name'],done=done,total=total));last_progress[0]=now
    started=time.perf_counter()
    if engine is not None and compute['backend']=='OpenCL':
        try:
            if bound[0] is not src:
                engine.bind(src,dst,mapping,settings,cancelled);bound[0]=src
            raster=engine.rasterize(images,settings,texture['is_normal'],update,cancelled,allow_conflicts=True)
        except GPUUnavailable as error:
            compute.update(backend='CPU',device='CPU',fallback_reason=str(error))
    if engine is None or compute['backend']=='CPU':
        if progress:progress(dict(phase='CPU: '+texture['node'],material=job['name']))
        raster=(rasterize_transfer if texture['is_normal'] else rasterize_color_transfer)(
            src,dst,mapping,images,settings,progress=update,cancelled=cancelled,allow_conflicts=True)
    raster_seconds=time.perf_counter()-started
    if progress:progress(dict(phase='Writing PNG: '+texture['node'],material=job['name']))
    started=time.perf_counter()
    safe=lambda value:re.sub(r'[^\w.-]+','_',value,flags=re.UNICODE).strip('._') or 'texture'
    folder=Path(prepared['directory'])
    use_udim=len(raster)>1 or any(index!=1001 for index in raster)
    original=Path(texture['path']).stem.replace('<UDIM>','').rstrip('._')
    original=re.sub(r'__SS_\d{8}_\d{6}_[a-f0-9]{8}(?:_[a-f0-9]{8}){1,2}(?:_Maya)?$','',original,flags=re.IGNORECASE)
    base=safe(original)[:80]+'__SS_'+prepared['run_id']+'_'+job['uuid'][:8]+'_'+texture['uuid'][:8]
    pattern=str(folder/(base+('.<UDIM>' if use_udim else '')+'.png'))
    companion=texture['is_normal'] and (settings.input_green_sign!=settings.output_green_sign or settings.input_bc5!=settings.output_bc5)
    linked_pattern=str(folder/(base+'_Maya'+('.<UDIM>' if use_udim else '')+'.png')) if companion else pattern
    for udim,item in sorted(raster.items()):
        check()
        pixels=encode_normals(item.vectors,green_sign=settings.output_green_sign,bc5=settings.output_bc5) if texture['is_normal'] else item.pixels
        if texture['is_normal']:
            pixels=pixels.copy();pixels[item.owner<0]=np.asarray((.5,.5,1.)[:pixels.shape[-1]])
        path=output_path(pattern,udim,multiple_tiles=use_udim);written.append(path)
        backend.write(path,pixels,bit_depth=output_depth)
        if companion:
            compatible=encode_normals(item.vectors,green_sign=settings.input_green_sign,bc5=settings.input_bc5)
            compatible[item.owner<0]=np.asarray((.5,.5,1.)[:compatible.shape[-1]])
            path=output_path(linked_pattern,udim,multiple_tiles=use_udim);written.append(path)
            backend.write(path,compatible,bit_depth=output_depth)
    output=dict(path=linked_pattern,export_path=pattern,udim=use_udim,files=written,
                                 normal=texture['is_normal'],source=texture['path'],companion=companion,bit_depth=output_depth)
    output['overlap_conflicts']=[]
    for udim,item in sorted(raster.items()):
        count=int(np.count_nonzero(item.conflicts))
        if count:
            y,x=np.unravel_index(int(np.flatnonzero(item.conflicts)[0]),item.conflicts.shape)
            output['overlap_conflicts'].append(dict(udim=int(udim),pixels=count,x=int(x),y=int(y)))
    output['covered_pixels']=sum(int(np.count_nonzero(item.coverage)) for item in raster.values())
    timing=dict(material=job['name'],texture=texture['node'],backend=compute['backend'],read_seconds=read_seconds,
                        raster_seconds=raster_seconds,write_seconds=time.perf_counter()-started)
    return output,timing


def _plan(prepared,progress,cancel,backend,engine,compute):
    """No scene reads/writes: image buffers, rasterization and new files only."""
    from uv_straight_pack_gpu import GPUUnavailable
    backend=backend or RawImageBackend();settings=prepared['settings'];results=[];timings=[];warnings=list(prepared.get('warnings',()))
    cancelled=lambda:cancel is not None and cancel.is_set()
    def check():
        if cancelled():raise CancelledError('Transfer cancelled. Materials were not changed.')
    for job_index,job in enumerate(prepared['jobs']):
        outputs={};bound=[None]
        # Frames/corners retain the full meshes, triangles contain only the
        # material's faces. Identity corner correspondence survives UV splits.
        warnings.extend(job.get('prepare_warnings',()))
        for item in job['graph'].get('warnings',()):
            warnings.append(dict(stage='prepare',material=job['name'],**item))
        for file_index,texture in enumerate(job.get('textures',job['graph']['files'])):
            check();written=[]
            try:
                error=job.get('texture_errors',{}).get(texture['uuid'])
                if error:raise ValueError(error)
                output,timing=_plan_texture(prepared,job,texture,file_index,backend,engine,compute,progress,check,cancelled,written,bound)
                outputs[texture['uuid']]=output;timings.append(timing)
                if output['overlap_conflicts']:
                    conflicts=output['overlap_conflicts'];count=sum(item['pixels'] for item in conflicts)
                    locations='; '.join('UDIM {}, {} pixels, first ({}, {})'.format(item['udim'],item['pixels'],item['x'],item['y']) for item in conflicts)
                    warnings.append(dict(stage='overlap',material=job['name'],texture=texture['node'],texture_uuid=texture['uuid'],
                        code='OVERLAP_RESOLVED',context=dict(conflict_pixels=count,tiles=conflicts,policy='first_triangle'),
                        reason='Map transferred. Conflicting pixels: {}. {}. All unambiguous regions were saved; conflicting pixels retain the first result without blending.'.format(count,locations)))
            except CancelledError:raise
            except (ValueError,RuntimeError,OSError) as error:
                for path in written:
                    try:Path(path).unlink(missing_ok=True)
                    except OSError:pass
                warnings.append(dict(stage='image',material=job['name'],texture=texture['node'],
                                     texture_uuid=texture['uuid'],source=texture['path'],reason=str(error),
                                     code=getattr(error,'code',type(error).__name__),context=getattr(error,'context',{})))
                if progress:progress(dict(phase='Map skipped: '+texture['node']+' · '+str(error),material=job['name']))
        results.append(dict(uuid=job['uuid'],name=job['name'],outputs=outputs))
    check()
    report_path=str(Path(prepared['directory'])/('SS_Transfer_'+prepared['run_id']+'.json'))
    report=dict(directory=prepared['directory'],report_path=report_path,materials=results,settings=vars(settings),
                compute=compute,timings=timings,connected=False,warnings=warnings)
    try:
        Path(prepared['directory']).mkdir(parents=True,exist_ok=True)
        Path(report_path).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    except OSError as error:report['report_warning']=str(error)
    return report


def validate(prepared,outputs=None):
    for record in prepared['validation']:
        if _signature(capture(record['path'],record['uv_set']))!=record['signature']:
            raise ValueError('Mesh or UVs changed during transfer. New files were not connected: '+record['path'])
    for job in prepared['jobs']:
        if _roster(job['uuid'])!=job['roster']:raise ValueError('Material assignments changed: '+job['name'])
        files=job['graph']['files']
        ignored=set() if outputs is None else {texture['uuid'] for texture in files if texture['uuid'] not in outputs}
        change=graph_change(job['graph'],describe(job['name'],tolerant=True),ignored)
        if change:raise ValueError('Material network changed: '+job['name']+'. '+change)
        selected=[texture for texture in files if texture['uuid'] not in ignored]
        if selected:validate_files(dict(job['graph'],files=selected))
        for binding in job['bindings']:
            entry=metadata(binding['path'])['sets'][binding['uv_set']]['materials'][job['uuid']]
            if not entry['pending'] or entry['source']!=binding['source']:raise ValueError('The saved material source changed.')


def apply(prepared,result):
    from uv_straight import _undo
    by_uuid={row['uuid']:row for row in result['materials']};cloned={}
    warnings=result.setdefault('warnings',[])
    for job in prepared['jobs']:
        row=by_uuid[job['uuid']];outputs=row['outputs']
        row['connected']=False
        for key,output in list(outputs.items()):
            if any(not Path(path).is_file() for path in output['files']):
                warnings.append(dict(stage='connect',material=job['name'],texture_uuid=key,
                                     reason='Not all output files were written. The map was not connected.'))
                del outputs[key]
        if not outputs:continue
        try:
            validate(dict(prepared,jobs=[job],validation=job.get('validation',prepared['validation'])),outputs)
            # Independent chunks roll back a failed material without undoing
            # previously connected materials. Each successful material is undoable.
            with _undo('ShellShifter Transfer Textures'):
                nodes=reconnect(job['graph'],outputs,job['bindings'])
                done=set(job.get('transferred_files',())) | set(outputs)
                pending=any(texture['uuid'] not in done for texture in job['graph']['files'])
                current=describe(job['name'],tolerant=True) if pending else None
                ids={texture['uuid']:identity(nodes[texture['node']]) for texture in job['graph']['files']}
                for binding in job['bindings']:
                    data=metadata(binding['path']);state=data['sets'][binding['uv_set']];entry=state['materials'][job['uuid']]
                    entry['pending']=pending
                    if pending:
                        # Successful images now use the current UVs; failed ones
                        # still use their own immutable source, even after repacking.
                        epoch=uuid.uuid4().hex
                        state['sources'][epoch]=encode(capture(binding['path'],binding['uv_set']))
                        previous=entry.get('file_sources',{})
                        entry['file_sources']={ids[old]:epoch if old in outputs else previous.get(old,entry['source']) for old in ids}
                        entry['transferred_files']=[ids[old] for old in done if old in ids]
                        entry['transferred_uv']=digest(*capture_uv(binding['path'],binding['uv_set']))
                        entry['source_errors']={ids[old]:reason for old,reason in entry.get('source_errors',{}).items() if old in ids}
                        entry['graph']=current
                    else:
                        entry.pop('transferred_files',None);entry.pop('file_sources',None);entry.pop('transferred_uv',None)
                    set_metadata(binding['path'],data)
            cloned[job['uuid']]=nodes;row['connected']=True
        except (ValueError,RuntimeError,OSError,KeyError) as error:
            warnings.append(dict(stage='connect',material=job['name'],reason=str(error)))
    result['connected']=bool(cloned);result['cloned_nodes']=cloned
    result['partial']=bool(warnings or result.get('report_warning'))
    try:Path(result.get('report_path',str(Path(result['directory'],'transfer_report.json')))).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except OSError as error:result['report_warning']=str(error)
    if warnings:
        cmds.warning('ShellShifter: transfer completed with warnings. '+
                     '; '.join(item['material']+(' / '+item['texture'] if item.get('texture') else '')+': '+item['reason'] for item in warnings))
    return result
