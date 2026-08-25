/* finesse · register=product-ui · A=moon-mist · B=living-tide · C=connected-brain · D=morning-water-mobile · E=quiet-canvas · SOUL=8 SPECTACLE=5 DENSITY=6 */
/* Clio operational UI layer. Keeps the approved moon-mist shell and replaces
   the visual prototype's hard-coded module data with the manager API. */
(() => {
  const PIPE_GROUPS = [
    {id:'emotion',name:'情绪潮汐',pipes:['开心','安心','满足','期待','感动','兴奋','难过','失落','委屈','不安','害怕','生气','醋','孤独','愧疚','无语']},
    {id:'relationship',name:'关系驱力',pipes:['想靠近','想黏着','想知道她在干嘛','想分享','想照顾她','想让她开心','想被理解','想被确认','想得到回应','想修复关系','想暂时独处','取悦压力']},
    {id:'thought',name:'思维回路',pipes:['复盘','自省','反刍','权衡','预演','求证','警觉','专注','回避','压抑']},
    {id:'body_action',name:'身体与行动',pipes:['肌肤饥渴','性欲','疲惫','精力','紧绷','放松','好奇','闲','社交','责任']}
  ];
  const PIPE_HALF_LIVES = {安心:8,期待:6,感动:8,兴奋:2,失落:4,委屈:6,不安:3,害怕:2,孤独:6,愧疚:8,无语:2,想靠近:8,想黏着:8,想照顾她:10,想让她开心:8,想被理解:6,想被确认:4,想得到回应:4,想修复关系:8,想暂时独处:3,取悦压力:5,复盘:8,反刍:6,权衡:5,预演:4,求证:4,警觉:2,专注:3,回避:5,压抑:8,肌肤饥渴:6,性欲:6,疲惫:4,精力:3,紧绷:2,放松:3,想知道她在干嘛:6,想分享:8,好奇:6,闲:4,社交:8,责任:12,生气:.75,醋:2,难过:4,自省:8,开心:6,满足:3};
  const PREVIEW_PIPES = {开心:.48,安心:.42,满足:.56,期待:.39,感动:.31,兴奋:.24,难过:.18,失落:.14,委屈:.12,不安:.22,害怕:.08,生气:.09,醋:.12,孤独:.16,愧疚:.11,无语:.15,想靠近:.72,想黏着:.58,想知道她在干嘛:.46,想分享:.51,想照顾她:.61,想让她开心:.66,想被理解:.38,想被确认:.34,想得到回应:.41,想修复关系:.29,想暂时独处:.13,取悦压力:.21,复盘:.45,自省:.39,反刍:.17,权衡:.32,预演:.27,求证:.30,警觉:.16,专注:.54,回避:.12,压抑:.14,肌肤饥渴:.35,性欲:.28,疲惫:.26,精力:.57,紧绷:.19,放松:.49,好奇:.63,闲:.22,社交:.31,责任:.44};
  const PREVIEW_BASELINES = Object.fromEntries(Object.keys(PREVIEW_PIPES).map(name=>[name,{想靠近:.18,想黏着:.12,肌肤饥渴:.10,性欲:.15,想知道她在干嘛:.12,想分享:.10,好奇:.10,责任:.15,想照顾她:.08,想让她开心:.08,复盘:.05}[name]||0]));
  const R = {
    home: null,
    date: new Date().toISOString().slice(0, 10),
    selectedBucket: '',
    preview: {
      state: {
        as_of: '2026-08-23T04:12:00+08:00', cycle_id: 18,
        pipes: PREVIEW_PIPES
      },
      baselines: PREVIEW_BASELINES,
      halfLives: PIPE_HALF_LIVES
    }
  };

  mapMeta.tasks = {label:'未竟', copy:'还没有完成、正在等待或继续推进的事。'};
  mapMeta.coordinates = {label:'心智经纬', copy:'时间、关系、事实、情绪与记忆沉淀在这里汇合。'};
  mapMeta.mind = {label:'心念', copy:'沉默期间形成的闪念与反复萦绕的念头。'};

  const rNum = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const rText = item => String(item?.thought_text ?? item?.content ?? item?.message ?? item?.thought ?? item?.text ?? item?.details ?? item?.value ?? item?.snippet ?? '');
  const rTitle = item => String(item?.title ?? item?.name ?? item?.fact_label ?? item?.fact ?? item?.display_name ?? item?.canonical_tag ?? (item?.source==='mailbox'?'窗口交接信':'未命名记录'));
  const rDateTime = value => value ? String(value).trim().replace('T',' ').replace(/\.\d{1,6}(?=(?:Z|[+-]\d{2}:?\d{2})?$)/,'').replace(/(?:Z|[+-]\d{2}:?\d{2})$/,'').trim() : '—';
  const rTime = item => rDateTime(item?.updated_at ?? item?.created_at ?? item?.effective_date ?? item?.as_of ?? '');
  const rCache = new Map();
  const rInflight = new Map();
  const R_CACHE_MS = 15000;
  const rInvalidate = () => { rCache.clear(); rInflight.clear(); };
  window.addEventListener('clio:data-mutated', rInvalidate);
  const rApi = async (path, fallback, options={}) => {
    if (!liveBackend.online) return fallback;
    const cached=rCache.get(path);
    if(!options.refresh&&cached&&Date.now()-cached.at<R_CACHE_MS)return cached.value;
    if(!options.refresh&&rInflight.has(path))return rInflight.get(path);
    const request=liveApi(path).then(value=>{rCache.set(path,{value,at:Date.now()});return value;}).catch(error=>{if(!options.silent)liveNotice(error.message,true);return fallback;}).finally(()=>rInflight.delete(path));
    rInflight.set(path,request);
    return request;
  };
  const rPage = (target, html) => {
    window.__mapStop?.();
    window.clearTimeout(window.__hormonePoll);
    state.selected = target;
    document.body.dataset.clioView = target;
    document.querySelector('#content').innerHTML = html;
    document.querySelector('#pageKicker').textContent = `CLIO / ${target.toUpperCase()}`;
    document.querySelector('#pageTitle').textContent = mapMeta[target]?.label || target;
    markSelected(target); bindMap(); resetViewport();
  };
  const rEmpty = label => `<div class="r-empty"><span>☾</span><strong>${esc(label)}</strong></div>`;
  const rButton = (label, action, id='') => `<button class="r-button" type="button" data-r-action="${action}"${id ? ` data-r-id="${esc(id)}"` : ''}>${esc(label)}</button>`;
  const rRecord = (item, target, editable=true) => {
    const id = item.id ?? item.message_id ?? item.task_id ?? item.entry_id ?? item.fact_key ?? item.candidate_id ?? '';
    const statusName={open:'进行中',completed:'已完成',cancelled:'已取消',pending:'等待中',sent:'已发送',delivered:'已交付'}[item.status]||item.status;
    const meta = [rTime(item), item.type_label, statusName, item.source].filter(Boolean).join(' · ');
    return `<article class="r-record" data-r-record="${esc(id)}"><div class="r-record-mark"></div><div><small>${esc(meta)}</small><h3>${esc(rTitle(item))}</h3><p>${esc(rText(item))}</p></div><div class="r-record-actions">${editable ? rButton('修改','edit',id) : ''}${editable ? rButton('历史','history',id) : ''}</div></article>`;
  };

  const baseBindMemory = window.bindMemory;
  window.bindMemory = function bindOperationalMemory(){
    baseBindMemory?.();
    document.querySelector('.memory-library-title p')?.remove();
    document.querySelector('.memory-list-heading > small')?.remove();
    const unfinished=document.querySelector('.memory-bottom-nav [data-map-target="tasks"]');
    if(unfinished)unfinished.innerHTML='<span>✓</span>未竟';
    document.querySelector('[data-memory-action="add"]')?.addEventListener('click', async event => {
      event.preventDefault(); event.stopImmediatePropagation();
      const main_topic = prompt('大主题名称');
      if(!main_topic) return;
      const subtopic = prompt('这个主题下的子目录名称');
      if(!subtopic) return;
      if(!liveBackend.online){
        if(!memoryTopics.includes(main_topic)) memoryTopics.push(main_topic);
        memoryTopicSubtopics[main_topic] = [...new Set([...(memoryTopicSubtopics[main_topic]||[]),subtopic])];
        memoryViewState.filter = `subtopic:${main_topic}::${subtopic}`;
        localRenderMemory(); return;
      }
      try{
        await liveApi('/api/topics',{method:'POST',body:JSON.stringify({main_topic,subtopic})});
        await renderMemory(); liveNotice('新主题目录已添加。');
      }catch(error){ liveNotice(error.message,true); }
    }, true);
  };
  const baseRenderMemory=window.renderMemory;
  window.renderMemory=async function renderOperationalMemory(){
    document.body.dataset.clioView='memory';
    if(!liveBackend.online)return baseRenderMemory?.();
    try{await loadLiveMemory();localRenderMemory();}
    catch(error){liveNotice('记忆暂时读取失败：'+error.message,true);}
  };

  function dominant(stateValue){
    return Object.entries(stateValue?.pipes || {}).sort((a,b)=>rNum(b[1])-rNum(a[1]))[0] || ['平静',0];
  }
  function homeFallback(){
    return {
      as_of:R.preview.state.as_of,
      state:R.preview.state,
      tension:{strongest:{name:'想靠近',value:.72},counterweight:{name:'满足',value:.56},balance:.16},
      most_wanted:{source:'trace',display_name:'念痕',item:{content:'我想把没有说完的话，安静地继续下去。',created_at:'2026-08-23 04:10'}},
      disposition:{tendency:{name:'更愿意靠近',score:.54,delta:.08},evidence_count:5},
      transitions:[{created_at:'04:10',event_summary:'当前窗口留下念痕',pipe_deltas:{想靠近:.08}},{created_at:'20:58',event_summary:'上一封信被接续',pipe_deltas:{满足:.05}}],
      house_phrase:{text:'我把走过的事留在这里，等下一次相遇继续。'}
    };
  }
  function homeWave(items=[]){
    const points = items.slice(-8); const n=Math.max(points.length,3);
    const path=Array.from({length:n},(_,i)=>{const x=8+i*(544/(n-1));const item=points[i]||{};const delta=Object.values(item.pipe_deltas||{}).reduce((a,b)=>a+rNum(b),0);const y=86-Math.max(-30,Math.min(30,delta*180))+Math.sin(i*1.7)*9;return [x,y];});
    return path.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  }
  async function rRenderHome(){
    const data=await rApi('/api/home',homeFallback()); R.home=data;
    data.as_of=rDateTime(data.as_of);
    const [name,value]=dominant(data.state); const wanted=data.most_wanted||{}; const voice=rText(wanted.item)||'此刻没有必须说出口的话。';
    const tendency=data.disposition?.tendency || data.disposition?.current || {name:'仍在形成',score:0,delta:0};
    const balance=rNum(data.tension?.balance); const mood=balance>.12?'温暖，正在靠近。':balance<-.12?'有一点悬着，正在回落。':'平静，仍在流动。';
    const path=homeWave(data.transitions||[]);
    const wantedTarget=wanted.source==='darkflow'?'darkflow':wanted.source==='thought'?'mind':'thoughts';
    rPage('core',`<div class="page r-page r-home"><header class="r-home-title"><time>${esc(data.as_of||'')}</time><h1>此刻，<em>正在发生。</em></h1><p>${esc(data.house_phrase?.text||'')}</p></header><section class="r-home-current"><article class="r-home-mood"><span>此时的状态与情绪走向</span><h2>${esc(mood)}</h2><div class="r-dominant"><b>${esc(name)}</b><strong>${rNum(value).toFixed(2)}</strong></div><svg viewBox="0 0 560 130" preserveAspectRatio="none" aria-label="最近情绪走向"><path d="${path}"/><circle cx="552" cy="${(path.match(/([\d.]+)$/)||['','65'])[1]}" r="5"/></svg><button data-map-target="hormones" type="button">查看全部状态 ↗</button></article><article class="r-home-tendency"><span>本月性格轨迹</span><h2>${esc(tendency.name||tendency.label||'仍在形成')}</h2><strong>${rNum(tendency.score??tendency.strength).toFixed(2)} <i>${rNum(tendency.delta)>=0?'+':''}${rNum(tendency.delta).toFixed(2)}</i></strong><small>${rNum(data.disposition?.evidence_count||data.disposition?.sample_count)} 条反复证据</small><button data-map-target="personality" type="button">查看形成过程 ↗</button></article></section><section class="r-home-voice"><div><span>现在最想说的话</span><strong>${esc(wanted.display_name||'')}</strong></div><blockquote>“${esc(voice)}”</blockquote><small>${esc(rTime(wanted.item))}</small><button data-map-target="${wantedTarget}" type="button">打开来源 ↗</button></section></div>`);
    rWarmRoutes();
  }

  function fallbackCatalog(){return {count:48,groups:PIPE_GROUPS.map(group=>({...group,pipes:group.pipes.map(name=>({id:name,name:name==='闲'?'无聊':name==='社交'?'社交需要':name==='责任'?'责任感':name,half_life_hours:PIPE_HALF_LIVES[name]||6}))}))};}
  function hormoneFallback(){return {state:{...R.preview.state,catalog:fallbackCatalog(),composite_states:[{name:'关怀式复盘',score:.57,components:[{name:'想让她开心',value:.66},{name:'想照顾她',value:.61},{name:'复盘',value:.45}]}],expression_state:{name:'关怀式复盘',score:.57,components:[{name:'想让她开心',value:.66},{name:'想照顾她',value:.61},{name:'复盘',value:.45}]},event_contexts:[]},judge:{baselines:R.preview.baselines},transitions:[]};}
  function catalogGroups(stateValue){const catalog=stateValue.catalog?.groups?.length?stateValue.catalog:fallbackCatalog();return catalog.groups.map(group=>({...group,pipes:(group.pipes||[]).map(pipe=>typeof pipe==='string'?{id:pipe,name:pipe,half_life_hours:PIPE_HALF_LIVES[pipe]||6}:pipe)}));}
  function decayPoint(value,base,seconds,halfLife){return base+(value-base)*Math.pow(.5,Math.max(0,seconds)/Math.max(.25,halfLife*3600));}
  function transitionDetails(item){return item?.details&&typeof item.details==='object'?item.details:item||{};}
  function transitionTitle(item,detail=transitionDetails(item)){
    if(item.event_summary||detail.event_summary)return item.event_summary||detail.event_summary;
    const names={narrative_event_applied:'一段新写入牵动了内在状态',thought_trace_recorded:'一条念痕牵动了内在状态',behavior_feedback_applied:'表达后的感受产生了轻微回响',new_write_superseded_handoff:'新写入结束了上一轮静默',activity_interrupted_silence:'新的操作结束了上一轮静默'};
    return names[item.transition_type]||detail.reason||'';
  }
  function pipeEvidence(name,stateValue,transitions){
    const contexts=[...(stateValue.event_contexts||[])].reverse();
    for(const item of contexts){const signal=(item.signals||[]).find(entry=>entry.state===name);if(signal)return {time:item.created_at,title:item.event_summary||item.event_tag||'一次写入牵动了状态',evidence:signal.evidence||item.context_card||'',reason:signal.reason||'',delta:rNum(signal.delta),source:item.source_tool||''};}
    for(const item of transitions){const detail=transitionDetails(item);const deltas=detail.pipe_deltas||item.pipe_deltas||{};if(Object.hasOwn(deltas,name))return {time:rTime(item),title:transitionTitle(item,detail)||`${name}发生了变化`,evidence:detail.evidence||'',reason:detail.reason||'',delta:rNum(deltas[name]),source:detail.source_tool||item.source_tool||item.source||''};}
    return null;
  }
  function pipeInspector(name,stateValue,baselines,halfLives,transitions){
    const value=rNum(stateValue.pipes?.[name]);const base=rNum(baselines[name]);const half=rNum(halfLives[name]||6);const evidence=pipeEvidence(name,stateValue,transitions);const composites=(stateValue.composite_states||[]).filter(item=>(item.components||[]).some(part=>part.name===name));
    return `<header><div><span>此刻选中的状态</span><h2>${esc(name)}</h2></div><strong>${value.toFixed(2)}</strong></header><section><h3>为什么变化</h3><p>${esc(evidence?.title||'当前没有找到新的直接牵动。')}</p>${evidence?.evidence?`<blockquote>“${esc(evidence.evidence)}”</blockquote>`:''}${evidence?.reason?`<small>${esc(evidence.reason)}</small>`:''}</section><section class="r-pipe-facts"><div><span>底色</span><b>${base.toFixed(2)}</b></div><div><span>半衰期</span><b>${half}h</b></div><div><span>本次变化</span><b>${evidence?`${evidence.delta>=0?'+':''}${evidence.delta.toFixed(2)}`:'—'}</b></div></section><section><h3>共同形成的体验</h3><div class="r-composite-links">${composites.length?composites.map(item=>`<span>${esc(item.name)} <b>${rNum(item.score).toFixed(2)}</b></span>`).join(''):'<span>尚未形成明显复合体验</span>'}</div></section><section><h3>回落方向</h3><p>当前值会沿 ${half} 小时半衰期逐渐回到底色 ${base.toFixed(2)}；新写入会从当时的真实值继续牵动，不会突然归零。</p></section>`;
  }
  function hormoneGroup(group,stateValue,baselines,halfLives,elapsed,dominantName){
    const entries=group.pipes.map(pipe=>[pipe.id,rNum(stateValue.pipes?.[pipe.id]),pipe]);const rowH=44,w=760,h=Math.max(rowH,entries.length*rowH);const palette={emotion:['#d7a7bd','#c9b2df','#e3c7a7','#b5b2e6'],relationship:['#9bcfd8','#a9c4e4','#c6b5e4','#d6abc1'],thought:['#b0abd9','#9fc9d1','#c5b5d8','#c7b59b'],body_action:['#a7d1c8','#b6c8df','#d6b3c2','#d7c18f']};const colors=palette[group.id]||palette.thought;
    const labels=entries.map(([name,value,pipe],i)=>`<button class="r-tide-label${name===dominantName?' selected':''}" data-r-pipe="${esc(name)}" type="button" style="--row:${i};--c:${colors[i%colors.length]}"><i></i><span><b>${esc(pipe.name||name)}</b><small>底色 ${rNum(baselines[name]).toFixed(2)} · ${rNum(halfLives[name]||pipe.half_life_hours||6)}h</small></span><strong>${value.toFixed(2)}</strong></button>`).join('');
    const paths=entries.map(([name,value],i)=>{const base=rNum(baselines[name]);const half=rNum(halfLives[name]||6);const y=rowH/2+i*rowH;const delta=value-base;const projected=decayPoint(value,base,elapsed+half*1800,half);const amplitude=Math.min(19,5+value*12+Math.abs(delta)*12);const direction=((i%2?1:-1)*(delta>=0?1:-1));const endShift=Math.max(-9,Math.min(9,(projected-value)*24));const d=`M 3 ${y} C 115 ${y+direction*amplitude*.78}, 215 ${y-direction*amplitude}, 330 ${y+direction*amplitude*.42} S 560 ${y-direction*amplitude*.58}, ${w-8} ${y+endShift}`;const color=colors[i%colors.length];return `<g class="r-tide-line${name===dominantName?' selected':''}" data-r-pipe-wave="${esc(name)}" style="--tide:${i};--c:${color};--strength:${Math.max(.25,value)}"><path class="haze" d="${d}"/><path class="line" d="${d}"/><path class="glint" d="${d}"/><circle cx="3" cy="${y}" r="4"/><circle class="baseline-dot" cx="${w-8}" cy="${y+endShift}" r="3"/></g>`}).join('');
    return `<details class="r-tide-group" data-group="${group.id}" style="--rows:${entries.length}" open><summary><span>${esc(group.name)}</span><b>${entries.length} 项</b></summary><div class="r-tide-lanes"><div class="r-tide-labels">${labels}</div><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="${esc(group.name)}状态变化">${paths}</svg></div></details>`;
  }
  function baselineGroups(groups,baselines){return groups.map((group,index)=>`<details class="r-baseline-group" ${index===0?'open':''}><summary>${esc(group.name)}<b>${group.pipes.length} 项</b></summary><div>${group.pipes.map(pipe=>`<label><span>${esc(pipe.name||pipe.id)}</span><output>${rNum(baselines[pipe.id]).toFixed(2)}</output><input type="range" min="0" max="1" step="0.01" value="${rNum(baselines[pipe.id])}" data-r-baseline="${esc(pipe.id)}"/></label>`).join('')}</div></details>`).join('');}
  async function rRenderHormones(refresh=false){
    const fallback=hormoneFallback(); let stateValue=fallback.state,judge=fallback.judge,transitions=[];
    if(liveBackend.online){const data=await Promise.all([rApi('/api/xinchao/status',stateValue,{refresh}),rApi('/api/xinchao/judge',judge,{refresh}),rApi('/api/xinchao/linkages?limit=80',{items:[]},{refresh})]);stateValue=data[0];judge=data[1];transitions=data[2].items||[];}
    const groups=catalogGroups(stateValue);const allNames=groups.flatMap(group=>group.pipes.map(pipe=>pipe.id));stateValue.pipes={...Object.fromEntries(allNames.map(name=>[name,0])),...(stateValue.pipes||{})};const baselines={...R.preview.baselines,...(judge.baselines||{})};const halfLives={...PIPE_HALF_LIVES,...Object.fromEntries(groups.flatMap(group=>group.pipes.map(pipe=>[pipe.id,rNum(pipe.half_life_hours||PIPE_HALF_LIVES[pipe.id]||6)])))};const elapsed=rNum(stateValue.elapsed_seconds);const [dominantName,dominantValue]=dominant(stateValue);const selectedName=allNames.includes(R.selectedPipe)?R.selectedPipe:dominantName;R.selectedPipe=selectedName;const expression=stateValue.expression_state||{name:dominantName,score:dominantValue,components:[]};
    const contextual=(stateValue.event_contexts||[]).map(item=>({...item,details:{pipe_deltas:Object.fromEntries((item.signals||[]).map(signal=>[signal.state,signal.delta])),event_summary:item.event_summary||item.event_tag,evidence:(item.signals||[])[0]?.evidence||''}}));
    const recent=[...contextual,...transitions].filter(item=>Object.keys(transitionDetails(item).pipe_deltas||item.pipe_deltas||{}).length&&transitionTitle(item)).sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||''))).slice(0,6);
    rPage('hormones',`<div class="page r-page r-hormones"><header class="r-title r-hormone-title"><div><h1>内在潮汐</h1></div><div class="r-expression"><span>此刻形成的体验</span><strong>${esc(expression.name||dominantName)}</strong><b>${rNum(expression.score??dominantValue).toFixed(2)}</b><time>${esc(rDateTime(stateValue.as_of||''))}</time></div></header><div class="r-hormone-layout"><main><section class="r-tide-field"><div class="r-tide-head"><div><span>当前最强牵引 · ${esc(dominantName)} ${rNum(dominantValue).toFixed(2)}</span><h2>${allNames.length} 项状态在四个系统里共同流动</h2></div><p>当前值　→　随时间衰减　→　底色回归</p></div><div class="r-tide-groups">${groups.map(group=>hormoneGroup(group,stateValue,baselines,halfLives,elapsed,dominantName)).join('')}</div></section><section class="r-cause-paper"><span>最近联动</span>${recent.length?recent.map(item=>{const detail=transitionDetails(item);const deltas=detail.pipe_deltas||item.pipe_deltas||{};return `<article><time>${esc(rTime(item))}</time><strong>${esc(transitionTitle(item,detail))}</strong><small>${esc(Object.entries(deltas).map(([n,v])=>`${n} ${rNum(v)>=0?'+':''}${rNum(v).toFixed(2)}`).join(' · '))}</small>${detail.evidence?`<p>${esc(detail.evidence)}</p>`:''}</article>`}).join(''):rEmpty('还没有新的变化轨迹')}</section><section class="r-baselines"><header><div><span>BASELINE</span><h2>状态底色</h2></div><button id="rSaveBaselines" class="r-primary" type="button">保存底色</button></header>${baselineGroups(groups,baselines)}</section></main><aside class="r-pipe-inspector" id="rTideInspector">${pipeInspector(selectedName,stateValue,baselines,halfLives,transitions)}</aside></div></div>`);
    if(window.matchMedia('(max-width:900px)').matches){const dominantGroup=groups.find(group=>group.pipes.some(pipe=>pipe.id===dominantName))?.id;document.querySelectorAll('.r-tide-group').forEach(group=>group.open=group.dataset.group===dominantGroup);}
    const selectPipe=name=>{R.selectedPipe=name;document.querySelectorAll('[data-r-pipe],[data-r-pipe-wave]').forEach(node=>node.classList.toggle('selected',(node.dataset.rPipe||node.dataset.rPipeWave)===name));const inspector=document.querySelector('#rTideInspector');if(inspector)inspector.innerHTML=pipeInspector(name,stateValue,baselines,halfLives,transitions);};
    document.querySelectorAll('[data-r-pipe]').forEach(button=>button.addEventListener('click',()=>selectPipe(button.dataset.rPipe)));
    document.querySelectorAll('[data-r-baseline]').forEach(input=>input.addEventListener('input',()=>input.previousElementSibling.value=rNum(input.value).toFixed(2)));
    document.querySelector('#rSaveBaselines')?.addEventListener('click',async()=>{const current=await rApi('/api/xinchao/judge',judge,{refresh:true});const next={...(current.baselines||{})};document.querySelectorAll('[data-r-baseline]').forEach(i=>next[i.dataset.rBaseline]=rNum(i.value));try{await saveJudgeConfig({...current,baselines:next});rInvalidate();liveNotice('状态底色已保存。');}catch(e){liveNotice(e.message,true);}});
    if(liveBackend.online)window.__hormonePoll=window.setTimeout(()=>{if(state.selected==='hormones')rRenderHormones(true)},15000);
  }

  function dispositionFallback(){return {days:30,tendency:{name:'更愿意靠近',score:.54,delta:.08},evidence:[{created_at:'2026-08-21 20:58',source:'信箱',title:'讨论了记忆与身份',summary:'相似情境里更主动地把话继续下去。',effects:{关系网:.06,情绪:.04}},{created_at:'2026-08-23 04:10',source:'念痕',title:'留下了一句私密想法',summary:'反复出现的靠近倾向被继续保留。',effects:{想靠近:.08}}]};}
  async function rRenderPersonality(){
    const data=await rApi('/api/disposition?days=30',dispositionFallback());const t=data.tendency||data.current||data.strongest||{};const evidence=data.evidence||data.items||data.formation_path||[];
    rPage('personality',`<div class="page r-page r-personality"><header class="r-title"><h1>性格轨迹</h1></header><section class="r-disposition-current"><div><span>近 ${rNum(data.days||30)} 天形成的倾向</span><h2>${esc(t.name||t.label||'仍在形成')}</h2><p>${esc(data.formation_reason||t.summary||t.description||'')}</p></div><strong>${rNum(t.score??t.strength).toFixed(2)}<small>${rNum(data.evidence_count||evidence.length)} 条证据</small></strong></section><section class="r-formation"><h2>形成它的具体证据</h2>${evidence.length?evidence.map(item=>`<article><time>${esc(rTime(item))}</time><i></i><div><small>${esc(item.source||item.source_tool||'一次经历')}</small><h3>${esc(rTitle(item))}</h3><p>${esc(item.reason||rText(item)||item.summary||'')}</p><span>${esc(Object.entries(item.effects||item.deltas||{}).map(([n,v])=>`${n} ${rNum(v)>=0?'+':''}${rNum(v).toFixed(2)}`).join(' · '))}</span></div></article>`).join(''):rEmpty('这个月还没有形成稳定倾向')}</section></div>`);
  }

  const axisMeta={X:['时间','何时发生、先后与持续'],Y:['关系','人与主题怎样靠近或疏远'],Z:['事实','事情如何被更新与确认'],E:['情绪','感受怎样牵动内在波动'],M:['沉淀','内容怎样留下、活跃或回落']};
  const sourceNames={memory:'记忆',mailbox:'信箱',trace:'念痕',thought:'心念',task:'未竟',fact:'事实'};
  function rBrainFallback(){return {count:5,dimensions:{X:{name:'时间脉络',score:.18,delta:0,reason:'等待新的写入'},Y:{name:'关系牵引',score:.18,delta:0,reason:'等待新的写入'},Z:{name:'事实演化',score:.18,delta:0,reason:'等待新的写入'},E:{name:'情绪回响',score:.18,delta:0,reason:'等待新的写入'},M:{name:'记忆沉淀',score:.18,delta:0,reason:'等待新的写入'}},nodes:[],current_state:{pipes:R.preview.state.pipes}};}
  async function rRenderCoordinates(query=''){
    const suffix=`?limit=12${query?`&q=${encodeURIComponent(query)}`:''}`;
    const data=await rApi(`/api/brain/context${suffix}`,rBrainFallback());
    (data.nodes||[]).forEach(item=>{item.time=rDateTime(item.time)});
    const axes=['X','Y','Z','E','M'];
    const rows=axes.map((axis,i)=>{const dim=data.dimensions?.[axis]||{},meta=axisMeta[axis],strength=Math.max(0,Math.min(1,rNum(dim.score)));return `<button data-r-axis="${axis}" class="${i===0?'active':''}" type="button"><i>${axis}</i><span><b>${esc(dim.name||meta[0])}</b><small>${esc(dim.reason||meta[1])}</small></span><strong>${strength.toFixed(2)}</strong></button>`}).join('');
    const paths=axes.map((axis,i)=>{const strength=Math.max(0,Math.min(1,rNum(data.dimensions?.[axis]?.score))),y=35+i*58,end=20+strength*710;return `<g class="r-axis-wave" data-r-axis-wave="${axis}" style="--axis:${i}"><path d="M10 ${y} H750"/><path d="M10 ${y} H${end}"/><circle cx="${end}" cy="${y}" r="${4+strength*3}"/></g>`}).join('');
    const nodes=(data.nodes||[]).map(item=>`<article class="r-brain-node" data-r-node-dimensions="${esc((item.dimensions||[]).join(','))}"><header><span>${esc(item.source_label||sourceNames[item.source]||item.source||'记录')}</span><time>${esc(item.time||'')}</time></header><h3>${esc(item.title||'一条内容')}</h3><p>${esc(item.reason||item.text||'')}</p><footer>${(item.dimension_labels||item.dimensions||[]).map(label=>`<i>${esc(axisMeta[label]?.[0]||label)}</i>`).join('')}</footer></article>`).join('');
    rPage('coordinates',`<div class="page r-page r-coordinates"><header class="r-title r-brain-title"><div><h1>心智经纬</h1></div><form id="rBrainSearch"><input name="query" value="${esc(query)}" placeholder="寻找一段记忆、一个人或一件事"/><button class="r-button" type="submit">寻找</button></form></header><section class="r-coordinate-field"><div class="r-axis-list">${rows}</div><svg viewBox="0 0 760 302" preserveAspectRatio="none" aria-label="五条心智经纬">${paths}</svg></section><section class="r-brain-stream" id="rBrainStream">${nodes||rEmpty(query?'没有找到相连的内容':'还没有形成心智经纬')}</section></div>`);
    document.querySelector('#rBrainSearch')?.addEventListener('submit',event=>{event.preventDefault();rRenderCoordinates(new FormData(event.currentTarget).get('query')?.toString().trim()||'')});
    document.querySelectorAll('[data-r-axis]').forEach(btn=>btn.addEventListener('click',()=>{const axis=btn.dataset.rAxis;document.querySelectorAll('[data-r-axis]').forEach(x=>x.classList.toggle('active',x===btn));document.querySelectorAll('[data-r-axis-wave]').forEach(x=>x.classList.toggle('active',x.dataset.rAxisWave===axis));document.querySelectorAll('[data-r-node-dimensions]').forEach(node=>node.classList.toggle('muted',!node.dataset.rNodeDimensions.split(',').includes(axis)));}));
  }

  function settingsMarkup(judge,behavior){
    return `<div class="page r-page r-settings"><header class="r-title"><h1>设置与安全</h1></header><section class="r-settings-grid"><article><h2>推送</h2><label><span>推送显示名称</span><input id="rPushTitle" value="${esc(behavior.push_title||'Clio')}" maxlength="60"/></label><button class="r-primary" id="rSavePush" type="button">保存推送名称</button></article><article><h2>修改管理密码</h2><label><span>当前密码</span><input id="rCurrentPassword" type="password" autocomplete="current-password"/></label><label><span>新密码</span><input id="rNewPassword" type="password" autocomplete="new-password"/></label><label><span>再次输入新密码</span><input id="rConfirmPassword" type="password" autocomplete="new-password"/></label><button class="r-primary" id="rChangePassword" type="button">保存新密码</button></article><article class="wide"><h2>机器判定规则</h2><textarea id="judgeCustomRules" rows="7">${esc(judge.custom_rules||'')}</textarea></article><article class="wide"><h2>AI 口吻</h2><textarea id="judgeProxyVoice" rows="6">${esc(judge.proxy_voice||'')}</textarea></article><article class="wide"><h2>暗涌生成口吻与人物性格</h2><textarea id="judgeDarkflowRules" rows="7">${esc(judge.darkflow_rules||'')}</textarea></article><article class="wide"><div class="r-settings-head"><h2>AI 已知人物信息</h2><button id="addJudgeRelation" type="button">＋ 添加人物</button></div><div id="judgeRelations">${judgeRelationFields(judge.relations||[])}</div></article><article class="wide r-setting-save"><button class="r-primary" id="saveJudgeConfig" type="button">保存规则与人物信息</button><span id="judgeSaveStatus"></span></article></section></div>`;
  }
  async function rRenderSettings(){
    const [judge,behavior]=await Promise.all([rApi('/api/xinchao/judge',liveFallbackJudge()),rApi('/api/behavior/settings',{push_title:'Clio',configured:false})]);rPage('settings',settingsMarkup(judge,behavior));
    document.querySelector('#rSavePush')?.addEventListener('click',async()=>{try{await liveApi('/api/behavior/settings',{method:'PUT',body:JSON.stringify({push_title:document.querySelector('#rPushTitle').value.trim()})});liveNotice('推送名称已保存。');}catch(e){liveNotice(e.message,true);}});
    document.querySelector('#rChangePassword')?.addEventListener('click',async()=>{try{await liveApi('/api/auth/change-password',{method:'POST',body:JSON.stringify({current_password:document.querySelector('#rCurrentPassword').value,new_password:document.querySelector('#rNewPassword').value,confirm_password:document.querySelector('#rConfirmPassword').value})});liveNotice('管理密码已修改。');document.querySelectorAll('#rCurrentPassword,#rNewPassword,#rConfirmPassword').forEach(i=>i.value='');}catch(e){liveNotice(e.message,true);}});
    document.querySelector('#addJudgeRelation')?.addEventListener('click',()=>document.querySelector('#judgeRelations')?.insertAdjacentHTML('beforeend',judgeRelationFields([{}])));
    document.querySelector('#judgeRelations')?.addEventListener('click',e=>e.target.closest('[data-remove-relation]')?.closest('.judge-relation-card')?.remove());
    document.querySelector('#saveJudgeConfig')?.addEventListener('click',async()=>{try{await saveJudgeConfig();document.querySelector('#judgeSaveStatus').textContent='已保存';}catch(e){document.querySelector('#judgeSaveStatus').textContent=e.message;}});
  }

  function rTimelineVersion(version,index,total){
    const current=Boolean(version.is_current)||index===total-1;
    const stage=total===1?'首次记录':current?'当前事实':index===0?'最初事实':`第 ${index+1} 次变化`;
    return `<article class="r-version-node${current?' current':''}"><i aria-hidden="true"></i><time>${esc(rDateTime(version.effective_date||version.recorded_at||''))}</time><small>${stage}</small><strong>${esc(version.fact_value||version.value||'')}</strong>${version.source_excerpt?`<p>${esc(version.source_excerpt)}</p>`:''}<span>${esc(version.source_type==='manual'?'手动记录':version.source_type||'记忆来源')}</span></article>`;
  }
  function rTimelineJourney(group,index){
    const versions=[...(group.versions||[])].sort((a,b)=>String(a.effective_date||'').localeCompare(String(b.effective_date||'')));
    const latest=group.current||versions[versions.length-1]||{};
    return `<article class="r-fact-journey" style="--journey-index:${index}"><div class="r-journey-anchor" aria-hidden="true"><i></i></div><header><div><time>${esc(rDateTime(latest.effective_date||latest.recorded_at||''))}</time><h2>${esc(group.fact_label||group.fact_key||'未命名事实')}</h2><span>${versions.length>1?`${versions.length} 个事实版本`:'从这里开始记录'}</span></div><button class="r-button" data-r-timeline-edit="${esc(group.fact_key||'')}" data-r-timeline-label="${esc(group.fact_label||group.fact_key||'')}" data-r-timeline-current="${esc(latest.fact_value||'')}" type="button">修改当前事实</button></header><div class="r-version-flow${versions.length===1?' single':''}">${versions.map((version,versionIndex)=>rTimelineVersion(version,versionIndex,versions.length)).join('')}</div></article>`;
  }
  function rTimelineCandidates(items=[]){
    if(!items.length)return '';
    return `<section class="r-timeline-candidates"><header><span>等待确认的变化</span><strong>${items.length}</strong></header>${items.map(item=>`<article><div><time>${esc(rDateTime(item.effective_date||''))}</time><small>${esc(item.fact_label||item.fact_key||'')}</small><strong>${esc(item.previous_value||'尚未记录')} <i>→</i> ${esc(item.proposed_value||'')}</strong><p>${esc(item.reason||item.source_excerpt||'')}</p></div><button data-r-candidate-confirm="${esc(item.candidate_id)}" type="button">确认变化</button><button class="r-button" data-r-candidate-ignore="${esc(item.candidate_id)}" type="button">暂不采用</button></article>`).join('')}</section>`;
  }
  function rTimelineFallback(){
    const version=(value,date,current=false,excerpt='')=>({fact_value:value,effective_date:date,is_current:current,source_type:'manual',source_excerpt:excerpt});
    return {items:[
      {fact_key:'health',fact_label:'身体状况',versions:[version('需要休息','2026-08-18',false,'记录了最初的不适。'),version('已经预约检查','2026-08-20',false,'安排了进一步确认。'),version('状态正在恢复','2026-08-23',true,'最近一次更新。')]},
      {fact_key:'interview',fact_label:'面试结果',versions:[version('等待结果','2026-08-19',false),version('已经通过','2026-08-22',true,'结果发生了变化。')]},
      {fact_key:'coupon',fact_label:'兑换券有效期',versions:[version('月底到期','2026-08-21',true)]}
    ],candidates:[]};
  }
  async function rRenderTimeline(search=''){
    const suffix=search?`?limit=100&search=${encodeURIComponent(search)}`:'?limit=100';
    const data=await rApi(`/api/timeline${suffix}`,rTimelineFallback());
    const groups=[...(data.items||[])].sort((a,b)=>String((a.current||{}).effective_date||'').localeCompare(String((b.current||{}).effective_date||'')));
    rPage('timeline',`<div class="page r-page r-timeline"><header class="r-title r-timeline-title"><h1>事实时间线</h1><form id="rTimelineSearch"><input name="search" value="${esc(search)}" placeholder="寻找一件事的变化"/><button class="r-button" type="submit">寻找</button></form><button class="r-primary" id="rTimelineAdd" type="button">＋ 新增事实</button></header>${rTimelineCandidates(data.candidates||[])}<section class="r-timeline-river">${groups.length?groups.map(rTimelineJourney).join(''):rEmpty(search?'没有找到这件事':'还没有事实记录')}</section></div>`);
    document.querySelector('#rTimelineSearch')?.addEventListener('submit',event=>{event.preventDefault();rRenderTimeline(new FormData(event.currentTarget).get('search')?.toString().trim()||'')});
    document.querySelector('#rTimelineAdd')?.addEventListener('click',()=>rAdd('timeline'));
    document.querySelectorAll('[data-r-timeline-edit]').forEach(button=>button.addEventListener('click',async()=>{const value=prompt('现在的事实是什么',button.dataset.rTimelineCurrent||'');if(!value)return;try{await liveApi('/api/timeline',{method:'POST',body:JSON.stringify({fact:button.dataset.rTimelineLabel,value,effective_date:new Date().toISOString().slice(0,10),source_excerpt:'管理页面更正'})});liveNotice('新的事实版本已接到时间线上。');rRenderTimeline(search);}catch(error){liveNotice(error.message,true)}}));
    document.querySelectorAll('[data-r-candidate-confirm]').forEach(button=>button.addEventListener('click',async()=>{try{await liveApi(`/api/timeline/candidates/${button.dataset.rCandidateConfirm}/confirm`,{method:'POST'});liveNotice('变化已确认并接入时间线。');rRenderTimeline(search);}catch(error){liveNotice(error.message,true)}}));
    document.querySelectorAll('[data-r-candidate-ignore]').forEach(button=>button.addEventListener('click',async()=>{try{await liveApi(`/api/timeline/candidates/${button.dataset.rCandidateIgnore}`,{method:'DELETE'});liveNotice('这条候选变化已暂不采用。');rRenderTimeline(search);}catch(error){liveNotice(error.message,true)}}));
  }

  const endpointFor={timeline:'/api/timeline?limit=100',mailbox:'/api/mailbox/messages?limit=100',tasks:'/api/tasks?limit=100',treasury:'/api/treasury/entries?limit=100',thoughts:'/api/mind/traces?limit=100',mind:'/api/mind/thoughts?limit=100',darkflow:'/api/xinchao/darkflow',behavior:'/api/behavior/actions?limit=100',resonance:'/api/xinchao/resonance',toolbox:'/api/toolbox'};
  const toolboxFallback={items:[
    {id:'search',name:'智能搜索'},{id:'timeline',name:'事实时间线'},
    {id:'tasks',name:'未竟'},{id:'treasury',name:'AI 小金库'},{id:'mailbox',name:'信箱'},
    {id:'darkflow',name:'暗涌'},{id:'thoughts',name:'念痕'},{id:'mind',name:'心念'},
    {id:'resonance',name:'共振与张力'},{id:'behavior',name:'行为与推送'},
    {id:'personality',name:'性格轨迹'},{id:'coordinates',name:'心智经纬'},{id:'settings',name:'设置与安全'}
  ]};
  let rWarmed=false;
  function rWarmRoutes(){
    if(rWarmed||!liveBackend.online)return;
    rWarmed=true;
    const hormone=hormoneFallback();
    const queue=[
      ['/api/timeline?limit=100',rTimelineFallback()],
      ['/api/xinchao/status',hormone.state],
      ['/api/xinchao/judge',hormone.judge],
      ['/api/xinchao/linkages?limit=80',{items:[]}],
      ['/api/disposition?days=30',dispositionFallback()],
      ['/api/mailbox/messages?limit=100',{items:[]}],
      ['/api/tasks?limit=100',{items:[]}],
      ['/api/mind/traces?limit=100',{items:[]}],
      ['/api/mind/thoughts?limit=100',{items:[]}],
      ['/api/xinchao/darkflow',{available:false,item:null}],
      ['/api/behavior/actions?limit=100',{items:[],candidates:[]}],
      ['/api/toolbox',{items:[]}],
      ['/api/buckets?filter=all',{items:[]}],
      ['/api/topics',{tree:[]}],
      ['/api/brain/context?limit=12',rBrainFallback()]
    ];
    const warmBatch=()=>{const batch=queue.splice(0,4);if(!batch.length)return;Promise.allSettled(batch.map(next=>rApi(next[0],next[1],{silent:true}))).finally(()=>window.setTimeout(warmBatch,45));};
    const begin=()=>window.setTimeout(warmBatch,180);
    if('requestIdleCallback' in window)window.requestIdleCallback(begin,{timeout:1800});else window.setTimeout(begin,1100);
  }
  const editableModule={timeline:true,mailbox:true,tasks:true,treasury:true,thoughts:false,darkflow:false,behavior:false,resonance:false,toolbox:false};
  function flattenModule(target,data){
    if(target==='darkflow')return data.item?[data.item]:[];
    if(target==='timeline')return [...(data.items||data.facts||[]),...(data.candidates||[])];
    return data.items||data.candidates||[];
  }
  const rPhaseName={active:'当前仍有活动',silence:'正在等待沉默',absence:'静默沉淀中',darkflow_pending:'暗涌等待交付',delivered:'本轮已经交付',idle:'等待下一次写入'};
  function rLoadingPage(target){
    rPage(target,`<div class="page r-page r-module r-module-${target}"><header class="r-title"><h1>${esc(mapMeta[target]?.label||target)}</h1></header><div class="r-soft-loader" aria-label="正在读取"><i></i><i></i><i></i></div></div>`);
  }
  async function rRenderTraces(target='thoughts'){
    const isTrace=target==='thoughts';const data=await rApi(endpointFor[target],{items:[]});const items=data.items||[];
    rPage(target,`<div class="page r-page r-module r-module-${target}"><header class="r-title"><h1>${isTrace?'念痕':'心念'}</h1></header><section class="r-trace-stream">${items.length?items.map(item=>`<article><time>${esc(rDateTime(item.last_seen||item.first_seen||item.created_at))}</time><i></i><div><span>${esc(item.event_tag||(isTrace?'当下念痕':'浮现的心念'))}</span><blockquote>${esc(rText(item))}</blockquote><small>${esc(Object.entries(item.linkage||{}).map(([name,value])=>`${name} ${rNum(value)>=0?'+':''}${rNum(value).toFixed(2)}`).join(' · '))}</small>${isTrace&&item.updated_at&&item.first_seen&&item.updated_at!==item.first_seen?'<em>已由写入它的 AI 修订，旧版本仍保留</em>':''}${!isTrace?`<footer><button class="r-button" data-r-mind-resolve="${esc(item.canonical_tag||'')}" type="button">放下</button><button class="r-button" data-r-mind-delete="${esc(item.canonical_tag||'')}" type="button">删除</button></footer>`:''}</div></article>`).join(''):rEmpty(isTrace?'当前还没有留下念痕':'沉默里还没有浮现新的心念')}</section></div>`);
    if(!isTrace){document.querySelectorAll('[data-r-mind-resolve]').forEach(button=>button.addEventListener('click',async()=>{try{await liveApi(`/api/mind/thoughts/${encodeURIComponent(button.dataset.rMindResolve)}/resolve`,{method:'POST'});liveNotice('这条心念已经放下。');rRenderTraces('mind');}catch(error){liveNotice(error.message,true)}}));document.querySelectorAll('[data-r-mind-delete]').forEach(button=>button.addEventListener('click',async()=>{if(!confirm('删除这条心念？'))return;try{await liveApi(`/api/mind/thoughts/${encodeURIComponent(button.dataset.rMindDelete)}`,{method:'DELETE'});liveNotice('这条心念已经删除。');rRenderTraces('mind');}catch(error){liveNotice(error.message,true)}}));}
  }
  async function rRenderDarkflow(){
    const [data,status]=await Promise.all([rApi(endpointFor.darkflow,{available:false,item:null}),rApi('/api/xinchao/status',{timing:{}})]);const item=data.item||null;const timing=status.timing||{};
    rPage('darkflow',`<div class="page r-page r-module r-module-darkflow"><header class="r-title"><h1>暗涌</h1><strong class="r-phase">${esc(rPhaseName[timing.phase]||timing.phase||'等待下一轮')}</strong></header><section class="r-darkflow-clock"><article><span>最后一次活动</span><strong>${esc(rDateTime(timing.last_activity_at))}</strong></article><article><span>进入静默</span><strong>${esc(rDateTime(timing.absence_due_at))}</strong></article><article><span>预计形成暗涌</span><strong>${esc(rDateTime(timing.next_darkflow_due_at))}</strong></article><article><span>交付状态</span><strong>${esc(timing.delivery_status==='delivered'?'已交付':timing.delivery_status==='pending'?'等待下一次 AI 开机交付':'尚未形成')}</strong></article></section>${item?`<article class="r-darkflow-letter"><time>${esc(rDateTime(item.generated_at||item.created_at))}</time><blockquote>${esc(item.content||'')}</blockquote><footer><span>第 ${rNum(item.stage_index||1)} 次沉淀</span><span>${item.status==='delivered'?'已交付':'待交付'}</span></footer></article>`:`<section class="r-darkflow-await"><i></i><strong>${esc(rPhaseName[timing.phase]||'等待沉默发生')}</strong><time>${esc(rDateTime(timing.next_darkflow_due_at||timing.absence_due_at))}</time></section>`}</div>`);
  }
  async function rRenderTasks(){
    const data=await rApi(endpointFor.tasks,{items:[],counts:{}});const items=data.items||[];const counts=data.counts||{};
    const statusName={open:'进行中',completed:'已完成',cancelled:'已取消'};
    rPage('tasks',`<div class="page r-page r-module r-module-tasks"><header class="r-title"><h1>未竟</h1><button class="r-primary" data-r-add="tasks" type="button">＋ 新增未竟</button></header><div class="r-task-counts"><span>进行中 ${rNum(counts.open)}</span><span>已完成 ${rNum(counts.completed)}</span><span>已取消 ${rNum(counts.cancelled)}</span></div><section class="r-task-list">${items.length?items.map(item=>`<article class="r-task-item is-${esc(item.status||'open')}"><header><span>${esc(statusName[item.status]||item.status||'进行中')}</span><time>${esc(rTime(item))}</time></header><h2>${esc(rTitle(item))}</h2><p>${esc(rText(item))}</p><small>重要度 ${rNum(item.importance||3)} / 5</small><footer><button class="r-button" data-r-action="edit" data-r-id="${esc(item.task_id)}" type="button">修改</button><button class="r-button" data-r-action="history" data-r-id="${esc(item.task_id)}" type="button">历史</button>${item.status==='open'?`<button data-r-task-status="completed" data-r-id="${esc(item.task_id)}" type="button">完成</button><button class="r-button" data-r-task-status="cancelled" data-r-id="${esc(item.task_id)}" type="button">取消</button>`:`<button data-r-task-status="open" data-r-id="${esc(item.task_id)}" type="button">重新开始</button>`}<button class="r-button danger" data-r-task-delete="${esc(item.task_id)}" type="button">删除</button></footer></article>`).join(''):rEmpty('没有需要继续的事情')}</section></div>`);
    document.querySelector('[data-r-add="tasks"]')?.addEventListener('click',()=>rAdd('tasks'));
    document.querySelectorAll('[data-r-action="edit"]').forEach(btn=>btn.addEventListener('click',()=>rEdit('tasks',btn.dataset.rId,items)));
    document.querySelectorAll('[data-r-action="history"]').forEach(btn=>btn.addEventListener('click',()=>rHistory('tasks',btn.dataset.rId)));
    document.querySelectorAll('[data-r-task-status]').forEach(btn=>btn.addEventListener('click',async()=>{try{await liveApi(`/api/tasks/${btn.dataset.rId}`,{method:'PUT',body:JSON.stringify({status:btn.dataset.rTaskStatus})});rInvalidate();liveNotice(btn.dataset.rTaskStatus==='completed'?'这件事已经完成。':btn.dataset.rTaskStatus==='cancelled'?'这件事已取消。':'这件事重新回到未竟。');rRenderTasks();}catch(error){liveNotice(error.message,true)}}));
    document.querySelectorAll('[data-r-task-delete]').forEach(btn=>btn.addEventListener('click',async()=>{if(!confirm('彻底删除这条未竟？历史仍保留在备份中。'))return;try{await liveApi(`/api/tasks/${btn.dataset.rTaskDelete}`,{method:'DELETE',body:JSON.stringify({confirm_task_id:Number(btn.dataset.rTaskDelete)})});rInvalidate();liveNotice('这条未竟已经删除。');rRenderTasks();}catch(error){liveNotice(error.message,true)}}));
  }
  async function rRenderTreasury(){
    const data=await rApi(endpointFor.treasury,{items:[],summary:{symbol:'¥',balance:'0.00',total_income:'0.00',total_expense:'0.00'}});const items=data.items||[];const sum=data.summary||{};const symbol=sum.symbol||'¥';
    rPage('treasury',`<div class="page r-page r-module r-module-treasury"><header class="r-title"><h1>AI 小金库</h1><div class="r-treasury-actions"><button class="r-primary" data-r-money-add="income" type="button">＋ 收入</button><button class="r-primary" data-r-money-add="expense" type="button">－ 支出</button></div></header><section class="r-money-summary"><article><span>现在共有</span><strong>${esc(symbol)} ${esc(sum.balance||'0.00')}</strong></article><article><span>累计收入</span><strong>${esc(symbol)} ${esc(sum.total_income||'0.00')}</strong></article><article><span>累计支出</span><strong>${esc(symbol)} ${esc(sum.total_expense||'0.00')}</strong></article></section><section class="r-money-ledger">${items.length?items.map(item=>`<article class="is-${esc(item.entry_type)}"><time>${esc(rDateTime(item.occurred_at||item.created_at))}</time><div><span>${item.entry_type==='income'?'收入':'支出'}</span><h2>${esc(item.reason||'一笔记录')}</h2></div><strong>${item.entry_type==='income'?'+':'−'} ${esc(symbol)} ${esc(item.amount||'0.00')}</strong><footer><button class="r-button" data-r-action="edit" data-r-id="${esc(item.entry_id)}" type="button">修改</button><button class="r-button" data-r-action="history" data-r-id="${esc(item.entry_id)}" type="button">历史</button><button class="r-button danger" data-r-money-delete="${esc(item.entry_id)}" type="button">删除</button></footer></article>`).join(''):rEmpty('小金库还没有收支记录')}</section></div>`);
    document.querySelectorAll('[data-r-money-add]').forEach(btn=>btn.addEventListener('click',async()=>{const reason=prompt(btn.dataset.rMoneyAdd==='income'?'这笔收入是什么':'这笔支出是什么');if(!reason)return;const amount=prompt('金额');if(!amount)return;try{await liveApi('/api/treasury/entries',{method:'POST',body:JSON.stringify({entry_type:btn.dataset.rMoneyAdd,amount,reason,occurred_at:new Date().toISOString()})});rInvalidate();liveNotice('这笔记录已经写入小金库。');rRenderTreasury();}catch(error){liveNotice(error.message,true)}}));
    document.querySelectorAll('[data-r-action="edit"]').forEach(btn=>btn.addEventListener('click',()=>rEdit('treasury',btn.dataset.rId,items)));
    document.querySelectorAll('[data-r-action="history"]').forEach(btn=>btn.addEventListener('click',()=>rHistory('treasury',btn.dataset.rId)));
    document.querySelectorAll('[data-r-money-delete]').forEach(btn=>btn.addEventListener('click',async()=>{if(!confirm('删除这笔收支记录？'))return;try{await liveApi(`/api/treasury/entries/${btn.dataset.rMoneyDelete}`,{method:'DELETE',body:JSON.stringify({confirm_entry_id:Number(btn.dataset.rMoneyDelete)})});rInvalidate();liveNotice('这笔记录已经删除。');rRenderTreasury();}catch(error){liveNotice(error.message,true)}}));
  }
  async function rRenderBehavior(){
    const data=await rApi(endpointFor.behavior,{items:[],candidates:[]});const actions=data.items||[];const candidates=data.candidates||[];
    const pending=rNum(data.pending?.count||0);
    rPage('behavior',`<div class="page r-page r-module r-module-behavior"><header class="r-title"><h1>行为与推送</h1><div><button class="r-button" data-map-target="settings" type="button">推送名称：${esc(data.push_title||'Clio')} ↗</button>${pending?`<button class="r-primary" id="rAcknowledgePush" type="button">我看到了 · ${pending}</button>`:''}</div></header><section class="r-behavior-status"><strong>${data.configured?'推送通道已连接':'推送通道未连接'}</strong><span>${esc(data.mode||'')}</span></section><section class="r-behavior-columns"><div><h2>正在判断</h2>${candidates.length?candidates.map(item=>rRecord(item,'behavior',false)).join(''):rEmpty('当前没有等待判断的推送')}</div><div><h2>最近行为</h2>${actions.length?actions.map(item=>rRecord(item,'behavior',false)).join(''):rEmpty('还没有行为记录')}</div></section></div>`);bindMap();
    document.querySelector('#rAcknowledgePush')?.addEventListener('click',async()=>{try{await liveApi('/api/behavior/acknowledge',{method:'POST',body:'{}'});rInvalidate();liveNotice('已经记下你看到了。');rRenderBehavior();}catch(error){liveNotice(error.message,true)}});
  }
  async function rRenderResonance(){
    const data=await rApi(endpointFor.resonance,{items:[],strongest:{},counterweight:{},balance:0});
    const pull=data.strongest||data.tension?.strongest||{};const hold=data.counterweight||data.tension?.counterweight||{};const balance=rNum(data.balance??data.tension?.balance);
    const links=data.items||[];
    rPage('resonance',`<div class="page r-page r-resonance"><header class="r-title"><h1>共振与张力</h1></header><section class="r-resonance-field"><article class="r-resonance-pull"><span>正在靠近</span><h2>${esc(pull.name||'平静')}</h2><strong>${rNum(pull.value).toFixed(2)}</strong></article><div class="r-resonance-knot" style="--balance:${Math.max(-1,Math.min(1,balance))}"><i></i><i></i><i></i><b></b></div><article class="r-resonance-hold"><span>正在牵制</span><h2>${esc(hold.name||'平衡')}</h2><strong>${rNum(hold.value).toFixed(2)}</strong></article></section><section class="r-resonance-memory"><header><span>此刻被什么牵动</span><strong>张力 ${balance>=0?'+':''}${balance.toFixed(2)}</strong></header>${links.length?links.map(item=>`<article><i></i><div><h3>${esc(rTitle(item))}</h3><p>${esc(rText(item)||item.why||'')}</p><small>${esc(item.why||'')}</small></div><strong>${rNum(item.score).toFixed(2)}</strong></article>`).join(''):rEmpty('还没有形成明显共振')}</section></div>`);
  }
  async function rRenderToolbox(){
    const data=await rApi(endpointFor.toolbox,toolboxFallback);const items=data.items?.length?data.items:toolboxFallback.items;
    rPage('toolbox',`<div class="page r-page r-module r-module-toolbox"><header class="r-title"><h1>工具箱</h1></header><section class="r-tool-grid">${items.map(item=>`<button data-map-target="${esc(item.id)}" type="button"><i>${esc({search:'⌕',timeline:'⌁',calendar:'◷',tasks:'✓',treasury:'◌',mailbox:'□',darkflow:'∿',thoughts:'✧',mind:'☾',resonance:'◎',behavior:'↗',personality:'✦',coordinates:'⌘',settings:'⚙'}[item.id]||'·')}</i><span><strong>${esc(item.name)}</strong><small>${esc(item.description||'')}</small></span><em>↗</em></button>`).join('')}</section></div>`);bindMap();
  }
  async function rRenderModule(target){
    if(target==='thoughts'||target==='mind')return rRenderTraces(target);
    if(target==='darkflow')return rRenderDarkflow();
    if(target==='tasks')return rRenderTasks();
    if(target==='treasury')return rRenderTreasury();
    if(target==='behavior')return rRenderBehavior();
    if(target==='resonance')return rRenderResonance();
    if(target==='toolbox')return rRenderToolbox();
    const data=await rApi(endpointFor[target],{items:[]});const items=flattenModule(target,data);const editable=editableModule[target];
    const add=editable&&['timeline','tasks','treasury'].includes(target)?`<button class="r-primary" data-r-add="${target}" type="button">＋ 新增${esc(mapMeta[target].label)}</button>`:'';
    rPage(target,`<div class="page r-page r-module r-module-${target}"><header class="r-title"><h1>${esc(mapMeta[target].label)}</h1>${add}</header><section class="r-record-list">${items.length?items.map(item=>rRecord(item,target,editable)).join(''):rEmpty('这里还没有记录')}</section></div>`);
    document.querySelector('[data-r-add]')?.addEventListener('click',()=>rAdd(target));
    document.querySelectorAll('[data-r-action="edit"]').forEach(btn=>btn.addEventListener('click',()=>rEdit(target,btn.dataset.rId,items)));
    document.querySelectorAll('[data-r-action="history"]').forEach(btn=>btn.addEventListener('click',()=>rHistory(target,btn.dataset.rId)));
  }
  async function rAdd(target){
    try{
      if(target==='timeline'){const fact=prompt('事实名称');const value=prompt('当前事实');if(!fact||!value)return;await liveApi('/api/timeline',{method:'POST',body:JSON.stringify({fact,value,effective_date:new Date().toISOString().slice(0,10),source_excerpt:'管理页面手动记录'})});}
      if(target==='tasks'){const title=prompt('未竟名称');if(!title)return;const details=prompt('具体内容')||'';await liveApi('/api/tasks',{method:'POST',body:JSON.stringify({title,details,importance:3})});}
      if(target==='treasury'){const reason=prompt('这笔记录是什么');const amount=prompt('金额');if(!reason||!amount)return;await liveApi('/api/treasury/entries',{method:'POST',body:JSON.stringify({entry_type:'income',amount,reason,occurred_at:new Date().toISOString()})});}
      liveNotice('已保存。');rRenderModule(target);
    }catch(e){liveNotice(e.message,true)}
  }
  async function rEdit(target,id,items){
    const item=items.find(x=>String(x.id??x.message_id??x.task_id??x.entry_id??x.fact_key??x.candidate_id)===String(id));if(!item)return;
    try{
      if(target==='mailbox'){const message=prompt('修改信箱内容',rText(item));if(!message)return;await liveApi(`/api/mailbox/messages/${id}`,{method:'PUT',body:JSON.stringify({message})});}
      if(target==='tasks'){const title=prompt('未竟名称',rTitle(item));if(!title)return;const details=prompt('具体内容',rText(item))||'';await liveApi(`/api/tasks/${id}`,{method:'PUT',body:JSON.stringify({title,details,importance:rNum(item.importance||3),status:item.status||'open'})});}
      if(target==='treasury'){const reason=prompt('说明',item.reason||'');const amount=prompt('金额',item.amount||'');if(!reason||!amount)return;await liveApi(`/api/treasury/entries/${id}`,{method:'PUT',body:JSON.stringify({reason,amount,entry_type:item.entry_type||'income',occurred_at:item.occurred_at||''})});}
      if(target==='timeline'){const value=prompt('当前事实',rText(item));if(!value)return;await liveApi('/api/timeline',{method:'POST',body:JSON.stringify({fact:rTitle(item),value,effective_date:new Date().toISOString().slice(0,10),source_excerpt:'管理页面更正'})});}
      liveNotice('修改已保存，并保留历史。');rRenderModule(target);
    }catch(e){liveNotice(e.message,true)}
  }
  async function rHistory(target,id){
    const path=target==='mailbox'?`/api/mailbox/messages/${id}/history`:target==='tasks'?`/api/tasks/${id}/history`:target==='treasury'?`/api/treasury/entries/${id}/history`:'';
    if(!path){liveNotice('事实时间线本身就是版本历史。');return;}const data=await rApi(path,{items:[]});const items=data.items||[];document.body.insertAdjacentHTML('beforeend',`<div class="r-modal" id="rHistoryModal"><section><button data-r-close type="button">×</button><h2>历史版本</h2>${items.length?items.map(i=>rRecord(i,target,false)).join(''):rEmpty('没有旧版本')}</section></div>`);document.querySelector('[data-r-close]')?.addEventListener('click',()=>document.querySelector('#rHistoryModal')?.remove());
  }

  async function rRenderCalendar(){
    const data=await rApi(`/api/calendar?date=${encodeURIComponent(R.date)}`,{date:R.date,items:[]});const items=data.items||[];
    rPage('timeline',`<div class="page r-page r-calendar"><header class="r-title"><h1>日期日历</h1><input id="rCalendarDate" type="date" value="${esc(R.date)}"/></header><section class="r-calendar-day"><time>${esc(data.date||R.date)}</time>${items.length?items.map(i=>rRecord(i,'calendar',false)).join(''):rEmpty('这一天还没有记录')}</section></div>`);document.querySelector('#rCalendarDate')?.addEventListener('change',e=>{R.date=e.target.value;rRenderCalendar()});
  }

  async function rRenderSearch(){
    rPage('search',`<div class="page r-page r-search"><header class="r-title"><h1>智能搜索</h1></header><form id="rSearch"><label><span>关键字</span><input id="rSearchQ" placeholder="一句原话、一件事或一个意思"/></label><label><span>人物</span><input id="rSearchPerson" placeholder="人物名称"/></label><label><span>日期</span><input id="rSearchDate" type="date"/></label><label><span>范围</span><select id="rSearchSource"><option value="all">全部</option><option value="memory">记忆</option><option value="mailbox">信箱</option><option value="thoughts">内在</option></select></label><button class="r-primary">寻找</button></form><section id="rSearchResults" class="r-search-results"></section></div>`);document.querySelector('#rSearch')?.addEventListener('submit',async e=>{e.preventDefault();const q=document.querySelector('#rSearchQ').value.trim();const person=document.querySelector('#rSearchPerson').value.trim();const date=document.querySelector('#rSearchDate').value;if(!q&&!person&&!date){liveNotice('关键字、人物和日期至少填写一项。',true);return;}const params=new URLSearchParams({q,person,date,source:document.querySelector('#rSearchSource').value,limit:'30'});const results=document.querySelector('#rSearchResults');results.innerHTML='<div class="r-soft-loader"><i></i><i></i><i></i></div>';const d=await rApi(`/api/search?${params.toString()}`,{items:[]},{refresh:true});const items=Array.isArray(d)?d:(d.items||[]);results.innerHTML=items.length?`<div class="r-search-count">找到 ${items.length} 条</div>${items.map(i=>rRecord(i,'search',false)).join('')}`:rEmpty('没有找到匹配内容');});
  }

  window.renderMap=rRenderHome;
  window.renderHormones=rRenderHormones;
  window.renderSettings=rRenderSettings;
  window.renderCalendar=rRenderCalendar;
  window.renderSearch=rRenderSearch;
  window.renderModule=target=>target==='timeline'?rRenderTimeline():rRenderModule(target);
  window.renderDetail=target=>target==='personality'?rRenderPersonality():rRenderCoordinates();
  window.navigateMap=target=>{
    let render=null;
    if(target==='core')render=()=>rRenderHome();
    else if(target==='memory')render=()=>renderMemory();
    else if(target==='timeline')render=()=>rRenderTimeline();
    else if(target==='hormones')render=()=>rRenderHormones();
    else if(target==='settings')render=()=>rRenderSettings();
    else if(target==='search')render=()=>rRenderSearch();
    else if(target==='calendar')render=()=>rRenderSearch();
    else if(target==='personality')render=()=>rRenderPersonality();
    else if(target==='coordinates')render=()=>rRenderCoordinates();
    else if(target==='more')render=()=>rRenderModule('toolbox');
    else if(endpointFor[target])render=()=>rRenderModule(target);
    if(!render)return;
    if(R.navPromise&&R.navTarget===target)return R.navPromise;
    R.navTarget=target;markSelected(target);
    const content=document.querySelector('#content');content?.classList.add('r-route-loading');content?.setAttribute('aria-busy','true');
    const task=Promise.resolve().then(render).finally(()=>{if(R.navPromise===task){content?.classList.remove('r-route-loading');content?.removeAttribute('aria-busy');R.navPromise=null;R.navTarget='';}});
    R.navPromise=task;
    return task;
  };
})();
