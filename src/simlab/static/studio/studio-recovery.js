/* Server-side recovery; only model fields are captured, never AI settings. */
"use strict";
(() => {
  let key = null, restoring = false, timer, loadTask = Promise.resolve(), queue = Promise.resolve();
  const revisions = new Map(), signatures = new Map(), blocked = new Set();
  let pendingPlayback = null;
  const strip = node('div','recovery-strip'); strip.id='recovery-status'; strip.hidden=true;
  const label = node('span'); const reload=node('button','button quiet','重新载入草稿'); reload.id='reload-server-draft'; reload.hidden=true;
  strip.append(label,reload); $('model-form').before(strip);
  function status(message,error=false) { strip.hidden=false; label.textContent=message; strip.classList.toggle('tools-error',error); }
  function capture() {
    if (!state.session || !state.draft) return null;
    const values={};
    document.querySelectorAll('#model-form input[id],#model-form select[id],#model-name,#model-mode').forEach(input => {
      if (/^(model-name|model-mode|until-days|warmup-days|replications|base-seed|machine-\d+-(name|cycle_time_seconds|availability|mttr_seconds|idle_power_kw|processing_power_kw)|buffer-\d+-(name|capacity|delay_seconds))$/.test(input.id)) values[input.id]=input.value;
    });
    const preview = [...state.runs.values()].reverse().find(r=>r.kind==='preview' && r.version_id===state.session.active_version_id && r.status==='succeeded');
    return {sessionId:state.session.id,base_version_id:state.session.active_version_id,
      model:{config:clone(state.draft),breaks:clone(state.breaks),mode:$('model-mode').value,values,dirty:state.dirty},
      prompt:$('prompt').value,graph:window.StudioVisualEditor?.snapshot() || null,
      playback:preview ? {run_id:preview.id,frame_index:state.frameIndex,speed:$('playback-speed').value} : null};
  }
  function save(snapshot=capture(),keepalive=false) {
    if (!snapshot || blocked.has(snapshot.sessionId)) return queue;
    const {sessionId,...body}=snapshot, signature=JSON.stringify(body);
    if (signatures.get(sessionId)===signature) return queue;
    queue=queue.catch(()=>{}).then(async()=>{
      if (blocked.has(sessionId)) return;
      if (!revisions.has(sessionId)) revisions.set(sessionId,(await api(`/sessions/${sessionId}/draft`)).revision);
      try {
        const result=await api(`/sessions/${sessionId}/draft`,{method:'PUT',keepalive,body:{...body,revision:revisions.get(sessionId)}});
        revisions.set(sessionId,result.revision); signatures.set(sessionId,signature);
        if (state.session?.id===sessionId) status('草稿已保存到本机 · '+new Date(result.saved_at).toLocaleTimeString('zh-CN'));
      } catch(error) {
        if (error.status===409) {blocked.add(sessionId);reload.hidden=false;}
        status(error.status===409 ? '另一窗口更新了草稿。当前输入仍保留；请先处理冲突。' : '草稿未保存：'+error.message,true);
      }
    });
    return queue;
  }
  function schedule() { if (restoring || !key) return; clearTimeout(timer); timer=setTimeout(()=>save(),500); }
  function apply(snapshot) {
    restoring=true;
    try {
      if (snapshot.model?.dirty) {
        renderEditor(snapshot.model.config,snapshot.model.mode); state.breaks=clone(snapshot.model.breaks);
        for (const [id,value] of Object.entries(snapshot.model.values || {})) if ($(id)) $(id).value=value;
        state.dirty=true; updateMode(); renderBreakSummary();
      }
      $('prompt').value=snapshot.prompt || '';
      if (snapshot.graph) window.StudioVisualEditor?.recover(snapshot.graph);
      pendingPlayback=snapshot.playback; updateControls();
    } finally { restoring=false; }
  }
  async function load(session) {
    const currentKey=session.id+'/'+session.active_version_id;
    await queue;
    const stored=await api(`/sessions/${session.id}/draft`);
    if (currentKey!==state.session?.id+'/'+state.session?.active_version_id) return;
    revisions.set(session.id,stored.revision); blocked.delete(session.id); reload.hidden=true;
    if (stored.base_version_id===session.active_version_id) {
      apply(stored);
      if (stored.revision) status('已恢复本机草稿 · 未应用的输入与未发送文字均保留');
    } else {
      status('已载入当前方案；旧草稿属于另一方案，没有自动覆盖当前模型。');
    }
    key=currentKey;
    await api('/workspace',{method:'PUT',body:{selected_session_id:session.id}});
  }
  reload.addEventListener('click',action(async()=>{
    if (!confirm('载入服务器上最后保存的草稿会替换当前未保存输入，是否继续？')) return;
    await load(state.session);
  }));
  document.addEventListener('input',event=>{if(event.target.closest('#model-form,#prompt-form')) schedule();});
  document.addEventListener('change',event=>{if(event.target.id==='playback-speed') schedule();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden && !restoring && key) save(capture(),true);});
  window.addEventListener('pagehide',()=>{if(!restoring && key) save(capture(),true);});
  setInterval(()=>{if(key && !restoring && state.session) save();},5000);
  window.StudioRecovery={schedule,flush:()=>save(),
    beforeAccept(session) {
      const same=key && state.session?.id===session.id && state.session?.active_version_id===session.active_version_id;
      const snapshot=key && !restoring ? capture() : null;
      if(snapshot) save(snapshot);
      return same ? snapshot : null;
    },
    accepted(session,preserved) {
      if(preserved) {apply(preserved);return;}
      key=null; loadTask=load(session).catch(error=>status('草稿恢复失败：'+error.message,true));
    },
    async ready() {
      await loadTask;
      if(pendingPlayback && state.frames.length) {
        const run=state.runs.get(pendingPlayback.run_id);
        if(run?.version_id===state.session.active_version_id) {
          pause(); state.frameIndex=Math.max(0,Math.min(state.frames.length-1,Number(pendingPlayback.frame_index)||0));
          if(['0.5','1','2','5','10'].includes(pendingPlayback.speed)) $('playback-speed').value=pendingPlayback.speed;
          renderFrame();
        }
      }
      pendingPlayback=null;
    }
  };
})();
