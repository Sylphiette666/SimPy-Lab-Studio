"""Studio workflow extensions, kept separate from the simulation core."""
from __future__ import annotations

import copy
import itertools
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from simlab.manufacturing import ManufacturingConfig
from simlab.studio_ai import AdjustmentProposal, ParameterChange, allowed_paths, apply_proposal
from simlab.studio_analysis import METRICS, calibrate, diagnostics, finite, paired_comparison, precision
from simlab.studio_data import DataManager, Draft, SessionMetadata, now
from simlab.studio_profiles import ProfileInput
from simlab.studio_tables import TableFile, map_parameters, read_table

ROOT='/api/studio'


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)


class ValidationInput(Body):
    config: dict
    mode: Literal['paper','custom']='paper'


class MappedTable(TableFile):
    config: dict
    mode: Literal['paper','custom']='paper'
    kind: Literal['machines','buffers']='machines'
    mapping: dict[str,str]
    availability_percent: bool=False


class Workspace(Body):
    selected_session_id: str | None=None
    backup_minutes: Literal[0,15,60,360] | None=None
    backup_keep: int | None=Field(default=None,ge=1,le=20)


class ConnectionInput(ProfileInput):
    profile_id: str | None=None


class BatchInput(Body):
    expected_version_id: str
    parameters: dict[str,list[float]]


class Reference(Body):
    values: dict[str,float]
    conditions: str=Field(min_length=1,max_length=1000)
    confirmed_same_conditions: Literal[True]


def connection_probe(settings):
    if not settings.api_key:
        return {'ok':False,'code':'missing_key','message':'未填写密钥；请填写，或选择已有密钥的同一服务配置。'}
    start=time.monotonic()
    try:
        with OpenAI(api_key=settings.api_key,base_url=settings.base_url or None,timeout=15,max_retries=0) as client:
            if settings.uses_responses:
                response=client.responses.create(model=settings.model,input='Reply only OK.',max_output_tokens=32,store=False)
                if not getattr(response,'id',None): raise ValueError
            else:
                response=client.chat.completions.create(model=settings.model,messages=[{'role':'user','content':'Reply only OK.'}],max_tokens=32)
                if not response.choices: raise ValueError
        return {'ok':True,'code':'connected','message':'模型已接受测试请求，连接成功。','latency_ms':round((time.monotonic()-start)*1000)}
    except APITimeoutError:
        code,message='timeout','请求超时，请检查网络或稍后重试。'
    except APIConnectionError:
        code,message='address','无法连接地址，请检查 API Base URL、网络与证书。'
    except APIStatusError as exc:
        status=exc.status_code
        provider_code=str(getattr(exc,'code','') or '').lower()
        if status in (401,403): code,message='authentication','密钥无效、已失效或无访问权限。'
        elif provider_code in ('model_not_found','invalid_model','model_not_available','model_access_denied'): code,message='model','该模型不存在或当前密钥不可用。'
        elif status==404: code,message='address_or_model','路径或模型不存在；请检查 Base URL 是否包含 /v1，以及模型标识。'
        elif status==429: code,message='quota','服务限流或额度不足，请查看提供商账户。'
        elif status in (400,422): code,message='protocol','模型或接口不接受测试请求，请核对模型名称与接口格式。'
        else: code,message='provider','服务暂时不可用，请稍后重试。'
    except Exception:
        code,message='protocol','响应格式不兼容，未确认模型连接成功。'
    return {'ok':False,'code':code,'message':message}


def install_tools(app,store,profiles,validate,do_run,connection_tester=None):
    data=DataManager(store,validate)
    app.state.data_manager=data
    batch_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='studio-batch')
    batch_stop=threading.Event()
    for session in store.sessions.values():
        for batch in session.get('batches',[]):
            if batch['status'] in ('queued','running','cancelling'):
                batch.update(status='interrupted',error='软件退出中断队列；已完成结果保留，可重新创建批量实验。')
                store.save(session)

    def close():
        batch_stop.set(); data.stop(); batch_executor.shutdown(wait=False,cancel_futures=True)
    app.state.close_tools=close

    @app.get(ROOT+'/workspace')
    def workspace():
        with store.lock: return {**data.settings,'backup_error':data.last_error}

    @app.put(ROOT+'/workspace')
    def save_workspace(body: Workspace):
        return data.update_settings(body.selected_session_id,body.backup_minutes,body.backup_keep)

    @app.get(ROOT+'/sessions/{session_id}/draft')
    def get_draft(session_id:str):
        with store.lock: return data.draft(session_id)

    @app.put(ROOT+'/sessions/{session_id}/draft')
    def save_draft(session_id:str,body:Draft):
        try: return data.save_draft(session_id,body)
        except (ValueError,KeyError,TypeError): raise HTTPException(422,'草稿格式无效。') from None

    @app.put(ROOT+'/sessions/{session_id}/metadata')
    def metadata(session_id:str,body:SessionMetadata):
        if any(not tag.strip() or len(tag)>40 for tag in body.tags): raise HTTPException(422,'标签须为 1–40 个字符。')
        with store.lock:
            session=store.get(session_id); session['metadata']=body.model_dump()
            session['metadata']['tags']=list(dict.fromkeys(tag.strip() for tag in body.tags))
            store.save(session); return copy.deepcopy(session)

    @app.post(ROOT+'/validate')
    def validate_input(body:ValidationInput):
        errors=[]; warnings=[]
        try:
            config=ManufacturingConfig.model_validate(body.config); validate(config,body.mode)
            for i,m in enumerate(config.machines):
                if m.processing_power_kw < m.idle_power_kw: warnings.append({'path':f'machines.{i}.processing_power_kw','message':'加工功率低于空闲功率，请核对实测值与 kW 单位。'})
            if config.replications<2: warnings.append({'path':'replications','message':'仅 1 次重复无法估计置信区间；建议先运行至少 10 次。'})
        except ValidationError as exc:
            errors=[{'path':'.'.join(map(str,e['loc'])),'message':e['msg']} for e in exc.errors()]
        except HTTPException as exc:
            errors=[{'path':'','message':str(exc.detail)}]
        return {'ok':not errors,'errors':errors,'warnings':warnings}

    @app.post(ROOT+'/tables/inspect')
    def inspect_table(body:TableFile): return read_table(body)

    @app.post(ROOT+'/tables/map')
    def map_table(body:MappedTable):
        try:
            config=ManufacturingConfig.model_validate(body.config)
            result=map_parameters(config.model_dump(),read_table(body),body.kind,body.mapping,body.availability_percent)
            if result['ok']:
                try: validate(ManufacturingConfig.model_validate(result['config']),body.mode)
                except HTTPException as exc: result.update(ok=False,config=None,errors=[{'row':None,'field':'model','message':str(exc.detail)}])
            return result
        except ValidationError: raise HTTPException(422,'当前模型无效，请先修正输入。') from None

    @app.post(ROOT+'/ai/test-connection')
    def test_connection(body:ConnectionInput):
        with store.lock:
            old=profiles.snapshot(body.profile_id) if body.profile_id else profiles.snapshot()
            key=body.api_key if body.api_key is not None else (old.api_key if body.profile_id and old.base_url==body.base_url else '')
            settings=replace(old,model=body.model,base_url=body.base_url,api_format=body.api_format,api_key=key)
        return (connection_tester or connection_probe)(settings)

    def completed(session,version_id,kind='study'):
        store.version(session,version_id)
        for run in reversed(session['runs']):
            if run['version_id']==version_id and run['kind']==kind and run['status']=='succeeded':
                full=store.run_result(session['id'],run)
                if full['status']=='succeeded': return full['result']
        return None

    @app.get(ROOT+'/sessions/{session_id}/analysis/{version_id}')
    def analysis(session_id:str,version_id:str,relative:float=.05,baseline:str|None=None):
        with store.lock:
            session=store.get(session_id); result=completed(session,version_id)
            if not result: raise HTTPException(409,'请先完成该方案的完整重复评估。')
            preview=completed(session,version_id,'preview')
            comparison=completed(session,baseline) if baseline else None
            reference=copy.deepcopy(session.get('reference'))
        try:
            return {'diagnostics':diagnostics(result,preview),'precision':precision(result,relative),
                    'comparison':paired_comparison(comparison,result) if comparison else None,
                    'calibration':calibrate(result,reference['values']) if reference else None,'reference':reference}
        except ValueError as exc: raise HTTPException(422,str(exc)) from None

    @app.put(ROOT+'/sessions/{session_id}/reference')
    def set_reference(session_id:str,body:Reference):
        if not body.values or any(key not in METRICS or not finite(value) or value<=0 for key,value in body.values.items()): raise HTTPException(422,'请填写至少一项有效的正数实测指标。')
        with store.lock:
            session=store.get(session_id); session['reference']={**body.model_dump(),'saved_at':now()}
            store.save(session); return session['reference']

    def batch_worker(session_id,batch_id):
        with store.lock:
            session=store.get(session_id); batch=next(b for b in session['batches'] if b['id']==batch_id)
            if batch['status']!='cancelling': batch['status']='running'
            store.save(session)
        for job in batch['jobs']:
            with store.lock:
                if batch_stop.is_set() or batch['status']=='cancelling':
                    for pending in batch['jobs']:
                        run=store.run(session,pending['run_id'])
                        if run['status']=='queued': run.update(status='cancelled',error='队列已取消；未开始计算。')
                    batch['status']='interrupted' if batch_stop.is_set() else 'cancelled'; store.save(session); return
                config=ManufacturingConfig.model_validate(store.version(session,job['version_id'])['config'])
            do_run(session_id,job['run_id'],config,'study')
            with store.lock: batch['completed']+=1; store.save(session)
        with store.lock:
            batch['status']='succeeded' if all(store.run(session,j['run_id'])['status']=='succeeded' for j in batch['jobs']) else 'completed_with_errors'
            store.save(session)

    @app.post(ROOT+'/sessions/{session_id}/batches',status_code=202)
    def create_batch(session_id:str,body:BatchInput):
        with store.lock:
            session=store.get(session_id); version=store.head(session,body.expected_version_id)
            if any(b['status'] in ('queued','running','cancelling') for s in store.sessions.values() for b in s.get('batches',[])): raise HTTPException(409,'已有批量队列运行中，请等待或取消后重试。')
            forbidden={'until_seconds','warmup_seconds','replications','base_seed','confidence_level'}
            paths=set(allowed_paths(version['config'],version['mode']))-forbidden
            params=body.parameters
            if not 1<=len(params)<=3 or any(k not in paths or not 1<=len(v)<=6 or len(set(v))!=len(v) for k,v in params.items()): raise HTTPException(422,'请选择 1–3 个可调整参数，每个 1–6 个不重复数值；实验口径固定。')
            count=math.prod(len(v) for v in params.values())
            if count>20: raise HTTPException(422,'每批最多 20 个参数组合。')
            candidates=[]; effort=0
            for values in itertools.product(*params.values()):
                change=dict(zip(params,values))
                try:
                    candidate=ManufacturingConfig.model_validate(apply_proposal(version['config'],AdjustmentProposal(note='参数扫描',changes=[ParameterChange(path=k,value=v) for k,v in change.items()]),version['mode']))
                except ValueError:
                    raise HTTPException(422,'参数组合违反数值或关联约束；请检查可用率、维修时间、节拍与容量。') from None
                validate(candidate,version['mode']); effort+=candidate.until_seconds*candidate.replications*sum(1/m.cycle_time_seconds+(1/m.mttf_seconds if m.availability<1 else 0) for m in candidate.machines)
                candidates.append((change,candidate))
            if effort>20_000_000: raise HTTPException(422,'批次预计事件量过大，请减少参数组合或缩短实验设置。')
            batch={'id':str(uuid.uuid4()),'base_version_id':version['id'],'created_at':now(),'status':'queued','completed':0,'jobs':[]}
            active=session['active_version_id']
            for i,(changes,config) in enumerate(candidates):
                session['active_version_id']=version['id']
                new=store.append_version(session,config.model_dump(mode='json'),version['mode'],label=f'参数扫描 {len(session.get("batches",[]))+1}-{i+1}',source='batch',note='同一实验口径下的参数组合：'+str(changes))
                run={'id':str(uuid.uuid4()),'version_id':new['id'],'kind':'study','status':'queued','progress':0,'created_at':now(),'error':None,'result':None}
                session['runs'].append(run); batch['jobs'].append({'version_id':new['id'],'run_id':run['id'],'parameters':changes})
            session['active_version_id']=active; session.setdefault('batches',[]).append(batch); store.save(session)
            batch_executor.submit(batch_worker,session_id,batch['id'])
            return copy.deepcopy(batch)

    @app.get(ROOT+'/sessions/{session_id}/batches')
    def batches(session_id:str):
        with store.lock:
            session=store.get(session_id); result=copy.deepcopy(session.get('batches',[]))
            for batch in result:
                for job in batch['jobs']:
                    run=store.run(session,job['run_id']); job['status']=run['status']; job['metrics']={}
                    if run['status']=='succeeded':
                        output=store.run_result(session_id,run)['result']
                        if output:
                            job['metrics']={m['metric']:m['mean'] for m in output['summary'] if m['metric'] in METRICS}
                            if session.get('reference'): job['calibration_score']=calibrate(output,session['reference']['values'])['normalized_rmse']
            return {'batches':result}

    @app.post(ROOT+'/sessions/{session_id}/batches/{batch_id}/cancel')
    def cancel_batch(session_id:str,batch_id:str):
        with store.lock:
            session=store.get(session_id)
            batch=next((b for b in session.get('batches',[]) if b['id']==batch_id),None)
            if not batch: raise HTTPException(404,'批量队列不存在。')
            if batch['status'] in ('queued','running'): batch['status']='cancelling'; store.save(session)
            return copy.deepcopy(batch)

    @app.post(ROOT+'/backups',status_code=201)
    def backup(): return data.backup()

    @app.get(ROOT+'/backups')
    def backups():
        folder=store.root/'backups'
        return {'backups':[{'name':p.name,'bytes':p.stat().st_size} for p in sorted(folder.glob('backup-*.zip'),reverse=True) if p.is_file()]}

    @app.get(ROOT+'/backups/{filename}')
    def download_backup(filename:str):
        import re
        if not re.fullmatch(r'backup-\d{8}T\d{6}-[0-9a-f]{8}\.zip',filename): raise HTTPException(404,'备份不存在。')
        path=store.root/'backups'/filename
        if not path.is_file() or path.is_symlink(): raise HTTPException(404,'备份不存在。')
        return FileResponse(path,filename=filename,media_type='application/zip')

    @app.post(ROOT+'/experiments/restore',status_code=201)
    async def restore(request:Request):
        content=bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>64_000_000: raise HTTPException(413,'实验包不能超过 64 MB。')
        # Offload archive parsing and disk I/O so long imports do not block the event loop.
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(data.restore,bytes(content))

    return data
