"""Application-owned drafts and bounded experiment backup/restore."""
from __future__ import annotations

import copy
import io
import json
import os
import re
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from simlab.manufacturing import ManufacturingConfig


def now():
    return datetime.now(UTC).isoformat()


def write_json(path: Path, data):
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


class Draft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version_id: str
    revision: int = Field(ge=0)
    model: dict | None = None
    prompt: str = Field(default='',max_length=6000)
    graph: dict | None = None
    playback: dict | None = None


class SessionMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
    name: str = Field(min_length=1,max_length=160)
    tags: list[str] = Field(default_factory=list,max_length=12)
    archived: bool = False


class DataManager:
    def __init__(self, store, validate):
        self.store, self.validate = store, validate
        self.settings_path = store.root/'workspace.json'
        self.settings = {'selected_session_id':None,'backup_minutes':60,'backup_keep':5,'last_backup':None}
        try:
            saved = json.loads(self.settings_path.read_text(encoding='utf-8'))
            self.settings.update({key:saved[key] for key in self.settings if key in saved})
            if self.settings['backup_minutes'] not in (0,15,60,360): self.settings['backup_minutes'] = 60
            if not isinstance(self.settings['backup_keep'],int) or not 1 <= self.settings['backup_keep'] <= 20: self.settings['backup_keep'] = 5
        except (OSError,ValueError,TypeError):
            pass
        self.stop_event = threading.Event()
        self.backup_lock = threading.Lock()
        self.thread = None
        self.last_error = None

    def start(self):
        self.thread = threading.Thread(target=self._loop,daemon=True,name='studio-backups')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=5)

    def _loop(self):
        while not self.stop_event.wait(30):
            with self.store.lock: settings = self.settings.copy()
            interval = settings['backup_minutes']
            try:
                last = datetime.fromisoformat(settings['last_backup']) if settings['last_backup'] else None
                due = not last or (datetime.now(UTC)-last).total_seconds() >= interval*60
                if interval and due and self.store.sessions: self.backup()
            except Exception:
                self.last_error = '自动备份未完成，请检查磁盘空间；原始实验仍保留。'

    def update_settings(self, selected=None, minutes=None, keep=None):
        with self.store.lock:
            if selected is not None:
                self.store.get(selected)
                self.settings['selected_session_id'] = selected
            if minutes is not None:
                if minutes not in (0,15,60,360): raise HTTPException(422,'备份间隔须为 0、15、60 或 360 分钟。')
                self.settings['backup_minutes'] = minutes
            if keep is not None:
                if not isinstance(keep,int) or isinstance(keep,bool) or not 1 <= keep <= 20: raise HTTPException(422,'备份保留数量须为 1–20。')
                self.settings['backup_keep'] = keep
            write_json(self.settings_path,self.settings)
            return {**self.settings,'backup_error':self.last_error}

    def draft(self, session_id):
        self.store.get(session_id)
        try:
            result = json.loads((self.store.root/session_id/'draft.json').read_text(encoding='utf-8'))
            Draft.model_validate({k:v for k,v in result.items() if k != 'saved_at'})
            return result
        except (OSError,ValueError,TypeError):
            return {'revision':0,'base_version_id':self.store.get(session_id)['active_version_id'],'model':None,'prompt':'','graph':None,'playback':None}

    def save_draft(self, session_id, draft: Draft):
        with self.store.lock:
            session = self.store.get(session_id)
            self.store.version(session,draft.base_version_id)
            old = self.draft(session_id)
            if draft.revision != old['revision']: raise HTTPException(409,'另一窗口更新了草稿，请重新载入草稿后继续。')
            data = draft.model_dump()
            self.validate_draft(draft)
            data.update(revision=old['revision']+1,saved_at=now())
            write_json(self.store.root/session_id/'draft.json',data)
            return data

    def validate_draft(self, draft):
        data = draft.model_dump()
        # Model state and graph are intentionally allowed to be incomplete, but not credentials.
        if len(json.dumps(data,ensure_ascii=False)) > 150_000: raise HTTPException(413,'草稿过大。')
        if draft.model:
            if set(draft.model) - {'config','breaks','mode','values','dirty'}: raise HTTPException(422,'模型草稿包含未知字段。')
            config = ManufacturingConfig.model_validate(draft.model['config'])
            if len(config.machines) > 12: raise HTTPException(422,'草稿设备数量超限。')
            values = draft.model.get('values',{})
            if len(values) > 180 or any(not re.fullmatch(r'(model-name|model-mode|until-days|warmup-days|replications|base-seed|machine-\d+-(name|cycle_time_seconds|availability|mttr_seconds|idle_power_kw|processing_power_kw)|buffer-\d+-(name|capacity|delay_seconds))', key) or not isinstance(value,str) or len(value)>200 for key,value in values.items()):
                raise HTTPException(422,'草稿字段不合法。')
        if draft.playback and (set(draft.playback)-{'run_id','frame_index','speed'}): raise HTTPException(422,'回放草稿字段不合法。')
        if draft.graph and set(draft.graph)-{'graph','base','mode','sourceStamp','savedGraph'}: raise HTTPException(422,'图形草稿字段不合法。')

    def archive_bytes(self, sessions):
        buffer = io.BytesIO()
        if len(sessions)>100: raise HTTPException(413,'单份备份最多 100 个实验，请分实验导出。')
        with self.store.lock, zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            ids = []
            total = 0
            def add(name, payload):
                nonlocal total
                payload=payload.encode('utf-8') if isinstance(payload,str) else payload
                total+=len(payload)
                if total>256_000_000 or len(archive.infolist())>=2999: raise HTTPException(413,'备份体积或文件数超限，请分实验导出。')
                archive.writestr(name,payload)
            for session in sessions:
                sid=session['id']; ids.append(sid)
                add(f'experiments/{sid}/session.json',json.dumps(session,ensure_ascii=False,allow_nan=False))
                for run in session['runs']:
                    path=self.store.root/sid/(run['id']+'.json')
                    if run['status']=='succeeded' and path.is_file():
                        add(f'experiments/{sid}/runs/{run["id"]}.json',json.dumps(self.store.run_result(sid,run),ensure_ascii=False,allow_nan=False))
                draft=self.store.root/sid/'draft.json'
                if draft.is_file(): add(f'experiments/{sid}/draft.json',draft.read_bytes())
            add('backup-manifest.json',json.dumps({'schema':'studio-backup-1','created_at':now(),'sessions':ids}))
        result=buffer.getvalue()
        if len(result)>64_000_000: raise HTTPException(413,'备份 ZIP 超过 64 MB，请分实验导出。')
        return result

    def backup(self):
        if not self.backup_lock.acquire(blocking=False): raise HTTPException(409,'已有备份正在生成。')
        try:
            with self.store.lock:
                content=self.archive_bytes(list(self.store.sessions.values()))
                folder=self.store.root/'backups'; folder.mkdir(exist_ok=True)
                name='backup-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.zip'
                target=folder/name; temporary=folder/(name+'.tmp')
                temporary.write_bytes(content); temporary.replace(target)
                self.settings['last_backup']=now(); write_json(self.settings_path,self.settings)
                # Only prune this manager's verified generated filenames inside its own backup folder.
                entries=sorted(folder.glob('backup-*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)
                for old in entries[self.settings['backup_keep']:]:
                    if re.fullmatch(r'backup-\d{8}T\d{6}-[0-9a-f]{8}\.zip',old.name) and old.resolve().parent==folder.resolve() and not old.is_symlink(): old.unlink()
                self.last_error=None
                return {'name':name,'bytes':len(content),'created_at':self.settings['last_backup']}
        finally:
            self.backup_lock.release()

    def restore(self, content: bytes):
        """Read known members only, never extract paths or overwrite existing experiments."""
        if len(content)>64_000_000: raise HTTPException(413,'压缩包不能超过 64 MB。')
        prepared=[]
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries=archive.infolist(); names=[i.filename for i in entries]
                if len(names)!=len(set(names)) or len(entries)>3000 or sum(i.file_size for i in entries)>256_000_000: raise ValueError('压缩包过大或含重复路径。')
                if any('..' in Path(name.replace('\\','/')).parts or name.startswith(('/','\\')) or ':' in name for name in names): raise ValueError('压缩包含非法路径。')
                def read(name):
                    def reject(value): raise ValueError('包含非有限数值。')
                    return json.loads(archive.read(name),parse_constant=reject)
                if 'backup-manifest.json' in names:
                    manifest=read('backup-manifest.json')
                    if manifest.get('schema')!='studio-backup-1' or len(manifest['sessions'])>100: raise ValueError('备份格式或实验数量无效。')
                    prefixes=[f'experiments/{str(uuid.UUID(sid))}/' for sid in manifest['sessions']]
                else: prefixes=['']
                for prefix in prefixes:
                    original=read(prefix+'session.json'); versions=original['versions']; runs=original['runs']
                    if not 1<=len(versions)<=500 or len(runs)>1000: raise ValueError('方案或运行数量超限。')
                    ids=set(); clean_versions=[]
                    for version in versions:
                        vid=str(uuid.UUID(version['id']))
                        if vid in ids: raise ValueError('重复方案标识。')
                        ids.add(vid); config=ManufacturingConfig.model_validate(version['config'])
                        if version['mode'] not in ('paper','custom'): raise ValueError('模型模式无效。')
                        self.validate(config,version['mode'])
                        clean={k:version.get(k) for k in ('id','parent_id','label','source','created_at','mode','changes','note')}
                        if not isinstance(clean['label'],str) or len(clean['label'])>100: raise ValueError('方案名称无效。')
                        clean['config']=config.model_dump(mode='json')
                        if isinstance(version.get('ai_config'),dict): clean['ai_config']={k:v for k,v in version['ai_config'].items() if k in ('profile_id','profile_name','model','base_url','api_format','provider')}
                        clean_versions.append(clean)
                    if original['active_version_id'] not in ids: raise ValueError('活动方案无效。')
                    result_files={}; clean_runs=[]; run_ids=set()
                    for run in runs:
                        rid=str(uuid.UUID(run['id']))
                        if rid in run_ids or run['version_id'] not in ids or run['kind'] not in ('study','preview'): raise ValueError('运行记录无效。')
                        run_ids.add(rid)
                        clean={k:run.get(k) for k in ('id','version_id','kind','status','created_at','finished_at','error','progress')}
                        clean['result']=None
                        if run['status']=='succeeded':
                            result=read(prefix+'runs/'+rid+'.json')['result']
                            if not isinstance(result,dict): raise ValueError('运行结果缺失。')
                            if run['kind']=='study':
                                cfg=next(v['config'] for v in clean_versions if v['id']==run['version_id'])
                                if result.get('config') != cfg or not isinstance(result.get('replications'),list) or len(result['replications']) != cfg['replications']: raise ValueError('评估结果与模型不匹配。')
                            elif not isinstance(result.get('frames'),list) or len(result['frames'])>601: raise ValueError('回放帧格式无效。')
                            result_files[rid]=result
                        elif run['status'] not in ('failed','cancelled'):
                            clean.update(status='failed',error='导入的未完成运行需要重新执行。')
                        clean_runs.append(clean)
                    messages=[]
                    for message in original.get('messages',[])[:2000]:
                        if message.get('role') not in ('user','assistant') or not isinstance(message.get('content'),str): raise ValueError('对话格式无效。')
                        clean={k:message[k] for k in ('role','content','created_at','version_id','applied') if k in message}
                        if isinstance(message.get('ai_config'),dict): clean['ai_config']={k:v for k,v in message['ai_config'].items() if k in ('profile_id','profile_name','model','base_url','api_format','provider')}
                        messages.append(clean)
                    metadata=SessionMetadata.model_validate(original.get('metadata') or {'name':clean_versions[-1]['config']['name']})
                    session={'id':str(uuid.uuid4()),'created_at':now(),'active_version_id':original['active_version_id'],
                             'versions':clean_versions,'runs':clean_runs,'messages':messages,'metadata':metadata.model_dump(),
                             'restored_from':str(uuid.UUID(original['id']))}
                    for version in clean_versions:
                        if version['parent_id'] is not None and version['parent_id'] not in ids: raise ValueError('父方案不存在。')
                        if not isinstance(version['note'],str) or len(version['note'])>16000 or not isinstance(version['changes'],list): raise ValueError('方案说明无效。')
                    if original.get('reference'):
                        reference=original['reference']; values=reference['values']
                        import math
                        if not values or set(values)-{'throughput_per_hour','avg_wip','specific_energy_kwh_per_part'} or any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<=0 for v in values.values()): raise ValueError('实测参照无效。')
                        if not isinstance(reference.get('conditions'),str) or len(reference['conditions'])>1000: raise ValueError('实测口径无效。')
                        session['reference']={k:reference[k] for k in ('values','conditions','confirmed_same_conditions','saved_at') if k in reference}
                    clean_batches=[]
                    for batch in original.get('batches',[]):
                        str(uuid.UUID(batch['id']))
                        if batch['base_version_id'] not in ids or len(batch['jobs'])>20: raise ValueError('批次格式无效。')
                        if any(j['version_id'] not in ids or j['run_id'] not in run_ids for j in batch['jobs']): raise ValueError('批次运行记录不完整。')
                        clean={k:copy.deepcopy(batch[k]) for k in ('id','base_version_id','created_at','status','completed','jobs')}
                        if clean['status'] in ('queued','running','cancelling'): clean['status']='interrupted'
                        clean_batches.append(clean)
                    if clean_batches: session['batches']=clean_batches
                    draft = None
                    if prefix+'draft.json' in names:
                        raw=read(prefix+'draft.json'); draft=Draft.model_validate({k:v for k,v in raw.items() if k != 'saved_at'})
                        if draft.base_version_id not in ids: raise ValueError('草稿方案无效。')
                        self.validate_draft(draft)
                    prepared.append((session,result_files,draft))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422,'实验包不完整、格式错误或超出限制；现有实验未被覆盖。') from None
        staging=self.store.root/('.restore-'+uuid.uuid4().hex)
        moved=[]
        try:
            staging.mkdir()
            for session,results,draft in prepared:
                folder=staging/session['id']; folder.mkdir()
                for rid,result in results.items(): write_json(folder/(rid+'.json'),result)
                write_json(folder/'session.json',session)
                if draft: write_json(folder/'draft.json',{**draft.model_dump(),'revision':1,'saved_at':now()})
            with self.store.lock:
                try:
                    for session,_,_ in prepared:
                        target=self.store.root/session['id']
                        if target.exists(): raise OSError('restore identifier collision')
                        (staging/session['id']).rename(target); moved.append(session['id'])
                except OSError:
                    for sid in reversed(moved): (self.store.root/sid).rename(staging/sid)
                    raise
                self.store.sessions.update({session['id']:session for session,_,_ in prepared})
            staging.rmdir()
            return {'sessions':[s['id'] for s,_,_ in prepared]}
        except OSError:
            raise HTTPException(500,'恢复未完成，请检查磁盘空间与权限；原有实验未被覆盖。') from None
