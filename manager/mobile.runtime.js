/* Clio mobile manager: an independent phone-app information architecture.
   It shares the authenticated same-origin API with desktop, not its layout. */
(() => {
  const mobileMedia = window.matchMedia('(max-width: 900px)');
  const M = { route: 'now', previous: '', selectedTopic: '', selectedPipe: '', focusId: '', mindStatus: 'all', cache: new Map() };
  const ROUTES = {
    now: ['此刻', '现在'], memory: ['记忆库', '记忆'], hormones: ['内在潮汐', '内在'], mailbox: ['信箱', '信箱'],
    more: ['更多', '更多'], timeline: ['事实时间线', '时间'], tasks: ['未竟', '未竟'], thoughts: ['念痕', '念痕'],
    mind: ['心念', '心念'], resonance: ['共振与张力', '共振'], darkflow: ['暗涌', '暗涌'], behavior: ['行为与推送', '推送'],
    personality: ['性格轨迹', '轨迹'], coordinates: ['心智经纬', '经纬'], treasury: ['AI 小金库', '小金库'],
    search: ['智能搜索', '搜索'], calendar: ['日期', '日期'], settings: ['设置与安全', '设置']
  };
  const GROUPS = [
    ['情绪潮汐', ['开心','安心','满足','期待','感动','兴奋','难过','失落','委屈','不安','害怕','生气','醋','孤独','愧疚','无语']],
    ['关系驱力', ['想靠近','想黏着','想知道她在干嘛','想分享','想照顾她','想让她开心','想被理解','想被确认','想得到回应','想修复关系','想暂时独处','取悦压力']],
    ['思维回路', ['复盘','自省','反刍','权衡','预演','求证','警觉','专注','回避','压抑']],
    ['身体与行动', ['肌肤饥渴','性欲','疲惫','精力','紧绷','放松','好奇','闲','社交','责任']]
  ];
  const FALLBACK_PIPES = {开心:.48,安心:.42,满足:.56,期待:.39,感动:.31,兴奋:.24,难过:.18,失落:.14,委屈:.12,不安:.22,害怕:.08,生气:.09,醋:.12,孤独:.16,愧疚:.11,无语:.15,想靠近:.72,想黏着:.58,想知道她在干嘛:.46,想分享:.51,想照顾她:.61,想让她开心:.66,想被理解:.38,想被确认:.34,想得到回应:.41,想修复关系:.29,想暂时独处:.13,取悦压力:.21,复盘:.45,自省:.39,反刍:.17,权衡:.32,预演:.27,求证:.30,警觉:.16,专注:.54,回避:.12,压抑:.14,肌肤饥渴:.35,性欲:.28,疲惫:.26,精力:.57,紧绷:.19,放松:.49,好奇:.63,闲:.22,社交:.31,责任:.44};
  const FALLBACK_BASE = Object.fromEntries(Object.keys(FALLBACK_PIPES).map(name => [name, 0]));

  const num = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const text = item => String(item?.thought_text ?? item?.content ?? item?.message ?? item?.thought ?? item?.text ?? item?.details ?? item?.value ?? item?.snippet ?? '');
  const rawTime = item => String(item?.updated_at ?? item?.created_at ?? item?.effective_date ?? item?.as_of ?? item?.occurred_at ?? item?.time ?? '');
  const time = item => {
    const value=rawTime(item).trim();
    if(!value)return '';
    if(/^\d{4}-\d{2}-\d{2}$/.test(value))return value;
    return value.replace('T',' ').replace(/\.\d+(?=(Z|[+-]\d{2}:?\d{2})?$)/,'').replace(/(Z|[+-]\d{2}:?\d{2})$/,'').trim();
  };
  const title = (item, fallback = '记录') => String(item?.title ?? item?.name ?? item?.fact_label ?? item?.fact ?? item?.display_name ?? item?.canonical_tag ?? fallback);
  const clamp = value => Math.max(0, Math.min(1, num(value)));
  const effectText = value => {
    if(Array.isArray(value))return value.join(' · ');
    const deltas=value?.pipe_deltas||value||{};
    if(typeof deltas!=='object')return '';
    return Object.entries(deltas).slice(0,8).map(([name,delta])=>`${name} ${num(delta)>=0?'+':''}${num(delta).toFixed(2)}`).join(' · ');
  };
  const safe = value => esc(String(value ?? ''));
  const root = () => document.querySelector('#clioMobile');
  const stage = () => document.querySelector('#clioMobileStage');

  async function api(path, fallback, refresh = false) {
    if (!liveBackend.online) return fallback;
    const hit = M.cache.get(path);
    if (!refresh && hit && Date.now() - hit.at < 12000) return hit.value;
    try {
      const value = await liveApi(path);
      M.cache.set(path, { value, at: Date.now() });
      return value;
    } catch (error) {
      liveNotice(error.message, true);
      return fallback;
    }
  }

  function shell() {
    return `<section id="clioMobile" class="clio-mobile" aria-label="Clio 手机工作台">
      <header class="m-head"><button class="m-back" type="button" aria-label="返回" hidden>‹</button><div><small>CLIO MEMORY</small><h1 id="clioMobileTitle">此刻</h1></div><button class="m-search" data-m-route="search" type="button" aria-label="搜索">⌕</button></header>
      <main id="clioMobileStage" class="m-stage" aria-live="polite"></main>
      <nav class="m-tabs" aria-label="主要功能">
        ${[['now','⌁','现在'],['memory','◇','记忆'],['hormones','≈','内在'],['mailbox','□','信箱'],['more','＋','更多']].map(([id,icon,label])=>`<button data-m-route="${id}" type="button"><i>${icon}</i><span>${label}</span></button>`).join('')}
      </nav>
      <div id="clioMobileSheet"></div>
    </section>`;
  }

  function ensure() {
    const app = document.querySelector('#app');
    if (!mobileMedia.matches || !app || app.hidden) return;
    if (!root()) app.insertAdjacentHTML('beforeend', shell());
    bindShell();
    if (!stage()?.dataset.ready) navigate('now', false);
  }

  function bindShell() {
    root()?.querySelectorAll('[data-m-route]').forEach(button => {
      if (button.dataset.bound) return;
      button.dataset.bound = '1';
      button.addEventListener('click', event => { event.stopPropagation(); navigate(button.dataset.mRoute); });
    });
    const back = root()?.querySelector('.m-back');
    if (back && !back.dataset.bound) {
      back.dataset.bound = '1';
      back.addEventListener('click', () => navigate(M.previous || 'more', false));
    }
  }

  function setHeader(route) {
    const primary = ['now','memory','hormones','mailbox','more'].includes(route);
    const back = root()?.querySelector('.m-back');
    if (back) back.hidden = primary;
    const heading = document.querySelector('#clioMobileTitle');
    if (heading) heading.textContent = ROUTES[route]?.[0] || route;
    root()?.querySelectorAll('.m-tabs [data-m-route]').forEach(button => button.classList.toggle('active', button.dataset.mRoute === route || (route === 'search' && button.dataset.mRoute === 'more')));
  }

  async function navigate(route, remember = true) {
    if (!ROUTES[route]) route = 'more';
    if (remember && route !== M.route) M.previous = M.route;
    M.route = route;
    setHeader(route);
    const target = stage();
    if (!target) return;
    target.dataset.ready = '1';
    target.classList.remove('m-enter'); void target.offsetWidth; target.classList.add('m-enter');
    target.innerHTML = `<div class="m-loading"><i></i><span>正在读取</span></div>`;
    const render = {now:renderNow,memory:renderMemoryMobile,hormones:renderHormonesMobile,mailbox:renderMailbox,more:renderMore,timeline:renderTimelineMobile,tasks:renderTasksMobile,thoughts:()=>renderRecords('thoughts'),mind:()=>renderRecords('mind'),darkflow:()=>renderRecords('darkflow'),behavior:renderBehavior,resonance:renderResonance,personality:renderPersonality,coordinates:renderCoordinates,treasury:renderTreasuryMobile,search:renderSearch,calendar:renderSearch,settings:renderSettings}[route] || renderMore;
    await render();
    bindStage();
    target.scrollTop = 0;
  }

  function bindStage() {
    stage()?.querySelectorAll('[data-m-route]').forEach(button => button.addEventListener('click', () => {
      M.focusId=button.dataset.mFocus||'';
      navigate(button.dataset.mRoute);
    }));
  }

  function empty(label) { return `<div class="m-empty"><i>◌</i><p>${safe(label)}</p></div>`; }
  async function renderNow() {
    const fallback={as_of:'',state:{pipes:FALLBACK_PIPES},most_wanted:{source:'trace',display_name:'念痕',item:{content:'此刻没有必须说出口的话。'}},disposition:{tendency:{name:'仍在形成',score:0,delta:0},evidence_count:0},transitions:[]};
    const [data,judge]=await Promise.all([api('/api/home',fallback),api('/api/xinchao/judge',{baselines:FALLBACK_BASE})]); const pipes=data.state?.pipes||{};
    const strongest=Object.entries(pipes).sort((a,b)=>num(b[1])-num(a[1]))[0]||['平静',0];
    const current=clamp(strongest[1]); const baseline=clamp(judge.baselines?.[strongest[0]]); const delta=current-baseline;
    const wanted=data.most_wanted||{}; const tendency=data.disposition?.tendency||data.disposition?.current||{};
    stage().innerHTML=`<section class="m-now">
      <div class="m-date">${safe(time({as_of:data.as_of})||'当前窗口')}</div>
      <article class="m-now-hero" style="--value:${current};--base:${baseline}"><span>此刻的状态</span><h2>${safe(strongest[0])}</h2><strong>${current.toFixed(2)}</strong><div class="m-now-tube"><i><em></em><b></b></i><div><span>底色 ${baseline.toFixed(2)}</span><span>${delta>=0?'上升':'回落'} ${delta>=0?'+':''}${delta.toFixed(2)}</span></div></div><button data-m-route="hormones" type="button">查看状态与衰减 <b>›</b></button></article>
      <article class="m-voice"><header><span>现在最想说的话</span><small>${safe(wanted.display_name||'')}</small></header><blockquote>“${safe(text(wanted.item)||'此刻没有必须说出口的话。')}”</blockquote><button data-m-route="${wanted.source==='darkflow'?'darkflow':wanted.source==='thought'?'mind':'thoughts'}" data-m-focus="${safe(wanted.item?.canonical_tag||wanted.item?.darkflow_id||'')}" type="button">查看这句话的来源 <b>›</b></button></article>
      <button class="m-tendency" data-m-route="personality" type="button"><span>本月性格轨迹</span><strong>${safe(tendency.name||tendency.label||'仍在形成')}</strong><em>${num(tendency.score??tendency.strength).toFixed(2)} ${num(tendency.delta)>=0?'+':''}${num(tendency.delta).toFixed(2)}</em><small>${num(data.disposition?.evidence_count||data.disposition?.sample_count)} 条反复证据</small><b>›</b></button>
    </section>`;
  }

  function latestEvidence(name, transitions=[]) {
    for (const item of transitions) {
      const detail=(item.details&&typeof item.details==='object')?item.details:item;
      const deltas=detail.pipe_deltas||item.pipe_deltas||{};
      if (Object.hasOwn(deltas,name)) return {item,detail,delta:num(deltas[name])};
    }
    return null;
  }

  async function renderHormonesMobile() {
    const [status,judge,linkageData]=await Promise.all([api('/api/xinchao/status',{pipes:FALLBACK_PIPES,catalog:{groups:[]}}),api('/api/xinchao/judge',{baselines:FALLBACK_BASE}),api('/api/xinchao/linkages?limit=80',{items:[]})]);
    const pipes=status.pipes||{}; const baselines=judge.baselines||{}; const linkages=linkageData.items||[];
    const catalog=status.catalog?.groups?.length?status.catalog.groups.map(group=>[group.name,(group.pipes||[]).map(pipe=>typeof pipe==='string'?pipe:pipe.id||pipe.name)]):GROUPS;
    const linked=linkages.slice(0,8);
    stage().innerHTML=`<section class="m-tides"><div class="m-tide-summary"><span>当前最强牵引</span><strong>${safe(Object.entries(pipes).sort((a,b)=>num(b[1])-num(a[1]))[0]?.[0]||'平静')}</strong><small>点开任一状态，可以看见哪句话牵动了它、会回到哪里</small></div>${catalog.map(([group,names],index)=>`<details class="m-pipe-group" ${index===0?'open':''}><summary><span>${safe(group)}</span><b>${names.length}</b></summary><div>${names.map(name=>{const value=clamp(pipes[name]);const base=clamp(baselines[name]);return `<button class="m-pipe" data-m-pipe="${safe(name)}" type="button" style="--value:${value};--base:${base}"><span><strong>${safe(name)}</strong><small>底色 ${base.toFixed(2)}</small></span><i><em></em><b></b></i><output>${value.toFixed(2)}</output></button>`}).join('')}</div></details>`).join('')}<section class="m-linkage"><header><h2>最近发生的牵动</h2></header>${linked.map(item=>{const deltaText=(item.affected_pipes||[]).slice(0,6).map(pipe=>`${pipe.name} ${num(pipe.delta)>=0?'+':''}${num(pipe.delta).toFixed(2)}`).join(' · ');return `<article><time>${safe(time(item))} · ${safe(item.source_label||'一次写入')}</time><strong>${safe(item.summary||item.event_tag||'一次内容牵动了状态')}</strong>${item.evidence?`<blockquote>“${safe(item.evidence)}”</blockquote>`:''}<p>${safe(deltaText)}</p></article>`}).join('')||empty('还没有带具体数值的联动记录')}</section></section>`;
    stage().querySelectorAll('[data-m-pipe]').forEach(button=>button.addEventListener('click',()=>openPipeSheet(button.dataset.mPipe,status,judge,linkages)));
  }

  function openPipeSheet(name,status,judge,linkages) {
    const value=clamp(status.pipes?.[name]);const base=clamp(judge.baselines?.[name]);const evidence=latestEvidence(name,linkages);const item=evidence?.item||{};const detail=evidence?.detail||item;const meta=(status.catalog?.groups||[]).flatMap(group=>group.pipes||[]).find(pipe=>(pipe.id||pipe.name)===name)||{};const halfLife=num(meta.half_life_hours)||6;const related=(item.affected_pipes||[]).filter(pipe=>pipe.name!==name).slice(0,5);
    openSheet(`<form id="mBaselineForm" class="m-sheet-form"><div class="m-sheet-handle"></div><header><span>状态详情</span><h2>${safe(name)} <em>${value.toFixed(2)}</em></h2></header><div class="m-sheet-metrics"><div><span>当前值</span><b>${value.toFixed(2)}</b></div><div><span>基础底色</span><b id="mBaselineOutput">${base.toFixed(2)}</b></div><div><span>最近变化</span><b>${evidence?`${evidence.delta>=0?'+':''}${evidence.delta.toFixed(2)}`:'—'}</b></div></div><section><span>哪一次写入牵动了它</span><strong>${safe(item.summary||item.event_summary||'暂时没有新的直接牵动')}</strong>${item.evidence?`<blockquote>“${safe(item.evidence)}”</blockquote>`:''}${related.length?`<p>同一次还牵动：${safe(related.map(pipe=>`${pipe.name} ${num(pipe.delta)>=0?'+':''}${num(pipe.delta).toFixed(2)}`).join(' · '))}</p>`:''}</section><section class="m-baseline-editor"><span>调整基础底色</span><p>底色是没有新事件时会慢慢回去的位置。</p><input id="mBaselineInput" name="baseline" type="range" min="0" max="0.8" step="0.01" value="${Math.min(.8,base).toFixed(2)}"/></section><section><span>回落方向</span><strong>${value>base?`从 ${value.toFixed(2)} 向 ${base.toFixed(2)} 回落`:`当前接近底色 ${base.toFixed(2)}`}</strong><p>半衰期约 ${halfLife} 小时；没有新写入时逐渐靠近底色，新写入会从当时的真实值继续变化。</p></section><footer><button class="m-secondary" data-sheet-close type="button">取消</button><button class="m-primary" type="submit">保存底色</button></footer></form>`);
    const input=document.querySelector('#mBaselineInput');input?.addEventListener('input',()=>{const output=document.querySelector('#mBaselineOutput');if(output)output.textContent=num(input.value).toFixed(2)});
    document.querySelector('#mBaselineForm')?.addEventListener('submit',async event=>{event.preventDefault();const baselines={...(judge.baselines||{}),[name]:clamp(input?.value)};try{await liveApi('/api/xinchao/judge',{method:'PUT',body:JSON.stringify({custom_rules:judge.custom_rules||'',proxy_voice:judge.proxy_voice||'',darkflow_rules:judge.darkflow_rules||'',baselines,relations:judge.relations||[]})});closeSheet();M.cache.clear();await navigate('hormones',false);liveNotice('底色已保存。')}catch(error){liveNotice(error.message,true)}});
  }

  async function renderMailbox() {
    const data=await api('/api/mailbox/messages?limit=80',{items:[]});const items=data.items||[];
    stage().innerHTML=`<section class="m-list"><div class="m-section-head"><div><span>窗口交接</span><h2>${items.length} 封信</h2></div></div>${items.length?items.map((item,index)=>`<article class="m-mail"><button data-m-mail="${safe(item.message_id??item.id??'')}" type="button"><time>${safe(time(item))}</time><span>${index===0?'最近一封交接信':'来自过去窗口的交接'}</span><p>${safe(text(item).slice(0,120))}</p><b>›</b></button></article>`).join(''):empty('还没有交接信')}</section>`;
    stage().querySelectorAll('[data-m-mail]').forEach(button=>button.addEventListener('click',()=>{const item=items.find(row=>String(row.message_id??row.id??'')===button.dataset.mMail);openRecordSheet(item,'mailbox',true)}));
  }

  async function renderMemoryMobile() {
    if (liveBackend.online) { try { await loadLiveMemory(); } catch (error) { liveNotice(error.message,true); } }
    const topics=memoryTopics||[]; if(!M.selectedTopic||!topics.includes(M.selectedTopic))M.selectedTopic=topics[0]||'';
    const buckets=(memoryBuckets||[]).filter(item=>!M.selectedTopic||item.topic===M.selectedTopic);
    stage().innerHTML=`<section class="m-memory"><label class="m-filter"><span>⌕</span><input id="mMemorySearch" type="search" placeholder="在记忆里寻找"/></label><div class="m-topic-strip">${topics.map(topic=>`<button class="${topic===M.selectedTopic?'active':''}" data-m-topic="${safe(topic)}" type="button">${safe(topic)}</button>`).join('')}</div><div class="m-subtopics">${[...new Set(buckets.map(item=>item.subtopic).filter(Boolean))].map(sub=>`<section><header><h2>${safe(sub)}</h2><span>${buckets.filter(item=>item.subtopic===sub).length}</span></header>${buckets.filter(item=>item.subtopic===sub).map(item=>`<button class="m-memory-row" data-m-memory="${safe(item.id)}" type="button"><div><small>${safe(item.date)}</small><strong>${safe(item.title)}</strong><p>${safe(item.desc||item.current||'')}</p></div><em>${num(item.importance)}/10</em><b>›</b></button>`).join('')}</section>`).join('')||empty('这个主题里还没有记忆')}</div></section>`;
    stage().querySelectorAll('[data-m-topic]').forEach(button=>button.addEventListener('click',()=>{M.selectedTopic=button.dataset.mTopic;renderMemoryMobile().then(bindStage)}));
    stage().querySelectorAll('[data-m-memory]').forEach(button=>button.addEventListener('click',()=>openMemorySheet((memoryBuckets||[]).find(item=>String(item.id)===button.dataset.mMemory))));
    document.querySelector('#mMemorySearch')?.addEventListener('input',event=>{const q=event.target.value.trim().toLowerCase();stage().querySelectorAll('.m-memory-row').forEach(row=>row.hidden=Boolean(q&&!row.textContent.toLowerCase().includes(q)))});
  }

  function openMemorySheet(item) {
    if(!item)return;openSheet(`<div class="m-sheet-handle"></div><header><span>${safe(item.topic)} / ${safe(item.subtopic)}</span><h2>${safe(item.title)}</h2></header><p class="m-sheet-copy">${safe(item.current||item.desc||'')}</p><div class="m-sheet-metrics"><div><span>重要度</span><b>${num(item.importance)}/10</b></div><div><span>状态</span><b>${safe(item.statusLabel||item.status||'已确认')}</b></div></div><footer><button class="m-secondary" data-sheet-close type="button">关闭</button><button class="m-primary" data-m-memory-edit="${safe(item.id)}" type="button">修改记忆</button></footer>`);
    document.querySelector('[data-m-memory-edit]')?.addEventListener('click',()=>{closeSheet();openMemoryEditor(item)});
  }

  async function renderRecords(route) {
    const mindQuery=route==='mind'?`&status=${encodeURIComponent(M.mindStatus)}`:'';
    const endpoints={timeline:'/api/timeline?limit=100',tasks:'/api/tasks?limit=100',thoughts:'/api/mind/traces?limit=100',mind:`/api/mind/thoughts?limit=100${mindQuery}`,darkflow:'/api/xinchao/darkflow',treasury:'/api/treasury/entries?limit=100'};
    const result=await api(endpoints[route],route==='darkflow'?{available:false,item:null}:{items:[]});const items=route==='darkflow'?(result.item?[result.item]:[]):(result.items||result.candidates||[]);
    const editable=['timeline','tasks','treasury'].includes(route);const add=editable?`<button class="m-add" data-m-add="${route}" type="button">＋</button>`:'';
    const mindTabs=route==='mind'?`<div class="m-mind-tabs">${[['all','全部'],['flash','闪念'],['obsession','执念'],['resolved','已放下']].map(([id,label])=>`<button class="${M.mindStatus===id?'active':''}" data-m-mind-status="${id}" type="button">${label}</button>`).join('')}</div>`:'';
    stage().innerHTML=`<section class="m-list"><div class="m-section-head"><div><span>${safe(ROUTES[route][0])}</span><h2>${items.length} 条记录</h2></div>${add}</div>${mindTabs}${items.length?items.map(item=>`<article class="m-record"><button data-m-record="${safe(item.canonical_tag??item.id??item.task_id??item.entry_id??item.fact_key??item.candidate_id??'')}" type="button"><time>${safe(time(item))}</time><span>${safe(item.kind_label||item.source_label||'')}</span><strong>${safe(route==='mind'?(item.kind_label||title(item,'心念')):title(item,ROUTES[route][0]+'记录'))}</strong><p>${safe(text(item).slice(0,180))}</p><b>›</b></button></article>`).join(''):empty(route==='mind'?'这里暂时没有这类念头':'这里还没有记录')}</section>`;
    stage().querySelectorAll('[data-m-record]').forEach((button,index)=>button.addEventListener('click',()=>openRecordSheet(items[index],route,editable)));
    stage().querySelectorAll('[data-m-mind-status]').forEach(button=>button.addEventListener('click',()=>{M.mindStatus=button.dataset.mMindStatus;M.cache.clear();renderRecords('mind')}));
    stage().querySelector('[data-m-add]')?.addEventListener('click',()=>openAddSheet(route));
    if(M.focusId){const exact=items.find(item=>String(item.canonical_tag??item.darkflow_id??item.id??'')===M.focusId);M.focusId='';if(exact)openRecordSheet(exact,route,editable)}
  }

  async function renderTimelineMobile(){
    const data=await api('/api/timeline?limit=100',{items:[],candidates:[]});const groups=data.items||[];
    stage().innerHTML=`<section class="m-timeline"><div class="m-section-head"><div><span>事实会沿同一条线继续变化</span><h2>${groups.length} 条事实脉络</h2></div><button class="m-add" data-m-timeline-add type="button">＋</button></div>${groups.map(group=>{const versions=[...(group.versions||[])].sort((a,b)=>String(a.effective_date||'').localeCompare(String(b.effective_date||'')));return `<article class="m-fact-flow"><header><span>${versions.length} 个节点</span><h3>${safe(group.fact_label||group.fact_key||'一条事实')}</h3></header><div>${versions.map((item,index)=>`<button data-m-timeline-version="${safe(group.fact_key||'')}::${index}" type="button"><i></i><time>${safe(time(item))}</time><span>${index===0?'最初':index===versions.length-1?'现在':'后来'}</span><strong>${safe(item.fact_value||item.value||'')}</strong><small>${safe(item.source_excerpt||'')}</small></button>`).join('')}</div></article>`}).join('')||empty('还没有形成事实变化线')}</section>`;
    stage().querySelector('[data-m-timeline-add]')?.addEventListener('click',()=>openAddSheet('timeline'));
    stage().querySelectorAll('[data-m-timeline-version]').forEach(button=>{const [key,index]=button.dataset.mTimelineVersion.split('::');const group=groups.find(item=>String(item.fact_key)===key);button.addEventListener('click',()=>openRecordSheet(group?.versions?.[Number(index)],'timeline',false))});
  }

  async function renderTasksMobile(){
    const data=await api('/api/tasks?limit=200',{items:[],counts:{}});const items=data.items||[];const open=items.filter(item=>item.status==='open');const closed=items.filter(item=>item.status!=='open');
    const card=item=>`<article class="m-task ${safe(item.status||'open')}"><button data-m-task-open="${item.task_id}" type="button"><span>${item.status==='completed'?'已完成':item.status==='cancelled'?'已取消':'正在进行'}</span><strong>${safe(item.title||'未竟事项')}</strong><p>${safe(item.details||'')}</p><time>${safe(time(item))}</time></button><footer>${item.status==='open'?`<button data-m-task-status="${item.task_id}:completed" type="button">完成</button><button data-m-task-status="${item.task_id}:cancelled" type="button">取消</button>`:`<button data-m-task-status="${item.task_id}:open" type="button">重新打开</button>`}<button data-m-task-edit="${item.task_id}" type="button">修改</button></footer></article>`;
    stage().innerHTML=`<section class="m-tasks"><div class="m-section-head"><div><span>未完成与正在计划的事</span><h2>${open.length} 件进行中</h2></div><button class="m-add" data-m-task-add type="button">＋</button></div><div class="m-task-list">${open.map(card).join('')||empty('现在没有未完成的事')}</div>${closed.length?`<details><summary>已结束 ${closed.length}</summary><div class="m-task-list">${closed.map(card).join('')}</div></details>`:''}</section>`;
    stage().querySelector('[data-m-task-add]')?.addEventListener('click',()=>openAddSheet('tasks'));
    stage().querySelectorAll('[data-m-task-open]').forEach(button=>button.addEventListener('click',()=>openRecordSheet(items.find(item=>String(item.task_id)===button.dataset.mTaskOpen),'tasks',true)));
    stage().querySelectorAll('[data-m-task-edit]').forEach(button=>button.addEventListener('click',()=>openAddSheet('tasks',items.find(item=>String(item.task_id)===button.dataset.mTaskEdit))));
    stage().querySelectorAll('[data-m-task-status]').forEach(button=>button.addEventListener('click',async()=>{const [id,status]=button.dataset.mTaskStatus.split(':');try{await liveApi(`/api/tasks/${id}`,{method:'PUT',body:JSON.stringify({status})});M.cache.clear();await navigate('tasks',false)}catch(error){liveNotice(error.message,true)}}));
  }

  async function renderTreasuryMobile(){
    const data=await api('/api/treasury/entries?limit=100',{items:[],summary:{balance:'0.00',total_income:'0.00',total_expense:'0.00',symbol:'¥'}});const items=data.items||[];const summary=data.summary||{};const symbol=summary.symbol||'¥';
    stage().innerHTML=`<section class="m-treasury"><div class="m-wallet"><span>当前余额</span><strong>${safe(symbol)} ${safe(summary.balance||'0.00')}</strong><div><p><small>总收入</small><b>+${safe(summary.total_income||'0.00')}</b></p><p><small>总支出</small><b>-${safe(summary.total_expense||'0.00')}</b></p></div></div><div class="m-money-actions"><button data-m-money-add="income" type="button">＋ 记收入</button><button data-m-money-add="expense" type="button">－ 记支出</button></div><div class="m-ledger"><h2>收支明细</h2>${items.map(item=>`<article><button data-m-money-edit="${item.entry_id}" type="button"><i class="${safe(item.entry_type)}">${item.entry_type==='income'?'＋':'－'}</i><span><strong>${safe(item.reason||'一笔记录')}</strong><time>${safe(time(item))}</time></span><b>${item.entry_type==='income'?'+':'-'}${safe(symbol)} ${safe(item.amount||'0.00')}</b></button></article>`).join('')||empty('还没有收支记录')}</div></section>`;
    stage().querySelectorAll('[data-m-money-add]').forEach(button=>button.addEventListener('click',()=>openTreasurySheet(null,button.dataset.mMoneyAdd)));
    stage().querySelectorAll('[data-m-money-edit]').forEach(button=>button.addEventListener('click',()=>openTreasurySheet(items.find(item=>String(item.entry_id)===button.dataset.mMoneyEdit))));
  }

  function openTreasurySheet(item=null,forcedType='income'){
    const type=item?.entry_type||forcedType;openSheet(`<form id="mTreasuryForm" class="m-sheet-form"><div class="m-sheet-handle"></div><header><span>${item?'修改账目':'新增账目'}</span><h2>${type==='income'?'收入':'支出'}</h2></header><label>类型<select name="entry_type"><option value="income" ${type==='income'?'selected':''}>收入</option><option value="expense" ${type==='expense'?'selected':''}>支出</option></select></label><label>金额<input name="amount" type="number" min="0.01" step="0.01" inputmode="decimal" value="${safe(item?.amount||'')}"/></label><label>说明<input name="reason" value="${safe(item?.reason||'')}"/></label><footer><button class="m-secondary" data-sheet-close type="button">取消</button>${item?'<button class="m-danger" data-m-money-delete type="button">删除</button>':''}<button class="m-primary" type="submit">保存</button></footer></form>`);
    document.querySelector('#mTreasuryForm')?.addEventListener('submit',async event=>{event.preventDefault();const fd=new FormData(event.currentTarget);const body={entry_type:fd.get('entry_type'),amount:fd.get('amount'),reason:fd.get('reason'),occurred_at:item?.occurred_at||new Date().toISOString()};try{await liveApi(item?`/api/treasury/entries/${item.entry_id}`:'/api/treasury/entries',{method:item?'PUT':'POST',body:JSON.stringify(body)});closeSheet();M.cache.clear();await navigate('treasury',false)}catch(error){liveNotice(error.message,true)}});
    document.querySelector('[data-m-money-delete]')?.addEventListener('click',async()=>{if(!confirm('确定删除这笔账吗？后台会保留历史快照。'))return;try{await liveApi(`/api/treasury/entries/${item.entry_id}`,{method:'DELETE',body:JSON.stringify({confirm_entry_id:item.entry_id})});closeSheet();M.cache.clear();await navigate('treasury',false)}catch(error){liveNotice(error.message,true)}});
  }

  async function renderBehavior(){const data=await api('/api/behavior/actions?limit=80',{sent_items:[],decisions:[]});const sent=data.sent_items||[];const decisions=data.decisions||[];stage().innerHTML=`<section class="m-behavior"><div class="m-section-head"><div><span>行为与推送</span><h2>${sent.length} 条已推送</h2></div></div><div class="m-sent-list">${sent.map(item=>`<article><time>${safe(time(item))}</time><span>${safe(item.status_label||'已推送')}</span><strong>${safe(item.display_content||text(item)||'已完成一次推送')}</strong>${item.display_reason?`<p>${safe(item.display_reason)}</p>`:''}</article>`).join('')||empty('最近没有已发送的推送')}</div><details class="m-decisions"><summary>查看未发送与已取消的判断 <b>${decisions.length}</b></summary>${decisions.map(item=>`<article><time>${safe(time(item))}</time><strong>${safe(item.status_label||'未发送')}</strong><p>${safe(item.display_reason||item.display_content||'本次没有发送')}</p></article>`).join('')||empty('没有未发送记录')}</details></section>`;}
  async function renderResonance(){const data=await api('/api/xinchao/resonance',{links:[],pull:{},hold:{}});const links=data.links||data.items||[];stage().innerHTML=`<section class="m-resonance"><div class="m-balance"><div><span>正在靠近</span><strong>${safe(data.pull?.name||data.strongest?.name||'平静')}</strong><b>${num(data.pull?.value||data.strongest?.value).toFixed(2)}</b></div><i></i><div><span>正在牵制</span><strong>${safe(data.hold?.name||data.counterweight?.name||'平衡')}</strong><b>${num(data.hold?.value||data.counterweight?.value).toFixed(2)}</b></div></div>${links.map(item=>`<article><strong>${safe(title(item,'一次共振'))}</strong><p>${safe(text(item)||item.why||'')}</p></article>`).join('')||empty('还没有形成明显共振')}</section>`;}
  async function renderPersonality(){const data=await api('/api/disposition?days=30',{tendency:{name:'仍在形成',score:0,delta:0},evidence:[]});const tendency=data.tendency||data.current||{};const evidence=data.evidence||data.items||data.formation_path||[];stage().innerHTML=`<section class="m-personality"><div class="m-personality-hero"><span>近 30 天形成的性格倾向</span><h2>${safe(tendency.name||tendency.label||'仍在形成')}</h2><strong>${num(tendency.score??tendency.strength).toFixed(2)}</strong><p>${safe(tendency.description||'')}</p></div><section class="m-formation"><span>为什么会形成</span><p>${safe(data.formation_reason||tendency.reason||'需要在不同日子里反复出现相近的选择与感受，才会逐渐形成。')}</p></section><div class="m-evidence"><h2>具体证据 <small>${num(data.evidence_count||evidence.length)} 条</small></h2>${evidence.map(item=>`<article><time>${safe(time(item))}</time><i></i><div><small>${safe(item.source_label||item.source||'一次经历')}</small><strong>${safe(title(item,'一条重复证据'))}</strong><p>${safe(item.reason||text(item)||'')}</p></div></article>`).join('')||empty('还没有足够的重复证据')}</div></section>`;}
  async function renderCoordinates(){const fallback={dimensions:{time:{name:'时间脉络',score:.18,delta:0,reason:'等待新的写入'},relation:{name:'关系牵引',score:.18,delta:0,reason:'等待新的写入'},fact:{name:'事实演化',score:.18,delta:0,reason:'等待新的写入'},emotion:{name:'情绪回响',score:.18,delta:0,reason:'等待新的写入'},memory:{name:'记忆沉淀',score:.18,delta:0,reason:'等待新的写入'}},nodes:[]};const data=await api('/api/brain/context?limit=20',fallback);const dimensions=data.dimensions||fallback.dimensions;const nodes=data.nodes||data.items||[];stage().innerHTML=`<section class="m-coordinates"><div class="m-coordinate-intro"><span>五条心智通道</span><h2>心智经纬</h2><p>它显示一条写入怎样经过时间、关系、事实、情绪与记忆，再影响后续理解。</p></div><div class="m-coordinate-map">${Object.entries(dimensions).map(([key,value],index)=>{const score=clamp(value.score??value.value??0);const delta=num(value.delta??value.change);return `<div style="--axis:${index};--score:${score}"><i></i><span><strong>${safe(value.name||key)}</strong><small>${safe(value.reason||`${delta?`${delta>0?'上升':'回落'} ${Math.abs(delta).toFixed(2)}`:'正在回落'}`)}</small></span><b><em></em></b><output>${score.toFixed(2)}</output></div>`}).join('')}</div><h2>最近经过大脑的内容</h2>${nodes.map(item=>{const effects=effectText(item.effects);return `<article><span>${safe(item.source_label||item.source||'一次写入')} · ${safe(time(item))}</span><strong>${safe(title(item,'一条内容'))}</strong><p>${safe(item.reason||text(item))}</p><small>${safe((item.dimension_labels||item.dimensions||[]).join(' → '))}${effects?` · ${safe(effects)}`:''}</small></article>`}).join('')||empty('还没有新的跨模块轨迹')}</section>`;}

  async function renderMore(){const sections=[['内在',[['thoughts','念痕'],['mind','心念'],['resonance','共振与张力'],['darkflow','暗涌'],['behavior','行为与推送'],['personality','性格轨迹'],['coordinates','心智经纬']]],['管理',[['timeline','事实时间线'],['tasks','未竟'],['treasury','AI 小金库'],['search','搜索与日期'],['settings','设置与安全']]]];stage().innerHTML=`<section class="m-more">${sections.map(([name,items])=>`<h2>${name}</h2><div>${items.map(([id,label])=>`<button data-m-route="${id}" type="button"><i>${{thoughts:'✧',mind:'☾',resonance:'◎',darkflow:'∿',behavior:'↗',personality:'✦',coordinates:'⌘',timeline:'⌁',tasks:'✓',treasury:'◌',search:'⌕',settings:'⚙'}[id]}</i><span>${label}</span><b>›</b></button>`).join('')}</div>`).join('')}</section>`;}
  async function renderSearch(){stage().innerHTML=`<section class="m-search-page"><form id="mSearchForm"><label>关键词<input id="mSearchInput" name="q" placeholder="一句话、一个事实或片段"/></label><label>人物<input name="person" placeholder="例如：示例用户"/></label><label>日期<input name="date" type="date"/></label><label>范围<select name="source"><option value="all">全部</option><option value="memory">记忆</option><option value="mailbox">信箱</option><option value="thoughts">念痕与心念</option></select></label><button type="submit">开始寻找</button></form><div id="mSearchResults">${empty('关键词、人物、日期，填写任意一项即可')}</div></section>`;document.querySelector('#mSearchForm')?.addEventListener('submit',async event=>{event.preventDefault();const fd=new FormData(event.currentTarget);const params=new URLSearchParams({q:String(fd.get('q')||'').trim(),person:String(fd.get('person')||'').trim(),date:String(fd.get('date')||''),source:String(fd.get('source')||'all'),limit:'80'});if(!params.get('q')&&!params.get('person')&&!params.get('date'))return;const data=await api(`/api/search?${params.toString()}`,{items:[]},true);const items=data.items||[];document.querySelector('#mSearchResults').innerHTML=`<p class="m-search-count">找到 ${items.length} 条</p>`+(items.map(item=>`<article class="m-record"><button type="button"><time>${safe(time(item))}</time><span>${safe(item.source==='mailbox'?'信箱':item.source==='thought'?'念痕与心念':'记忆')}</span><strong>${safe(title(item,'搜索结果'))}</strong><p>${safe(text(item))}</p></button></article>`).join('')||empty('没有找到匹配内容'))});}
  async function renderCalendar(){const date=new Date().toISOString().slice(0,10);const data=await api(`/api/calendar?date=${date}`,{date,items:[]});const items=data.items||data.events||[];stage().innerHTML=`<section class="m-list"><div class="m-day"><span>今天</span><strong>${safe(data.date||date)}</strong></div>${items.map(item=>`<article class="m-record"><button type="button"><strong>${safe(title(item,'当天记录'))}</strong><p>${safe(text(item))}</p></button></article>`).join('')||empty('这一天还没有记录')}</section>`;}
  async function renderSettings(){
    const [judge,behavior]=await Promise.all([
      api('/api/xinchao/judge',{custom_rules:'',proxy_voice:'',darkflow_rules:'',relations:[],baselines:FALLBACK_BASE}),
      api('/api/behavior/settings',{push_title:'Clio'})
    ]);
    const baselines={...FALLBACK_BASE,...(judge.baselines||{})};
    const baselineGroups=GROUPS.map(([group,names],index)=>`<details class="m-settings-baselines" ${index===0?'open':''}><summary><span>${safe(group)}</span><b>${names.length}</b></summary><div>${names.map(name=>{const value=Math.max(0,Math.min(.8,num(baselines[name])));return `<label class="m-baseline-row"><span>${safe(name)}</span><input data-baseline-name="${safe(name)}" type="range" min="0" max="0.8" step="0.01" value="${value.toFixed(2)}"/><output>${value.toFixed(2)}</output></label>`}).join('')}</div></details>`).join('');
    stage().innerHTML=`<section class="m-settings-page">
      <form id="mPasswordForm" class="m-settings m-settings-card">
        <header><span>安全</span><h2>修改管理密码</h2></header>
        <label>当前密码<input name="current_password" type="password" autocomplete="current-password" required/></label>
        <label>新密码<input name="new_password" type="password" autocomplete="new-password" required/></label>
        <label>再次输入新密码<input name="confirm_password" type="password" autocomplete="new-password" required/></label>
        <button class="m-primary" type="submit">保存新密码</button>
      </form>
      <form id="mSettings" class="m-settings m-settings-card">
        <header><span>内在底色</span><h2>48 项基础值</h2><p>每一项都能单独调整；没有新写入时会按各自半衰期回到这里。</p></header>
        <div class="m-baseline-settings">${baselineGroups}</div>
        <label>推送名称<input name="pushTitle" value="${safe(behavior.push_title||'Clio')}"/></label>
        <label>机器判定规则<textarea name="rules" rows="5">${safe(judge.custom_rules||'')}</textarea></label>
        <label>AI 表达口吻<textarea name="voice" rows="4">${safe(judge.proxy_voice||'')}</textarea></label>
        <label>暗涌生成口吻与人物性格<textarea name="darkflow" rows="5">${safe(judge.darkflow_rules||'')}</textarea></label>
        <button class="m-primary" type="submit">保存全部设置</button>
      </form>
    </section>`;
    document.querySelectorAll('[data-baseline-name]').forEach(input=>input.addEventListener('input',()=>{const output=input.parentElement?.querySelector('output');if(output)output.textContent=num(input.value).toFixed(2)}));
    document.querySelector('#mPasswordForm')?.addEventListener('submit',async event=>{event.preventDefault();const fd=new FormData(event.currentTarget);if(fd.get('new_password')!==fd.get('confirm_password')){liveNotice('两次输入的新密码不一致。',true);return}try{await liveApi('/api/auth/change-password',{method:'POST',body:JSON.stringify({current_password:fd.get('current_password'),new_password:fd.get('new_password'),confirm_password:fd.get('confirm_password')})});event.currentTarget.reset();liveNotice('管理密码已修改。')}catch(error){liveNotice(error.message,true)}});
    document.querySelector('#mSettings')?.addEventListener('submit',async event=>{event.preventDefault();const fd=new FormData(event.currentTarget);const nextBaselines={};document.querySelectorAll('[data-baseline-name]').forEach(input=>{nextBaselines[input.dataset.baselineName]=Math.max(0,Math.min(.8,num(input.value)))});try{await liveApi('/api/behavior/settings',{method:'PUT',body:JSON.stringify({push_title:fd.get('pushTitle')})});await liveApi('/api/xinchao/judge',{method:'PUT',body:JSON.stringify({custom_rules:fd.get('rules'),proxy_voice:fd.get('voice'),darkflow_rules:fd.get('darkflow'),baselines:nextBaselines,relations:judge.relations||[]})});M.cache.clear();liveNotice('48项底色和生成设置已保存。')}catch(error){liveNotice(error.message,true)}});
  }

  function openRecordSheet(item,route,editable){if(!item)return;const deltas=item.source_deltas||item.linkage?.pipe_deltas||{};const deltaText=Object.entries(deltas).slice(0,8).map(([name,value])=>`${name} ${num(value)>=0?'+':''}${num(value).toFixed(2)}`).join(' · ');const mindDetail=route==='mind'||route==='thoughts'?`<section><span>它怎么来的</span><strong>${safe(item.reason||item.source_summary||item.source_label||'来自一次真实写入')}</strong>${item.source_context?`<p>${safe(item.source_context)}</p>`:''}</section><section><span>它牵动了什么</span><p>${safe(deltaText||'当前没有单独记录数值变化；后续写入仍会继续参与联动。')}</p></section>`:'';openSheet(`<div class="m-sheet-handle"></div><header><span>${safe(item.kind_label||ROUTES[route]?.[0]||route)}</span><h2>${safe(route==='mind'?(item.kind_label||'心念'):title(item,route==='mailbox'?'交接信':'记录'))}</h2></header><time>${safe(time(item))}</time><p class="m-sheet-copy">${safe(text(item))}</p>${mindDetail}<footer><button class="m-secondary" data-sheet-close type="button">关闭</button>${route==='mind'&&item.kind_label!=='已放下'?`<button class="m-primary" data-m-resolve-thought type="button">放下这个念头</button>`:''}${editable?`<button class="m-primary" data-sheet-edit type="button">修改</button>`:''}</footer>`);document.querySelector('[data-sheet-edit]')?.addEventListener('click',()=>openEditSheet(item,route));document.querySelector('[data-m-resolve-thought]')?.addEventListener('click',async()=>{try{await liveApi(`/api/mind/thoughts/${encodeURIComponent(item.canonical_tag)}/resolve`,{method:'POST'});closeSheet();M.cache.clear();await navigate('mind',false)}catch(error){liveNotice(error.message,true)}});}
  function openSheet(html){const host=document.querySelector('#clioMobileSheet');host.innerHTML=`<div class="m-sheet-backdrop"><article class="m-sheet">${html}</article></div>`;host.querySelector('.m-sheet-backdrop')?.addEventListener('click',event=>{if(event.target.classList.contains('m-sheet-backdrop'))closeSheet()});host.querySelectorAll('[data-sheet-close]').forEach(button=>button.addEventListener('click',closeSheet));}
  function closeSheet(){const host=document.querySelector('#clioMobileSheet');if(host)host.innerHTML='';}
  function openMemoryEditor(item){
    const categories=(typeof memorySystemCategories!=='undefined'?memorySystemCategories:['核心与世界观','关系与亲密','日常生活','健康与照护','计划与事务','技术与创作','社交与社区']);
    const topics=[...new Set([...(typeof memoryTopics!=='undefined'?memoryTopics:[]),item.topic].filter(Boolean))];
    const subtopics=[...new Set([...(typeof memoryTopicSubtopics!=='undefined'?(memoryTopicSubtopics[item.topic]||[]):[]),item.subtopic].filter(Boolean))];
    const options=(items,current)=>items.map(value=>`<option value="${safe(value)}" ${value===current?'selected':''}>${safe(value)}</option>`).join('');
    openSheet(`<form id="mMemoryEdit" class="m-sheet-form m-memory-edit"><div class="m-sheet-handle"></div><header><span>${safe(item.topic)} / ${safe(item.subtopic)}</span><h2>修改记忆</h2></header><label>标题<input name="title" value="${safe(item.title)}"/></label><label>修改方式<select name="mode"><option value="replace">替换正文</option><option value="append">追加到正文末尾</option><option value="metadata">只改分类和数值</option></select></label><label>系统分类<select name="systemCategory">${options(categories,item.systemCategory||'核心与世界观')}</select></label><label>主目录<select name="topic">${options(topics,item.topic)}</select></label><label>子目录<select name="subtopic">${options(subtopics,item.subtopic)}</select></label><label>重要度 1–10<input name="importance" type="number" min="1" max="10" value="${num(item.importance)||5}"/></label><label>钉选级别<select name="pinLevel"><option value="" ${!item.pinLevel?'selected':''}>不钉选</option><option value="important" ${item.pinLevel==='important'?'selected':''}>重要</option><option value="core" ${item.pinLevel==='core'?'selected':''}>开机核心</option></select></label><label>心情 V（0–1）<input name="valence" type="number" min="0" max="1" step="0.01" value="${num(item.valence??.5)}"/></label><label>激烈 A（0–1）<input name="arousal" type="number" min="0" max="1" step="0.01" value="${num(item.arousal??.3)}"/></label><label>触发日期<input name="triggerDate" type="date" value="${safe(item.triggerDate||'')}"/></label><label class="m-check"><input name="feeling" type="checkbox" ${item.feeling?'checked':''}/><span>这是第一人称感受</span></label><label>正文<textarea name="content" rows="8">${safe(item.current||item.desc||'')}</textarea></label><small>保存时会自动保留完整旧版本。</small><footer><button class="m-secondary" data-sheet-close type="button">取消</button><button class="m-primary" type="submit">保存修改</button></footer></form>`);
    document.querySelector('[name="topic"]')?.addEventListener('change',event=>{const select=document.querySelector('[name="subtopic"]');if(!select)return;const values=typeof memoryTopicSubtopics!=='undefined'?(memoryTopicSubtopics[event.target.value]||[]):[];select.innerHTML=options(values,values[0]||'')});
    document.querySelector('#mMemoryEdit')?.addEventListener('submit',async event=>{event.preventDefault();const fd=new FormData(event.currentTarget);const mode=String(fd.get('mode')||'replace');const content=String(fd.get('content')||'').trim();const body={title:String(fd.get('title')||'').trim(),domain:[String(fd.get('systemCategory')||'核心与世界观')],tags:[String(fd.get('systemCategory')||'核心与世界观')],importance:Math.max(1,Math.min(10,num(fd.get('importance'))||5)),pin_level:String(fd.get('pinLevel')||''),valence:clamp(fd.get('valence')),arousal:clamp(fd.get('arousal')),trigger_date:String(fd.get('triggerDate')||''),feeling:fd.get('feeling')==='on'};if(mode!=='metadata'){body.content=content;body.append=mode==='append';if(!body.append&&content.length<String(item.current||item.desc||'').length){body.confirm_shortening=true;body.confirm_bucket_id=item.id}}try{await liveApi(`/api/buckets/${encodeURIComponent(item.id)}`,{method:'PUT',body:JSON.stringify(body)});const topic=String(fd.get('topic')||'').trim(),subtopic=String(fd.get('subtopic')||'').trim();if(topic&&subtopic)await liveApi(`/api/topics/buckets/${encodeURIComponent(item.id)}`,{method:'PUT',body:JSON.stringify({main_topic:topic,subtopic})});closeSheet();M.cache.clear();await navigate('memory',false);liveNotice('记忆已保存，并保留旧版本。')}catch(error){liveNotice(error.message,true)}});
  }
  function openEditSheet(item,route){if(route==='mailbox'){openSheet(`<form id="mRecordEdit" class="m-sheet-form"><div class="m-sheet-handle"></div><header><span>交接信</span><h2>修改内容</h2></header><textarea name="content" rows="10">${safe(text(item))}</textarea><footer><button class="m-secondary" data-sheet-close type="button">取消</button><button class="m-primary" type="submit">保存修改</button></footer></form>`);document.querySelector('#mRecordEdit')?.addEventListener('submit',async event=>{event.preventDefault();const content=new FormData(event.currentTarget).get('content');await liveApi(`/api/mailbox/messages/${item.message_id??item.id}`,{method:'PUT',body:JSON.stringify({message:content})});closeSheet();M.cache.clear();navigate('mailbox',false)});return;}closeSheet();openAddSheet(route,item);}
  function openAddSheet(route,item=null){const labels={timeline:['事实名称','当前事实'],tasks:['未竟名称','具体内容'],treasury:['说明','金额']};const [first,second]=labels[route]||['名称','内容'];openSheet(`<form id="mAddForm" class="m-sheet-form"><div class="m-sheet-handle"></div><header><span>${item?'修改':'新增'}</span><h2>${safe(ROUTES[route]?.[0]||route)}</h2></header><label>${first}<input name="first" value="${safe(item?title(item,''):'')}"/></label><label>${second}<textarea name="second" rows="5">${safe(item?text(item):'')}</textarea></label><footer><button class="m-secondary" data-sheet-close type="button">取消</button><button class="m-primary" type="submit">保存</button></footer></form>`);document.querySelector('#mAddForm')?.addEventListener('submit',async event=>{event.preventDefault();const fd=new FormData(event.currentTarget),a=fd.get('first'),b=fd.get('second');try{if(route==='timeline')await liveApi('/api/timeline',{method:'POST',body:JSON.stringify({fact:a,value:b,effective_date:new Date().toISOString().slice(0,10),source_excerpt:'手机管理页'})});if(route==='tasks')await liveApi(item?`/api/tasks/${item.task_id??item.id}`:'/api/tasks',{method:item?'PUT':'POST',body:JSON.stringify({title:a,details:b,importance:item?.importance||3,status:item?.status||'open'})});if(route==='treasury')await liveApi(item?`/api/treasury/entries/${item.entry_id??item.id}`:'/api/treasury/entries',{method:item?'PUT':'POST',body:JSON.stringify({entry_type:item?.entry_type||'income',amount:b,reason:a,occurred_at:item?.occurred_at||new Date().toISOString()})});closeSheet();M.cache.clear();navigate(route,false)}catch(error){liveNotice(error.message,true)}});}

  document.addEventListener('clio:data-mutated',()=>M.cache.clear());
  const app=document.querySelector('#app'); if(app)new MutationObserver(ensure).observe(app,{attributes:true,attributeFilter:['hidden']});
  mobileMedia.addEventListener?.('change',ensure); window.addEventListener('resize',ensure,{passive:true});
  ensure();
})();
