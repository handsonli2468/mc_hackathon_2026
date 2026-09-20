const $=id=>document.getElementById(id);
let sessionId=localStorage.getItem('robotBrainSession')||null,missionId=null,ws=null,currentCandidates={},activeKey=null,currentBtXml='',enginePollTimer=null,engineInfo={};

function addMessage(role,text,meta=''){
  const d=document.createElement('div');d.className=`message ${role}`;d.textContent=text||'';
  if(meta){const m=document.createElement('span');m.className='meta';m.textContent=meta;d.appendChild(m)}
  $('chatLog').appendChild(d);$('chatLog').scrollTop=$('chatLog').scrollHeight;
}
function setSession(id){sessionId=id;if(id)localStorage.setItem('robotBrainSession',id);else localStorage.removeItem('robotBrainSession');$('sessionId').textContent=id||'new'}
async function apiJson(url,options={}){
  const r=await fetch(url,options);let d=null;try{d=await r.json()}catch{d={raw:await r.text().catch(()=> '')}};
  if(!r.ok){const detail=d?.detail??d?.error??d?.raw??JSON.stringify(d);throw new Error(typeof detail==='string'?detail:JSON.stringify(detail))}
  return d;
}
async function loadStatus(){
  try{
    const [h,i]=await Promise.all([fetch('/health').then(r=>r.json()),fetch('/api/info').then(r=>r.json())]);
    engineInfo=h.bt_engine||i.bt_engine||{};
    $('serverStatus').textContent=h.ok?'online':'degraded';
    $('plannerStatus').textContent=h.planner?.ok?'ready':'offline';
    $('compilerStatus').textContent=h.compiler?.ok?'ready':(h.compiler?.enabled?'offline':'disabled');
    $('engineStatus').textContent=engineInfo.ok?'ready':(engineInfo.error_kind==='auth'?'auth error':(engineInfo.reachable?'degraded':(engineInfo.enabled?'offline':'disabled')));
    const sync=h.bt_node_sync||i.bt_node_sync||{};
    $('nodeSyncStatus').textContent=!sync.attempted?'not run':(!sync.ok?'failed':(sync.semantic_complete===false?`formal ${sync.engine_node_count||0} / semantic ${sync.planner_skill_count||0}`:`${sync.engine_node_count||0} nodes`));
    $('skillCount').textContent=i.skill_count??'-';
    const rag=h.rag||i.rag||{};$('ragStatus').textContent=rag.enabled?`${Object.values(rag.documents||{}).reduce((a,b)=>a+Number(b||0),0)} docs`:'disabled';
    $('engineEndpoint').textContent=engineInfo.url||i.bt_engine?.url||'—';
    $('engineAuthState').textContent=engineInfo.auth_ok===true?'authenticated':(engineInfo.auth_ok===false?'token required / invalid':(engineInfo.token_configured?'configured':'not verified'));
  }catch(e){$('serverStatus').textContent='offline';$('engineStatus').textContent='unknown';$('nodeSyncStatus').textContent='unknown'}
}
function prettyXml(xml){try{const doc=new DOMParser().parseFromString(xml,'application/xml'),raw=new XMLSerializer().serializeToString(doc);let out='',pad=0;raw.replace(/(>)(<)(\/*)/g,'$1\n$2$3').split('\n').forEach(line=>{if(/^<\//.test(line))pad=Math.max(0,pad-1);out+=`${'  '.repeat(pad)}${line}\n`;if(/^<[^!?/][^>]*[^/]>$/.test(line))pad++});return out.trim()}catch{return xml}}
function renderTree(xml){
  const root=$('treeView');root.innerHTML='';if(!xml){root.textContent='No BehaviorTree yet.';root.className='tree-empty';return}root.className='';
  const doc=new DOMParser().parseFromString(xml,'application/xml');const tree=doc.querySelector('BehaviorTree');if(!tree?.firstElementChild){root.textContent='No valid tree.';return}
  function build(el){const li=document.createElement('li'),box=document.createElement('div');box.className='bt-node';const n=el.getAttribute('name')||'';if(n)box.dataset.nodeName=n;box.innerHTML=`<span class="type">${el.tagName}</span>${n?`<span class="node-name">${n}</span>`:''}`;const attrs=[...el.attributes].filter(a=>a.name!=='name').map(a=>`${a.name}=${a.value}`).join(' · ');if(attrs){const x=document.createElement('span');x.className='attrs';x.textContent=attrs;box.appendChild(x)}li.appendChild(box);if(el.children.length){const ul=document.createElement('ul');ul.className='bt-tree';[...el.children].forEach(c=>ul.appendChild(build(c)));li.appendChild(ul)}return li}
  const ul=document.createElement('ul');ul.className='bt-tree';ul.appendChild(build(tree.firstElementChild));root.appendChild(ul);
}
function clearTreeNodeStatuses(){document.querySelectorAll('.bt-node').forEach(el=>el.classList.remove('running','success','failure','error'))}
function setNodeStatus(name,status){document.querySelectorAll('.bt-node').forEach(el=>{if(el.dataset.nodeName===name){el.classList.remove('running','success','failure','error');const s=(status||'').toLowerCase();if(['running','success','failure','error'].includes(s))el.classList.add(s)}})}
function applyEngineTrace(meta){clearTreeNodeStatuses();for(const t of (meta?.trace||[])){if(t?.node&&t?.to)setNodeStatus(t.node,t.to)}}
function appendExecutionEvent(ev){
  if($('executionLog').classList.contains('subtle')){$('executionLog').classList.remove('subtle');$('executionLog').textContent=''}
  const item=document.createElement('div');item.className='execution-item';item.textContent=`${String(ev.status||'event').toUpperCase()} ${ev.node_name||ev.node_type||'mission'} ${ev.message||''}`;$('executionLog').prepend(item);setNodeStatus(ev.node_name,ev.status);applyEngineTrace(ev.metadata);
}
function mainPhaseNames(xml){
  if(!xml)return[];try{const doc=new DOMParser().parseFromString(xml,'application/xml'),tree=doc.querySelector('BehaviorTree'),root=tree?.firstElementChild;if(!root||root.tagName!=='Sequence')return[];return[...root.children].map((el,i)=>({name:el.getAttribute('name')||`${el.tagName}_${i+1}`,type:el.tagName,index:i+1}))}catch{return[]}
}
function lastTraceState(trace,nodeName){let state='IDLE';for(const t of trace||[]){if(t?.node===nodeName&&t?.to)state=t.to}return String(state).toUpperCase()}
function renderPhaseProgress(payload){
  const phases=mainPhaseNames(currentBtXml),trace=payload?.trace||[],wrap=$('enginePhaseStrip');wrap.innerHTML='';let success=0;
  if(!phases.length){wrap.innerHTML='<span class="subtle">No mission phases available.</span>';$('enginePhaseProgress').textContent='0 / 0';return}
  phases.forEach(p=>{const state=lastTraceState(trace,p.name),card=document.createElement('div');card.className=`phase-card ${state.toLowerCase()}`;if(state==='SUCCESS')success++;card.innerHTML=`<span class="phase-index">Phase ${p.index} · ${p.type}</span><strong class="phase-name" title="${p.name}">${p.name}</strong><span class="subtle">${state}</span>`;wrap.appendChild(card)});
  $('enginePhaseProgress').textContent=`${success} / ${phases.length} complete`;
}
function renderRunningLeaves(payload){const wrap=$('engineRunningLeaves');wrap.innerHTML='';const leaves=payload?.running_leaves||[];if(!leaves.length){wrap.innerHTML='<span class="subtle">none</span>';return}leaves.forEach(x=>{const c=document.createElement('span');c.className='status-chip running';c.textContent=x;wrap.appendChild(c)})}
function renderFailure(payload){const card=$('engineFailureCard'),state=String(payload?.state||'').toLowerCase();if(!['failure','error','rejected'].includes(state)){card.classList.add('hidden');return}card.classList.remove('hidden');const failed=payload?.last_leaf_failure||{};$('engineFailureSummary').textContent=state==='error'?(payload.error||'BT Engine runtime error'):`${failed.node||'Unknown node'}${failed.type?` (${failed.type})`:''}`;const notes=$('engineNotes');notes.innerHTML='';const arr=payload?.notes||[];if(!arr.length){notes.innerHTML='<div class="subtle">No textual notes reported.</div>'}else arr.forEach(n=>{const d=document.createElement('div');d.className='note-item';d.textContent=typeof n==='string'?n:`${n.node||'node'}: ${n.message||''}`;notes.appendChild(d)})}
function renderTrace(payload){const wrap=$('engineTraceTimeline'),trace=(payload?.trace||[]).slice(-40).reverse();wrap.innerHTML='';if(!trace.length){wrap.innerHTML='<span class="subtle">No trace yet.</span>';return}trace.forEach(t=>{const r=document.createElement('div');r.className='trace-row';const state=String(t.to||'').toUpperCase();r.innerHTML=`<span class="trace-time">${Number(t.t||0).toFixed(2)} s</span><span class="trace-node" title="${t.node||''}">${t.node||t.type||'node'}</span><span class="trace-state ${state}">${t.from||'IDLE'} → ${state||'?'}</span>`;wrap.appendChild(r)})}
function renderEngineExecution(execution){
  const payload=execution?.latest_payload||execution||{};const state=String(payload.state||execution?.state||'not_started').toLowerCase();const badge=$('engineRunState');badge.className=`engine-state-badge ${state==='not_started'?'idle':state}`;badge.textContent=state.replaceAll('_',' ').toUpperCase();$('engineRunId').textContent=payload.run_id||execution?.run_id||'—';$('engineElapsed').textContent=`${Number(payload.elapsed_s||0).toFixed(1)} s`;const leaves=payload.running_leaves||[];$('engineCurrentLeaf').textContent=leaves[0]||((payload.last_leaf_failure||{}).node)||'—';$('engineTraceCount').textContent=`${(payload.trace||[]).length} events`;$('engineExecutionView').textContent=Object.keys(payload).length?JSON.stringify(payload,null,2):'No engine run yet.';renderRunningLeaves(payload);renderFailure(payload);renderTrace(payload);renderPhaseProgress(payload);applyEngineTrace(payload);return state
}
function stopEngineUiPoll(){if(enginePollTimer){clearInterval(enginePollTimer);enginePollTimer=null}}
function startEngineUiPoll(){stopEngineUiPoll();enginePollTimer=setInterval(()=>{if(missionId)loadMissionExecution(false,true)},800)}
function connectWebSocket(){
  if(!sessionId)return;if(ws)ws.close();const proto=location.protocol==='https:'?'wss':'ws';ws=new WebSocket(`${proto}://${location.host}/ws/${sessionId}`);
  ws.onmessage=e=>{const p=JSON.parse(e.data);
    if(p.type==='execution_event'){appendExecutionEvent(p.event);if(p.event?.metadata){const state=renderEngineExecution(p.event.metadata);if(state==='running')startEngineUiPoll();else stopEngineUiPoll()}}
    if(p.type==='bt_engine_terminal'){addMessage('assistant',p.report||`BT Engine: ${p.state}`,`engine:${p.state}`);renderEngineExecution(p.payload||p);stopEngineUiPoll()}
    if(p.type==='bt_engine_monitor_error'){addMessage('assistant',`BT Engine monitor error: ${p.error}`,'engine-error');stopEngineUiPoll()}
  };
}
function setMissionButtons(){const disabled=!missionId;['engineRefreshBtn','engineCancelBtn','engineReplanBtn'].forEach(id=>$(id).disabled=disabled)}
async function loadMissionExecution(refresh=false,silent=false){
  if(!missionId){renderEngineExecution({});return}
  try{const d=await apiJson(`/api/missions/${missionId}/engine/status?refresh=${refresh?'true':'false'}`);const state=renderEngineExecution(d.execution||{});$('executionLog').classList.remove('subtle');$('executionLog').innerHTML='';(d.events||[]).slice().reverse().forEach(appendExecutionEvent);if(state==='running'&&!enginePollTimer)startEngineUiPoll();if(state!=='running')stopEngineUiPoll()}catch(e){if(!silent){$('engineExecutionView').textContent=`Status error: ${e.message}`}}
}

function ragSummary(c){
  const r=c?.rag_context||{};if(!r||!Object.keys(r).length)return {message:'No RAG retrieval yet.'};
  return {
    strategy:r.strategy,
    retrieval_sketch:r.retrieval_sketch,
    required_capabilities:r.required_capabilities,
    skill_coverage:r.skill_coverage,
    missing_capabilities:r.missing_capabilities,
    allowed_skill_ids:r.allowed_skill_ids,
    retrieved_skills:(r.retrieved_skills||[]).map(x=>({id:x.id,title:x.title,score:x.score})),
    patterns:(r.patterns||[]).map(x=>({id:x.id,title:x.title,score:x.score})),
    scene:(r.scene||[]).map(x=>({id:x.id,title:x.title,score:x.score,knowledge_type:x.metadata?.knowledge_type,location_ids:x.metadata?.location_ids})),
    locations:r.locations,
    experiences:(r.experiences||[]).map(x=>({id:x.id,mission:x.title,summary:x.content,score:x.score})),
    counts:r.counts,
    utilization:c?.rag_utilization||{},
    closed_set_validation:c?.rag_validation
  };
}
function chooseCandidate(key){
  activeKey=key;document.querySelectorAll('#candidateButtons button').forEach(b=>b.classList.toggle('active',b.dataset.key===key));const c=currentCandidates[key];if(!c)return;
  missionId=c.mission_id||null;currentBtXml=c.bt_xml||'';$('missionId').textContent=missionId||'none';setMissionButtons();$('modeBadge').textContent=`${key}: ${c.status||'-'}`;
  $('summaryView').textContent=JSON.stringify({pipeline:c.pipeline,status:c.status,message:c.message,bt_generation:c.bt_generation,auto_execution:c.auto_execution,questions:c.questions,missing_capabilities:c.missing_capabilities,compact_planner_used:c.compact_planner_used,planner_escalated:c.planner_escalated,compact_plan_normalization:c.compact_plan_normalization,location_grounding_policy:c.location_grounding_policy,conversation_grounding:c.conversation_grounding,requirements:c.requirements,requirement_normalization:c.requirement_normalization,metrics:c.metrics},null,2);
  $('irView').textContent=c.task_plan_ir?JSON.stringify(c.task_plan_ir,null,2):'No TaskPlanIR in direct pipeline.';
  $('xmlView').textContent=c.bt_xml?prettyXml(c.bt_xml):'No XML.';
  $('validationView').textContent=JSON.stringify({bt_engine_contract:c.bt_engine_contract,compact_semantic_plan:c.compact_semantic_plan,compact_plan_normalization:c.compact_plan_normalization,compact_planner_used:c.compact_planner_used,planner_escalated:c.planner_escalated,compact_fallback_reason:c.compact_fallback_reason,conversation_grounding:c.conversation_grounding,location_grounding_policy:c.location_grounding_policy,grounding_policy:c.grounding_policy,requirement_normalization:c.requirement_normalization,ir_validation:c.ir_validation,quality_gate:c.quality_gate,quality_gate_before_critic:c.quality_gate_before_critic,quality_gate_after_critic:c.quality_gate_after_critic,critic_attempted:c.critic_attempted,rag_utilization:c.rag_utilization,planner_task_plan_ir:c.planner_task_plan_ir,skill_contract_normalization:c.skill_contract_normalization,plan_hardening:c.plan_hardening,ir_normalization:c.ir_normalization,compiler_capabilities:c.compiler_capabilities,compiler_task_plan_ir:c.compiler_task_plan_ir,bt_normalization:c.bt_normalization,bt_validation:c.bt_validation,compiler_task:c.compiler_task,compiler_warnings:c.compiler_warnings,compiler_fallback_count:c.compiler_fallback_count,raw_bt_response:c.raw_bt_response},null,2);
  $('ragView').textContent=JSON.stringify(ragSummary(c),null,2);
  $('timingView').textContent=c.timing?JSON.stringify(c.timing,null,2):'No timing trace.';renderTree(c.bt_xml);renderPhaseProgress({});loadMissionExecution(false);
}
function renderCandidates(cands){currentCandidates=cands||{};const wrap=$('candidateButtons');wrap.innerHTML='';Object.keys(currentCandidates).forEach(k=>{const b=document.createElement('button');b.textContent=k;b.dataset.key=k;b.onclick=()=>chooseCandidate(k);wrap.appendChild(b)});const preferred=currentCandidates.hybrid?'hybrid':Object.keys(currentCandidates)[0];if(preferred)chooseCandidate(preferred)}

$('chatForm').addEventListener('submit',async e=>{e.preventDefault();const message=$('messageInput').value.trim();if(!message)return;let world={};const raw=$('worldState').value.trim();if(raw){try{world=JSON.parse(raw)}catch{addMessage('assistant','World state must be valid JSON.','error');return}}$('sendBtn').disabled=true;addMessage('user',message);$('messageInput').value='';$('modeBadge').textContent='planning';try{const d=await apiJson('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message,session_id:sessionId,pipeline_mode:$('pipelineMode').value,world_state:world})});if(!sessionId||sessionId!==d.session_id){setSession(d.session_id);connectWebSocket()}if(d.candidates){renderCandidates(d.candidates);addMessage('assistant',`Comparison complete: direct=${d.candidates.direct?.status}, hybrid=${d.candidates.hybrid?.status}. Compare mode does not execute either tree.`,'compare')}else{renderCandidates({[d.candidate.pipeline]:d.candidate});addMessage('assistant',d.candidate.message||d.candidate.status,d.candidate.status);const auto=d.candidate.auto_execution||{};if(auto.status==='STARTED'){addMessage('assistant',`BT Engine automatically started run ${auto.run_id}${auto.preempted_previous?' (previous run preempted)':''}.`,'engine');startEngineUiPoll()}else if(auto.status==='FAILED'){addMessage('assistant',`BehaviorTree was generated, but automatic execution failed: ${JSON.stringify(auto.error||auto.reason)}`,'engine-error')}}}catch(err){addMessage('assistant',`Request failed: ${err.message}`,'error');$('modeBadge').textContent='error'}finally{$('sendBtn').disabled=false}});
$('engineRefreshBtn').onclick=()=>loadMissionExecution(true);
$('engineCancelBtn').onclick=async()=>{if(!missionId)return;try{const d=await apiJson(`/api/missions/${missionId}/engine/cancel`,{method:'POST'});addMessage('assistant',`BT Engine cancel: ${JSON.stringify(d)}`,'engine');await loadMissionExecution(true)}catch(e){addMessage('assistant',`BT Engine cancel failed: ${e.message}`,'engine-error')}};
$('engineSyncNodesBtn').onclick=async()=>{try{const d=await apiJson('/api/bt-engine/sync-nodes',{method:'POST'});const missing=(d.missing_semantic_overlay||[]).length,invalid=Object.keys(d.invalid_semantic_overlay||{}).length;addMessage('assistant',`BT Engine /nodes synchronized: formal=${d.engine_node_count}, planner=${d.planner_skill_count}, semantic_complete=${d.semantic_complete}, missing=${missing}, invalid=${invalid}.`,'engine-contract');await loadStatus()}catch(e){addMessage('assistant',`BT Engine node sync failed: ${e.message}`,'engine-error')}};
$('engineReplanBtn').onclick=async()=>{if(!missionId)return;const oldMission=missionId;try{$('engineReplanBtn').disabled=true;addMessage('assistant','Replanning with the latest BT Engine failure feedback...','replan');const d=await apiJson(`/api/missions/${oldMission}/engine/replan`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pipeline_mode:'hybrid'})});renderCandidates({[d.candidate.pipeline]:d.candidate});addMessage('assistant',d.candidate.message||d.candidate.status,`replanned from ${oldMission}`);const auto=d.candidate.auto_execution||{};if(auto.status==='STARTED'){addMessage('assistant',`Replanned BT automatically started as run ${auto.run_id}.`,'engine');startEngineUiPoll()}else if(auto.status==='FAILED'){addMessage('assistant',`Replanned BT was generated, but automatic execution failed: ${JSON.stringify(auto.error||auto.reason)}`,'engine-error')}}catch(e){addMessage('assistant',`Replan failed: ${e.message}`,'engine-error')}finally{setMissionButtons()}};

$('newSessionBtn').onclick=async()=>{const previous=sessionId;stopEngineUiPoll();try{const d=await apiJson('/api/sessions/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({previous_session_id:previous})});setSession(d.session_id)}catch{setSession(null)}missionId=null;currentBtXml='';if(ws)ws.close();if(sessionId)connectWebSocket();$('chatLog').innerHTML='';$('candidateButtons').innerHTML='';currentCandidates={};$('summaryView').textContent='No result yet.';$('irView').textContent='No TaskPlanIR yet.';$('xmlView').textContent='No XML yet.';$('validationView').textContent='No validation result yet.';$('ragView').textContent='No RAG retrieval yet.';$('timingView').textContent='No timing trace yet.';$('executionLog').textContent='No execution events yet.';$('executionLog').classList.add('subtle');renderEngineExecution({});renderTree(null);$('missionId').textContent='none';setMissionButtons()};
document.querySelectorAll('.tab').forEach(btn=>btn.onclick=()=>{document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));btn.classList.add('active');$(`${btn.dataset.tab}Tab`).classList.add('active');if(btn.dataset.tab==='execution')loadMissionExecution(false,true)});

setSession(sessionId);setMissionButtons();renderEngineExecution({});if(sessionId)connectWebSocket();loadStatus();setInterval(loadStatus,15000);
