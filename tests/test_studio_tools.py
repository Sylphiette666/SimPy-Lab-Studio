from __future__ import annotations

import base64
import copy
import io
import json
import time
import threading
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIStatusError, APITimeoutError, AuthenticationError, NotFoundError
from openpyxl import Workbook

from simlab.live_manufacturing import run_config_study
from simlab.manufacturing import ManufacturingConfig
from simlab.studio import create_studio_app
from simlab.studio_ai import AISettings
from simlab.studio_analysis import calibrate, diagnostics, paired_comparison, precision
from simlab.studio_data import Draft
from simlab.studio_tools import connection_probe

API='/api/studio'


def config():
    return ManufacturingConfig(name='测试实验',until_seconds=30,warmup_seconds=0,replications=3,
                               machines=[{'name':'M1','cycle_time_seconds':2}]).model_dump(mode='json')


@pytest.fixture
def app(tmp_path):
    return create_studio_app(output_root=tmp_path/'data',connection_tester=lambda settings:{'ok':True,'code':'connected','message':'fixture'})


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def session(client):
    response=client.post(API+'/sessions',json={'config':config(),'mode':'custom'})
    assert response.status_code==201
    return response.json()


def run(client,sid,vid,kind='study'):
    started=client.post(f'{API}/sessions/{sid}/runs',json={'version_id':vid,'kind':kind})
    assert started.status_code==202,started.text
    for _ in range(300):
        record=client.get(f'{API}/sessions/{sid}/runs/'+started.json()['id']).json()
        if record['status'] not in ('queued','running'):
            assert record['status']=='succeeded',record
            return record
        time.sleep(.01)
    pytest.fail('run timed out')


def file_body(text,name='parameters.csv'):
    data=text if isinstance(text,bytes) else text.encode('utf-8-sig')
    return {'filename':name,'data':base64.b64encode(data).decode()}


def test_draft_survives_service_restart_without_browser_storage(client,app):
    s=session(client); sid=s['id']; url=f'{API}/sessions/{sid}/draft'
    draft={'base_version_id':s['active_version_id'],'revision':0,'model':{'config':config(),'mode':'custom','breaks':[],'values':{'machine-0-cycle_time_seconds':'','model-name':'尚未应用'},'dirty':True},'prompt':'尚未发送的目标','graph':None,'playback':None}
    saved=client.put(url,json=draft); assert saved.status_code==200,saved.text
    assert client.put(url,json=draft).status_code==409
    assert client.put(API+'/workspace',json={'selected_session_id':sid}).status_code==200
    other=create_studio_app(output_root=app.state.store.root)
    with TestClient(other) as second:
        restored=second.get(url).json()
        assert restored['prompt']==draft['prompt']
        assert restored['model']['values']['machine-0-cycle_time_seconds']==''
        assert second.get(API+'/workspace').json()['selected_session_id']==sid
        assert second.get(f'{API}/sessions/{sid}').json()['versions'][0]['config']==config()


def test_draft_rejects_credentials_and_stale_revision(client):
    s=session(client)
    body={'base_version_id':s['active_version_id'],'revision':0,'model':{'config':config(),'values':{'ai-api-key':'DO-NOT-STORE'}}}
    assert client.put(f'{API}/sessions/{s["id"]}/draft',json=body).status_code==422
    assert client.get(f'{API}/sessions/{s["id"]}/draft').json()['revision']==0


def test_csv_mapping_is_preview_only_and_reports_row_errors(client):
    s=session(client); body=file_body('设备,节拍\nM1,3\n')
    inspect=client.post(API+'/tables/inspect',json=body).json()
    assert inspect['rows'][0]['row']==2
    mapped={**body,'config':config(),'mode':'custom','kind':'machines','mapping':{'name':'设备','cycle_time_seconds':'节拍'}}
    result=client.post(API+'/tables/map',json=mapped).json()
    assert result['ok'] and result['config']['machines'][0]['cycle_time_seconds']==3
    assert client.get(f'{API}/sessions/{s["id"]}').json()['versions'][0]['config']['machines'][0]['cycle_time_seconds']==2
    result=client.post(API+'/tables/map',json={**mapped,**file_body('设备,节拍\nM1,-3\n')}).json()
    assert not result['ok'] and result['errors'][0]['row']==2 and result['config'] is None
    result=client.post(API+'/tables/map',json={**mapped,**file_body('设备,节拍\nunknown,3\n')}).json()
    assert not result['ok'] and result['errors'][0]['row']==2


@pytest.mark.parametrize('contents',['name,name\nM1,2','name,value\nM1,NaN','name,value\nM1,Infinity'])
def test_invalid_table_never_applies(client,contents):
    body=file_body(contents)
    response=client.post(API+'/tables/map',json={**body,'config':config(),'mode':'custom','mapping':{'name':'name','cycle_time_seconds':'value'}})
    assert response.status_code==422 or not response.json()['ok']


def test_xlsx_sheets_and_formula_rejection(client):
    workbook=Workbook(); sheet=workbook.active; sheet.title='参数'; sheet.append(['name','cycle_time_seconds']); sheet.append(['M1',4]); workbook.create_sheet('空表')
    stream=io.BytesIO(); workbook.save(stream)
    result=client.post(API+'/tables/inspect',json=file_body(stream.getvalue(),'设备.xlsx'))
    assert result.status_code==200 and result.json()['sheets']==['参数','空表']
    sheet['B2']='=2+2'; stream=io.BytesIO(); workbook.save(stream)
    result=client.post(API+'/tables/inspect',json=file_body(stream.getvalue(),'设备.xlsx'))
    assert result.status_code==422 and '第 2 行' in result.json()['detail']


def test_field_validation_and_warnings(client):
    cfg=config(); cfg['machines'][0]['availability']=.9
    result=client.post(API+'/validate',json={'config':cfg,'mode':'custom'}).json()
    assert not result['ok'] and result['errors'][0]['path']=='machines.0'
    cfg=config(); cfg['machines'][0]['idle_power_kw']=4; cfg['replications']=1
    result=client.post(API+'/validate',json={'config':cfg,'mode':'custom'}).json()
    assert result['ok'] and len(result['warnings'])==2


def test_metadata_search_catalog_and_archive_do_not_mutate_model(client):
    s=session(client)
    result=client.put(f'{API}/sessions/{s["id"]}/metadata',json={'name':'校准实验','tags':['机床','机床'],'archived':True})
    assert result.status_code==200 and result.json()['versions']==s['versions']
    item=client.get(API+'/sessions').json()['sessions'][0]
    assert item['name']=='校准实验' and item['tags']==['机床'] and item['archived']


def test_export_restore_roundtrip_with_real_results(client,app):
    s=session(client); sid=s['id']; original=run(client,sid,s['active_version_id'])
    client.put(f'{API}/sessions/{sid}/draft',json={'base_version_id':s['active_version_id'],'revision':0,'prompt':'草稿'})
    archive=client.get(f'{API}/sessions/{sid}/export').content
    result=client.post(API+'/experiments/restore',content=archive,headers={'Content-Type':'application/zip'})
    assert result.status_code==201,result.text
    new=result.json()['sessions'][0]; assert new!=sid
    restored=client.get(f'{API}/sessions/{new}').json()
    assert restored['versions']==s['versions']
    assert client.get(f'{API}/sessions/{new}/runs/{original["id"]}').json()['result']==original['result']
    assert client.get(f'{API}/sessions/{new}/draft').json()['prompt']=='草稿'
    assert len(app.state.store.sessions)==2


@pytest.mark.parametrize('filename',['../session.json','/session.json','C:/session.json','..\\session.json'])
def test_unsafe_zip_paths_do_not_create_experiments(client,app,filename):
    s=session(client); memory=io.BytesIO()
    with zipfile.ZipFile(memory,'w') as archive: archive.writestr(filename,json.dumps(s))
    assert client.post(API+'/experiments/restore',content=memory.getvalue()).status_code==422
    assert len(app.state.store.sessions)==1


def test_backup_contains_experiments_not_credentials_and_prunes_only_own_files(client,app):
    s=session(client)
    client.post(API+'/ai/profiles',json={'name':'private','model':'fake','api_key':'never-in-backup'})
    assert client.put(API+'/workspace',json={'backup_minutes':15,'backup_keep':1}).status_code==200
    first=client.post(API+'/backups').json(); second=client.post(API+'/backups').json()
    names=[b['name'] for b in client.get(API+'/backups').json()['backups']]
    assert names==[second['name']] and first['name']!=second['name']
    raw=client.get(API+'/backups/'+second['name']).content
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert not any('profiles' in name for name in archive.namelist())
        assert all(b'never-in-backup' not in archive.read(name) for name in archive.namelist())
    restored=client.post(API+'/experiments/restore',content=raw).json()
    assert len(restored['sessions'])==1 and restored['sessions'][0]!=s['id']


def test_connection_test_does_not_save_or_activate_draft(client):
    before=client.get(API+'/ai/profiles').json()
    result=client.post(API+'/ai/test-connection',json={'name':'未保存','model':'fixture','base_url':'https://fixture.invalid','api_key':'secret-test-only'})
    assert result.status_code==200 and result.json()['ok']
    assert client.get(API+'/ai/profiles').json()==before


@pytest.mark.parametrize('status,code,expected',[(401,'invalid_api_key','authentication'),(404,'model_not_found','model'),(404,'not_found','address_or_model'),(429,'rate_limit','quota'),(500,'error','provider'),(400,'bad_request','protocol')])
def test_connection_probe_redacts_provider_errors(monkeypatch,status,code,expected):
    class Client:
        def __init__(self,**kwargs): self.responses=self
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def create(self,**kwargs):
            response=httpx.Response(status,request=httpx.Request('POST','https://fixture.invalid'))
            raise APIStatusError('secret-provider-body',response=response,body={'code':code,'message':'secret-provider-body'})
    monkeypatch.setattr('simlab.studio_tools.OpenAI',Client)
    result=connection_probe(AISettings(api_key='secret-provider-body'))
    assert result['code']==expected and 'secret-provider-body' not in json.dumps(result)


def test_batch_real_runs_rankable_and_active_version_unchanged(client):
    s=session(client); sid=s['id']
    response=client.post(f'{API}/sessions/{sid}/batches',json={'expected_version_id':s['active_version_id'],'parameters':{'machines.0.cycle_time_seconds':[2,3]}})
    assert response.status_code==202,response.text
    for _ in range(300):
        batch=client.get(f'{API}/sessions/{sid}/batches').json()['batches'][0]
        if batch['status']=='succeeded': break
        time.sleep(.01)
    assert batch['status']=='succeeded' and batch['completed']==2
    assert batch['jobs'][0]['metrics']['throughput_per_hour']>batch['jobs'][1]['metrics']['throughput_per_hour']
    assert client.get(f'{API}/sessions/{sid}').json()['active_version_id']==s['active_version_id']
    compare=client.get(f'{API}/sessions/{sid}/analysis/{batch["jobs"][1]["version_id"]}?baseline={batch["jobs"][0]["version_id"]}')
    assert compare.status_code==200 and compare.json()['comparison']['comparable']


@pytest.mark.parametrize('parameters',[{'replications':[5]},{'machines.0.cycle_time_seconds':[1,2,3,4,5,6,7]},{'machines.0.cycle_time_seconds':[2,2]},{'machines.0.cycle_time_seconds':[-1]}])
def test_batch_invalid_grid_does_not_append_versions(client,parameters):
    s=session(client)
    result=client.post(f'{API}/sessions/{s["id"]}/batches',json={'expected_version_id':s['active_version_id'],'parameters':parameters})
    assert result.status_code==422
    assert len(client.get(f'{API}/sessions/{s["id"]}').json()['versions'])==1


def synthetic(values):
    cfg=config();cfg['replications']=len(values)
    return {'config':cfg,'replications':[{'replication':i,'seed':100+i,'metrics':{'throughput_per_hour':v,'avg_wip':v,'specific_energy_kwh_per_part':v}} for i,v in enumerate(values)]}


def test_sign_test_exact_multiple_comparison_and_missing_values():
    left=synthetic(list(range(1,11)));right=synthetic(list(range(2,12)))
    result=paired_comparison(left,right)
    assert result['comparable']
    for row in result['metrics']:
        assert row['p_value']==2/1024 and row['adjusted_p_value']==6/1024 and row['significant']
    right['replications'][0]['metrics']['avg_wip']=None
    row=paired_comparison(left,right)['metrics'][1]
    assert row['missing_pairs']==1 and not row['significant']
    assert not paired_comparison(left,{**right,'config':{**right['config'],'base_seed':5}})['comparable']


def test_identical_and_zero_variance_precision():
    data=synthetic([2]*10)
    assert all(not r['significant'] and r['p_value']==1 for r in paired_comparison(data,data)['metrics'])
    assert all(r['estimated_total']==2 for r in precision(data))
    assert precision(synthetic([0,0]))[0]['estimated_total'] is None
    assert calibrate(data,{'avg_wip':4})['normalized_rmse']==.5


def test_diagnostics_use_full_post_warmup_duration():
    result=run_config_study(ManufacturingConfig.model_validate(config()))
    report=diagnostics(result)
    assert report['observation_seconds']==30
    assert sum(report['machines'][0]['fractions'].values())==pytest.approx(1)
    assert sum(report['machines'][0]['hours'].values())==pytest.approx(30/3600)
    assert report['bottleneck_candidates']==['M1']


def test_restore_keeps_reference_and_batch_results(client):
    s=session(client); sid=s['id']
    assert client.put(f'{API}/sessions/{sid}/reference',json={'values':{'avg_wip':2},'conditions':'相同班次','confirmed_same_conditions':True}).status_code==200
    client.post(f'{API}/sessions/{sid}/batches',json={'expected_version_id':s['active_version_id'],'parameters':{'machines.0.cycle_time_seconds':[3]}})
    for _ in range(300):
        batch=client.get(f'{API}/sessions/{sid}/batches').json()['batches'][0]
        if batch['status']=='succeeded': break
        time.sleep(.01)
    assert batch['status']=='succeeded'
    result=client.post(API+'/experiments/restore',content=client.get(f'{API}/sessions/{sid}/export').content)
    assert result.status_code==201,result.text
    restored=result.json()['sessions'][0]
    imported=client.get(f'{API}/sessions/{restored}').json()
    assert imported['reference']['values']=={'avg_wip':2}
    restored_batch=client.get(f'{API}/sessions/{restored}/batches').json()['batches'][0]
    assert restored_batch['jobs'][0]['metrics']==batch['jobs'][0]['metrics']


def test_restore_io_failure_does_not_publish_partial_experiments(client,app,monkeypatch):
    s=session(client)
    content=client.get(f'{API}/sessions/{s["id"]}/export').content
    before=set(app.state.store.sessions)
    def fail(*args,**kwargs): raise OSError('simulated disk full')
    monkeypatch.setattr('simlab.studio_data.write_json',fail)
    result=client.post(API+'/experiments/restore',content=content)
    assert result.status_code==500 and set(app.state.store.sessions)==before
    assert {p.parent.name for p in app.state.store.root.glob('*/session.json')}==before


def test_connection_timeout_and_missing_key_are_clear(monkeypatch):
    assert connection_probe(AISettings(api_key=''))['code']=='missing_key'
    class Client:
        def __init__(self,**kwargs): self.responses=self
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def create(self,**kwargs): raise APITimeoutError(request=httpx.Request('POST','https://fixture.invalid'))
    monkeypatch.setattr('simlab.studio_tools.OpenAI',Client)
    assert connection_probe(AISettings(api_key='test-only'))['code']=='timeout'


def test_batch_cancellation_finishes_current_and_cancels_pending(client,monkeypatch):
    entered=threading.Event(); release=threading.Event()
    original=run_config_study
    def delayed(cfg):
        entered.set(); assert release.wait(10)
        return original(cfg)
    monkeypatch.setattr('simlab.live_manufacturing.run_config_study',delayed)
    s=session(client); path=f'{API}/sessions/{s["id"]}/batches'
    batch=client.post(path,json={'expected_version_id':s['active_version_id'],'parameters':{'machines.0.cycle_time_seconds':[2,3,4]}}).json()
    try:
        assert entered.wait(5)
        assert client.post(path+'/'+batch['id']+'/cancel').status_code==200
    finally: release.set()
    for _ in range(300):
        batch=client.get(path).json()['batches'][0]
        if batch['status']=='cancelled': break
        time.sleep(.01)
    assert batch['status']=='cancelled'
    assert [job['status'] for job in batch['jobs']]==['succeeded','cancelled','cancelled']


@pytest.mark.parametrize('cancel_before_start',[True,False])
def test_queued_batch_cancellation_or_restart_never_silently_restarts(tmp_path,monkeypatch,cancel_before_start):
    pending=[]
    class DeferredExecutor:
        def __init__(self,**kwargs): pass
        def submit(self,fn,*args): pending.append((fn,args))
        def shutdown(self,**kwargs): pass
    monkeypatch.setattr('simlab.studio_tools.ThreadPoolExecutor',DeferredExecutor)
    app=create_studio_app(output_root=tmp_path/'data')
    with TestClient(app) as client:
        s=session(client); path=f'{API}/sessions/{s["id"]}/batches'
        batch=client.post(path,json={'expected_version_id':s['active_version_id'],'parameters':{'machines.0.cycle_time_seconds':[2,3]}}).json()
        if cancel_before_start:
            client.post(path+'/'+batch['id']+'/cancel')
            fn,args=pending.pop();fn(*args)
            batch=client.get(path).json()['batches'][0]
            assert batch['status']=='cancelled'
            assert all(job['status']=='cancelled' for job in batch['jobs'])
    if not cancel_before_start:
        with TestClient(create_studio_app(output_root=tmp_path/'data')) as reopened:
            batch=reopened.get(path).json()['batches'][0]
            assert batch['status']=='interrupted'
            assert all(job['status']=='failed' for job in batch['jobs'])
