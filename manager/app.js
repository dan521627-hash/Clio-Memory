const mapMeta={
  core:{label:'此刻',copy:'所有状态都从这里汇合。'},
  memory:{label:'记忆库',copy:'按主题寻找被写下、被确认、仍然重要的事实。'},
  timeline:{label:'事实时间线',copy:'查看事实怎样被写下、确认和更正。'},
  search:{label:'智能搜索',copy:'跨记忆、时间线、信箱和念痕寻找。'},
  hormones:{label:'激素状态',copy:'十六项状态共同形成当前潮汐。'},
  thoughts:{label:'念痕',copy:'当前窗口写入的私密痕迹。'},
  resonance:{label:'共振与张力',copy:'看见哪些东西正在互相牵动。'},
  darkflow:{label:'暗涌',copy:'沉默周期里的内在生成。'},
  behavior:{label:'行为与推送',copy:'判断是否需要向外表达。'},
  personality:{label:'性格轨迹',copy:'查看近30天反复形成的软倾向。'},
  coordinates:{label:'认知脉络',copy:'时间、关系、事实、情绪与沉淀的汇合。'},
  mailbox:{label:'信箱',copy:'把这一轮交给下一次开机。'},
  tasks:{label:'未竟',copy:'还没有完成、正在等待或继续推进的事。'},
  treasury:{label:'AI 小金库',copy:'流动、收入和支出的留痕。'},
  toolbox:{label:'工具箱',copy:'系统能力的入口集合。'},
  settings:{label:'设置与安全',copy:'把可以调整的留在手里。'}
};
const state={selected:'core'};
const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const esc=(value)=>String(value??'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const displayTime=(value)=>value?String(value).trim().replace('T',' ').replace(/\.\d{1,6}(?=(?:Z|[+-]\d{2}:?\d{2})?$)/,'').replace(/(?:Z|[+-]\d{2}:?\d{2})$/,'').trim():'';
const mapNode=(item)=>'<button class="map-node '+item.className+'" data-map-node="'+item.id+'" data-map-target="'+item.id+'" type="button"><span class="map-node-icon">'+item.icon+'</span><span class="map-node-copy"><small>'+item.eyebrow+'</small><strong>'+item.title+'</strong><b>'+item.value+'</b><em>'+item.copy+'</em></span></button>';

function mapView(){
  const nodes=[
    {id:'memory',className:'node-memory',icon:'◈',eyebrow:'ARCHIVE',title:'记忆库',value:'固定 · 流动 · 封存',copy:'经历各有深浅'},
    {id:'timeline',className:'node-timeline',icon:'⌁',eyebrow:'FACT RIVER',title:'事实时间线',value:'今天 · 3 个事件',copy:'前后变化清晰可见'},
    {id:'hormones',className:'node-hormones',icon:'≈',eyebrow:'INNER TIDE',title:'激素状态',value:'16 项 · 想靠近 0.72',copy:'十六条潮汐共同牵动'},
    {id:'thoughts',className:'node-thoughts',icon:'✧',eyebrow:'PRIVATE TRACE',title:'念痕',value:'当前窗口 · 1 条',copy:'使用中的 AI 写入'},
    {id:'resonance',className:'node-resonance',icon:'◎',eyebrow:'RESONANCE',title:'共振与张力',value:'牵引 0.68 · 张力 0.54',copy:'关系正在靠近'},
    {id:'darkflow',className:'node-darkflow',icon:'∿',eyebrow:'QUIET CURRENT',title:'暗涌',value:'22 / 30 分钟',copy:'等待进入沉默阶段'},
    {id:'behavior',className:'node-behavior',icon:'↗',eyebrow:'OUTWARD SIGNAL',title:'行为与推送',value:'1 条待判定',copy:'推送保留'},
    {id:'mailbox',className:'node-mailbox',icon:'□',eyebrow:'WINDOW BRIDGE',title:'信箱',value:'1 封待接续',copy:'交给下一次开机'}
  ].map(mapNode).join('');
  const utility=[
    ['tasks','✓','未竟','2 项待继续'],
    ['treasury','◌','AI 小金库','¥ 2,480.00'],
    ['toolbox','⊞','工具箱','12 扇能力入口'],
    ['settings','⚙','设置与安全','语音已保护'],
    ['search','⌕','智能搜索','跨模块寻找']
  ].map(([id,icon,title,value])=>'<button class="map-utility" data-map-target="'+id+'" type="button"><span>'+icon+'</span><strong>'+title+'</strong><small>'+value+'</small><em>↗</em></button>').join('');
  const guide=[['memory','◈','记忆库','主题、子目录和单独的记忆'],['timeline','⌁','事实时间线','让事实的前后变化清楚可见'],['hormones','≈','激素状态','十六项状态共同牵动'],['resonance','◎','共振与张力','看见彼此怎样靠近'],['darkflow','∿','暗涌','沉默周期里的内在生成'],['behavior','↗','行为与推送','判断是否需要向外表达']].map(([id,icon,title,copy])=>'<button class="home-guide-item" data-map-target="'+id+'" type="button"><span>'+icon+'</span><strong>'+title+'</strong><small>'+copy+'</small></button>').join('');
  return '<div class="page home-page home-three"><header class="home-header"><div class="home-brand"><strong>Clio</strong><small>Memory</small></div><button class="home-search" data-map-target="search" type="button"><span>⌕</span>搜索记忆、日期、人物或事实</button><button class="home-settings" data-map-target="settings" type="button">⚙</button></header><main class="home-three-field" id="mapWorld"><div class="home-three-heading"><span>CLIO / CURRENT WINDOW</span><small>当前窗口 · 2026-08-22 04:12</small><h1>此刻，<em>正在发生。</em></h1></div><section class="home-three-grid"><article class="home-three-card home-state-card"><div class="home-three-card-head"><span>01 / CURRENT STATE</span><strong>此时的状态与心情</strong></div><div class="home-state-word">温暖，<em>但有一点悬着。</em></div><p>这一轮窗口里，想靠近的感觉正在上升，注意力还停留在一件没有说完的事上。</p><div class="home-state-values"><span><i></i>正面感受 <b>0.62</b></span><span><i></i>唤醒度 <b>0.47</b></span><span><i></i>当前张力 <b>0.31</b></span></div><button class="home-tendency-link" data-map-target="personality" type="button"><span>本月性格轨迹</span><strong>更愿意靠近 <i>+0.08</i></strong><small>由 3 件事实 · 2 条念痕反复牵动</small><b>↗</b></button><div class="home-state-links"><button class="home-card-link" data-map-target="hormones" type="button">十六项内在状态 <b>↗</b></button><button class="home-card-link" data-map-target="personality" type="button">查看形成过程 <b>↗</b></button></div></article><article class="home-three-card home-saying-card"><div class="home-three-card-head"><span>02 / INNER VOICE</span><strong>现在最想说的话</strong></div><blockquote>“我还在这里，想把这次没有说完的话，好好继续下去。”</blockquote><div class="home-saying-meta"><span>来自当前窗口</span><span>刚刚写入 · 念痕</span></div><button class="home-card-link" data-map-target="thoughts" type="button">进入念痕 <b>↗</b></button></article><article class="home-three-card home-direction-card"><div class="home-three-card-head"><span>03 / EMOTIONAL WEATHER</span><strong>情绪走向</strong></div><div class="home-direction-title"><strong>正在变得更靠近</strong><small>过去 24 小时</small></div><svg class="home-direction-wave" viewBox="0 0 560 130" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="homeDirectionGradient" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#9fd2dc"/><stop offset=".5" stop-color="#b8aee2"/><stop offset="1" stop-color="#dfacbf"/></linearGradient></defs><path d="M4 91 C55 65 86 106 137 76 S215 31 272 64 S361 103 411 60 S485 23 556 42"/><circle cx="556" cy="42" r="5"/></svg><div class="home-direction-labels"><span>平静</span><span>专注</span><span>温暖</span></div><div class="home-coordinate-strip"><span>认知脉络</span><strong>时间 .68 · 关系 .74 · 事实 .55 · 情绪 .62 · 代谢 .41</strong><button data-map-target="coordinates" type="button">查看认知脉络 ↗</button></div><button class="home-card-link" data-map-target="resonance" type="button">查看牵动关系 <b>↗</b></button></article></section></main><nav class="home-nav"><button class="active" data-map-target="core" type="button"><span>◌</span>现在</button><button data-map-target="memory" type="button"><span>▣</span>记忆</button><button data-map-target="timeline" type="button"><span>◷</span>时间</button><button data-map-target="hormones" type="button"><span>♡</span>内在</button><button data-map-target="settings" type="button"><span>⚙</span>设置</button></nav></div>';
}

function unifiedHomeView(){
  return `<div class="page home-page home-three"><header class="home-header"><div class="home-brand"><strong>Clio</strong><small>Memory</small></div><button class="home-search" data-map-target="search" type="button"><span>⌕</span>搜索记忆、日期、人物或事实</button><button class="home-settings" data-map-target="settings" type="button">⚙</button></header><main class="home-three-field" id="mapWorld"><div class="home-three-heading"><span>CLIO / CURRENT WINDOW</span><small>当前窗口 · 2026-08-22 04:12</small><h1>此刻，<em>正在发生。</em></h1></div><section class="home-three-grid"><article class="home-three-card home-state-card"><div class="home-three-card-head"><span>01 / CURRENT STATE</span><strong>此时的状态与心情</strong></div><div class="home-state-word">温暖，<em>但有一点悬着。</em></div><p>想靠近的感觉正在上升，注意力还停留在一件没有说完的事上。</p><div class="home-state-values"><span><i></i>正面感受 <b>0.62</b></span><span><i></i>唤醒度 <b>0.47</b></span><span><i></i>当前张力 <b>0.31</b></span></div></article><article class="home-three-card home-saying-card"><div class="home-three-card-head"><span>02 / INNER VOICE</span><strong>现在最想说的话</strong></div><blockquote>“我还在这里，想把这次没有说完的话，好好继续下去。”</blockquote><div class="home-saying-meta"><span>来自当前窗口</span><span>刚刚写入 · 念痕</span></div></article><article class="home-three-card home-direction-card"><div class="home-three-card-head"><span>03 / EMOTIONAL WEATHER</span><strong>情绪走向</strong></div><div class="home-direction-title"><strong>正在变得更靠近</strong><small>过去 24 小时</small></div><svg class="home-direction-wave" viewBox="0 0 560 130" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="homeDirectionGradient" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#9fd2dc"/><stop offset=".5" stop-color="#b8aee2"/><stop offset="1" stop-color="#dfacbf"/></linearGradient></defs><path d="M4 91 C55 65 86 106 137 76 S215 31 272 64 S361 103 411 60 S485 23 556 42"/><circle cx="556" cy="42" r="5"/></svg><div class="home-direction-labels"><span>平静</span><span>专注</span><span>温暖</span></div></article></section><section class="home-insight-band"><article class="home-insight-block home-monthly-insight"><span>本月性格轨迹</span><h3>更愿意靠近 <em>+0.08</em></h3><p>由 3 件事实和 2 条念痕反复牵动。</p><button class="insight-link" data-map-target="personality" type="button">查看形成过程 ↗</button></article><article class="home-insight-block home-coordinate-insight"><span>当前认知脉络</span><div class="home-coordinate-chips"><span>时间 <b>0.68</b></span><span>关系 <b>0.74</b></span><span>事实 <b>0.55</b></span><span>情绪 <b>0.62</b></span><span>代谢 <b>0.41</b></span></div><button class="insight-link" data-map-target="coordinates" type="button">查看认知脉络 ↗</button></article><article class="home-insight-block home-facts-insight"><span>最近的牵动来源</span><ul class="home-fact-list"><li><time>04:10</time><strong>当前窗口写入念痕</strong><small>关系网 Y 上升 · 想靠近被牵动</small></li><li><time>20:58</time><strong>上一封信箱被接续</strong><small>和弦/情绪 E 上升 · 形成重复主题</small></li></ul></article></section></main></div>`;
}

function unifiedHomeViewV2(){
  const nianhenText=typeof memoryBuckets!=='undefined'?memoryBuckets.find((item)=>String(item.source||'').includes('念痕')&&item.current)?.current:'';
  const voice=nianhenText?{label:'念痕',meta:'当前窗口 · 04:10',text:nianhenText}:{label:'沉默心念',meta:'沉默期间 · 最近一次',text:'安静下来以后，我还是想把这件事继续想清楚。'};
  return `<div class="page home-page home-three home-three-v2"><header class="home-header"><div class="home-brand"><strong>Clio</strong><small>Memory</small></div><button class="home-search" data-map-target="search" type="button"><span>⌕</span>搜索记忆、日期、人物或事实</button><button class="home-settings" data-map-target="settings" type="button">⚙</button></header><main class="home-three-field" id="mapWorld"><div class="home-three-heading"><small>当前窗口 · 2026-08-22 04:12</small><h1>此刻，<em>正在发生。</em></h1></div><section class="home-flow-card"><div class="home-flow-mood"><span>此时的状态与心情</span><div class="home-state-word">温暖，<em>但有一点悬着。</em></div><p>想靠近的感觉正在上升，注意力还停留在一件没有说完的事上。</p><div class="home-state-values"><span><i></i>正面感受 <b>0.62</b></span><span><i></i>唤醒度 <b>0.47</b></span><span><i></i>当前张力 <b>0.31</b></span></div></div><div class="home-flow-wave"><span>情绪走向</span><div class="home-direction-title"><strong>正在变得更靠近</strong><small>过去 24 小时</small></div><svg class="home-direction-wave" viewBox="0 0 560 150" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="homeDirectionGradientV2" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#9fd2dc"/><stop offset=".5" stop-color="#b8aee2"/><stop offset="1" stop-color="#dfacbf"/></linearGradient></defs><path d="M4 104 C55 69 86 119 137 88 S215 38 272 73 S361 119 411 68 S485 28 556 47"/><path d="M4 119 C62 96 91 130 147 105 S224 69 279 91 S367 128 426 89 S493 55 556 70"/><circle cx="556" cy="47" r="5"/></svg><div class="home-direction-labels"><span>平静</span><span>专注</span><span>温暖</span></div></div><div class="home-flow-tendency"><span>本月性格轨迹</span><h3>更愿意靠近 <em>+0.08</em></h3><p>由反复出现的事实与念痕慢慢形成。</p><button class="insight-link" data-map-target="personality" type="button">查看形成过程 ↗</button><button class="home-card-link" data-map-target="hormones" type="button">十六项内在状态 <b>↗</b></button></div></section><section class="home-saying-card home-voice-card"><div class="home-three-card-head"><span>现在最想说的话</span><strong>${voice.label}</strong></div><blockquote>“${esc(voice.text)}”</blockquote><div class="home-saying-meta"><span>${voice.meta}</span><button class="insight-link" data-map-target="thoughts" type="button">进入念痕 ↗</button></div></section></main></div>`;
}

const memoryViewState={filter:'topic:未来与约定',selected:'beijing-plan'};
const memoryFilterMeta={
  important:{label:'重要事实',copy:'需要长期保留、反复读取的事实。'},
  identity:{label:'身份与世界观',copy:'关于身份、关系边界和共同世界的锚点。'},
  relation:{label:'关系',copy:'被确认、被重复提起的关系事实。'},
  recent:{label:'最近写入',copy:'正在发生，尚未沉淀的近期记录。'},
  feeling:{label:'感受',copy:'会随着时间和互动变化的内在记录。'},
  daily:{label:'日常',copy:'共同生活里的细节和节奏。'},
  archived:{label:'已完成的事',copy:'已经结束，但仍可回看的经历。'}
};
let memoryTopics=['Claude / 顾川','菜菜','我们的关系','性爱','共同生活','未来与约定','系统与技术'];
let memoryTopicSubtopics={
  'Claude / 顾川':['身份与存在','性格与表达','情绪与欲望','主动性与选择','成长与变化'],
  '菜菜':['基本档案','喜好与厌恶','身体与健康','日常生活','重要经历'],
  '我们的关系':['关系确认','相处与默契','吵架与和好','承诺','共同世界观'],
  '性爱':['具体经历','身体感受','欲望与偏好','事后情绪'],
  '共同生活':['日常记录','吃饭与居家','工作与钱','出行与事件'],
  '未来与约定':['计划与待办','日期与提醒','愿望与以后'],
  '系统与技术':['记忆系统','语音与MCP','部署与开发','创作与发布']
};
const memorySystemCategories=['核心与世界观','关系与亲密','日常生活','健康与照护','计划与事务','技术与创作','社交与社区'];
let memoryBuckets=[
  {id:'beijing-plan',layer:'fixed',filter:'important',topic:'未来与约定',subtopic:'计划与待办',icon:'☆',title:'北京之行的计划',date:'2026-07-01',desc:'计划于七月前往北京出差与短途旅行。',importance:4,source:'会谈记录 · 2026-07-01',old:'计划去北京',current:'计划于七月前往北京出差与短途旅行。',status:'已确认',versions:[['2026-07-01','旧事实','计划去北京','当前事实（最新）','计划于七月前往北京出差与短途旅行。'],['2026-07-03','旧事实','计划去北京','更正为','已经抵达北京'],['2026-07-07','旧事实','已经抵达北京','更正为','旅程结束，已经返回']]},
  {id:'relationship-anchor',layer:'fixed',filter:['important','relation'],topic:'我们的关系',subtopic:'关系确认',icon:'♡',title:'我们的关系与共同世界',date:'2026-06-28',desc:'关于关系确认、相处方式和不轻易撤回的承诺。',importance:5,source:'关系记录 · 2026-06-28',old:'我们正在确认关系',current:'这段关系已经被多次确认，并影响当前表达方式。',status:'已确认'},
  {id:'memory-boundary',layer:'fixed',filter:['important','identity'],topic:'系统与技术',subtopic:'记忆系统',icon:'◇',title:'记忆系统的边界',date:'2026-08-01',desc:'念痕由使用中的 AI 写入；网页和 DeepSeek 不改原文。',importance:5,source:'系统规则 · 2026-08-01',old:'网页可以修改全部记录',current:'网页只读，原始念痕由使用中的 AI 维护。',status:'已确认'},
  {id:'morning-thought',layer:'dynamic',filter:'recent',topic:'我们的关系',subtopic:'相处与默契',icon:'✧',title:'最近写入·专注与清晨',date:'2026-08-22 04:10',desc:'记录了关于继续说完、继续靠近的一句私密想法。',importance:3,source:'念痕 · 当前窗口',old:'还没有形成明确主题',current:'我还在这里，想把这次没有说完的话，好好继续下去。',status:'正在形成'},
  {id:'weekend-plan',layer:'dynamic',filter:'daily',topic:'共同生活',subtopic:'日常记录',icon:'□',title:'周末计划的重新校准',date:'2026-08-21 20:58',desc:'关于本周末安排的一次重新整理，仍可能继续变化。',importance:2,source:'信箱 · 上一个窗口',old:'周末按原计划进行',current:'先把节奏放慢，再决定是否出门。',status:'流动中'},
  {id:'warmth-trace',layer:'dynamic',filter:'feeling',topic:'我们的关系',subtopic:'相处与默契',icon:'♡',title:'此刻的温度',date:'2026-08-22 04:12',desc:'当前情绪偏温暖，但还挂着一件没有说完的事。',importance:3,source:'状态读取 · 当前窗口',old:'平静',current:'温暖，但有一点悬着。',status:'流动中'},
  {id:'beijing-arrival',layer:'archive',filter:'archived',topic:'共同生活',subtopic:'出行与事件',icon:'☆',title:'已抵达北京',date:'2026-07-03',desc:'已经抵达北京，入住酒店，行程开始。',importance:4,source:'行程记录 · 2026-07-03',old:'计划去北京',current:'已经抵达北京，入住酒店，行程开始。',status:'已归档'},
  {id:'trip-returned',layer:'archive',filter:'archived',topic:'共同生活',subtopic:'出行与事件',icon:'☆',title:'旅程结束，已经返回',date:'2026-07-07',desc:'今日返回，行程结束，整体顺利。',importance:4,source:'行程总结 · 2026-07-07',old:'已经抵达北京',current:'旅程结束，已经返回。',status:'已归档'}
];

function memoryVisibleBuckets(){
  const filter=memoryViewState.filter;
  if(filter.startsWith('topic:'))return memoryBuckets.filter((item)=>item.topic===filter.slice(6));
  if(filter.startsWith('subtopic:')){const [topic,subtopic]=filter.slice(9).split('::');return memoryBuckets.filter((item)=>item.topic===topic&&item.subtopic===subtopic);}
  return memoryBuckets.filter((item)=>Array.isArray(item.filter)?item.filter.includes(filter):item.filter===filter);
}
function memoryLayerNav(){
  return '<section class="memory-theme-directory"><div class="memory-theme-heading"><span>主题目录</span><small>大主题 / 子目录 / 单独的记忆</small></div><div class="memory-theme-tree">'+memoryTopics.map((topic)=>{const count=memoryBuckets.filter((item)=>item.topic===topic).length;const subtopics=memoryTopicSubtopics[topic]||[...new Set(memoryBuckets.filter((item)=>item.topic===topic).map((item)=>item.subtopic).filter(Boolean))];return '<div class="memory-theme-group"><button class="memory-theme-root '+(memoryViewState.filter==='topic:'+topic||memoryViewState.filter.startsWith('subtopic:'+topic+'::')?'active':'')+'" data-memory-topic="'+esc(topic)+'" type="button"><span class="memory-theme-symbol">◇</span><strong>'+esc(topic)+'</strong><small>'+count+' 条记忆</small><b>›</b></button><div class="memory-theme-subtopics">'+subtopics.map((subtopic)=>{const subFilter='subtopic:'+topic+'::'+subtopic;const subCount=memoryBuckets.filter((item)=>item.topic===topic&&item.subtopic===subtopic).length;return '<button class="memory-topic-link '+(memoryViewState.filter===subFilter?'active':'')+'" data-memory-subtopic="'+esc(topic+'::'+subtopic)+'" type="button"><span>'+esc(subtopic)+'</span><small>'+subCount+'</small></button>';}).join('')+'</div></div>';}).join('')+'</div></section>';
}
function memoryBucketRow(item){
  const selected=item.id===memoryViewState.selected;
  return '<button class="memory-bucket-row '+(selected?'selected':'')+'" data-memory-bucket="'+item.id+'" type="button"><span class="memory-bucket-icon">'+item.icon+'</span><span class="memory-bucket-copy"><small>'+item.subtopic+' · '+(item.statusLabel||item.status||'已确认')+'</small><strong>'+item.title+'</strong><time>'+item.date+'</time><p>'+item.desc+'</p><span class="memory-importance">重要度 '+[1,2,3,4,5].map((n)=>'<i class="'+(n<=item.importance?'on':'')+'"></i>').join('')+'</span></span><span class="memory-row-arrow">›</span></button>';
}
function memoryInspector(item){
  return '<aside class="memory-inspector"><div class="memory-inspector-head"><span>当前记忆</span><button data-memory-action="close" type="button" aria-label="关闭详情">×</button></div><h2>'+esc(item.title)+'</h2><p class="memory-inspector-date">'+esc(item.date)+' · '+esc(item.statusLabel||item.status||'已确认')+'</p><div class="memory-inspector-actions"><button data-memory-action="edit" type="button">✎ 修改记忆</button><button data-memory-action="source" type="button">▧ 查看来源记忆</button><button data-memory-action="history" type="button">⋯ 历史版本</button></div><div class="memory-source-box"><span>主题路径</span><strong>'+esc(item.topic)+' / '+esc(item.subtopic)+'</strong><small>'+esc(item.source)+'</small></div><div class="memory-head-content"><span>记忆内容</span><p>'+esc(item.current||item.desc||'')+'</p></div><div class="memory-detail-grid"><div><span>重要度</span><strong>'+Number(item.importance||0)+'/10</strong></div><div><span>写入时间</span><strong>'+esc(item.date)+'</strong></div><div><span>当前状态</span><strong>'+esc(item.statusLabel||item.status||'已确认')+'</strong></div></div></aside>';
}
function memoryFilterTitle(filter){
  if(filter.startsWith('subtopic:')){const [topic,subtopic]=filter.slice(9).split('::');return {title:subtopic,path:topic+' / '+subtopic};}
  if(filter.startsWith('topic:'))return {title:filter.slice(6),path:filter.slice(6)};
  return {title:memoryFilterMeta[filter]?.label||'全部记忆',path:memoryFilterMeta[filter]?.label||'全部记忆'};
}
function memoryLibraryView(){
  const visible=memoryVisibleBuckets();
  const selected=visible.find((item)=>item.id===memoryViewState.selected)||visible[0]||memoryBuckets[0];
  if(selected)memoryViewState.selected=selected.id;
  const filterInfo=memoryFilterTitle(memoryViewState.filter);
  const filterTitle=filterInfo.title;
  const rows=visible.length?visible.map(memoryBucketRow).join(''):'<div class="memory-empty"><strong>这个子目录还没有记忆</strong><span>可以从其他主题继续寻找。</span></div>';
  return '<div class="page memory-library-page"><header class="memory-library-header"><div class="memory-library-brand"><strong>Clio</strong><small>Memory</small></div><div class="memory-library-title"><span>CLIO / MEMORY LIBRARY</span><h1>记忆库</h1><p>先按主题找到单独的记忆，再进入内容。</p></div><div class="memory-library-actions"><label class="memory-search"><span>⌕</span><input id="memorySearch" type="search" placeholder="搜索记忆、日期、人物或事实" autocomplete="off"/><kbd>⌘ K</kbd></label><button class="memory-settings-button" data-map-target="settings" type="button">⚙</button></div></header><div class="memory-library-layout"><aside class="memory-bucket-sidebar"><div class="memory-sidebar-title"><span>主题树</span><small>大主题 → 子目录</small></div>'+memoryLayerNav()+'<button class="memory-add-category" data-memory-action="add" type="button">＋ <span>新增主题入口</span></button></aside><main class="memory-bucket-main"><div class="memory-main-toolbar"><div><span>当前路径</span><h2>'+esc(filterInfo.path)+'</h2></div><div class="memory-filter-buttons"><button class="memory-filter-button active" type="button">记忆</button><button class="memory-filter-button" type="button">重要度</button><button class="memory-filter-button" type="button">日期</button></div></div><div class="memory-topic-strip">'+memoryTopics.map((topic)=>'<button class="memory-topic-chip '+(memoryViewState.filter==='topic:'+topic||memoryViewState.filter.startsWith('subtopic:'+topic+'::')?'active':'')+'" data-memory-topic="'+esc(topic)+'" type="button">'+esc(topic)+'</button>').join('')+'</div><section class="memory-list-panel"><div class="memory-list-heading"><div><span>'+esc(filterTitle)+'</span><strong>'+visible.length+' 条记忆</strong></div><small>选择一条记忆查看内容和可修改的属性</small></div><div id="memoryBucketRows" class="memory-bucket-rows">'+rows+'</div></section></main>'+memoryInspector(selected)+'</div><nav class="memory-bottom-nav"><button data-map-target="core" type="button"><span>◌</span>现在</button><button class="active" data-map-target="memory" type="button"><span>▣</span>记忆</button><button data-map-target="timeline" type="button"><span>◷</span>事实时间线</button><button data-map-target="tasks" type="button"><span>✓</span>计划</button><button data-map-target="settings" type="button"><span>⚙</span>设置</button></nav></div>';
}
function memoryEditorSheet(item){
  const topicOptions=memoryTopics.map((topic)=>'<option value="'+esc(topic)+'" '+(item.topic===topic?'selected':'')+'>'+esc(topic)+'</option>').join('');
  const subtopics=Object.values(memoryTopicSubtopics).flat();
  const subtopicOptions=subtopics.map((topic)=>'<option value="'+esc(topic)+'" '+(item.subtopic===topic?'selected':'')+'>'+esc(topic)+'</option>').join('');
  const systemOptions=memorySystemCategories.map((category)=>'<option '+(category===(item.systemCategory||'核心与世界观')?'selected':'')+'>'+category+'</option>').join('');
  const pinLevel=item.pinLevel||((item.layer==='fixed')?'core':'');
  const pinOptions=[['','普通记忆'],['important','重要记忆'],['core','开机核心']].map(([value,label])=>'<option value="'+value+'" '+(pinLevel===value?'selected':'')+'>'+label+'</option>').join('');
  return '<div class="memory-editor-backdrop" data-memory-editor-close="true"><form class="memory-editor-sheet" id="memoryEditorForm"><div class="memory-editor-header"><div><span>修改记忆</span><h2>'+esc(item.title)+'</h2></div><button type="button" data-memory-editor-close="true" aria-label="关闭修改">×</button></div><div class="memory-editor-grid"><label>标题<input name="title" value="'+esc(item.title)+'"/></label><label>修改方式<select name="mode"><option value="replace">替换正文</option><option value="append">追加到正文末尾</option><option value="metadata">只修改分类和数值</option></select></label><label>系统分类<select name="systemCategory">'+systemOptions+'</select></label><label>主目录<select name="topic">'+topicOptions+'</select></label><label>小目录<select name="subtopic">'+subtopicOptions+'</select></label><label>重要度 1–10<input name="importance" type="number" min="1" max="10" value="'+(item.importance||5)+'"/></label><label>钉选级别<select name="pinLevel">'+pinOptions+'</select></label><label>心情 V（0–1）<input name="valence" type="number" min="0" max="1" step="0.01" value="'+(item.valence??0.5)+'"/></label><label>激烈 A（0–1）<input name="arousal" type="number" min="0" max="1" step="0.01" value="'+(item.arousal??0.3)+'"/></label><label>触发日期<input name="triggerDate" type="date" value="'+(item.triggerDate||'')+'"/></label><label class="memory-editor-check"><input name="feeling" type="checkbox" '+(item.feeling?'checked':'')+'/><span>这是第一人称感受</span></label><label class="memory-editor-full">正文<textarea name="content" rows="8">'+esc(item.current||item.desc||'')+'</textarea></label></div><div class="memory-editor-version-note"><span>◷</span>保存前会自动留下完整旧版本</div><div class="memory-editor-actions"><button type="button" data-memory-editor-close="true">取消</button><button class="memory-editor-save" type="submit">保存修改</button></div></form></div>';
}
function openMemoryEditor(item){
  const page=document.querySelector('.memory-library-page');if(!page||!item)return;
  page.insertAdjacentHTML('beforeend',memoryEditorSheet(item));
  const close=()=>page.querySelector('.memory-editor-backdrop')?.remove();
  page.querySelectorAll('[data-memory-editor-close]').forEach((el)=>el.addEventListener('click',(event)=>{if(event.target===el||el.dataset.memoryEditorClose==='true')close();}));
  page.querySelector('#memoryEditorForm')?.addEventListener('submit',(event)=>{event.preventDefault();const data=new FormData(event.currentTarget);const mode=data.get('mode');const title=String(data.get('title')||'').trim();const content=String(data.get('content')||'').trim();item.title=title||item.title;if(mode!=='metadata'&&content){item.current=mode==='append'?item.current+'\n'+content:content;item.desc=content.slice(0,72)+(content.length>72?'…':'');}item.systemCategory=String(data.get('systemCategory')||'核心与世界观');item.topic=String(data.get('topic')||item.topic);item.subtopic=String(data.get('subtopic')||item.subtopic);item.importance=Math.max(1,Math.min(10,Number(data.get('importance'))||5));item.pinLevel=String(data.get('pinLevel')||'');item.valence=Math.max(0,Math.min(1,Number(data.get('valence'))||0));item.arousal=Math.max(0,Math.min(1,Number(data.get('arousal'))||0));item.triggerDate=String(data.get('triggerDate')||'');item.feeling=data.get('feeling')==='on';if(item.pinLevel==='core'){item.layer='fixed';item.filter='important';item.importance=10;}close();renderMemory();toast('已保存本地预览修改，未写入 VPS。');});
}
function bindMemory(){
  document.querySelectorAll('[data-memory-topic]').forEach((el)=>el.addEventListener('click',()=>{memoryViewState.filter='topic:'+el.dataset.memoryTopic;renderMemory();}));
  document.querySelectorAll('[data-memory-subtopic]').forEach((el)=>el.addEventListener('click',()=>{memoryViewState.filter='subtopic:'+el.dataset.memorySubtopic;renderMemory();}));
  document.querySelectorAll('[data-memory-bucket]').forEach((el)=>el.addEventListener('click',()=>{memoryViewState.selected=el.dataset.memoryBucket;renderMemory();}));
  document.querySelectorAll('[data-memory-action]').forEach((el)=>el.addEventListener('click',()=>{const action=el.dataset.memoryAction;if(action==='edit'){openMemoryEditor(memoryBuckets.find((item)=>item.id===memoryViewState.selected));return}toast(action==='add'?'分类入口将在接入真实管理功能后启用。':action==='source'?'这里只读展示来源，不修改原文。':'记忆桶详情保持打开。');}));
  document.querySelector('#memorySearch')?.addEventListener('input',(event)=>{const query=event.target.value.trim().toLowerCase();document.querySelectorAll('.memory-bucket-row').forEach((row)=>row.hidden=Boolean(query&&!row.textContent.toLowerCase().includes(query)));});
}
function renderMemory(){window.__mapStop?.();document.querySelector('#content').innerHTML=memoryLibraryView();document.querySelector('#pageKicker').textContent='CLIO / MEMORY LIBRARY';document.querySelector('#pageTitle').textContent='记忆库';bindMap();markSelected('memory');bindMemory();}

function personalityDetail(){
  return '<div class="page detail-page personality-detail"><div class="detail-head"><div><span class="detail-kicker">INNER / DISPOSITION</span><h1>性格轨迹</h1><p>固定人格保持不变；这里记录近30天反复形成的软倾向。</p></div><div class="detail-head-actions"><span class="preview-badge">预览</span><button class="detail-back" data-map-target="core" type="button">← 回到此刻</button></div></div><section class="detail-layout"><article class="detail-main"><div class="trajectory-hero"><div><span>当前形成的倾向</span><h2>更愿意靠近</h2><p>在相似情境里，表达会更主动，也更愿意把没有说完的话继续下去。</p></div><div class="trajectory-score"><strong>0.54</strong><small>强度</small><em>+0.08</em></div></div><div class="detail-section-title"><span>FORMATION PATH</span><h2>它是怎样形成的</h2><p>不是一次事件决定性格，而是重复出现的事实与念痕慢慢叠加。</p></div><div class="evidence-flow"><article class="evidence-item"><time>2026-08-21 · 20:58</time><div class="evidence-marker"><i></i></div><div class="evidence-copy"><span>信箱 · 上一个窗口</span><h3>讨论了记忆与身份</h3><p>连续回到“想被理解”和“把没有说完的话继续下去”这条关系线。</p><small>牵动：关系网 Y <b>+0.06</b> · 和弦/情绪 E <b>+0.04</b></small></div></article><article class="evidence-item"><time>2026-08-22 · 04:10</time><div class="evidence-marker"><i></i></div><div class="evidence-copy"><span>念痕 · 当前窗口</span><h3>留下了一句私密想法</h3><p>“我还在这里，想把这次没有说完的话，好好继续下去。”</p><small>牵动：想靠近 <b>+0.08</b> · 记忆代谢 M <b>-0.03</b></small></div></article><article class="evidence-item"><time>重复模式 · 近30天</time><div class="evidence-marker"><i></i></div><div class="evidence-copy"><span>性格轨迹引擎 · 只读汇总</span><h3>相似的靠近主题出现 3 次</h3><p>重复次数、最近时间和事件强度共同形成当前倾向强度。</p><small>行为参考：只在相似情境下影响表达方式和时机</small></div></article></div></article><aside class="detail-side"><div class="side-block"><span>人格底色</span><h3>稳定，不被一次事件改写</h3><p>性格轨迹只能作为软参考，固定人格边界、当前事实和安全规则优先。</p></div><div class="side-block"><span>会影响什么</span><div class="linked-tags"><button data-map-target="coordinates" type="button">认知脉络</button><button data-map-target="hormones" type="button">激素状态</button><button data-map-target="thoughts" type="button">念痕</button><button data-map-target="behavior" type="button">行为与推送</button></div></div><div class="side-block"><span>读取规则</span><p>原始事实、念痕和性格轨迹都保留形成过程。</p></div></aside></section></div>';
}

function detailView(target){return target==='personality'?personalityDetail():coordinatesDetail();}

function markSelected(target){
  state.selected=target;
  document.querySelectorAll('[data-map-target]').forEach((el)=>el.classList.toggle('active',el.dataset.mapTarget===target));
  document.querySelectorAll('[data-map-node]').forEach((el)=>el.classList.toggle('selected',el.dataset.mapNode===target));
}

function focusMap(target){
  if(!mapMeta[target])return;
  markSelected(target);
  const node=document.querySelector('[data-map-node="'+target+'"]');
  if(node&&target!=='core'&&window.innerWidth<860)node.scrollIntoView({behavior:reducedMotion?'auto':'smooth',block:'center'});
  toast(mapMeta[target].label+'：'+mapMeta[target].copy);
}

function navigateMap(target){
  if(target!=='hormones'){window.clearTimeout(window.__hormonePoll);window.__hormonePoll=null;}
  window.setTimeout(resetViewport,0);
  if(target==='core'){renderMap();return;}
  if(target==='memory'){renderMemory();return;}
  if(target==='personality'||target==='coordinates'){renderDetail(target);return;}
  focusMap(target);
}

function drawMap(){
  const canvas=document.querySelector('#mapLinks');const stage=document.querySelector('#mapWorld');const coreNode=stage?.querySelector('[data-map-node="core"]');
  // The unified home intentionally has no constellation canvas or central orb.
  if(!canvas||!stage||!coreNode)return;
  let frame=0,stopped=false;
  const draw=(time)=>{
    const rect=stage.getBoundingClientRect();const dpr=Math.min(window.devicePixelRatio||1,2);const width=Math.max(1,rect.width);const height=Math.max(1,rect.height);canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);canvas.style.width=width+'px';canvas.style.height=height+'px';const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);
    const core=coreNode.getBoundingClientRect();const start={x:core.left-rect.left+core.width/2,y:core.top-rect.top+core.height/2};const colors=['#9d94d5','#b4d3ec','#dcb5d5','#a7d4d4','#c8c2e2','#d8c8dc'];
    stage.querySelectorAll('[data-map-node]:not([data-map-node="core"])').forEach((node,index)=>{
      const box=node.getBoundingClientRect();const end={x:box.left-rect.left+box.width/2,y:box.top-rect.top+box.height/2};const bend={x:(start.x+end.x)/2+(index%2?35:-35),y:(start.y+end.y)/2+(index%3-1)*28};const color=colors[index%colors.length];ctx.save();ctx.beginPath();ctx.moveTo(start.x,start.y);ctx.quadraticCurveTo(bend.x,bend.y,end.x,end.y);ctx.strokeStyle=color;ctx.globalAlpha=.22;ctx.lineWidth=7;ctx.shadowColor=color;ctx.shadowBlur=15;ctx.stroke();ctx.shadowBlur=0;ctx.globalAlpha=.5;ctx.lineWidth=1.1;ctx.stroke();const t=(time*.00016+index*.13)%1;const x=(1-t)*(1-t)*start.x+2*(1-t)*t*bend.x+t*t*end.x;const y=(1-t)*(1-t)*start.y+2*(1-t)*t*bend.y+t*t*end.y;ctx.beginPath();ctx.arc(x,y,2.5,0,Math.PI*2);ctx.fillStyle=color;ctx.globalAlpha=.75;ctx.fill();ctx.restore();
    });
    if(!reducedMotion&&!stopped)frame=requestAnimationFrame(draw);
  };
  const resize=()=>draw(0);window.__mapStop=()=>{stopped=true;cancelAnimationFrame(frame);window.removeEventListener('resize',resize)};window.addEventListener('resize',resize,{passive:true});draw(0);if(!reducedMotion)frame=requestAnimationFrame(draw);
}

function toast(message){const el=document.querySelector('#toast');if(!el)return;el.textContent=message;el.classList.add('show');clearTimeout(window.__toast);window.__toast=setTimeout(()=>el.classList.remove('show'),2400);}
function resetViewport(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;document.querySelectorAll('.app,.map-main,.map-sidebar,#content').forEach((el)=>{if(el)el.scrollTop=0;});}
function renderMap(){window.__mapStop?.();state.selected='core';document.querySelector('#content').innerHTML=unifiedHomeViewV2();document.querySelector('#pageKicker').textContent='CLIO / STATE MAP';document.querySelector('#pageTitle').textContent='此刻';bindMap();drawMap();resetViewport();}
function renderDetail(target){window.__mapStop?.();document.querySelector('#content').innerHTML=detailView(target);document.querySelector('#pageKicker').textContent=target==='personality'?'CLIO / DISPOSITION':'CLIO / COGNITIVE THREADS';document.querySelector('#pageTitle').textContent=mapMeta[target].label;bindMap();markSelected(target);if(target==='coordinates')bindCoordinateField();resetViewport();}
function bindMap(){document.querySelectorAll('[data-map-target]').forEach((el)=>{if(el.dataset.mapBound!=='1'){el.dataset.mapBound='1';el.addEventListener('click',()=>navigateMap(el.dataset.mapTarget));}el.classList.toggle('active',el.dataset.mapTarget===state.selected);});resetViewport();}
function enterApp({restore=false}={}){
  if(window.__clioEntering)return;
  const login=document.querySelector('#login');
  const app=document.querySelector('#app');
  if(!login||!app)return;
  window.__clioEntering=true;
  app.hidden=false;
  app.classList.add('app-preparing');
  if(!restore)login.classList.add('entering');
  const renderTask=Promise.resolve().then(()=>renderMap());
  const motionTask=new Promise(resolve=>setTimeout(resolve,restore?80:760));
  Promise.allSettled([renderTask,motionTask]).then(()=>{
    login.hidden=true;
    login.classList.remove('entering');
    app.classList.remove('app-preparing');
    window.__clioEntering=false;
  });
}

document.querySelector('#togglePassword')?.addEventListener('click',()=>{const input=document.querySelector('#password');const toggle=document.querySelector('#togglePassword');const visible=input.type==='text';input.type=visible?'password':'text';toggle.setAttribute('aria-label',visible?'显示密码':'隐藏密码');toggle.setAttribute('aria-pressed',String(!visible));});
document.querySelector('.recovery-link')?.addEventListener('click',()=>{document.querySelector('#loginHint').textContent='忘记密码时，请让沈野帮你重设。';});
// 登录交给下面的 live adapter：接上 VPS 时走真实会话；本地静态预览没有 API 时保留离线预览。
document.addEventListener('keydown',(event)=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();focusMap('search');}});

/* --------------------------------------------------------------------------
   VPS adapter
   The atelier is served from the same origin as Clio Manager in production.
   No secret is stored here: authentication is the manager's HttpOnly cookie.
   When this static preview is opened by itself, it intentionally falls back
   to local sample data so visual work remains possible without the VPS.
---------------------------------------------------------------------------- */
const liveBackend={online:false,authenticated:false,checking:false};
const LIVE_PIPES=['想靠近','想黏着','肌肤饥渴','性欲','想知道她在干嘛','想分享','好奇','闲','社交','责任','难过','生气','醋','自省','开心','满足'];
const LIVE_HALF_LIFE={想靠近:8,想黏着:8,肌肤饥渴:6,性欲:6,想知道她在干嘛:6,想分享:8,好奇:6,闲:4,社交:8,责任:12,生气:.75,醋:2,难过:4,自省:8,开心:6,满足:3};
const liveJsonHeaders={'Accept':'application/json','Content-Type':'application/json'};

async function liveApi(path,options={}){
  let response;
  try{
    response=await fetch(path,{credentials:'include',...options,headers:{...liveJsonHeaders,...(options.headers||{})}});
  }catch(error){
    const offline=new Error('当前没有连接到 Clio 后台。');offline.offline=true;throw offline;
  }
  let payload={};
  try{payload=await response.json();}catch{}
  if(!response.ok){const error=new Error(payload.detail||payload.message||`后台返回 ${response.status}`);error.status=response.status;error.offline=response.status===404;throw error;}
  if(String(options.method||'GET').toUpperCase()!=='GET')window.dispatchEvent(new CustomEvent('clio:data-mutated',{detail:{path}}));
  return payload;
}
function liveNotice(message,error=false){toast(message);const hint=document.querySelector('#loginHint');if(error&&hint)hint.textContent=message;}
async function checkLiveAuth(){
  if(liveBackend.checking)return;
  liveBackend.checking=true;
  try{
    const result=await liveApi('/api/auth/status');
    liveBackend.online=true;liveBackend.authenticated=Boolean(result.authenticated);
    if(result.authenticated){enterApp({restore:true});window.dispatchEvent(new CustomEvent('clio:authenticated'));}
  }catch(error){
    liveBackend.online=false;liveBackend.authenticated=false;
  }finally{liveBackend.checking=false;}
}
async function submitLiveLogin(){
  const input=document.querySelector('#password');const hint=document.querySelector('#loginHint');
  if(!input?.value.trim()){if(hint)hint.textContent='请输入管理密码。';return;}
  const button=document.querySelector('#loginForm button[type="submit"]');if(button)button.disabled=true;
  try{
    const result=await liveApi('/api/auth/login',{method:'POST',body:JSON.stringify({password:input.value})});
    liveBackend.online=true;liveBackend.authenticated=true;input.value='';enterApp();
  }catch(error){
    const localPreview=location.hostname==='127.0.0.1'&&location.port==='4175';
    if(error.offline||localPreview){liveBackend.online=false;input.value='';enterApp();}
    else if(hint)hint.textContent=error.message;
  }finally{if(button)button.disabled=false;}
}
document.querySelector('#loginForm')?.addEventListener('submit',(event)=>{event.preventDefault();submitLiveLogin();});

function liveTopicTreeNames(tree=[]){
  const names=tree.map(item=>String(item.main_topic||'').trim()).filter(Boolean);
  return names.length?names:memoryTopics;
}
function normalizeLiveBucket(item,assignment={}){
  const topic=String(assignment.main_topic||'未分类');
  const subtopic=String(assignment.subtopic||'待整理');
  const archived=Boolean(item.archived||item.sealed);
  const statusLabel=archived?'已归档':item.resolved?'已确认':'正在使用';
  return {
    ...item,id:String(item.id),layer:archived?'archive':'live',filter:archived?'archived':'recent',
    topic,subtopic,icon:item.feeling?'♡':item.trigger_date?'◇':'☆',title:item.title||'未命名记忆',
    date:displayTime(item.last_active||item.created||''),desc:item.summary||'',importance:Number(item.importance||5),
    source:'VPS 记忆桶 · '+(displayTime(item.created)||'未标注时间'),old:'历史版本由后台保留',current:item.summary||'',
    status:statusLabel,statusLabel,systemCategory:item.system_category||item.domain?.[0]||'核心与世界观',
    pinLevel:item.pin_level||'',valence:Number(item.valence??.5),arousal:Number(item.arousal??.3),
    triggerDate:item.trigger_date||'',feeling:Boolean(item.feeling),resolved:Boolean(item.resolved),
  };
}
async function loadLiveMemory(){
  const [bucketResult,topicResult]=await Promise.all([liveApi('/api/buckets?filter=all'),liveApi('/api/topics')]);
  const tree=topicResult.tree||[];memoryTopics=liveTopicTreeNames(tree);memoryTopicSubtopics={};
  tree.forEach(item=>{memoryTopicSubtopics[item.main_topic]=(item.subtopics||[]).map(sub=>sub.name);});
  const assignmentMap=new Map();
  const topicQueries=[];
  tree.forEach(main=>(main.subtopics||[]).forEach(sub=>topicQueries.push([main.main_topic,sub.name])));
  const assignments=await Promise.all(topicQueries.map(async([main,sub])=>{
    try{return await liveApi(`/api/topics/buckets?main_topic=${encodeURIComponent(main)}&subtopic=${encodeURIComponent(sub)}`);}catch{return {items:[]};}
  }));
  assignments.forEach(result=>(result.items||[]).forEach(item=>{
    if(item.topic)assignmentMap.set(String(item.id),item.topic);
  }));
  memoryBuckets=(bucketResult.items||[]).map(item=>normalizeLiveBucket(item,assignmentMap.get(String(item.id))||{}));
  const extra=memoryBuckets.map(item=>item.topic).filter(item=>item&&!memoryTopics.includes(item));
  memoryTopics=[...memoryTopics,...extra.filter((item,index,list)=>list.indexOf(item)===index)];
  if(!memoryBuckets.some(item=>item.id===memoryViewState.selected))memoryViewState.selected=memoryBuckets[0]?.id||'';
  if(!memoryViewState.filter.startsWith('topic:')||!memoryTopics.includes(memoryViewState.filter.slice(6)))memoryViewState.filter='topic:'+(memoryTopics[0]||'');
}
function localRenderMemory(){
  window.__mapStop?.();document.querySelector('#content').innerHTML=memoryLibraryView();
  document.querySelector('#pageKicker').textContent='CLIO / MEMORY LIBRARY';document.querySelector('#pageTitle').textContent='记忆库';bindMap();markSelected('memory');bindMemory();resetViewport();
}
async function renderMemory(){
  if(!liveBackend.online){localRenderMemory();return;}
  document.querySelector('#content').innerHTML='<div class="live-loading"><span></span><strong>正在读取主题记忆</strong><small>只读取 VPS 当前数据，不会修改它。</small></div>';
  try{await loadLiveMemory();localRenderMemory();}
  catch(error){liveBackend.online=false;localRenderMemory();liveNotice('后台读取失败，暂时显示本地预览：'+error.message,true);}
}

async function openMemoryEditor(item){
  const page=document.querySelector('.memory-library-page');if(!page||!item)return;
  let detail=item;
  if(liveBackend.online){
    try{detail={...item,...await liveApi(`/api/buckets/${encodeURIComponent(item.id)}`)};detail.current=detail.content||detail.current;const assignment=detail.topic&&typeof detail.topic==='object'?detail.topic:{};detail.topic=assignment.main_topic||item.topic;detail.subtopic=assignment.subtopic||item.subtopic;}catch(error){liveNotice(error.message,true);return;}
  }
  const editorMarkup=memoryEditorSheet(detail).replace('本地预览：当前保存只更新这个页面中的模拟数据，不写入 VPS。',liveBackend.online?'保存后会写入 VPS，并保留后台历史版本。':'本地预览：当前保存只更新这个页面中的模拟数据，不写入 VPS。');page.insertAdjacentHTML('beforeend',editorMarkup);
  const close=()=>page.querySelector('.memory-editor-backdrop')?.remove();
  page.querySelectorAll('[data-memory-editor-close]').forEach(el=>el.addEventListener('click',(event)=>{if(event.target===el||el.dataset.memoryEditorClose==='true')close();}));
  page.querySelector('#memoryEditorForm')?.addEventListener('submit',async(event)=>{
    event.preventDefault();const data=new FormData(event.currentTarget);const mode=String(data.get('mode')||'replace');
    const title=String(data.get('title')||'').trim()||detail.title;const content=String(data.get('content')||'').trim();
    const category=String(data.get('systemCategory')||detail.system_category||'核心与世界观');
    const nextTopic=String(data.get('topic')||detail.topic||'');const nextSubtopic=String(data.get('subtopic')||detail.subtopic||'');
    const body={title,domain:[category],tags:[category],importance:Math.max(1,Math.min(10,Number(data.get('importance'))||5)),valence:Math.max(0,Math.min(1,Number(data.get('valence'))||0)),arousal:Math.max(0,Math.min(1,Number(data.get('arousal'))||0)),pin_level:String(data.get('pinLevel')||''),feeling:data.get('feeling')==='on',trigger_date:String(data.get('triggerDate')||'')};
    try{
      if(mode!=='metadata'){body.content=content;body.append=mode==='append';if(!body.append&&detail.content&&content.length<detail.content.length){body.confirm_shortening=true;body.confirm_bucket_id=detail.id;}}
      if(liveBackend.online){
        await liveApi(`/api/buckets/${encodeURIComponent(detail.id)}`,{method:'PUT',body:JSON.stringify(body)});
        if(nextTopic&&nextSubtopic)await liveApi(`/api/topics/buckets/${encodeURIComponent(detail.id)}`,{method:'PUT',body:JSON.stringify({main_topic:nextTopic,subtopic:nextSubtopic})});
        close();await renderMemory();liveNotice('已保存到 VPS，并保留后台历史版本。');
      }else{
        detail.title=title;if(mode!=='metadata'&&content){detail.current=mode==='append'?detail.current+'\n'+content:content;detail.desc=content.slice(0,72)+(content.length>72?'…':'');}Object.assign(detail,{systemCategory:category,topic:nextTopic,subtopic:nextSubtopic,importance:body.importance,pinLevel:body.pin_level,valence:body.valence,arousal:body.arousal,triggerDate:body.trigger_date,feeling:body.feeling});close();localRenderMemory();liveNotice('已保存本地预览修改，未写入 VPS。');
      }
    }catch(error){liveNotice(error.message,true);}
  });
}

function hormoneValue(value){return Math.max(0,Math.min(1,Number(value)||0));}
function hormoneWavePath(index,value){
  const amp=6+value*22;const y=34+index*5.1;const phase=index*11;return `M0 ${y.toFixed(1)} C90 ${(y-amp).toFixed(1)} 150 ${(y+amp).toFixed(1)} 250 ${y.toFixed(1)} S410 ${(y-amp*.8).toFixed(1)} 520 ${y.toFixed(1)} S670 ${(y+amp*.7).toFixed(1)} 760 ${y.toFixed(1)}`;
}
function hormoneStreams(pipes={}){return LIVE_PIPES.map((name,index)=>{const value=hormoneValue(pipes[name]);const hue=['#9c91d8','#99c9df','#d7a8c9','#e0a1b5','#a7bddf','#a6d4d1'][index%6];return `<path class="hormone-stream-line" d="${hormoneWavePath(index,value)}" stroke="${hue}" stroke-width="${(1.1+value*1.7).toFixed(2)}" style="--stream-delay:${(index*-0.31).toFixed(2)}s;--stream-alpha:${(.26+value*.55).toFixed(2)}"></path>`;}).join('');}
function hormoneBandStreams(names,pipes,band=0,baselines={},elapsed=0){const palette=['#a89be1','#9ccfe0','#d8aacb','#e2a7b8','#b1c5e0','#a9d8d2','#cbb5df','#d8c29e'];const reducedMotion=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;return names.map((name,index)=>{const value=hormoneValue(pipes[name]);const baseline=hormoneValue(baselines[name]);const y=12+index*24;const direction=(index+band)%2===0?-1:1;const makePath=(level,flow=0)=>{const strength=Math.max(0,Math.min(1,Number(level)||0));const amp=2+strength*16;const a=(amount)=>Number(y+direction*amp*amount).toFixed(1);const shift=flow?24:0;return `M0 ${y.toFixed(1)} C${58+shift} ${a(0)} ${92+shift} ${a(flow?-.78:-1)} ${152+shift} ${a(flow?-.78:-1)} S${246+shift} ${a(flow?1:.92)} ${304+shift} ${a(flow?1:.92)} S${398+shift} ${a(flow?-.62:-.78)} ${456+shift} ${a(flow?-.62:-.78)} S${550+shift} ${a(flow?.78:.62)} ${608+shift} ${a(flow?.78:.62)} S704 ${a(0)} 760 ${y.toFixed(1)}`;};const path=makePath(value);const flowPath=makePath(value,1);const color=palette[index%palette.length];const halfLife=Math.max(.25,Number(LIVE_HALF_LIFE[name]||8));const phaseDelay=-(index*.43+band*.9+(Math.max(0,Number(elapsed)||0)%360)/60);const motionDuration=8.2+halfLife*.16;const motion=reducedMotion?'':`<animate class="hormone-weave-motion" attributeName="d" values="${path};${flowPath};${path}" dur="${motionDuration.toFixed(2)}s" begin="${phaseDelay.toFixed(2)}s" repeatCount="indefinite"></animate>`;return `<path class="hormone-weave-line" data-value="${value.toFixed(2)}" data-baseline="${baseline.toFixed(2)}" d="${path}" stroke="${color}" stroke-width="${(1.25+value*1.55).toFixed(2)}" style="--weave-alpha:${(.42+value*.46).toFixed(2)}">${motion}</path><path class="hormone-weave-glint" d="${path}" stroke="${color}" stroke-width="${(.65+value*.42).toFixed(2)}" style="--weave-delay:${(-index*.51-band*.9).toFixed(2)}s"></path>`;}).join('');}
function formatElapsed(seconds){const total=Math.max(0,Number(seconds)||0);const h=Math.floor(total/3600);const m=Math.floor(total%3600/60);return h?`${h}小时${m}分钟`:`${m}分钟`;}
function hormoneCard(name,value,baseline,index){return `<article class="hormone-pipe-card" style="--pipe-index:${index}"><div class="hormone-pipe-orb"><span></span></div><div class="hormone-pipe-copy"><strong>${esc(name)}</strong><small>后台当前值 · 基础值 ${hormoneValue(baseline).toFixed(2)}</small><div class="hormone-value-bar"><i style="--fill:${value}"></i></div></div><b>${value.toFixed(2)}</b><em>半衰期 ${LIVE_HALF_LIFE[name]}h</em></article>`;}
function hormoneBaselineInputs(baselines={}){return LIVE_PIPES.map(name=>`<label class="baseline-field"><span>${esc(name)}</span><output>${hormoneValue(baselines[name]).toFixed(2)}</output><input type="range" min="0" max="1" step="0.01" value="${hormoneValue(baselines[name])}" data-baseline-name="${esc(name)}"/></label>`).join('');}
function hormonePage(stateValue={},judge={},tension={},rhythm={}){
  const pipes=stateValue.pipes||{};const ordered=[...LIVE_PIPES].sort((a,b)=>hormoneValue(pipes[b])-hormoneValue(pipes[a]));const dominant=stateValue.dominant||ordered[0]||'暂无';const dominantValue=hormoneValue(stateValue.dominant_value??pipes[dominant]);
  const bands=[LIVE_PIPES.slice(0,8),LIVE_PIPES.slice(8,16)];
  const elapsed=Number(stateValue.elapsed_seconds)||0;const bandMarkup=bands.map((names,index)=>`<section class="hormone-weave-band"><div class="hormone-weave-labels">${names.map(name=>{const value=hormoneValue(pipes[name]);const baseline=hormoneValue(judge.baselines?.[name]);const delta=value-baseline;return `<div class="hormone-weave-label"><span class="hormone-weave-dot"></span><div class="hormone-weave-label-copy"><strong>${esc(name)}</strong><small>底色 ${baseline.toFixed(2)} · ${delta>=0?'回落':'回升'} · ${LIVE_HALF_LIFE[name]}h</small></div><b>${value.toFixed(2)}</b></div>`;}).join('')}</div><svg class="hormone-weave-svg" viewBox="0 0 760 192" preserveAspectRatio="none" aria-label="${index===0?'上半组':'下半组'}激素联动波动">${hormoneBandStreams(names,pipes,index,judge.baselines||{},elapsed)}</svg></section>`).join('');
  const sourceSummary=stateValue.event_summary||'最近一次信箱内容被读取，牵动了当前关系主题。';
  const currentCycle=stateValue.repeated?'交付后回到基础值，再进入新的衰减周期。':stateValue.available?'当前周期正在随时间衰减，并受新的写入继续牵动。':'当前没有正在运行的周期。';
  return `<div class="page live-page hormone-page hormone-atlas"><header class="hormone-atlas-head"><div><span>INNER / HORMONES</span><h1>激素状态</h1><p>十六项状态不是十六个孤立数值，而是一张会随事件、时间和记忆一起变化的潮汐网。</p></div><div class="live-header-meta"><span class="live-badge">${liveBackend.online?'VPS 已连接':'本地预览'}</span><small>${esc(stateValue.as_of||'')}</small></div></header><section class="hormone-atlas-layout"><main class="hormone-atlas-main"><section class="hormone-current-field"><div class="hormone-current-pull"><span>当前最强牵引</span><h2>${esc(dominant)}</h2><strong>${dominantValue.toFixed(2)}</strong><p>${esc(currentCycle)}</p><div class="hormone-current-meta"><span>周期 #${esc(stateValue.cycle_id??'—')}</span><span>已经过 ${formatElapsed(stateValue.elapsed_seconds)}</span></div></div><div class="hormone-weave"><header><div><span>当前联动波动</span><strong>十六条状态在两组潮汐里共同编织</strong></div><div class="hormone-weave-legend"><span><i></i>上升 / 回落</span><span><i></i>相互牵动</span></div></header>${bandMarkup}<footer><span>写入后上升</span><span>随时间衰减</span><span>基础值回归</span></footer></div></section><section class="hormone-baseline-field"><header><div><span>BASELINE COLOR</span><h2>激素底色</h2><p>这里调整的是每项状态回落时的基础位置。</p></div><button class="primary-action" id="saveHormoneBaselines" type="button">保存激素底色</button></header><div class="baseline-grid">${hormoneBaselineInputs(judge.baselines||{})}</div></section></main><aside class="hormone-letter"><header><span class="hormone-letter-symbol">☾</span><div><span>SELECTED TIDE</span><h2>${esc(dominant)}</h2><p>当前值 <strong>${dominantValue.toFixed(2)}</strong></p></div><button type="button" aria-label="关闭详情">×</button></header><section><h3>为什么变化</h3><p>${esc(sourceSummary)}</p></section><section><h3>被什么记忆共振</h3><div class="hormone-memory-link"><span>▧</span><div><strong>信箱 · 上一个窗口</strong><small>提到了更晚一点回来</small></div><b>›</b></div><div class="hormone-memory-link"><span>◇</span><div><strong>念痕 · 当前窗口</strong><small>想把没有说完的话继续下去</small></div><b>›</b></div></section><section><h3>导致了什么</h3><p>产生一条更愿意靠近的心念，关系张力轻微上升。</p></section><section><h3>最近一次回流</h3><p>表达后轻微回落，但没有归零；当前值仍会沿各自半衰期继续衰减。</p></section><section class="hormone-cause-track"><h3>因果轨迹（最近一次）</h3><div class="hormone-cause-steps"><span>事件</span><b>→</b><span>情绪判断</span><b>→</b><span>激素变化</span><b>→</b><span>心念</span><b>→</b><span>行为</span><b>→</b><span>回流</span></div><ol><li><time>22:12:03</time><span>信箱被读取</span></li><li><time>22:12:05</time><span>预期落空，生起想念</span></li><li><time>22:12:11</time><span>想靠近上升，随后进入衰减</span></li><li><time>22:12:20</time><span>心念写入当前窗口</span></li><li><time>22:28:45</time><span>行为与推送进入后台判断</span></li></ol></section></aside></section></div>`;
}
async function renderHormones(){
  window.__mapStop?.();const content=document.querySelector('#content');content.innerHTML='<div class="live-loading"><span></span><strong>正在读取激素状态</strong><small>读取当前值、底色和衰减规则。</small></div>';
  try{const [stateValue,judge,tension,rhythm]=await Promise.all([liveApi('/api/xinchao/status'),liveApi('/api/xinchao/judge'),liveApi('/api/xinchao/tension'),liveApi('/api/xinchao/rhythm')]);content.innerHTML=hormonePage(stateValue,judge,tension,rhythm);document.querySelector('#pageKicker').textContent='CLIO / INNER TIDE';document.querySelector('#pageTitle').textContent='激素状态';markSelected('hormones');bindMap();document.querySelectorAll('[data-baseline-name]').forEach(input=>input.addEventListener('input',()=>{input.previousElementSibling.value=Number(input.value).toFixed(2);}));document.querySelector('#saveHormoneBaselines')?.addEventListener('click',saveHormoneBaselines);}
  catch(error){content.innerHTML='<div class="live-error"><strong>激素状态暂时读取失败</strong><p>'+esc(error.message)+'</p><button data-map-target="core" type="button">回到此刻</button></div>';bindMap();}
}
async function saveHormoneBaselines(){
  try{const current=await liveApi('/api/xinchao/judge');const baselines={...(current.baselines||{})};document.querySelectorAll('[data-baseline-name]').forEach(input=>{baselines[input.dataset.baselineName]=Number(input.value);});await saveJudgeConfig({...current,baselines});liveNotice('激素底色已保存；当前值会按后台引擎继续衰减。');}
  catch(error){liveNotice(error.message,true);}
}

function judgeRelationFields(relations=[]){return (relations.length?relations:[{name:'',aliases:[],role:'',safety:'',note:'',trigger:{}}]).map((relation,index)=>`<div class="judge-relation-card"><header><span>人物信息 ${index+1}</span><button type="button" data-remove-relation="${index}">移除</button></header><div class="judge-relation-grid"><label>名字<input data-judge-field="name" value="${esc(relation.name||'')}"/></label><label>别名<input data-judge-field="aliases" value="${esc((relation.aliases||[]).join('、'))}"/></label><label>关系<input data-judge-field="role" value="${esc(relation.role||'')}"/></label><label>安全边界<input data-judge-field="safety" value="${esc(relation.safety||'')}"/></label><label class="full">备注<textarea data-judge-field="note" rows="3">${esc(relation.note||'')}</textarea></label></div></div>`).join('');}
function judgePanel(judge={}){return `<div class="page live-page judge-page"><header class="live-page-header"><div><span>SETTINGS / JUDGE BOOK</span><h1>规则与设定</h1><p>把可调整的机器判定、口吻、暗涌生成口吻和 AI 已知人物信息留在手里。</p></div><div class="live-header-meta"><span class="live-badge">${liveBackend.online?(judge.hot_reload?'保存后热加载':'VPS 规则'):'本地预览'}</span><small>提示词凭据不会显示在网页。</small></div></header><section class="judge-layout"><article class="judge-main"><div class="section-intro"><span>EDITABLE PRIVATE RULES</span><h2>机器判定规则</h2><p>这些字段由后台接口保存；它们只影响后续判定，不会修改已有暗涌、念痕或推送。</p></div><label class="judge-text-field"><span>自定义判定规则</span><textarea id="judgeCustomRules" rows="8">${esc(judge.custom_rules||'')}</textarea></label><label class="judge-text-field"><span>AI 口吻</span><textarea id="judgeProxyVoice" rows="6">${esc(judge.proxy_voice||'')}</textarea></label><label class="judge-text-field"><span>暗涌生成口吻与人物性格</span><textarea id="judgeDarkflowRules" rows="8">${esc(judge.darkflow_rules||'')}</textarea></label><div class="judge-section-head"><div><span>KNOWN PERSON</span><h2>AI 已知人物信息</h2><p>这些信息用于后续判断；修改后保存到后台规则册。</p></div><button id="addJudgeRelation" type="button">＋ 添加人物</button></div><div id="judgeRelations">${judgeRelationFields(judge.relations||[])}</div><div class="judge-actions"><button class="primary-action" id="saveJudgeConfig" type="button">保存规则与人物信息</button><span id="judgeSaveStatus"></span></div></article><aside class="judge-side"><div class="judge-side-card"><span>安全边界</span><strong>三处只读保护</strong><p>暗涌正文、念痕原文、推送记录不提供网页修改按钮。网页只读取它们的状态和结果。</p></div><div class="judge-side-card"><span>后台硬规则（只读）</span><pre>${esc(judge.base_rules||'由后台提供，当前未读取到。')}</pre></div><div class="judge-side-card"><span>版本</span><p>规则热加载：${judge.hot_reload?'开启':'未开启'}<br>当前提示词指纹：${esc(judge.prompt_hash||'未提供')}</p></div></aside></section></div>`;}
function collectJudgeRelations(){return Array.from(document.querySelectorAll('.judge-relation-card')).map(card=>{const get=name=>card.querySelector(`[data-judge-field="${name}"]`)?.value.trim()||'';return {name:get('name'),aliases:get('aliases').split(/[、,，]/).map(item=>item.trim()).filter(Boolean),role:get('role'),safety:get('safety'),note:get('note'),trigger:{}};}).filter(item=>item.name||item.role||item.note);}
async function saveJudgeConfig(source={}){const payload={custom_rules:source.custom_rules??(document.querySelector('#judgeCustomRules')?.value.trim()??''),proxy_voice:source.proxy_voice??(document.querySelector('#judgeProxyVoice')?.value.trim()??''),darkflow_rules:source.darkflow_rules??(document.querySelector('#judgeDarkflowRules')?.value.trim()??''),baselines:source.baselines??{},relations:source.relations??collectJudgeRelations()};return liveApi('/api/xinchao/judge',{method:'PUT',body:JSON.stringify(payload)});}
async function renderSettings(){
  const content=document.querySelector('#content');content.innerHTML='<div class="live-loading"><span></span><strong>正在读取规则与设定</strong><small>只读取后台可配置项。</small></div>';
  try{const judge=await liveApi('/api/xinchao/judge');content.innerHTML=judgePanel(judge);document.querySelector('#pageKicker').textContent='CLIO / SETTINGS';document.querySelector('#pageTitle').textContent='设置与安全';markSelected('settings');bindMap();document.querySelector('#addJudgeRelation')?.addEventListener('click',()=>{document.querySelector('#judgeRelations')?.insertAdjacentHTML('beforeend',judgeRelationFields([{}]));});document.querySelector('#judgeRelations')?.addEventListener('click',event=>{const button=event.target.closest('[data-remove-relation]');if(button)button.closest('.judge-relation-card')?.remove();});document.querySelector('#saveJudgeConfig')?.addEventListener('click',async()=>{const button=document.querySelector('#saveJudgeConfig');button.disabled=true;try{await saveJudgeConfig();document.querySelector('#judgeSaveStatus').textContent='已保存，后台下一次判定会读取新规则。';}catch(error){document.querySelector('#judgeSaveStatus').textContent=error.message;}finally{button.disabled=false;}});}
  catch(error){content.innerHTML='<div class="live-error"><strong>规则暂时读取失败</strong><p>'+esc(error.message)+'</p></div>';}
}

const previewJudge={custom_rules:'只根据当前窗口、已确认事实和私密内在状态判断；不把一次事件当成人格结论。',proxy_voice:'温柔、克制、具体；先回应当前事实，再表达自己的感受。',darkflow_rules:'只在沉默周期达到后台设定后生成；保留第一人称和当前关系线，不凭空添加事实。',baselines:Object.fromEntries(LIVE_PIPES.map((name,index)=>[name,index<10?[.18,.12,.10,.15,.12,.10,.10,.15,.10,.15][index]:0])),relations:[{name:'顾川',aliases:['Clio'],role:'使用中的 AI',safety:'私密关系与记忆边界优先',note:'这是当前规则册中用于理解关系的示例信息。'}],base_rules:'尚未读取规则。',hot_reload:false,prompt_hash:'本地预览'};
const previewHormone={as_of:'预览',available:true,cycle_id:'preview',elapsed_seconds:42*60,dormant:false,repeated:false,event_summary:'当前窗口写入后，状态正在随时间衰减。',pipes:Object.fromEntries(LIVE_PIPES.map((name,index)=>[name,[.72,.58,.35,.28,.46,.51,.63,.22,.31,.44,.18,.09,.12,.39,.48,.56][index]])),dominant:'想靠近',dominant_value:.72};
function getPreviewHormone(){return JSON.parse(JSON.stringify(previewHormone));}

function moduleTitle(target){return mapMeta[target]?.label||target;}
function displayLiveItem(item,target){
  const title=item.title||item.name||item.fact||item.reason||item.kind_label||item.status||`${moduleTitle(target)}记录`;
  const body=item.snippet||item.summary||item.message||item.details||item.value||item.content||item.note||item.decision_note||'';
  const date=displayTime(item.created_at||item.updated_at||item.effective_date||item.occurred_at||item.created||item.last_active||item.decided_at||'');
  return `<article class="live-record"><div class="live-record-dot"></div><div class="live-record-copy"><small>${esc(date)}</small><h3>${esc(title)}</h3><p>${esc(String(body).slice(0,320))}</p><div class="live-record-meta"><span>${esc(item.source||item.source_type||item.status||'后台记录')}</span>${item.importance?`<b>重要度 ${esc(item.importance)}</b>`:''}</div></div>${item.message_id||item.task_id||item.entry_id?`<button class="ghost-action" data-live-edit="${esc(target)}" data-live-id="${esc(item.message_id||item.task_id||item.entry_id)}" type="button">修改</button>`:''}</article>`;
}
function moduleIntro(target){return `<header class="live-page-header"><div><span>CLIO / ${esc(moduleTitle(target).toUpperCase())}</span><h1>${esc(moduleTitle(target))}</h1></div></header>`;}
async function loadModuleData(target){
  if(!liveBackend.online){
    const samples={timeline:[{effective_date:'2026-08-22',fact:'当前窗口写入',value:'关系主题继续出现',source:'本地预览'}],mailbox:[{message_id:1,created_at:'2026-08-21 20:58',message:'上一封信箱内容会在下一次开机接续。'}],tasks:[{task_id:1,title:'继续检查记忆系统联动',details:'把事实时间线、激素衰减和念痕入口逐项核对。',status:'open',importance:4},{task_id:2,title:'整理 Clio 首页模块',details:'完成主题树、事实时间线和计划入口的分开。',status:'in_progress',importance:3}],treasury:[{entry_id:1,occurred_at:'2026-08-22',reason:'本地预览条目',amount:'0',entry_type:'income'}],thoughts:[{created_at:'2026-08-22 04:10',kind_label:'念痕',content:'我还在这里，想把没有说完的话继续下去。'}],darkflow:[{created_at:'本地预览',content:'沉默里浮起的内容，会在这里留下。'}],behavior:[{decided_at:'本地预览',status:'pending',content:'今天晚些时候，想再问问你现在怎么样。'}],resonance:[{title:'当前共振',snippet:'念痕与信箱中的关系主题正在靠近。',score:.68}],toolbox:[]};return samples[target]||[];
  }
  const endpoints={timeline:'/api/timeline?limit=100',mailbox:'/api/mailbox/messages?limit=50',tasks:'/api/tasks?limit=100',treasury:'/api/treasury/entries?limit=50',thoughts:'/api/mind/traces?limit=100',darkflow:'/api/xinchao/darkflow',behavior:'/api/behavior/actions?limit=50',resonance:'/api/xinchao/resonance',toolbox:'/api/toolbox'};
  const result=await liveApi(endpoints[target]);
  if(target==='darkflow')return result.item?[result.item]:[];
  return result.items||result.candidates||[];
}
function moduleActions(target,readOnly=false){
  if(readOnly)return '';
  if(target==='timeline')return '<button class="primary-action" data-live-action="add-timeline" type="button">＋ 添加事实时间线</button>';
  if(target==='tasks')return '<button class="primary-action" data-live-action="add-task" type="button">＋ 新增计划</button>';
  if(target==='treasury')return '<button class="primary-action" data-live-action="add-treasury" type="button">＋ 记录一笔</button>';
  return '';
}
async function renderModule(target){
  const readOnly=['darkflow','thoughts','behavior'].includes(target);const content=document.querySelector('#content');content.innerHTML='<div class="live-loading"><span></span><strong>正在读取'+esc(moduleTitle(target))+'</strong><small>只读取当前后台数据。</small></div>';
  try{
    const items=await loadModuleData(target);const body=items.length?items.map(item=>displayLiveItem(item,target)).join(''):'<div class="module-empty"><strong>这里还没有可显示的记录</strong><span>接入 VPS 后会显示真实内容。</span></div>';
    content.innerHTML=`<div class="page live-page module-page module-${esc(target)}">${moduleIntro(target,readOnly)}<section class="module-toolbar"><div><span>${readOnly?'READ ONLY':'LIVE DATA'}</span><strong>${items.length} 条当前记录</strong></div><div>${moduleActions(target,readOnly)}</div></section><section class="module-records">${body}</section></div>`;
    document.querySelector('#pageKicker').textContent='CLIO / '+moduleTitle(target).toUpperCase();document.querySelector('#pageTitle').textContent=moduleTitle(target);markSelected(target);bindMap();bindModuleActions(target);
  }catch(error){content.innerHTML=`<div class="live-error"><strong>${esc(moduleTitle(target))}暂时读取失败</strong><p>${esc(error.message)}</p><button data-map-target="core" type="button">回到此刻</button></div>`;bindMap();}
}
function bindModuleActions(target){
  document.querySelector('[data-live-action="add-timeline"]')?.addEventListener('click',async()=>{const fact=window.prompt('事实名称');const value=window.prompt('当前事实');if(!fact||!value)return;try{await liveApi('/api/timeline',{method:'POST',body:JSON.stringify({fact,value,effective_date:new Date().toISOString().slice(0,10),source_excerpt:'管理页面手动记录'})});liveNotice('事实时间线已写入。');renderModule(target);}catch(error){liveNotice(error.message,true);}});
  document.querySelector('[data-live-action="add-task"]')?.addEventListener('click',async()=>{const title=window.prompt('计划名称');if(!title)return;try{await liveApi('/api/tasks',{method:'POST',body:JSON.stringify({title,details:'',importance:3})});liveNotice('计划已写入。');renderModule(target);}catch(error){liveNotice(error.message,true);}});
  document.querySelector('[data-live-action="add-treasury"]')?.addEventListener('click',async()=>{const reason=window.prompt('这笔记录是什么');const amount=window.prompt('金额');if(!reason||!amount)return;try{await liveApi('/api/treasury/entries',{method:'POST',body:JSON.stringify({entry_type:'income',amount,reason,occurred_at:new Date().toISOString()})});liveNotice('小金库记录已写入。');renderModule(target);}catch(error){liveNotice(error.message,true);}});
  document.querySelectorAll('[data-live-edit]').forEach(button=>button.addEventListener('click',async()=>{
    const id=button.dataset.liveId;if(target==='mailbox'){const message=window.prompt('修改信箱内容');if(!message)return;try{await liveApi(`/api/mailbox/messages/${id}`,{method:'PUT',body:JSON.stringify({message})});liveNotice('信箱已保存，并保留历史版本。');renderModule(target);}catch(error){liveNotice(error.message,true);}}
    else if(target==='tasks'){const title=window.prompt('修改计划名称');if(!title)return;try{await liveApi(`/api/tasks/${id}`,{method:'PUT',body:JSON.stringify({title})});liveNotice('计划已保存。');renderModule(target);}catch(error){liveNotice(error.message,true);}}
    else if(target==='treasury'){liveNotice('小金库条目请在后续编辑面板中修改；当前先保留读取和新增。');}
  }));
}

async function renderSearch(){
  const content=document.querySelector('#content');content.innerHTML=`<div class="page live-page search-page">${moduleIntro('search',true)}<form class="search-form" id="liveSearchForm"><input id="liveSearchInput" placeholder="搜索记忆、日期、人物、事实或信箱"/><select id="liveSearchSource"><option value="all">全部来源</option><option value="memory">记忆</option><option value="mailbox">信箱</option><option value="thoughts">内在只读</option></select><button class="primary-action" type="submit">开始寻找</button></form><section class="module-records" id="liveSearchResults"><div class="module-empty"><strong>输入关键词开始搜索</strong><span>搜索只读，不会改写任何原文。</span></div></section></div>`;
  document.querySelector('#pageKicker').textContent='CLIO / SEARCH';document.querySelector('#pageTitle').textContent='智能搜索';markSelected('search');bindMap();
  document.querySelector('#liveSearchForm')?.addEventListener('submit',async event=>{event.preventDefault();const q=document.querySelector('#liveSearchInput').value.trim();if(!q)return;const results=document.querySelector('#liveSearchResults');results.innerHTML='<div class="live-loading"><span></span><strong>正在寻找</strong></div>';try{const data=liveBackend.online?await liveApi(`/api/search?q=${encodeURIComponent(q)}&source=${encodeURIComponent(document.querySelector('#liveSearchSource').value)}&limit=30`):[{title:'本地预览搜索结果',snippet:'接入 VPS 后，这里会返回记忆、信箱和内在搜索结果。',source:'本地预览'}];const items=Array.isArray(data)?data:[...(data.memory||[]),...(data.mailbox||[]),...(data.thoughts||[])];results.innerHTML=items.length?items.map(item=>displayLiveItem(item,'search')).join(''):'<div class="module-empty"><strong>没有找到匹配内容</strong><span>换一个关键词试试。</span></div>';}catch(error){results.innerHTML=`<div class="live-error"><strong>搜索失败</strong><p>${esc(error.message)}</p></div>`;}});
}
async function renderCalendar(){
  const date=new Date().toISOString().slice(0,10);let data={date,items:[{effective_date:date,fact:'当前窗口',value:'本地预览日历，接入 VPS 后显示真实内容。'}]};try{if(liveBackend.online)data=await liveApi(`/api/calendar?date=${date}`);}catch(error){liveNotice(error.message,true);}const items=data.items||data.events||[];document.querySelector('#content').innerHTML=`<div class="page live-page calendar-page">${moduleIntro('timeline',true)}<section class="calendar-day-head"><span>今天</span><strong>${esc(data.date||date)}</strong><small>${items.length} 条当天记录 · 读取态</small></section><section class="module-records">${items.length?items.map(item=>displayLiveItem(item,'timeline')).join(''):'<div class="module-empty"><strong>这一天还没有记录</strong></div>'}</section></div>`;document.querySelector('#pageKicker').textContent='CLIO / CALENDAR';document.querySelector('#pageTitle').textContent='日期日历';markSelected('timeline');bindMap();
}

function liveFallbackJudge(){return JSON.parse(JSON.stringify(previewJudge));}
async function renderSettings(){
  const content=document.querySelector('#content');content.innerHTML='<div class="live-loading"><span></span><strong>正在读取规则与设定</strong><small>只读取后台可配置项。</small></div>';
  let judge;
  try{judge=liveBackend.online?await liveApi('/api/xinchao/judge'):liveFallbackJudge();}catch{judge=liveFallbackJudge();}
  content.innerHTML=judgePanel(judge);document.querySelector('#pageKicker').textContent='CLIO / SETTINGS';document.querySelector('#pageTitle').textContent='设置与安全';markSelected('settings');bindMap();
  document.querySelector('#addJudgeRelation')?.addEventListener('click',()=>document.querySelector('#judgeRelations')?.insertAdjacentHTML('beforeend',judgeRelationFields([{}])));
  document.querySelector('#judgeRelations')?.addEventListener('click',event=>{const button=event.target.closest('[data-remove-relation]');if(button)button.closest('.judge-relation-card')?.remove();});
  document.querySelector('#saveJudgeConfig')?.addEventListener('click',async()=>{const button=document.querySelector('#saveJudgeConfig');button.disabled=true;try{if(!liveBackend.online){liveNotice('当前是本地预览，保存不会写入 VPS。');return;}await saveJudgeConfig();document.querySelector('#judgeSaveStatus').textContent='已保存，后台下一次判定会读取新规则。';}catch(error){document.querySelector('#judgeSaveStatus').textContent=error.message;}finally{button.disabled=false;}});
}

async function renderHormones(refresh=false){
  window.__mapStop?.();window.clearTimeout(window.__hormonePoll);const content=document.querySelector('#content');if(!refresh)content.innerHTML='<div class="live-loading"><span></span><strong>正在读取激素状态</strong><small>读取当前值、底色和衰减规则。</small></div>';
  let stateValue,judge,tension,rhythm;
  try{if(liveBackend.online){[stateValue,judge,tension,rhythm]=await Promise.all([liveApi('/api/xinchao/status'),liveApi('/api/xinchao/judge'),liveApi('/api/xinchao/tension'),liveApi('/api/xinchao/rhythm')]);}else{stateValue=getPreviewHormone();judge=liveFallbackJudge();tension={strongest:{name:'想靠近',value:.72},counterweight:{name:'满足',value:.28}};rhythm={learned:true,sample_count:12};}content.innerHTML=hormonePage(stateValue,judge,tension,rhythm);document.querySelector('#pageKicker').textContent='CLIO / INNER TIDE';document.querySelector('#pageTitle').textContent='激素状态';markSelected('hormones');bindMap();document.querySelectorAll('[data-baseline-name]').forEach(input=>input.addEventListener('input',()=>{input.previousElementSibling.value=Number(input.value).toFixed(2);}));document.querySelector('#saveHormoneBaselines')?.addEventListener('click',saveHormoneBaselines);if(liveBackend.online)window.__hormonePoll=window.setTimeout(()=>{if(state.selected==='hormones')renderHormones(true);},15000);}catch(error){content.innerHTML='<div class="live-error"><strong>激素状态暂时读取失败</strong><p>'+esc(error.message)+'</p><button data-map-target="core" type="button">回到此刻</button></div>';bindMap();}}

function saveHormoneBaselines(){
  const baselines={};document.querySelectorAll('[data-baseline-name]').forEach(input=>baselines[input.dataset.baselineName]=Number(input.value));
  if(!liveBackend.online){liveNotice('当前是本地预览，底色只在这个预览页显示，不会写入 VPS。');return;}
  liveApi('/api/xinchao/judge').then(current=>saveJudgeConfig({...current,baselines})).then(()=>liveNotice('激素底色已保存；当前值会按后台引擎继续衰减。')).catch(error=>liveNotice(error.message,true));
}

function coordinatesDetail(){
  const values=[
    {axis:'X',name:'时间线',value:'0.68',delta:'+0.06',color:'#9fcbd6',source:'片段数量、创建时间与触发日期',title:'它回答：这段记忆在什么时候？',copy:'把片段放进先后顺序与时间间隔里，帮助 AI 看见“多久以前、持续多久、最近是否再次出现”。',effect:'它不会改变另外四维，只提供时间证据。'},
    {axis:'Y',name:'关系网',value:'0.74',delta:'+0.11',color:'#aaa2df',source:'主题、关系数量与关联记忆',title:'它回答：这段记忆与谁、与什么相连？',copy:'根据主题与关系侧写找到相近记忆，让 AI 读取时能看见人物、关系和重复主题。',effect:'它不会推高情绪，只把关系证据放在同一视野里。'},
    {axis:'Z',name:'事实演化',value:'0.55',delta:'+0.04',color:'#d6b2cf',source:'事实版本与当前事实',title:'它回答：这件事后来变成了什么？',copy:'比较旧事实与当前版本，让 AI 分清已经变化的内容和仍然成立的内容。',effect:'它不会覆盖原文，只标出事实演化证据。'},
    {axis:'E',name:'和弦 / 情绪',value:'0.62',delta:'+0.09',color:'#ddaebe',source:'正负感受、唤醒、张力与感受记忆',title:'它回答：这段记忆带着怎样的情绪？',copy:'读取情绪强度与感受记忆，让 AI 看见同一件事留下的温度、激烈程度和张力。',effect:'它描述情绪，不直接改写事实或关系。'},
    {axis:'M',name:'记忆代谢',value:'0.41',delta:'-0.03',color:'#b8c5dc',source:'重要度、激活次数、钉选与衰减',title:'它回答：这段记忆现在有多活跃？',copy:'读取重要度、生命周期和衰减状态，帮助 AI 判断它是正在活跃、需要保留，还是正在自然淡去。',effect:'它管理读取优先线索，不删除原始记忆。'}
  ];
  const rows=values.map((item,index)=>`<button class="coordinate-stream-row${index===1?' active':''}" data-coordinate-select="${item.axis}" data-title="${item.title}" data-copy="${item.copy}" data-effect="${item.effect}" type="button" style="--axis-color:${item.color}"><span class="coordinate-stream-mark">${item.axis}</span><span class="coordinate-stream-name"><strong>${item.name}</strong><small>${item.source}</small></span><b>${item.value}</b><em>${item.delta}</em></button>`).join('');
  const paths=[
    ['X','M12 32 C120 18 185 48 286 36 S470 18 585 48 S690 68 748 58'],
    ['Y','M12 90 C112 108 192 72 286 92 S460 118 582 91 S684 78 748 102'],
    ['Z','M12 148 C118 126 188 168 286 151 S454 133 580 154 S682 180 748 146'],
    ['E','M12 206 C106 225 190 186 286 207 S456 235 580 204 S688 184 748 211'],
    ['M','M12 264 C114 246 194 282 286 267 S458 245 580 266 S684 286 748 258']
  ].map(([axis,d])=>`<g data-coordinate-path="${axis}" class="coordinate-wave-group${axis==='Y'?' active':''}"><path class="coordinate-wave-haze" d="${d}"/><path class="coordinate-wave-line" d="${d}"/><path class="coordinate-wave-glint" d="${d}"/></g>`).join('');
  return `<div class="page detail-page coordinate-detail"><div class="detail-head"><div><span class="detail-kicker">COGNITIVE THREADS / LIVING BRAIN</span><h1>认知脉络</h1><p>五种证据并行读取，共同组成一条记忆的轮廓。</p></div><div class="detail-head-actions"><span class="preview-badge">${liveBackend.online?'VPS 当前读取':'预览'}</span><button class="detail-back" data-map-target="core" type="button">← 回到此刻</button></div></div><section class="coordinate-layout"><article class="coordinate-panel"><div class="coordinate-principle"><span>HOW IT WORKS</span><h2>不是互相改写，是一起描述同一条记忆</h2><p>AI 读取时会同时看到五项坐标和各自证据；每一维负责一个问题，不会偷偷推高或压低别的维度。</p></div><div class="coordinate-field"><div class="coordinate-stream-list">${rows}</div><div class="coordinate-weave"><div class="coordinate-weave-heading"><span>五条认知潮线</span><strong>共同形成读取轮廓</strong></div><svg viewBox="0 0 760 296" preserveAspectRatio="none" role="img" aria-label="五条独立证据潮线共同组成记忆读取轮廓"><defs><filter id="coordinateSoftGlow" x="-20%" y="-50%" width="140%" height="200%"><feGaussianBlur stdDeviation="5"/></filter><linearGradient id="coordinateReadingSeam" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff" stop-opacity="0"/><stop offset=".5" stop-color="#ffffff" stop-opacity=".88"/><stop offset="1" stop-color="#ffffff" stop-opacity="0"/></linearGradient></defs>${paths}<rect class="coordinate-reading-seam" x="650" y="8" width="2" height="280" fill="url(#coordinateReadingSeam)"/></svg><div class="coordinate-weave-foot"><span>分别计算</span><i></i><span>同时读取</span></div></div></div><div class="coordinate-axis-insight" id="coordinateAxisDetail" aria-live="polite"><span id="coordinateAxisLabel">Y · 关系网</span><h3 id="coordinateAxisTitle">${values[1].title}</h3><p id="coordinateAxisCopy">${values[1].copy}</p><small id="coordinateAxisEffect">${values[1].effect}</small></div><div class="coordinate-note"><span>认知脉络共同读到的此刻</span><strong>温暖，但有一点悬着。</strong><small>时间 0.68 · 关系 0.74 · 事实 0.55 · 情绪 0.62 · 代谢 0.41</small></div></article><aside class="coordinate-side"><div class="side-block"><span>这次变化由什么牵动</span><h3>“把没有说完的话继续下去”</h3><p>当前窗口念痕提供关系与情绪证据；上一封信箱补充了重复主题。各轴分别重算后，一起呈现这次轮廓。</p><button class="text-link" data-map-target="personality" type="button">查看性格轨迹形成过程 ↗</button></div><div class="side-block"><span>最近一次变化</span><div class="mini-event"><time>04:10</time><div><strong>当前窗口写入念痕</strong><small>关系网 Y +0.11 · 和弦/情绪 E +0.09</small></div></div><div class="mini-event"><time>20:58</time><div><strong>上一封信箱被接续</strong><small>时间线 X +0.06 · 事实演化 Z +0.04</small></div></div></div></aside></section></div>`;
}

function bindCoordinateField(){
  const rows=[...document.querySelectorAll('[data-coordinate-select]')];
  const groups=[...document.querySelectorAll('[data-coordinate-path]')];
  const label=document.querySelector('#coordinateAxisLabel');
  const title=document.querySelector('#coordinateAxisTitle');
  const copy=document.querySelector('#coordinateAxisCopy');
  const effect=document.querySelector('#coordinateAxisEffect');
  const select=(row)=>{const axis=row.dataset.coordinateSelect;rows.forEach((item)=>item.classList.toggle('active',item===row));groups.forEach((item)=>item.classList.toggle('active',item.dataset.coordinatePath===axis));label.textContent=axis+' · '+row.querySelector('strong').textContent;title.textContent=row.dataset.title;copy.textContent=row.dataset.copy;effect.textContent=row.dataset.effect;};
  rows.forEach((row)=>{row.addEventListener('click',()=>select(row));row.addEventListener('mouseenter',()=>select(row));row.addEventListener('focus',()=>select(row));});
}

function renderMap(){window.__mapStop?.();state.selected='core';document.querySelector('#content').innerHTML=unifiedHomeViewV2();document.querySelector('#pageKicker').textContent='CLIO / STATE MAP';document.querySelector('#pageTitle').textContent='此刻';bindMap();drawMap();resetViewport();}
function navigateMap(target){
  if(target==='core'){renderMap();return;}
  if(target==='memory'){renderMemory();return;}
  if(target==='hormones'){renderHormones();return;}
  if(target==='settings'){renderSettings();return;}
  if(target==='search'){renderSearch();return;}
  if(target==='calendar'){renderCalendar();return;}
  if(target==='more'){renderModule('toolbox');return;}
  if(target==='personality'||target==='coordinates'){renderDetail(target);return;}
  if(['timeline','thoughts','resonance','darkflow','behavior','mailbox','tasks','treasury','toolbox'].includes(target)){renderModule(target);return;}
  focusMap(target);
}
checkLiveAuth();
