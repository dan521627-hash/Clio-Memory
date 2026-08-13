const state = {
  items: [],
  selectedId: '',
  detail: null,
  filter: 'all',
  editorMode: 'update',
  pendingShortenPayload: null,
  pendingEditorTopic: null,
  segmentIndex: 0,
  showWholeBucket: false,
  editorOriginalContent: '',
  view: 'memories',
  treasuryItems: [],
  treasuryFilter: '',
  treasuryEditingId: 0,
  mailboxItems: [],
  mailboxBeforeId: 0,
  mailboxHasMore: false,
  mailboxEditingId: 0,
  taskItems: [],
  taskStatus: 'open',
  taskEditingId: 0,
  hormoneState: null,
  darkflow: null,
  judgeConfig: null,
  behaviorItems: [],
  behaviorCandidates: [],
  topicTree: [],
  topicMain: '',
  topicSub: '',
  topicItems: [],
  topicUnassigned: false,
  topicPreview: [],
  searchItems: [],
  timelineItems: [],
  timelineCandidates: [],
  thoughtItems: [],
  thoughtStatus: 'active',
  calendarItems: [],
  resonanceItems: [],
  toolboxItems: [],
  stats: null,
  pendingPush: null,
};

function icons() {
  if (window.lucide) window.lucide.createIcons();
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char]);
}

function iconFor(item) {
  if (item.feeling) return 'heart';
  if (item.trigger_date) return 'calendar-clock';
  if (item.sealed || item.archived) return 'archive';
  if (item.pin_level) return 'pin';
  return 'file-text';
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {'Content-Type': 'application/json', ...(options.headers || {})},
  });
  if (!response.ok) {
    let message = `操作失败 (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_) {}
    if (response.status === 401) showManagerLogin('登录已失效，请重新输入管理密码。');
    throw new Error(message);
  }
  return response.json();
}

function showManagerLogin(message = '') {
  const login = document.querySelector('#managerLogin');
  const opening = document.querySelector('#managerOpening');
  login.hidden = false;
  document.querySelector('#managerLoginError').textContent = message;
  opening.classList.add('is-hidden');
  document.body.classList.remove('is-opening');
  setTimeout(() => document.querySelector('#managerPassword').focus(), 50);
  icons();
}

async function checkManagerAuthentication() {
  const response = await fetch('/api/auth/status', {headers: {'Accept': 'application/json'}});
  if (!response.ok) throw new Error('无法检查管理页登录状态。');
  const result = await response.json();
  if (!result.configured) {
    showManagerLogin('服务器尚未配置管理密码。');
    return false;
  }
  if (!result.authenticated) {
    showManagerLogin();
    return false;
  }
  document.querySelector('#managerLogin').hidden = true;
  return true;
}

async function submitManagerLogin(event) {
  event.preventDefault();
  const password = document.querySelector('#managerPassword').value;
  const error = document.querySelector('#managerLoginError');
  error.textContent = '';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '登录失败。');
    const login = document.querySelector('#managerLogin');
    login.classList.add('is-entering');
    document.body.classList.add('is-entering-house');
    setTimeout(() => window.location.reload(), 720);
  } catch (loginError) {
    error.textContent = loginError.message;
  }
}

async function logoutManager() {
  await fetch('/api/auth/logout', {method: 'POST'});
  window.location.reload();
}

function toast(message, isError = false) {
  const element = document.querySelector('#managerToast');
  element.textContent = message;
  element.classList.toggle('is-error', isError);
  element.classList.add('is-visible');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove('is-visible'), 3000);
}

function setServiceStatus(ok) {
  const element = document.querySelector('#serviceStatus');
  element.classList.toggle('is-offline', !ok);
  element.innerHTML = `<i></i>${ok ? '服务正常' : '连接失败'}`;
}

async function loadStats() {
  const stats = await api('/api/stats');
  state.stats = stats;
  document.querySelector('#statTotal').textContent = stats.total;
  document.querySelector('#statCore').textContent = stats.core;
  document.querySelector('#statImportant').textContent = stats.important;
  document.querySelector('#statSealed').textContent = stats.sealed;
  document.querySelector('#countAll').textContent = stats.total;
  document.querySelector('#countCore').textContent = stats.core;
  document.querySelector('#countImportant').textContent = stats.important;
  document.querySelector('#countFeeling').textContent = stats.feeling;
  document.querySelector('#countFuture').textContent = stats.future;
  document.querySelector('#countArchived').textContent = stats.sealed;
  document.querySelector('#quickCoreCount').textContent = stats.core;
  document.querySelector('#quickImportantCount').textContent = stats.important;
  document.querySelector('#quickFeelingCount').textContent = stats.feeling;
  document.querySelector('#quickFutureCount').textContent = stats.future;
  document.querySelector('#quickArchivedCount').textContent = stats.sealed;
  document.querySelector('#houseMemoryCount').textContent = stats.total;
  document.querySelector('#houseTaskCount').textContent = stats.todo || 0;
}

function datetimeLocalValue(value = '') {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function displayDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value || '') : date.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
  });
}

function setActiveNavigation(activeId) {
  document.querySelectorAll('.daily-nav-button, .advanced-menu-panel button').forEach(item => {
    item.classList.toggle('is-active', item.id === activeId);
  });
  const menu = document.querySelector('#advancedMenu');
  if (menu) menu.open = false;
  document.body.classList.toggle('viewing-house', activeId === 'homeNav');
  document.body.classList.toggle('viewing-room', activeId !== 'homeNav');
  document.querySelector('.manager-shell').hidden = activeId === 'homeNav';
  document.querySelector('#homeMain').hidden = activeId !== 'homeNav';
  document.querySelector('#searchMain').hidden = activeId !== 'searchNav';
  document.querySelector('#timelineMain').hidden = activeId !== 'timelineNav';
  document.querySelector('#mindMain').hidden = activeId !== 'mindNav';
  document.querySelector('#calendarMain').hidden = activeId !== 'calendarNav';
  document.querySelector('#resonanceMain').hidden = activeId !== 'resonanceNav';
  document.querySelector('#toolboxMain').hidden = activeId !== 'toolboxNav';
}

function hideStandardViews() {
  document.querySelector('#homeMain').hidden = true;
  document.querySelector('.memory-main').hidden = true;
  document.querySelector('#treasuryMain').hidden = true;
  document.querySelector('#mailboxMain').hidden = true;
  document.querySelector('#taskMain').hidden = true;
  document.querySelector('#hormoneMain').hidden = true;
  document.querySelector('#darkflowMain').hidden = true;
  document.querySelector('#behaviorMain').hidden = true;
  document.querySelector('#topicMain').hidden = true;
}

function beijingHour() {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', hour: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date());
  return Number(parts.find(part => part.type === 'hour')?.value || 0);
}

function renderHouseGreeting() {
  const hour = beijingHour();
  const greeting = hour < 6 ? '夜还深，屋里替你留着灯'
    : hour < 11 ? '早上好，今天也从这里慢慢打开'
      : hour < 14 ? '中午好，回来歇一会儿'
        : hour < 18 ? '下午好，记忆都安静放在原处'
          : '晚上好，屋里一直给你留着位置';
  document.querySelector('#houseGreeting').textContent = greeting;
}

function renderHouseHormone(result = {}) {
  const available = Boolean(result.available) && !result.disabled;
  document.querySelector('#houseEmotionName').textContent = available ? (result.dominant || '平静') : '安静';
  document.querySelector('#houseEmotionValue').textContent = Number(available ? result.dominant_value : 0).toFixed(2);
  document.querySelector('#houseEmotionElapsed').textContent = available
    ? `沉默了 ${elapsedLabel(result.elapsed_seconds)}`
    : '还没有开始新的沉默周期';
}

function renderHousePendingPush(result = {}) {
  state.pendingPush = result;
  const panel = document.querySelector('#housePushAck');
  const button = document.querySelector('#acknowledgePush');
  if (!result.available || result.acknowledged) {
    panel.hidden = true;
    button.disabled = false;
    button.textContent = '我看到了';
    return;
  }
  panel.hidden = false;
  const latest = result.latest || {};
  const countText = Number(result.count || 0) > 1 ? `，共 ${result.count} 条` : '';
  document.querySelector('#housePushTime').textContent = latest.delivered_at
    ? `${displayDate(latest.delivered_at)} 发出${countText}`
    : `有一条推送等待交接${countText}`;
  button.disabled = false;
  button.textContent = latest.phase === 'silence' ? '我知道了' : '我看到了';
}

async function loadHouse() {
  renderHouseGreeting();
  const [hormone, darkflow, pendingPush, phrase] = await Promise.all([
    api('/api/xinchao/status'),
    api('/api/xinchao/darkflow'),
    api('/api/behavior/pending'),
    api('/api/house/phrase').catch(() => ({
      text: '我把走过的事留在这里。下一次见面，我们从这里继续。',
    })),
  ]);
  const housePromise = document.querySelector('.house-promise');
  if (housePromise && phrase?.text) housePromise.textContent = phrase.text;
  renderHouseHormone(hormone);
  renderHousePendingPush(pendingPush);
  document.querySelector('#houseDarkflowLink').hidden = !(
    darkflow?.item && darkflow.item.status === 'pending'
  );
}

async function showHomeView() {
  state.view = 'home';
  hideStandardViews();
  setActiveNavigation('homeNav');
  await Promise.all([loadStats(), loadHouse()]);
  icons();
}

async function acknowledgePush() {
  const button = document.querySelector('#acknowledgePush');
  const panel = document.querySelector('#housePushAck');
  button.disabled = true;
  panel.hidden = true;
  try {
    const actionId = Number(state.pendingPush?.latest?.action_id || 0);
    const result = await api('/api/behavior/acknowledge', {
      method: 'POST',
      body: JSON.stringify({action_id: actionId}),
    });
    toast(result.message || '已经告诉他你看到了。');
    await loadHouse();
  } catch (error) {
    button.disabled = false;
    panel.hidden = false;
    toast(error.message, true);
  }
}

function beijingDateValue() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());
}

function thoughtToneLabel(tone) {
  return {positive: '暖的', negative: '暗的', mixed: '说不清的'}[tone] || '说不清的';
}

function renderThoughts() {
  const list = document.querySelector('#thoughtList');
  if (!state.thoughtItems.length) {
    list.innerHTML = '<div class="manager-empty thought-empty"><i data-lucide="moon-star"></i><strong>这里现在很安静</strong><span>有依据的念头才会留下，没有就不硬写。</span></div>';
    icons();
    return;
  }
  list.innerHTML = state.thoughtItems.map(item => {
    const strength = Math.max(0, Math.min(1, Number(item.current_strength ?? item.intensity ?? 0)));
    const status = item.status === 'obsession' ? '反复萦绕' : item.status === 'resolved' ? '已经放下' : '一闪而过';
    return `<article class="thought-entry tone-${escapeHtml(item.tone || 'mixed')}">
      <header><span>${escapeHtml(status)}</span><small>${escapeHtml(thoughtToneLabel(item.tone))} · ${escapeHtml(displayDate(item.last_seen))}</small></header>
      <p>${escapeHtml(item.thought_text)}</p>
      ${item.reason ? `<blockquote>${escapeHtml(item.reason)}</blockquote>` : ''}
      <div class="thought-strength"><span><i style="width:${Math.round(strength * 100)}%"></i></span><b>${strength.toFixed(2)}</b></div>
      <footer><small>出现 ${escapeHtml(item.occurrence_count || 1)} 次 · 来自 ${escapeHtml(item.source_tool || '一次写入')}</small><div><button class="icon-button" type="button" data-thought-resolve="${escapeHtml(item.canonical_tag)}" title="放下" aria-label="放下"><i data-lucide="check"></i></button><button class="icon-button icon-button--danger" type="button" data-thought-delete="${escapeHtml(item.canonical_tag)}" title="永久删除" aria-label="永久删除"><i data-lucide="trash-2"></i></button></div></footer>
    </article>`;
  }).join('');
  list.querySelectorAll('[data-thought-resolve]').forEach(button => button.addEventListener('click', async () => {
    await api(`/api/mind/thoughts/${encodeURIComponent(button.dataset.thoughtResolve)}/resolve`, {method: 'POST'});
    await loadThoughts();
  }));
  list.querySelectorAll('[data-thought-delete]').forEach(button => button.addEventListener('click', async () => {
    if (!window.confirm('永久删除这条私密心念？它不会留下历史快照。')) return;
    await api(`/api/mind/thoughts/${encodeURIComponent(button.dataset.thoughtDelete)}`, {method: 'DELETE'});
    await loadThoughts();
  }));
  icons();
}

async function loadThoughts() {
  const result = await api(`/api/mind/thoughts?status=${encodeURIComponent(state.thoughtStatus)}&limit=100`);
  state.thoughtItems = result.items || [];
  renderThoughts();
}

async function showMindView() {
  state.view = 'mind';
  hideStandardViews();
  setActiveNavigation('mindNav');
  await loadThoughts();
}

function renderCalendar() {
  const list = document.querySelector('#calendarList');
  document.querySelector('#calendarCount').textContent = `${state.calendarItems.length} 条`;
  const selected = document.querySelector('#calendarDate').value;
  const date = new Date(`${selected}T12:00:00+08:00`);
  document.querySelector('#calendarWeekday').textContent = Number.isNaN(date.getTime()) ? selected : date.toLocaleDateString('zh-CN', {timeZone: 'Asia/Shanghai', weekday: 'long'});
  document.querySelector('#calendarTitle').textContent = selected === beijingDateValue() ? '今天留下的东西' : `${selected} 留下的东西`;
  if (!state.calendarItems.length) {
    list.innerHTML = '<div class="manager-empty calendar-empty"><i data-lucide="calendar-heart"></i><strong>这一天没有留下记录</strong><span>换个日期看看，空白也可以只是安静。</span></div>';
    icons();
    return;
  }
  const iconsByKind = {
    memory_segment: 'book-open', memory_created: 'book-plus',
    memory_updated: 'book-up', memory_trigger: 'calendar-clock',
    mailbox: 'mail', thought: 'sparkles', darkflow: 'waves',
    behavior: 'send', task: 'circle-check-big', treasury: 'wallet-cards',
    fact: 'git-branch',
  };
  list.innerHTML = state.calendarItems.map((item, index) => `<button type="button" class="calendar-entry kind-${escapeHtml(item.kind)}" data-calendar-index="${index}"><span><i data-lucide="${iconsByKind[item.kind] || 'dot'}"></i></span><div><small>${escapeHtml(displayDate(item.time))}</small><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.note || '')}</p></div><i class="calendar-entry-arrow" data-lucide="chevron-right"></i></button>`).join('');
  list.querySelectorAll('[data-calendar-index]').forEach(button => {
    button.addEventListener('click', () => {
      const item = state.calendarItems[Number(button.dataset.calendarIndex)];
      openCalendarItem(item).catch(error => toast(error.message, true));
    });
  });
  icons();
}

async function openCalendarItem(item) {
  if (!item) return;
  if (String(item.kind).startsWith('memory')) {
    showMemoryView();
    await selectMemory(String(item.id), false);
    return;
  }
  const routes = {
    mailbox: showMailboxView,
    thought: showMindView,
    darkflow: showDarkflowView,
    behavior: showBehaviorView,
    task: showTaskView,
    treasury: showTreasuryView,
    fact: showTimelineView,
  };
  const route = routes[item.kind];
  if (route) await route();
}

async function loadCalendar() {
  const date = document.querySelector('#calendarDate').value || beijingDateValue();
  const result = await api(`/api/calendar?date=${encodeURIComponent(date)}`);
  state.calendarItems = result.items || [];
  renderCalendar();
}

async function showCalendarView() {
  state.view = 'calendar';
  hideStandardViews();
  setActiveNavigation('calendarNav');
  if (!document.querySelector('#calendarDate').value) document.querySelector('#calendarDate').value = beijingDateValue();
  await loadCalendar();
}

function tensionBars(items) {
  return (items || []).map(item => `<div class="tension-line"><span><b>${escapeHtml(item.name)}</b><small>${Number(item.value || 0).toFixed(2)}</small></span><i><em style="width:${Math.round(Math.max(0, Math.min(1, Number(item.value || 0))) * 100)}%"></em></i></div>`).join('');
}

function renderResonance(resonance, tension) {
  state.resonanceItems = resonance.items || [];
  const bubbles = document.querySelector('#resonanceBubbles');
  bubbles.innerHTML = state.resonanceItems.length ? state.resonanceItems.map((item, index) => {
    const isMailbox = item.source === 'mailbox';
    const sourceLabel = isMailbox ? '旧信箱' : '记忆桶';
    const title = item.name || (isMailbox && item.message_id ? `信箱留言 #${item.message_id}` : '一段旧记忆');
    const excerpt = item.excerpt || '这段来源暂时没有可显示的片段。';
    return `<article class="resonance-bubble bubble-${index % 4}"><header><span>${Number(item.score || 0).toFixed(2)}</span><small>${sourceLabel}</small></header><strong>${escapeHtml(title)}</strong><blockquote>${escapeHtml(excerpt)}</blockquote><p>${escapeHtml(item.why || '')}</p></article>`;
  }).join('') : '<div class="manager-empty resonance-empty"><i data-lucide="orbit"></i><strong>此刻没有明显共振</strong><span>这不是故障，只是现在没有哪段旧记忆特别靠近。</span></div>';
  document.querySelector('#tensionSummary').innerHTML = `<div><small>最强牵引</small><strong>${escapeHtml(tension.strongest?.name || '平静')}</strong><b>${Number(tension.strongest?.value || 0).toFixed(2)}</b></div><i data-lucide="arrow-left-right"></i><div><small>最大收束</small><strong>${escapeHtml(tension.counterweight?.name || '无')}</strong><b>${Number(tension.counterweight?.value || 0).toFixed(2)}</b></div>`;
  document.querySelector('#tensionOutward').innerHTML = tensionBars(tension.outward);
  document.querySelector('#tensionRestraints').innerHTML = tensionBars(tension.restraints);
  const rhythm = tension.rhythm || {};
  document.querySelector('#rhythmStrip').innerHTML = `<header><strong>作息预期</strong><small>${rhythm.learned ? `已经从 ${escapeHtml(rhythm.sample_count)} 次出现中开始认识你的时间` : `还在学习，已有 ${escapeHtml(rhythm.sample_count || 0)} 次有效出现`}</small></header><div>${(rhythm.hours || []).map(item => `<i style="height:${Math.max(4, Math.min(42, Number(item.weight || 0) * 8))}px" title="${item.hour}:00"></i>`).join('')}</div>`;
  icons();
}

async function loadResonance() {
  const [resonance, tension] = await Promise.all([api('/api/xinchao/resonance'), api('/api/xinchao/tension')]);
  renderResonance(resonance, tension);
}

async function showResonanceView() {
  state.view = 'resonance';
  hideStandardViews();
  setActiveNavigation('resonanceNav');
  await loadResonance();
}

const toolboxRoutes = {
  search: showSearchView, timeline: showTimelineView, calendar: showCalendarView,
  tasks: showTaskView, treasury: showTreasuryView, mailbox: showMailboxView,
  darkflow: showDarkflowView, thoughts: showMindView,
};

async function loadToolbox() {
  const result = await api('/api/toolbox');
  state.toolboxItems = result.items || [];
  const grid = document.querySelector('#toolboxGrid');
  grid.innerHTML = state.toolboxItems.map(item => `<button type="button" data-toolbox="${escapeHtml(item.id)}"><span><i data-lucide="${escapeHtml(item.icon)}"></i></span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.description)}</small><i data-lucide="arrow-up-right"></i></button>`).join('');
  grid.querySelectorAll('[data-toolbox]').forEach(button => button.addEventListener('click', () => {
    const route = toolboxRoutes[button.dataset.toolbox];
    if (route) Promise.resolve(route()).catch(error => toast(error.message, true));
  }));
  icons();
}

async function showToolboxView() {
  state.view = 'toolbox';
  hideStandardViews();
  setActiveNavigation('toolboxNav');
  await loadToolbox();
}

function renderIntelligentSearch() {
  const list = document.querySelector('#intelligentSearchResults');
  document.querySelector('#intelligentSearchCount').textContent = `${state.searchItems.length} 条结果`;
  if (!state.searchItems.length) {
    list.innerHTML = `<div class="manager-empty discovery-empty"><i data-lucide="search-x"></i><strong>没有找到相关内容</strong><span>换一种说法再试试，封存内容不会出现在这里。</span></div>`;
    icons();
    return;
  }
  list.innerHTML = state.searchItems.map(item => {
    const isMemory = item.source === 'memory';
    const date = item.last_active || item.created || item.created_at || '';
    return `<article class="discovery-result">
      <span class="discovery-result-icon"><i data-lucide="${isMemory ? 'archive' : 'mail'}"></i></span>
      <div class="discovery-result-copy">
        <small>${isMemory ? '记忆' : '信箱'}${date ? ` · ${escapeHtml(displayDate(date))}` : ''}</small>
        <strong>${escapeHtml(item.title || '未命名')}</strong>
        <p>${escapeHtml(item.snippet || '')}</p>
      </div>
      ${isMemory ? `<button class="secondary-button" type="button" data-search-bucket="${escapeHtml(item.id)}"><i data-lucide="book-open"></i>打开</button>` : ''}
    </article>`;
  }).join('');
  list.querySelectorAll('[data-search-bucket]').forEach(button => {
    button.addEventListener('click', async () => {
      showMemoryView();
      await selectMemory(button.dataset.searchBucket);
    });
  });
  icons();
}

async function runIntelligentSearch() {
  const query = document.querySelector('#intelligentSearchInput').value.trim();
  if (!query) return;
  const source = document.querySelector('#intelligentSearchSource').value;
  document.querySelector('#intelligentSearchCount').textContent = '正在搜索';
  const result = await api(`/api/search?q=${encodeURIComponent(query)}&source=${encodeURIComponent(source)}&limit=20`);
  state.searchItems = result.items || [];
  renderIntelligentSearch();
}

function showSearchView() {
  state.view = 'search';
  hideStandardViews();
  setActiveNavigation('searchNav');
  document.querySelector('#searchMain').hidden = false;
  setTimeout(() => document.querySelector('#intelligentSearchInput').focus(), 0);
}

function renderTimeline() {
  const list = document.querySelector('#timelineList');
  document.querySelector('#timelineCount').textContent = `${state.timelineItems.length} 项事实`;
  renderTimelineCandidates();
  if (!state.timelineItems.length) {
    list.innerHTML = `<div class="manager-empty discovery-empty"><i data-lucide="history"></i><strong>还没有可展示的时间线</strong><span>事实有新旧变化时，会按日期排在这里。</span></div>`;
    icons();
    return;
  }
  list.innerHTML = state.timelineItems.map(group => {
    const current = group.current || {};
    const versions = group.versions || [];
    return `<details class="timeline-fact" ${versions.length <= 2 ? 'open' : ''}>
      <summary>
        <span><small>当前事实</small><strong>${escapeHtml(group.fact_label)}</strong></span>
        <span><b>${escapeHtml(current.fact_value || '')}</b><small>${escapeHtml(current.effective_date || '')}</small></span>
        <i data-lucide="chevron-down"></i>
      </summary>
      <div class="timeline-versions">
        ${versions.map(version => `<article class="timeline-version ${version.is_current ? 'is-current' : ''}">
          <time>${escapeHtml(version.effective_date)}</time>
          <div><strong>${escapeHtml(version.fact_value)}</strong><small>${version.is_current ? '现在' : `有效至 ${escapeHtml(version.valid_to || '下一版本')}`} · ${version.source_type === 'mailbox' ? '来自信箱' : version.source_type === 'manual' ? '人工确认' : '来自记忆'}</small></div>
          ${version.source_bucket_id ? `<button class="icon-button" type="button" data-timeline-bucket="${escapeHtml(version.source_bucket_id)}" title="打开来源记忆" aria-label="打开来源记忆"><i data-lucide="external-link"></i></button>` : '<span></span>'}
        </article>`).join('')}
      </div>
    </details>`;
  }).join('');
  list.querySelectorAll('[data-timeline-bucket]').forEach(button => {
    button.addEventListener('click', async () => {
      showMemoryView();
      await selectMemory(button.dataset.timelineBucket);
    });
  });
  icons();
}

function renderTimelineCandidates() {
  const section = document.querySelector('#timelineCandidatesSection');
  const list = document.querySelector('#timelineCandidates');
  const items = state.timelineCandidates || [];
  section.hidden = !items.length;
  document.querySelector('#timelineCandidateCount').textContent = `${items.length} 条`;
  if (!items.length) {
    list.innerHTML = '';
    return;
  }
  list.innerHTML = items.map(item => `<article class="timeline-candidate">
    <div>
      <small>${escapeHtml(item.effective_date)} · 把握 ${Math.round(Number(item.confidence || 0) * 100)}%</small>
      <strong>${escapeHtml(item.fact_label)}</strong>
      ${item.previous_value ? `<p><del>${escapeHtml(item.previous_value)}</del><i data-lucide="arrow-right"></i><b>${escapeHtml(item.proposed_value)}</b></p>` : `<p><b>${escapeHtml(item.proposed_value)}</b></p>`}
      ${item.reason ? `<span>${escapeHtml(item.reason)}</span>` : ''}
    </div>
    <div class="timeline-candidate-actions">
      <button class="secondary-button" type="button" data-ignore-fact="${item.candidate_id}">忽略</button>
      <button class="primary-button" type="button" data-confirm-fact="${item.candidate_id}"><i data-lucide="check"></i>确认加入</button>
    </div>
  </article>`).join('');
  list.querySelectorAll('[data-confirm-fact]').forEach(button => {
    button.addEventListener('click', () => confirmTimelineCandidate(Number(button.dataset.confirmFact)));
  });
  list.querySelectorAll('[data-ignore-fact]').forEach(button => {
    button.addEventListener('click', () => ignoreTimelineCandidate(Number(button.dataset.ignoreFact)));
  });
  icons();
}

async function confirmTimelineCandidate(candidateId) {
  await api(`/api/timeline/candidates/${candidateId}/confirm`, {method: 'POST'});
  toast('已加入事实时间线，原来的版本仍然保留。');
  await loadTimeline();
}

async function ignoreTimelineCandidate(candidateId) {
  await api(`/api/timeline/candidates/${candidateId}`, {method: 'DELETE'});
  toast('已忽略，这条不会进入事实时间线。');
  await loadTimeline();
}

async function loadTimeline() {
  const search = document.querySelector('#timelineSearchInput').value.trim();
  document.querySelector('#timelineCount').textContent = '正在读取';
  const result = await api(`/api/timeline?search=${encodeURIComponent(search)}&limit=100`);
  state.timelineItems = result.items || [];
  state.timelineCandidates = result.candidates || [];
  renderTimeline();
}

function openTimelineCreateDialog() {
  const source = document.querySelector('#timelineCreateSource');
  const options = (state.items || []).filter(item => !item.sealed && !item.archived);
  source.innerHTML = '<option value="">由我手动确认</option>' + options.map(item =>
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name || item.id)}</option>`
  ).join('');
  document.querySelector('#timelineCreateFact').value = '';
  document.querySelector('#timelineCreateValue').value = '';
  document.querySelector('#timelineCreateDate').value = new Date().toLocaleDateString('en-CA', {timeZone: 'Asia/Shanghai'});
  document.querySelector('#timelineCreateExcerpt').value = '';
  document.querySelector('#timelineCreateDialog').showModal();
  icons();
}

async function saveTimelineFact() {
  const payload = {
    fact: document.querySelector('#timelineCreateFact').value.trim(),
    value: document.querySelector('#timelineCreateValue').value.trim(),
    effective_date: document.querySelector('#timelineCreateDate').value,
    source_bucket_id: document.querySelector('#timelineCreateSource').value,
    source_excerpt: document.querySelector('#timelineCreateExcerpt').value.trim(),
  };
  if (!payload.fact || !payload.value || !payload.effective_date) {
    toast('请把事实名称、新内容和日期填完整。', true);
    return;
  }
  await api('/api/timeline', {method: 'POST', body: JSON.stringify(payload)});
  document.querySelector('#timelineCreateDialog').close();
  toast('事实变化已记录，旧版本没有被覆盖。');
  await loadTimeline();
}

async function showTimelineView() {
  state.view = 'timeline';
  hideStandardViews();
  setActiveNavigation('timelineNav');
  document.querySelector('#timelineMain').hidden = false;
  await loadTimeline();
}

function renderTreasurySummary(summary) {
  const symbol = summary.symbol || '¥';
  document.querySelector('#treasuryBalance').textContent = `${symbol}${summary.balance || '0.00'}`;
  document.querySelector('#treasuryIncome').textContent = `${symbol}${summary.total_income || '0.00'}`;
  document.querySelector('#treasuryExpense').textContent = `${symbol}${summary.total_expense || '0.00'}`;
}

function renderTreasuryList() {
  const list = document.querySelector('#treasuryList');
  if (!state.treasuryItems.length) {
    list.innerHTML = `
      <div class="manager-empty treasury-empty">
        <i data-lucide="wallet-cards"></i>
        <strong>小金库还是空的</strong>
        <span>AI收到或花出第一笔钱后，账目会出现在这里。</span>
      </div>`;
    icons();
    return;
  }
  list.innerHTML = state.treasuryItems.map(item => {
    const income = item.entry_type === 'income';
    const sign = income ? '+' : '-';
    const typeLabel = income ? '收入' : '支出';
    return `
      <article class="treasury-entry">
        <span class="treasury-entry-icon ${income ? 'is-income' : 'is-expense'}"><i data-lucide="${income ? 'arrow-down-left' : 'arrow-up-right'}"></i></span>
        <div class="treasury-entry-copy">
          <div><strong>${escapeHtml(item.reason)}</strong><small>#${escapeHtml(item.entry_id)} · ${escapeHtml(displayDate(item.occurred_at))}</small></div>
          <b class="${income ? 'is-income' : 'is-expense'}">${sign}¥${escapeHtml(item.amount)}</b>
        </div>
        <div class="treasury-entry-actions">
          <button class="icon-button" type="button" data-treasury-history="${escapeHtml(item.entry_id)}" title="历史版本" aria-label="历史版本"><i data-lucide="history"></i></button>
          <button class="icon-button" type="button" data-treasury-edit="${escapeHtml(item.entry_id)}" title="修改" aria-label="修改"><i data-lucide="pencil"></i></button>
          <button class="icon-button icon-button--danger" type="button" data-treasury-delete="${escapeHtml(item.entry_id)}" title="删除" aria-label="删除"><i data-lucide="trash-2"></i></button>
        </div>
      </article>`;
  }).join('');
  list.querySelectorAll('[data-treasury-edit]').forEach(button => {
    button.addEventListener('click', () => openTreasuryEdit(Number(button.dataset.treasuryEdit)));
  });
  list.querySelectorAll('[data-treasury-delete]').forEach(button => {
    button.addEventListener('click', () => openTreasuryDelete(Number(button.dataset.treasuryDelete)));
  });
  list.querySelectorAll('[data-treasury-history]').forEach(button => {
    button.addEventListener('click', () => openTreasuryHistory(Number(button.dataset.treasuryHistory)));
  });
  icons();
}

async function loadTreasury() {
  const query = state.treasuryFilter ? `?entry_type=${encodeURIComponent(state.treasuryFilter)}` : '';
  const result = await api(`/api/treasury/entries${query}`);
  state.treasuryItems = result.items || [];
  renderTreasurySummary(result.summary || {});
  renderTreasuryList();
}

async function showTreasuryView() {
  state.view = 'treasury';
  document.querySelector('#taskMain').hidden = true;
  setActiveNavigation('treasuryNav');
  document.querySelector('.memory-main').hidden = true;
  document.querySelector('#treasuryMain').hidden = false;
  document.querySelector('#mailboxMain').hidden = true;
  document.querySelector('#hormoneMain').hidden = true;
  document.querySelector('#darkflowMain').hidden = true;
  document.querySelector('#behaviorMain').hidden = true;
  document.querySelector('#topicMain').hidden = true;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.remove('is-active'));
  document.querySelector('#treasuryNav').classList.add('is-active');
  document.querySelector('#mailboxNav').classList.remove('is-active');
  document.querySelector('#hormoneNav').classList.remove('is-active');
  document.querySelector('#darkflowNav').classList.remove('is-active');
  document.querySelector('#behaviorNav').classList.remove('is-active');
  document.querySelector('#topicNav').classList.remove('is-active');
  await loadTreasury();
}

function showMemoryView() {
  state.view = 'memories';
  document.querySelector('#taskMain').hidden = true;
  setActiveNavigation('memoryNav');
  document.querySelector('.memory-main').hidden = false;
  document.querySelector('#treasuryMain').hidden = true;
  document.querySelector('#mailboxMain').hidden = true;
  document.querySelector('#hormoneMain').hidden = true;
  document.querySelector('#darkflowMain').hidden = true;
  document.querySelector('#behaviorMain').hidden = true;
  document.querySelector('#topicMain').hidden = true;
  document.querySelector('#treasuryNav').classList.remove('is-active');
  document.querySelector('#mailboxNav').classList.remove('is-active');
  document.querySelector('#hormoneNav').classList.remove('is-active');
  document.querySelector('#darkflowNav').classList.remove('is-active');
  document.querySelector('#behaviorNav').classList.remove('is-active');
  document.querySelector('#topicNav').classList.remove('is-active');
  document.querySelectorAll('[data-filter]').forEach(item => {
    item.classList.toggle('is-active', item.dataset.filter === state.filter);
  });
}

function mailboxItem(messageId) {
  return state.mailboxItems.find(item => Number(item.message_id) === Number(messageId));
}

function renderMailboxList() {
  const list = document.querySelector('#mailboxList');
  document.querySelector('#mailboxMore').hidden = !state.mailboxHasMore;
  if (!state.mailboxItems.length) {
    list.innerHTML = `
      <div class="manager-empty mailbox-empty">
        <i data-lucide="mail-open"></i>
        <strong>信箱还是空的</strong>
        <span>归档时留下的第一封接力留言会出现在这里。</span>
      </div>`;
    icons();
    return;
  }
  list.innerHTML = state.mailboxItems.map((item, index) => `
    <article class="mailbox-entry ${index === 0 ? 'is-latest' : ''}">
      <div class="mailbox-entry-head">
        <span class="mailbox-entry-icon"><i data-lucide="${index === 0 ? 'mail-open' : 'mail'}"></i></span>
        <div>
          <strong>${index === 0 ? '最新留言' : `留言 #${escapeHtml(item.message_id)}`}</strong>
          <small>#${escapeHtml(item.message_id)} · ${escapeHtml(displayDate(item.created_at))}${item.updated_at ? ' · 已修改' : ''}</small>
        </div>
        <div class="mailbox-entry-actions">
          <button class="icon-button" type="button" data-mailbox-history="${escapeHtml(item.message_id)}" title="历史版本" aria-label="历史版本"><i data-lucide="history"></i></button>
          <button class="icon-button" type="button" data-mailbox-edit="${escapeHtml(item.message_id)}" title="修改" aria-label="修改"><i data-lucide="pencil"></i></button>
          <button class="icon-button icon-button--danger" type="button" data-mailbox-delete="${escapeHtml(item.message_id)}" title="删除" aria-label="删除"><i data-lucide="trash-2"></i></button>
        </div>
      </div>
      <p>${escapeHtml(item.message).replace(/\n/g, '<br>')}</p>
    </article>`).join('');
  list.querySelectorAll('[data-mailbox-edit]').forEach(button => {
    button.addEventListener('click', () => openMailboxEdit(Number(button.dataset.mailboxEdit)));
  });
  list.querySelectorAll('[data-mailbox-delete]').forEach(button => {
    button.addEventListener('click', () => openMailboxDelete(Number(button.dataset.mailboxDelete)));
  });
  list.querySelectorAll('[data-mailbox-history]').forEach(button => {
    button.addEventListener('click', () => openMailboxHistory(Number(button.dataset.mailboxHistory)));
  });
  icons();
}

async function loadMailbox(reset = true) {
  const before = reset ? 0 : state.mailboxBeforeId;
  const query = document.querySelector('#mailboxSearchInput')?.value.trim() || '';
  const result = await api(`/api/mailbox/messages?limit=20&before_id=${before}&query=${encodeURIComponent(query)}`);
  const items = result.items || [];
  state.mailboxItems = reset ? items : [...state.mailboxItems, ...items];
  state.mailboxBeforeId = state.mailboxItems.length
    ? Number(state.mailboxItems[state.mailboxItems.length - 1].message_id)
    : 0;
  state.mailboxHasMore = !query && items.length === 20;
  document.querySelector('#mailboxCount').textContent = `${result.count || 0} 封`;
  renderMailboxList();
}

async function showMailboxView() {
  state.view = 'mailbox';
  document.querySelector('#taskMain').hidden = true;
  setActiveNavigation('mailboxNav');
  document.querySelector('.memory-main').hidden = true;
  document.querySelector('#treasuryMain').hidden = true;
  document.querySelector('#mailboxMain').hidden = false;
  document.querySelector('#hormoneMain').hidden = true;
  document.querySelector('#darkflowMain').hidden = true;
  document.querySelector('#behaviorMain').hidden = true;
  document.querySelector('#topicMain').hidden = true;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.remove('is-active'));
  document.querySelector('#treasuryNav').classList.remove('is-active');
  document.querySelector('#mailboxNav').classList.add('is-active');
  document.querySelector('#hormoneNav').classList.remove('is-active');
  document.querySelector('#darkflowNav').classList.remove('is-active');
  document.querySelector('#behaviorNav').classList.remove('is-active');
  document.querySelector('#topicNav').classList.remove('is-active');
  await loadMailbox(true);
}

const taskImportanceLabels = {1: '低', 2: '较低', 3: '普通', 4: '重要', 5: '紧要'};

function taskItem(taskId) {
  return state.taskItems.find(item => Number(item.task_id) === Number(taskId));
}

function taskStatusLabel(status) {
  return {open: '未完成', completed: '已完成', cancelled: '已取消'}[status] || status;
}

function renderTaskList() {
  const list = document.querySelector('#taskList');
  if (!state.taskItems.length) {
    list.innerHTML = `<div class="manager-empty task-empty"><i data-lucide="circle-check-big"></i><strong>这里暂时是空的</strong><span>明确要完成的事会自动出现在这里，也可以手动记一条。</span></div>`;
    icons();
    return;
  }
  list.innerHTML = state.taskItems.map(item => {
    const source = (item.sources || [])[0];
    const sourceText = source
      ? `来源：${escapeHtml(source.source_type)}${source.source_ref ? ` #${escapeHtml(source.source_ref)}` : ''}`
      : '手动记录';
    const primaryAction = item.status === 'open'
      ? `<button class="task-action is-complete" type="button" data-task-state="completed" data-task-id="${item.task_id}"><i data-lucide="check"></i>完成</button>`
      : `<button class="task-action" type="button" data-task-state="open" data-task-id="${item.task_id}"><i data-lucide="rotate-ccw"></i>重新开启</button>`;
    const cancelAction = item.status === 'open'
      ? `<button class="icon-button" type="button" data-task-state="cancelled" data-task-id="${item.task_id}" title="取消事项" aria-label="取消事项"><i data-lucide="ban"></i></button>`
      : '';
    return `<article class="task-entry is-${escapeHtml(item.status)}">
      <div class="task-priority"><b>${escapeHtml(item.importance)}</b><span>${escapeHtml(taskImportanceLabels[item.importance] || '普通')}</span></div>
      <div class="task-copy"><header><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(taskStatusLabel(item.status))}</span></header>${item.details ? `<p>${escapeHtml(item.details).replace(/\n/g, '<br>')}</p>` : ''}<small>#${escapeHtml(item.task_id)} · ${sourceText} · ${escapeHtml(displayDate(item.updated_at))}</small></div>
      <div class="task-entry-actions">${primaryAction}${cancelAction}<button class="icon-button" type="button" data-task-history="${item.task_id}" title="历史版本" aria-label="历史版本"><i data-lucide="history"></i></button><button class="icon-button" type="button" data-task-edit="${item.task_id}" title="修改" aria-label="修改"><i data-lucide="pencil"></i></button><button class="icon-button icon-button--danger" type="button" data-task-delete="${item.task_id}" title="删除" aria-label="删除"><i data-lucide="trash-2"></i></button></div>
    </article>`;
  }).join('');
  list.querySelectorAll('[data-task-state]').forEach(button => button.addEventListener('click', async () => {
    try {
      await api(`/api/tasks/${button.dataset.taskId}`, {method: 'PUT', body: JSON.stringify({status: button.dataset.taskState})});
      toast(button.dataset.taskState === 'completed' ? '这件事已经标为完成。' : button.dataset.taskState === 'open' ? '这件事已经重新开启。' : '这件事已经取消。');
      await loadTasks();
    } catch (error) { toast(error.message, true); }
  }));
  list.querySelectorAll('[data-task-edit]').forEach(button => button.addEventListener('click', () => openTaskEdit(Number(button.dataset.taskEdit))));
  list.querySelectorAll('[data-task-delete]').forEach(button => button.addEventListener('click', () => openTaskDelete(Number(button.dataset.taskDelete))));
  list.querySelectorAll('[data-task-history]').forEach(button => button.addEventListener('click', () => openTaskHistory(Number(button.dataset.taskHistory))));
  icons();
}

async function loadTasks() {
  const query = document.querySelector('#taskSearchInput')?.value.trim() || '';
  const result = await api(`/api/tasks?status=${encodeURIComponent(state.taskStatus)}&query=${encodeURIComponent(query)}&limit=200`);
  state.taskItems = result.items || [];
  const counts = result.counts || {};
  document.querySelector('#taskOpenCount').textContent = `${counts.open || 0} 件未完成`;
  document.querySelector('#countTodo').textContent = counts.open || 0;
  document.querySelector('#quickTodoCount').textContent = counts.open || 0;
  renderTaskList();
}

async function showTaskView() {
  state.view = 'tasks';
  setActiveNavigation('taskNav');
  document.querySelectorAll('.memory-main, #topicMain, #treasuryMain, #mailboxMain, #hormoneMain, #darkflowMain, #behaviorMain').forEach(item => { item.hidden = true; });
  document.querySelector('#taskMain').hidden = false;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.remove('is-active'));
  await loadTasks();
}

function openTaskEdit(taskId) {
  const item = taskItem(taskId);
  if (!item) return;
  state.taskEditingId = taskId;
  document.querySelector('#taskEditHeading').textContent = `未竟 #${taskId}`;
  document.querySelector('#taskEditTitle').value = item.title || '';
  document.querySelector('#taskEditDetails').value = item.details || '';
  document.querySelector('#taskEditImportance').value = String(item.importance || 3);
  document.querySelector('#taskEditStatus').value = item.status || 'open';
  document.querySelector('#taskEditDialog').showModal();
}

function openTaskDelete(taskId) {
  state.taskEditingId = taskId;
  document.querySelector('#taskDeleteId').textContent = taskId;
  document.querySelector('#taskDeleteConfirm').value = '';
  document.querySelector('#taskDeleteDialog').showModal();
}

async function openTaskHistory(taskId) {
  state.taskEditingId = taskId;
  const result = await api(`/api/tasks/${taskId}/history`);
  const rows = result.items || [];
  document.querySelector('#taskHistoryTitle').textContent = `未竟 #${taskId} 历史`;
  document.querySelector('#taskHistoryList').innerHTML = rows.length
    ? rows.map(row => `<article><strong>${escapeHtml(displayDate(row.snapshot_at))} · ${escapeHtml(row.operation)}</strong><small>${escapeHtml(taskStatusLabel(row.status))} · 重要程度 ${escapeHtml(row.importance)}</small><p>${escapeHtml(row.title)}${row.details ? `<br>${escapeHtml(row.details)}` : ''}</p></article>`).join('')
    : '<div class="manager-empty"><i data-lucide="history"></i><strong>还没有历史版本</strong></div>';
  document.querySelector('#taskHistoryDialog').showModal();
  icons();
}

function elapsedLabel(seconds = 0) {
  const total = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return `${hours}h${String(minutes).padStart(2, '0')}m`;
}

function renderHormone(stateValue) {
  state.hormoneState = stateValue;
  const available = Boolean(stateValue.available);
  const disabled = Boolean(stateValue.disabled);
  const intro = document.querySelector('#hormoneIntro');
  intro.innerHTML = disabled
    ? '<i data-lucide="circle-off"></i><div><strong>激素未启用</strong><span>记忆系统其余部分仍可正常使用。</span></div>'
    : available
      ? `<i data-lucide="activity"></i><div><strong>${stateValue.repeated ? '最近一次开机交接' : '当前沉默周期正在变化'}</strong><span>${escapeHtml(stateValue.event_summary || '这里只查看，不会结算、清零或改动记忆。')}</span></div>`
      : '<i data-lucide="circle-dashed"></i><div><strong>激素尚未开始</strong><span>下一次叙事记忆或信箱写入后开始计时。</span></div>';
  document.querySelector('#hormoneDominant').textContent = available ? (stateValue.dominant || '暂无') : '暂无';
  document.querySelector('#hormoneDominantValue').textContent = Number(stateValue.dominant_value || 0).toFixed(2);
  document.querySelector('#hormoneElapsed').textContent = elapsedLabel(stateValue.elapsed_seconds);
  document.querySelector('#hormoneCycle').textContent = available ? `#${stateValue.cycle_id || 0}` : '尚未开始';
  document.querySelector('#hormoneCycleNote').textContent = stateValue.repeated ? '已完成一次开机交接' : available ? '等待下一次 pulse_boot 交接' : '等待第一条叙事写入';
  const obsessions = stateValue.obsessions || [];
  document.querySelector('#hormoneObsessionCount').textContent = obsessions.length;
  document.querySelector('#hormoneObsessionText').textContent = obsessions.length ? obsessions.slice(0, 3).map(item => item.event_tag).join('、') : '暂无';
  const pipes = stateValue.pipes || {};
  const names = ['想靠近', '想黏着', '肌肤饥渴', '性欲', '想知道她在干嘛', '想分享', '好奇', '闲', '社交', '责任', '难过', '生气', '醋', '自省', '开心', '满足'];
  document.querySelector('#hormoneGrid').innerHTML = names.map(name => {
    const value = Math.max(0, Math.min(1, Number(pipes[name]) || 0));
    return `<div class="hormone-item"><div><strong>${escapeHtml(name)}</strong><b>${value.toFixed(2)}</b></div><span><i style="width:${Math.round(value * 100)}%"></i></span></div>`;
  }).join('');
  icons();
}

async function loadHormone() {
  renderHormone(await api('/api/xinchao/status'));
}

const judgePipeNames = ['想靠近', '想黏着', '肌肤饥渴', '性欲', '想知道她在干嘛', '想分享', '好奇', '闲', '社交', '责任', '难过', '生气', '醋', '自省', '开心', '满足'];

function renderJudgeBaselines(baselines = {}) {
  document.querySelector('#judgeBaselines').innerHTML = judgePipeNames.map(name => {
    const value = Number(baselines[name] || 0);
    return `<label class="judge-baseline-item"><span><strong>${escapeHtml(name)}</strong><b>${value.toFixed(2)}</b></span><input type="range" min="0" max="0.8" step="0.01" data-baseline="${escapeHtml(name)}" value="${value}"></label>`;
  }).join('');
}

function collectJudgeBaselines() {
  const result = {};
  document.querySelectorAll('[data-baseline]').forEach(input => {
    result[input.dataset.baseline] = Number(input.value);
  });
  return result;
}

function judgeRelationCard(relation = {}, index = 0) {
  const aliases = Array.isArray(relation.aliases) ? relation.aliases.join('、') : '';
  const trigger = relation.trigger || {};
  const safetyOptions = ['核心', '安全', '中等', '危险'].map(value =>
    `<option value="${value}" ${relation.safety === value ? 'selected' : ''}>${value}</option>`
  ).join('');
  const triggerFields = judgePipeNames.map(name => {
    const value = Object.prototype.hasOwnProperty.call(trigger, name) ? trigger[name] : '';
    return `<label><span>${escapeHtml(name)}</span><input type="number" min="-0.8" max="0.8" step="0.05" data-trigger="${escapeHtml(name)}" value="${escapeHtml(value)}" placeholder="0"></label>`;
  }).join('');
  return `
    <article class="judge-relation" data-judge-index="${index}">
      <div class="judge-relation-title"><strong>人物 ${index + 1}</strong><button class="icon-button icon-button--danger" type="button" data-remove-judge title="删除这个人物"><i data-lucide="trash-2"></i></button></div>
      <div class="judge-relation-grid">
        <label><span>名字</span><input maxlength="80" data-judge-field="name" value="${escapeHtml(relation.name || '')}" placeholder="这个人怎么称呼"></label>
        <label><span>别名</span><input data-judge-field="aliases" value="${escapeHtml(aliases)}" placeholder="多个别名用逗号分开"></label>
        <label><span>关系身份</span><input maxlength="120" data-judge-field="role" value="${escapeHtml(relation.role || '')}" placeholder="例如：朋友、家人、重要的人"></label>
        <label><span>安全程度</span><select data-judge-field="safety"><option value="">未设置</option>${safetyOptions}</select></label>
      </div>
      <label class="judge-note"><span>关系说明</span><textarea maxlength="300" data-judge-field="note" placeholder="写清相处背景，以及哪些事件容易引发什么感受。">${escapeHtml(relation.note || '')}</textarea></label>
      <details class="judge-triggers"><summary>情绪影响数值（选填）</summary><div class="judge-trigger-grid">${triggerFields}</div><small>正数增加，负数释放；范围 -0.8 到 0.8。留空时由 DeepSeek 根据正文判断。</small></details>
    </article>`;
}

function renderJudge(configValue) {
  state.judgeConfig = configValue;
  document.querySelector('#judgeCustomRules').value = configValue.custom_rules || '';
  document.querySelector('#judgeProxyVoice').value = configValue.proxy_voice || '';
  document.querySelector('#judgeDarkflowRules').value = configValue.darkflow_rules || '';
  document.querySelector('#judgeBaseRules').textContent = configValue.base_rules || '';
  renderJudgeBaselines(configValue.baselines || {});
  const relations = configValue.relations || [];
  document.querySelector('#judgeRelations').innerHTML = relations.length
    ? relations.map(judgeRelationCard).join('')
    : '<div class="manager-empty judge-empty"><i data-lucide="users"></i><strong>还没有人物关系</strong><span>可以先留空，DeepSeek 仍会按正文判断。</span></div>';
  document.querySelector('#judgeRelationCount').textContent = `${relations.length} 个人物`;
  document.querySelector('#judgeLive').innerHTML = '<i></i>保存后立即生效';
  document.querySelector('#judgeSaveNote').textContent = configValue.saved_at
    ? `上次保存：${displayDate(configValue.saved_at)}。只保存在这台电脑的私人目录。`
    : '只保存在这台电脑的私人目录；保存前会留下上一版。';
  icons();
}

async function loadJudge() {
  renderJudge(await api('/api/xinchao/judge'));
}

function collectJudgeRelations() {
  return Array.from(document.querySelectorAll('.judge-relation')).map(card => {
    const field = name => card.querySelector(`[data-judge-field="${name}"]`).value.trim();
    const trigger = {};
    card.querySelectorAll('[data-trigger]').forEach(input => {
      if (input.value === '') return;
      trigger[input.dataset.trigger] = Number(input.value);
    });
    return {
      name: field('name'),
      aliases: field('aliases').split(/[、,，]/).map(item => item.trim()).filter(Boolean),
      role: field('role'),
      safety: field('safety'),
      note: field('note'),
      trigger,
    };
  }).filter(item => item.name);
}

async function saveJudge() {
  const button = document.querySelector('#saveJudge');
  button.disabled = true;
  try {
    const result = await api('/api/xinchao/judge', {
      method: 'PUT',
      body: JSON.stringify({
        custom_rules: document.querySelector('#judgeCustomRules').value.trim(),
        proxy_voice: document.querySelector('#judgeProxyVoice').value.trim(),
        darkflow_rules: document.querySelector('#judgeDarkflowRules').value.trim(),
        baselines: collectJudgeBaselines(),
        relations: collectJudgeRelations(),
      }),
    });
    renderJudge({...state.judgeConfig, ...result});
    toast('裁判书已保存，下一次写入立即采用。');
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

document.querySelector('#judgeBaselines').addEventListener('input', event => {
  const input = event.target.closest('[data-baseline]');
  if (!input) return;
  input.closest('.judge-baseline-item').querySelector('b').textContent = Number(input.value).toFixed(2);
});

async function showHormoneView() {
  state.view = 'hormone';
  document.querySelector('#taskMain').hidden = true;
  setActiveNavigation('hormoneNav');
  document.querySelector('.memory-main').hidden = true;
  document.querySelector('#treasuryMain').hidden = true;
  document.querySelector('#mailboxMain').hidden = true;
  document.querySelector('#hormoneMain').hidden = false;
  document.querySelector('#darkflowMain').hidden = true;
  document.querySelector('#behaviorMain').hidden = true;
  document.querySelector('#topicMain').hidden = true;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.remove('is-active'));
  document.querySelector('#treasuryNav').classList.remove('is-active');
  document.querySelector('#mailboxNav').classList.remove('is-active');
  document.querySelector('#hormoneNav').classList.add('is-active');
  document.querySelector('#darkflowNav').classList.remove('is-active');
  document.querySelector('#behaviorNav').classList.remove('is-active');
  document.querySelector('#topicNav').classList.remove('is-active');
  await Promise.all([loadHormone(), loadJudge()]);
}

function renderDarkflow(result) {
  const item = result?.item || null;
  state.darkflow = item;
  document.querySelector('#darkflowCycle').textContent = item ? `#${item.cycle_id}` : '暂无';
  document.querySelector('#darkflowCreated').textContent = item ? displayDate(item.created_at) : '暂无';
  document.querySelector('#darkflowStatus').textContent = item
    ? (item.status === 'pending' ? '待 AI 接收' : 'AI 已接收')
    : '暂无';
  document.querySelector('#darkflowDelivered').textContent = item?.delivered_at
    ? `接收于 ${displayDate(item.delivered_at)}`
    : item ? '下一次开机交接一次' : '尚未生成';
  document.querySelector('#darkflowEventCount').textContent = item?.event_count || 0;
  const stageLabels = {
    awake_waiting: '清醒等待',
    drowsy: '困倦',
    light_sleep: '浅睡',
    dreaming: '梦境沉淀',
    deep_sleep: '深睡',
    hibernating: '深度休眠',
  };
  document.querySelector('#darkflowSleepStage').textContent = item
    ? (stageLabels[item.sleep_stage] || item.sleep_stage || '暂无')
    : '暂无';
  document.querySelector('#darkflowElapsed').textContent = item
    ? `已经过 ${elapsedLabel(item.elapsed_seconds)}`
    : '尚未计时';
  document.querySelector('#darkflowNextStage').textContent = item?.next_stage_at
    ? displayDate(item.next_stage_at)
    : item ? '等待唤醒' : '暂无';
  const card = document.querySelector('#darkflowCard');
  if (!item) {
    card.innerHTML = '<div class="manager-empty darkflow-empty"><i data-lucide="waves"></i><strong>还没有暗涌</strong><span>最后一次报到后持续沉默半小时，后台会在这里留下第一版。</span></div>';
  } else {
    const mailboxSource = item.mailbox_message_id
      ? `参考主信箱 #${escapeHtml(item.mailbox_message_id)} · ${escapeHtml(displayDate(item.mailbox_created_at))}`
      : '本轮没有可用的主信箱，暗涌只依据同周期事件生成';
    card.innerHTML = `<div class="darkflow-copy"><header><strong>最新暗涌</strong><small>周期 #${escapeHtml(item.cycle_id)} · 第 ${escapeHtml(item.stage_index || 1)} 次沉淀</small></header><p>${escapeHtml(item.content).replace(/\n/g, '<br>')}</p><div class="darkflow-source">离开起点：${escapeHtml(displayDate(item.absence_started_at))}<br>${mailboxSource}<br>依据 ${escapeHtml(item.event_count || 0)} 条事件卡。每次都把上一版融入新稿，只保留这一篇；管理页查看不会改变交接状态。</div></div>`;
  }
  icons();
}

async function loadDarkflow() {
  renderDarkflow(await api('/api/xinchao/darkflow'));
}

async function showDarkflowView() {
  state.view = 'darkflow';
  document.querySelector('#taskMain').hidden = true;
  setActiveNavigation('darkflowNav');
  document.querySelector('.memory-main').hidden = true;
  document.querySelector('#treasuryMain').hidden = true;
  document.querySelector('#mailboxMain').hidden = true;
  document.querySelector('#hormoneMain').hidden = true;
  document.querySelector('#darkflowMain').hidden = false;
  document.querySelector('#behaviorMain').hidden = true;
  document.querySelector('#topicMain').hidden = true;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.remove('is-active'));
  document.querySelector('#treasuryNav').classList.remove('is-active');
  document.querySelector('#mailboxNav').classList.remove('is-active');
  document.querySelector('#hormoneNav').classList.remove('is-active');
  document.querySelector('#darkflowNav').classList.add('is-active');
  document.querySelector('#behaviorNav').classList.remove('is-active');
  document.querySelector('#topicNav').classList.remove('is-active');
  await loadDarkflow();
}

function renderBehavior(result) {
  state.behaviorItems = result.items || [];
  state.behaviorCandidates = result.candidates || [];
  const mode = result.mode === 'live' ? '真实推送模式' : '演习模式';
  document.querySelector('#behaviorMode').textContent = mode;
  document.querySelector('#behaviorModeNote').textContent = result.mode === 'live'
    ? (result.configured ? 'Bark 已配置；每次发送结果都会留在这里。' : 'Bark 密钥未配置，行为会保留为等待状态。')
    : '只记录本来会发送的内容，不会真的推到手机。';
  const waitingCount = state.behaviorCandidates.filter(item => ['pending', 'waiting'].includes(item.status)).length;
  document.querySelector('#behaviorCount').textContent = `${result.count || 0} 条行为 · ${waitingCount} 条候场`;
  const list = document.querySelector('#behaviorList');
  if (!state.behaviorItems.length && !state.behaviorCandidates.length) {
    list.innerHTML = '<div class="manager-empty"><i data-lucide="send"></i><strong>还没有候场或行为记录</strong><span>写入真实事件后，系统会判断是否值得稍后跟进。</span></div>';
  } else {
    const labels = {rehearsal: '演习', sent: '已发送', held: '等待', failed: '失败'};
    const candidateLabels = {pending: '候场中', waiting: '继续等待', skipped: '已取消', cancelled: '已取消', expired: '已过期', sent: '已发送', rehearsal: '演习完成', failed: '失败'};
    const candidateHtml = state.behaviorCandidates.map(item => `
      <article class="behavior-entry">
        <header><strong>${escapeHtml(candidateLabels[item.status] || item.status)}</strong><small>预计判断 ${escapeHtml(displayDate(item.due_at))} · 事件 #${escapeHtml(item.source_event_id)}</small></header>
        <p>${escapeHtml(item.decision_note || '等待到点判断')}</p>
      </article>`).join('');
    const actionHtml = state.behaviorItems.map(item => `
      <article class="behavior-entry">
        <header><strong>${escapeHtml(labels[item.status] || item.status)}</strong><small>${escapeHtml(displayDate(item.decided_at))} · 周期 #${escapeHtml(item.cycle_id)} / 阶段 ${escapeHtml(item.stage_index)}</small></header>
        <p>${escapeHtml(item.content).replace(/\n/g, '<br>')}</p>
        ${item.error ? `<small class="behavior-error">${escapeHtml(item.error)}</small>` : ''}
      </article>`).join('');
    list.innerHTML = candidateHtml + actionHtml;
  }
  icons();
}

async function loadBehavior() {
  renderBehavior(await api('/api/behavior/actions?limit=50'));
}

async function showBehaviorView() {
  state.view = 'behavior';
  document.querySelector('#taskMain').hidden = true;
  setActiveNavigation('behaviorNav');
  document.querySelector('.memory-main').hidden = true;
  document.querySelector('#treasuryMain').hidden = true;
  document.querySelector('#mailboxMain').hidden = true;
  document.querySelector('#hormoneMain').hidden = true;
  document.querySelector('#darkflowMain').hidden = true;
  document.querySelector('#behaviorMain').hidden = false;
  document.querySelector('#topicMain').hidden = true;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.remove('is-active'));
  ['#treasuryNav', '#mailboxNav', '#hormoneNav', '#darkflowNav'].forEach(selector => document.querySelector(selector).classList.remove('is-active'));
  document.querySelector('#behaviorNav').classList.add('is-active');
  document.querySelector('#topicNav').classList.remove('is-active');
  await loadBehavior();
}

function currentTopicNode() {
  return state.topicTree.find(item => item.main_topic === state.topicMain) || null;
}

function topicIconName(name) {
  const value = String(name || '');
  if (value.includes('AI 自我') || value.includes('AI')) return 'sparkles';
  if (value.includes('用户')) return 'user-round';
  if (value.includes('关系')) return 'heart-handshake';
  if (value.includes('性爱')) return 'flame';
  if (value.includes('生活')) return 'house';
  if (value.includes('未来') || value.includes('约定')) return 'calendar-heart';
  if (value.includes('系统') || value.includes('技术')) return 'cpu';
  return 'folder';
}

function updateTopicStage() {
  const browser = document.querySelector('#topicBrowser');
  if (!browser) return;
  browser.dataset.stage = state.topicUnassigned || state.topicSub
    ? 'buckets'
    : (state.topicMain ? 'sub' : 'main');
  document.querySelector('#topicSubHeading').textContent = state.topicMain || '小目录';
}

function renderTopicMainList(unassignedCount = 0) {
  const container = document.querySelector('#topicMainList');
  const rows = state.topicTree.map((item, index) => {
    const count = item.subtopics.reduce((sum, sub) => sum + Number(sub.count || 0), 0);
    return `
      <button type="button" class="topic-directory topic-main-card topic-tone-${index % 7} ${!state.topicUnassigned && state.topicMain === item.main_topic ? 'is-active' : ''}" data-topic-main="${escapeHtml(item.main_topic)}">
        <i data-lucide="${topicIconName(item.main_topic)}"></i><span>${escapeHtml(item.main_topic)}</span><b>${count}</b><small>${item.subtopics.length} 个小目录</small>
      </button>`;
  }).join('');
  container.innerHTML = `${rows}
    <button type="button" class="topic-directory is-unassigned ${state.topicUnassigned ? 'is-active' : ''}" data-topic-unassigned="true">
      <i data-lucide="inbox"></i><span>待分类</span><b>${Number(unassignedCount || 0)}</b>
    </button>`;
  container.querySelectorAll('[data-topic-main]').forEach(button => {
    button.addEventListener('click', () => selectTopicMain(button.dataset.topicMain));
  });
  container.querySelector('[data-topic-unassigned]').addEventListener('click', selectUnassignedTopics);
  updateTopicStage();
  icons();
}

function renderTopicSubList() {
  const container = document.querySelector('#topicSubList');
  if (state.topicUnassigned) {
    container.innerHTML = '<div class="topic-step-note"><i data-lucide="arrow-right"></i><span>右边是还没有确定位置的记忆。逐条调整即可。</span></div>';
    icons();
    return;
  }
  const node = currentTopicNode();
  if (!node) {
    container.innerHTML = '<div class="topic-step-note">先选择一个主目录。</div>';
    return;
  }
  container.innerHTML = node.subtopics.map(item => `
    <button type="button" class="topic-directory ${state.topicSub === item.name ? 'is-active' : ''}" data-topic-sub="${escapeHtml(item.name)}">
      <i data-lucide="folder-closed"></i><span>${escapeHtml(item.name)}</span><b>${Number(item.count || 0)}</b>
    </button>`).join('');
  container.querySelectorAll('[data-topic-sub]').forEach(button => {
    button.addEventListener('click', () => selectTopicSub(button.dataset.topicSub));
  });
  updateTopicStage();
  icons();
}

function renderTopicBuckets() {
  const list = document.querySelector('#topicBucketList');
  document.querySelector('#topicBucketCount').textContent = `${state.topicItems.length} 条`;
  document.querySelector('#topicBucketHeading').textContent = state.topicUnassigned
    ? '待分类记忆'
    : (state.topicSub || '记忆桶');
  if (!state.topicItems.length) {
    if (state.topicMain && !state.topicSub) {
      list.innerHTML = '<div class="manager-empty"><i data-lucide="folder-tree"></i><strong>再选一个小目录</strong><span>具体记忆会在下一步显示。</span></div>';
    } else if (!state.topicMain && !state.topicUnassigned) {
      list.innerHTML = '<div class="manager-empty"><i data-lucide="mouse-pointer-2"></i><strong>先选一个主目录</strong><span>小目录会在选择后显示。</span></div>';
    } else {
      list.innerHTML = '<div class="manager-empty"><i data-lucide="folder-open"></i><strong>这里还是空的</strong><span>选择其他目录，或等待新记忆自动归入。</span></div>';
    }
    icons();
    updateTopicStage();
    return;
  }
  list.innerHTML = state.topicItems.map(item => `
    <article class="topic-bucket-row">
      <button type="button" class="topic-open-memory" data-topic-open="${escapeHtml(item.id)}">
        <span class="bucket-type"><i data-lucide="file-text"></i></span>
        <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.last_active || item.created || '')}</small></span>
      </button>
      <button type="button" class="icon-button" data-topic-edit="${escapeHtml(item.id)}" title="调整目录" aria-label="调整目录"><i data-lucide="folder-input"></i></button>
    </article>`).join('');
  list.querySelectorAll('[data-topic-open]').forEach(button => {
    button.addEventListener('click', async () => {
      showMemoryView();
      await selectMemory(button.dataset.topicOpen);
    });
  });
  list.querySelectorAll('[data-topic-edit]').forEach(button => {
    button.addEventListener('click', () => openTopicDialog(button.dataset.topicEdit));
  });
  updateTopicStage();
  icons();
}

async function returnToTopicMain() {
  state.topicUnassigned = false;
  state.topicMain = '';
  state.topicSub = '';
  state.topicItems = [];
  renderTopicMainList(Number(document.querySelector('#topicUnassignedCount').textContent || 0));
  renderTopicSubList();
  renderTopicBuckets();
}

async function returnToTopicSubs() {
  if (state.topicUnassigned) {
    await returnToTopicMain();
    return;
  }
  state.topicSub = '';
  state.topicItems = [];
  renderTopicSubList();
  renderTopicBuckets();
}

async function loadTopicBuckets() {
  if (state.topicUnassigned) {
    const result = await api('/api/topics/buckets?unassigned=true');
    state.topicItems = result.items || [];
  } else if (state.topicMain && state.topicSub) {
    const result = await api(`/api/topics/buckets?main_topic=${encodeURIComponent(state.topicMain)}&subtopic=${encodeURIComponent(state.topicSub)}`);
    state.topicItems = result.items || [];
  } else {
    state.topicItems = [];
  }
  renderTopicBuckets();
}

async function selectTopicMain(mainTopic) {
  state.topicUnassigned = false;
  state.topicMain = mainTopic;
  state.topicSub = '';
  renderTopicMainList(Number(document.querySelector('#topicUnassignedCount').textContent || 0));
  renderTopicSubList();
  await loadTopicBuckets();
}

async function selectTopicSub(subtopic) {
  state.topicSub = subtopic;
  renderTopicSubList();
  await loadTopicBuckets();
}

async function selectUnassignedTopics() {
  state.topicUnassigned = true;
  state.topicMain = '';
  state.topicSub = '';
  renderTopicMainList(Number(document.querySelector('#topicUnassignedCount').textContent || 0));
  renderTopicSubList();
  await loadTopicBuckets();
}

function fillTopicSubSelect(selected = '') {
  const main = document.querySelector('#topicMainSelect').value;
  const node = state.topicTree.find(item => item.main_topic === main);
  const select = document.querySelector('#topicSubSelect');
  select.innerHTML = (node?.subtopics || []).map(item =>
    `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`
  ).join('');
  if (selected && [...select.options].some(option => option.value === selected)) {
    select.value = selected;
  }
}

function openTopicDialog(bucketId) {
  const item = state.topicItems.find(candidate => candidate.id === bucketId);
  if (!item) return;
  document.querySelector('#topicBucketId').value = bucketId;
  document.querySelector('#topicDialogTitle').textContent = item.title;
  const mainSelect = document.querySelector('#topicMainSelect');
  mainSelect.innerHTML = state.topicTree.map(node =>
    `<option value="${escapeHtml(node.main_topic)}">${escapeHtml(node.main_topic)}</option>`
  ).join('');
  const assignment = item.topic || null;
  if (assignment) mainSelect.value = assignment.main_topic;
  else if (state.topicMain) mainSelect.value = state.topicMain;
  fillTopicSubSelect(assignment?.subtopic || state.topicSub || '');
  document.querySelector('#removeTopicAssignment').hidden = !assignment;
  document.querySelector('#topicDialog').showModal();
  icons();
}

async function loadTopics(reset = false) {
  const result = await api('/api/topics');
  state.topicTree = result.tree || [];
  document.querySelector('#topicAssignedCount').textContent = result.assigned || 0;
  document.querySelector('#topicUnassignedCount').textContent = result.unassigned || 0;
  if (reset) {
    state.topicUnassigned = false;
    state.topicMain = '';
    state.topicSub = '';
  }
  renderTopicMainList(result.unassigned || 0);
  renderTopicSubList();
  await loadTopicBuckets();
}

async function showTopicView() {
  state.view = 'topics';
  document.querySelector('#taskMain').hidden = true;
  setActiveNavigation('topicNav');
  document.querySelector('.memory-main').hidden = true;
  document.querySelector('#treasuryMain').hidden = true;
  document.querySelector('#mailboxMain').hidden = true;
  document.querySelector('#hormoneMain').hidden = true;
  document.querySelector('#darkflowMain').hidden = true;
  document.querySelector('#behaviorMain').hidden = true;
  document.querySelector('#topicMain').hidden = false;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.remove('is-active'));
  ['#treasuryNav', '#mailboxNav', '#hormoneNav', '#darkflowNav', '#behaviorNav'].forEach(selector => document.querySelector(selector).classList.remove('is-active'));
  document.querySelector('#topicNav').classList.add('is-active');
  await loadTopics(true);
}

function refreshTopicPreviewCounts() {
  const selected = document.querySelectorAll('#topicAutoList input[type="checkbox"]:checked').length;
  document.querySelector('#topicPreviewSelected').textContent = selected;
  document.querySelector('#topicPreviewHeld').textContent = Math.max(0, state.topicPreview.length - selected);
}

function renderTopicPreview() {
  const list = document.querySelector('#topicAutoList');
  document.querySelector('#topicPreviewTotal').textContent = state.topicPreview.length;
  list.innerHTML = state.topicPreview.map((item, index) => {
    const confidence = Number(item.confidence || 0);
    const valid = Boolean(item.main_topic && item.subtopic);
    const selected = valid && confidence >= 0.85;
    return `
      <label class="topic-auto-row ${selected ? 'is-selected' : ''}">
        <input type="checkbox" data-topic-preview-index="${index}" ${selected ? 'checked' : ''} ${valid ? '' : 'disabled'}>
        <span><strong>${escapeHtml(item.title)}</strong><small>${valid ? `${escapeHtml(item.main_topic)} / ${escapeHtml(item.subtopic)}` : '暂时留在待分类'}</small></span>
        <b>${Math.round(confidence * 100)}%</b>
        <em>${escapeHtml(item.reason || '')}</em>
      </label>`;
  }).join('') || '<div class="manager-empty"><i data-lucide="circle-check-big"></i><strong>已经整理完了</strong><span>目前没有待分类记忆。</span></div>';
  list.querySelectorAll('input[type="checkbox"]').forEach(input => {
    input.addEventListener('change', () => {
      input.closest('.topic-auto-row').classList.toggle('is-selected', input.checked);
      refreshTopicPreviewCounts();
    });
  });
  refreshTopicPreviewCounts();
  icons();
}

async function openTopicPreview() {
  const button = document.querySelector('#autoOrganizeTopics');
  button.disabled = true;
  try {
    const result = await api('/api/topics/preview?smart=true');
    state.topicPreview = result.items || [];
    renderTopicPreview();
    if (result.warning) toast(result.warning, true);
    document.querySelector('#topicAutoDialog').showModal();
  } finally {
    button.disabled = false;
  }
}

async function applyTopicPreview() {
  const selected = [...document.querySelectorAll('#topicAutoList input[type="checkbox"]:checked')]
    .map(input => state.topicPreview[Number(input.dataset.topicPreviewIndex)])
    .filter(Boolean)
    .map(item => ({bucket_id: item.bucket_id, main_topic: item.main_topic, subtopic: item.subtopic}));
  if (!selected.length) {
    toast('没有勾选要整理的记忆。', true);
    return;
  }
  const button = document.querySelector('#confirmTopicBulk');
  button.disabled = true;
  try {
    const result = await api('/api/topics/bulk-apply', {
      method: 'POST', body: JSON.stringify({items: selected, confirm: true}),
    });
    document.querySelector('#topicAutoDialog').close();
    await Promise.all([loadTopics(true), loadStats()]);
    toast(`已经整理 ${result.applied || 0} 条，正文没有修改。`);
  } finally {
    button.disabled = false;
  }
}

async function undoTopicBulk() {
  const button = document.querySelector('#undoTopicBulk');
  button.disabled = true;
  try {
    const result = await api('/api/topics/bulk-undo', {
      method: 'POST', body: JSON.stringify({confirm: true}),
    });
    document.querySelector('#topicAutoDialog').close();
    await Promise.all([loadTopics(true), loadStats()]);
    toast(result.restored ? `已撤回上次整理的 ${result.restored} 条。` : '没有可以撤回的整批整理。');
  } finally {
    button.disabled = false;
  }
}

function openMailboxEdit(messageId) {
  const item = mailboxItem(messageId);
  if (!item) return;
  state.mailboxEditingId = messageId;
  document.querySelector('#mailboxEditTitle').textContent = `留言 #${messageId}`;
  document.querySelector('#mailboxEditMessage').value = item.message;
  document.querySelector('#mailboxEditDialog').showModal();
}

function openMailboxDelete(messageId) {
  state.mailboxEditingId = messageId;
  document.querySelector('#mailboxDeleteId').textContent = messageId;
  document.querySelector('#mailboxDeleteConfirm').value = '';
  document.querySelector('#mailboxDeleteDialog').showModal();
}

async function openMailboxHistory(messageId) {
  state.mailboxEditingId = messageId;
  const result = await api(`/api/mailbox/messages/${messageId}/history`);
  const list = document.querySelector('#mailboxHistoryList');
  document.querySelector('#mailboxHistoryTitle').textContent = `留言 #${messageId}`;
  list.innerHTML = result.items?.length
    ? result.items.map(item => `
        <details class="history-entry">
          <summary><span>${escapeHtml(displayDate(item.snapshot_at))}</span><strong>${item.operation === 'delete' ? '删除前' : '修改前'}</strong><small>完整旧版本</small></summary>
          <pre>${escapeHtml(item.message)}</pre>
        </details>`).join('')
    : '<div class="manager-empty"><i data-lucide="history"></i><strong>暂无历史版本</strong></div>';
  document.querySelector('#mailboxHistoryDialog').showModal();
  icons();
}

function treasuryItem(entryId) {
  return state.treasuryItems.find(item => Number(item.entry_id) === Number(entryId));
}

function openTreasuryEdit(entryId) {
  const item = treasuryItem(entryId);
  if (!item) return;
  state.treasuryEditingId = entryId;
  document.querySelector('#treasuryEditTitle').textContent = `账目 #${entryId}`;
  document.querySelector('#treasuryEditType').value = item.entry_type;
  document.querySelector('#treasuryEditAmount').value = item.amount;
  document.querySelector('#treasuryEditReason').value = item.reason;
  document.querySelector('#treasuryEditTime').value = datetimeLocalValue(item.occurred_at);
  document.querySelector('#treasuryEditDialog').showModal();
}

function openTreasuryDelete(entryId) {
  state.treasuryEditingId = entryId;
  document.querySelector('#treasuryDeleteId').textContent = entryId;
  document.querySelector('#treasuryDeleteConfirm').value = '';
  document.querySelector('#treasuryDeleteDialog').showModal();
}

async function openTreasuryHistory(entryId) {
  try {
    const result = await api(`/api/treasury/entries/${entryId}/history`);
    const history = result.items || [];
    document.querySelector('#treasuryHistoryTitle').textContent = `账目 #${entryId} 历史`;
    document.querySelector('#treasuryHistoryList').innerHTML = history.length ? history.map(item => `
      <details class="history-entry">
        <summary><span>${escapeHtml(displayDate(item.snapshot_at))}</span><strong>${item.operation === 'update' ? '修改前快照' : '删除前快照'}</strong><small>¥${escapeHtml(item.amount)}</small></summary>
        <pre>${escapeHtml(item.entry_type === 'income' ? '收入' : '支出')} ¥${escapeHtml(item.amount)}
发生时间：${escapeHtml(displayDate(item.occurred_at))}
原因：${escapeHtml(item.reason)}</pre>
      </details>`).join('') : '<div class="detail-note">这笔账还没有历史版本。</div>';
    document.querySelector('#treasuryHistoryDialog').showModal();
    icons();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderList() {
  const list = document.querySelector('#bucketList');
  document.querySelector('#resultCount').textContent = `${state.items.length} 条记忆`;
  if (!state.items.length) {
    list.innerHTML = `
      <div class="manager-empty">
        <i data-lucide="inbox"></i>
        <strong>这里还是空的</strong>
        <span>写下第一条记忆后，它会出现在这里。</span>
      </div>`;
    document.querySelector('#detailPane').hidden = true;
    icons();
    return;
  }
  list.innerHTML = state.items.map(item => `
    <button class="bucket-item ${item.id === state.selectedId ? 'is-selected' : ''}" type="button" data-memory-id="${escapeHtml(item.id)}">
      <span class="bucket-type ${item.trigger_date ? 'is-amber' : item.category === 'facts' ? 'is-blue' : ''}"><i data-lucide="${iconFor(item)}"></i></span>
      <span class="bucket-copy"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.summary || '暂无摘要')}</p><small>${escapeHtml(item.last_active || item.created || '')}</small></span>
      <span class="bucket-markers">${item.pin_level ? '<i data-lucide="pin"></i>' : ''}${item.sealed || item.archived ? '<i data-lucide="archive"></i>' : ''}</span>
    </button>
  `).join('');
  list.querySelectorAll('[data-memory-id]').forEach(button => {
    button.addEventListener('click', () => selectMemory(button.dataset.memoryId));
  });
  icons();
}

async function loadList(keepSelection = true) {
  const search = document.querySelector('#memorySearch').value.trim();
  const result = await api(`/api/buckets?filter=${encodeURIComponent(state.filter)}&search=${encodeURIComponent(search)}`);
  state.items = result.items;
  const selectionStillVisible = state.items.some(item => item.id === state.selectedId);
  if (!keepSelection || !selectionStillVisible) {
    state.selectedId = state.items[0]?.id || '';
  }
  renderList();
  if (state.selectedId) await selectMemory(state.selectedId, false);
}

function renderHistory(history) {
  const container = document.querySelector('#historyTab');
  document.querySelector('#historyCount').textContent = history.length;
  container.innerHTML = history.length ? history.map(snapshot => `
    <details class="history-entry">
      <summary><span>${escapeHtml(snapshot.snapshot_at || '')}</span><strong>${escapeHtml(snapshot.operation_type || '修改前快照')}</strong><small>完整正文与元数据</small></summary>
      <pre>${escapeHtml(snapshot.content || '')}</pre>
    </details>
  `).join('') : '<div class="detail-note">这条记忆还没有历史版本。</div>';
}

function renderRelations(relations) {
  const container = document.querySelector('#relationsTab');
  document.querySelector('#relationCount').textContent = relations.length;
  container.innerHTML = relations.length ? relations.map(item => `
    <button type="button" data-related-id="${escapeHtml(item.id)}"><span class="relation-score">${Math.round(item.similarity * 100)}%</span><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.summary || '')}</small></div><i data-lucide="arrow-up-right"></i></button>
  `).join('') : '<div class="detail-note">暂时没有自动关联的记忆。</div>';
  container.querySelectorAll('[data-related-id]').forEach(button => {
    button.addEventListener('click', () => selectMemory(button.dataset.relatedId));
  });
}

function renderDetail() {
  const item = state.detail;
  if (!item) {
    document.querySelector('#detailPane').hidden = true;
    return;
  }
  document.querySelector('#detailPane').hidden = false;
  document.querySelector('#detailType').textContent = item.type;
  document.querySelector('#detailTitle').textContent = item.title;
  document.querySelector('#detailTypeIcon').innerHTML = `<i data-lucide="${iconFor(item)}"></i>`;
  document.querySelector('#detailMeta').innerHTML = `
    <span><i data-lucide="fingerprint"></i>${escapeHtml(item.id)}</span>
    <span><i data-lucide="clock-3"></i>${escapeHtml(item.last_active || item.created || '')}</span>
    <span><i data-lucide="star"></i>重要度 ${item.importance}/10</span>
    <span><i data-lucide="activity"></i>V ${item.valence.toFixed(1)} / A ${item.arousal.toFixed(1)}</span>`;
  document.querySelector('#detailTags').innerHTML = [...item.domain, ...item.tags].map(tag => `<span># ${escapeHtml(tag)}</span>`).join('');
  renderPacketContent();
  document.querySelector('#pinAction').classList.toggle('is-active', Boolean(item.pin_level));
  document.querySelector('#archiveAction').classList.toggle('is-active', item.sealed || item.archived);
  renderHistory(item.history || []);
  renderRelations(item.relations || []);
  icons();
}

async function selectMemory(id, rerenderList = true) {
  state.selectedId = id;
  state.detail = await api(`/api/buckets/${encodeURIComponent(id)}`);
  state.segmentIndex = Math.max(0, (state.detail.segments?.length || 1) - 1);
  state.showWholeBucket = false;
  if (rerenderList) renderList();
  renderDetail();
}

function renderPacketContent() {
  const item = state.detail;
  if (!item) return;
  const segments = item.segments || [];
  const index = Math.max(0, Math.min(state.segmentIndex, segments.length - 1));
  state.segmentIndex = index;
  const segment = segments[index];
  document.querySelector('#detailContent').textContent = state.showWholeBucket
    ? (item.content || '')
    : (segment?.content || item.content || '');
  document.querySelector('#packetPosition').textContent = state.showWholeBucket
    ? `整桶 · ${segments.length} 包`
    : `${segment?.timestamp || '初始正文'} · 第 ${index + 1}/${segments.length || 1} 包`;
  document.querySelector('#newerPacket').disabled = state.showWholeBucket || index >= segments.length - 1;
  document.querySelector('#olderPacket').disabled = state.showWholeBucket || index <= 0;
  document.querySelector('#toggleWholeBucket').innerHTML = state.showWholeBucket
    ? '<i data-lucide="package-open"></i>回到最新一包'
    : '<i data-lucide="layers-3"></i>查看整桶';
  icons();
}

function fillEditor(item = null) {
  const creating = !item;
  state.editorMode = creating ? 'create' : 'update';
  document.querySelector('#editEyebrow').textContent = creating ? '新建记忆' : '修改记忆';
  document.querySelector('#editTitle').textContent = creating ? '写下第一条内容' : item.title;
  document.querySelector('#editName').value = item?.title || '';
  document.querySelector('#editMode').value = 'replace';
  document.querySelector('#editMode').disabled = creating;
  document.querySelector('#editCategory').value = item?.system_category || '';
  document.querySelector('#editImportance').value = item?.importance ?? 5;
  document.querySelector('#editPinLevel').value = item?.pin_level || '';
  document.querySelector('#editValence').value = item?.valence ?? 0.5;
  document.querySelector('#editArousal').value = item?.arousal ?? 0.3;
  document.querySelector('#editTriggerDate').value = item?.trigger_date || '';
  document.querySelector('#editFeeling').checked = Boolean(item?.feeling);
  const topicMain = document.querySelector('#editTopicMain');
  topicMain.innerHTML = '<option value="">待分类</option>' + state.topicTree.map(node =>
    `<option value="${escapeHtml(node.main_topic)}">${escapeHtml(node.main_topic)}</option>`
  ).join('');
  topicMain.value = item?.topic?.main_topic || '';
  fillEditorTopicSub(item?.topic?.subtopic || '');
  document.querySelector('#editContent').value = item?.content || '';
  state.editorOriginalContent = item?.content || '';
  document.querySelector('#editContent').placeholder = '';
  document.querySelector('#editDialog').showModal();
}

function fillEditorTopicSub(selected = '') {
  const main = document.querySelector('#editTopicMain').value;
  const node = state.topicTree.find(item => item.main_topic === main);
  const sub = document.querySelector('#editTopicSub');
  sub.disabled = !node;
  sub.innerHTML = node
    ? node.subtopics.map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join('')
    : '<option value="">先选择主目录</option>';
  if (selected && [...sub.options].some(option => option.value === selected)) sub.value = selected;
}

async function saveEditorTopic(bucketId, topic) {
  if (topic.main_topic && topic.subtopic) {
    await api(`/api/topics/buckets/${encodeURIComponent(bucketId)}`, {
      method: 'PUT', body: JSON.stringify(topic),
    });
  } else {
    await api(`/api/topics/buckets/${encodeURIComponent(bucketId)}`, {method: 'DELETE'});
  }
}

async function saveEditor() {
  const category = document.querySelector('#editCategory').value;
  const payload = {
    title: document.querySelector('#editName').value.trim(),
    content: document.querySelector('#editContent').value.trim(),
    importance: Number(document.querySelector('#editImportance').value),
    valence: Number(document.querySelector('#editValence').value),
    arousal: Number(document.querySelector('#editArousal').value),
    pin_level: document.querySelector('#editPinLevel').value,
    feeling: document.querySelector('#editFeeling').checked,
    trigger_date: document.querySelector('#editTriggerDate').value,
  };
  const editorTopic = {
    main_topic: document.querySelector('#editTopicMain').value,
    subtopic: document.querySelector('#editTopicSub').value,
  };
  state.pendingEditorTopic = editorTopic;
  if (category || state.editorMode === 'create') {
    payload.tags = category ? [category] : [];
    payload.domain = category ? [category] : [];
  }
  if (!payload.title || !payload.content) {
    toast('标题和正文都要填写。', true);
    return;
  }
  if (state.editorMode === 'update') {
    payload.append = document.querySelector('#editMode').value === 'append';
    const oldLength = state.detail?.content?.length || 0;
    if (!payload.append && payload.content.length < oldLength) {
      state.pendingShortenPayload = payload;
      document.querySelector('#shortenCount').textContent = oldLength - payload.content.length;
      document.querySelector('#shortenBucketId').textContent = state.selectedId;
      document.querySelector('#shortenConfirmInput').value = '';
      document.querySelector('#shortenDialog').showModal();
      return;
    }
  }
  const button = document.querySelector('#saveEdit');
  button.disabled = true;
  try {
    let savedBucketId = state.selectedId;
    if (state.editorMode === 'create') {
      const result = await api('/api/buckets', {method: 'POST', body: JSON.stringify(payload)});
      state.selectedId = result.bucket_id;
      savedBucketId = result.bucket_id;
      toast('记忆已保存，并开始建立向量。');
    } else {
      await api(`/api/buckets/${encodeURIComponent(state.selectedId)}`, {method: 'PUT', body: JSON.stringify(payload)});
      toast('修改前快照已保存，记忆已更新。');
    }
    await saveEditorTopic(savedBucketId, editorTopic);
    document.querySelector('#editDialog').close();
    await Promise.all([loadStats(), loadList()]);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

document.querySelector('#confirmShorten').addEventListener('click', async () => {
  const payload = state.pendingShortenPayload;
  const confirmBucketId = document.querySelector('#shortenConfirmInput').value.trim();
  if (!payload || confirmBucketId !== state.selectedId) {
    toast('桶编号不一致，正文没有修改。', true);
    return;
  }
  const button = document.querySelector('#confirmShorten');
  button.disabled = true;
  try {
    payload.confirm_shortening = true;
    payload.confirm_bucket_id = confirmBucketId;
    await api(`/api/buckets/${encodeURIComponent(state.selectedId)}`, {
      method: 'PUT', body: JSON.stringify(payload),
    });
    await saveEditorTopic(state.selectedId, state.pendingEditorTopic || {main_topic: '', subtopic: ''});
    state.pendingShortenPayload = null;
    document.querySelector('#shortenDialog').close();
    document.querySelector('#editDialog').close();
    toast('修改前快照已保存，删减内容已更新。');
    await Promise.all([loadStats(), loadList()]);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

async function quickUpdate(payload, message) {
  try {
    await api(`/api/buckets/${encodeURIComponent(state.selectedId)}`, {method: 'PUT', body: JSON.stringify(payload)});
    toast(message);
    await Promise.all([loadStats(), loadList()]);
  } catch (error) {
    toast(error.message, true);
  }
}

async function openMemoryFilter(filter, label) {
  state.filter = filter;
  showMemoryView();
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.toggle('is-active', item.dataset.filter === filter));
  document.querySelector('#resultLabel').textContent = label;
  await loadList(false);
}

document.querySelectorAll('[data-filter]').forEach(button => {
  button.addEventListener('click', () => {
    openMemoryFilter(button.dataset.filter, button.querySelector('span').textContent)
      .catch(error => toast(error.message, true));
  });
});

document.querySelectorAll('[data-topic-filter]').forEach(button => {
  button.addEventListener('click', () => {
    openMemoryFilter(button.dataset.topicFilter, button.querySelector('strong').textContent)
      .catch(error => toast(error.message, true));
  });
});

document.querySelector('#editTopicMain').addEventListener('change', () => fillEditorTopicSub());

let searchTimer;
document.querySelector('#memorySearch').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadList(false).catch(error => toast(error.message, true)), 250);
});

document.querySelectorAll('[data-detail-tab]').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-detail-tab]').forEach(item => item.classList.toggle('is-active', item === button));
    document.querySelector('#contentTab').hidden = button.dataset.detailTab !== 'content';
    document.querySelector('#historyTab').hidden = button.dataset.detailTab !== 'history';
    document.querySelector('#relationsTab').hidden = button.dataset.detailTab !== 'relations';
  });
});

document.querySelector('#olderPacket').addEventListener('click', () => {
  state.segmentIndex = Math.max(0, state.segmentIndex - 1);
  renderPacketContent();
});
document.querySelector('#newerPacket').addEventListener('click', () => {
  const lastIndex = Math.max(0, (state.detail?.segments?.length || 1) - 1);
  state.segmentIndex = Math.min(lastIndex, state.segmentIndex + 1);
  renderPacketContent();
});
document.querySelector('#toggleWholeBucket').addEventListener('click', () => {
  state.showWholeBucket = !state.showWholeBucket;
  if (!state.showWholeBucket) {
    state.segmentIndex = Math.max(0, (state.detail?.segments?.length || 1) - 1);
  }
  renderPacketContent();
});

document.querySelector('#memoryNav').addEventListener('click', showMemoryView);
document.querySelector('#homeNav').addEventListener('click', () => {
  showHomeView().catch(error => toast(error.message, true));
});
document.querySelector('#searchNav').addEventListener('click', showSearchView);
document.querySelector('#timelineNav').addEventListener('click', () => {
  showTimelineView().catch(error => toast(error.message, true));
});
document.querySelector('#settingsNav').addEventListener('click', event => {
  event.stopPropagation();
  const menu = document.querySelector('#advancedMenu');
  menu.open = !menu.open;
  document.querySelector('#settingsNav').classList.toggle('is-active', menu.open);
  if (menu.open) loadPushTitleSetting().catch(error => toast(error.message, true));
});
document.querySelector('#advancedMenu').addEventListener('toggle', event => {
  document.querySelector('#settingsNav').classList.toggle('is-active', event.currentTarget.open);
});
document.addEventListener('click', event => {
  const menu = document.querySelector('#advancedMenu');
  if (!menu.open || menu.contains(event.target)) return;
  menu.open = false;
});

async function loadPushTitleSetting() {
  const result = await api('/api/behavior/settings');
  document.querySelector('#pushTitleInput').value = result.push_title || 'Clio';
  document.querySelector('#pushTitleStatus').textContent = '';
}

document.querySelector('#pushTitleSetting').addEventListener('submit', async event => {
  event.preventDefault();
  event.stopPropagation();
  const input = document.querySelector('#pushTitleInput');
  const status = document.querySelector('#pushTitleStatus');
  const pushTitle = input.value.trim();
  if (!pushTitle) return;
  status.textContent = '正在保存';
  try {
    const result = await api('/api/behavior/settings', {
      method: 'PUT',
      body: JSON.stringify({push_title: pushTitle}),
    });
    input.value = result.push_title;
    status.textContent = '已保存，以后的推送都会使用这个名字';
  } catch (error) {
    status.textContent = '';
    toast(error.message, true);
  }
});
document.querySelector('#intelligentSearchForm').addEventListener('submit', event => {
  event.preventDefault();
  runIntelligentSearch().catch(error => toast(error.message, true));
});
document.querySelector('#timelineSearchForm').addEventListener('submit', event => {
  event.preventDefault();
  loadTimeline().catch(error => toast(error.message, true));
});
document.querySelector('#clearTimelineSearch').addEventListener('click', () => {
  document.querySelector('#timelineSearchInput').value = '';
  loadTimeline().catch(error => toast(error.message, true));
});
document.querySelector('#newTimelineFact').addEventListener('click', openTimelineCreateDialog);
document.querySelector('#saveTimelineFact').addEventListener('click', () => {
  saveTimelineFact().catch(error => toast(error.message, true));
});
document.querySelector('#returnFromSearch').addEventListener('click', () => showHomeView().catch(error => toast(error.message, true)));
document.querySelector('#returnFromTimeline').addEventListener('click', () => showHomeView().catch(error => toast(error.message, true)));
document.querySelector('#newMemory').addEventListener('click', () => {
  showMemoryView();
  fillEditor();
});
document.querySelector('#treasuryNav').addEventListener('click', () => {
  showTreasuryView().catch(error => toast(error.message, true));
});
document.querySelector('#mailboxNav').addEventListener('click', () => {
  showMailboxView().catch(error => toast(error.message, true));
});
document.querySelectorAll('#taskNav, #taskFilterNav, #taskQuickNav').forEach(button => {
  button.addEventListener('click', () => showTaskView().catch(error => toast(error.message, true)));
});
document.querySelector('#hormoneNav').addEventListener('click', () => {
  showHormoneView().catch(error => toast(error.message, true));
});
document.querySelector('#mindNav')?.addEventListener('click', () => {
  showMindView().catch(error => toast(error.message, true));
});
document.querySelector('#calendarNav')?.addEventListener('click', () => {
  showCalendarView().catch(error => toast(error.message, true));
});
document.querySelector('#resonanceNav').addEventListener('click', () => {
  showResonanceView().catch(error => toast(error.message, true));
});
document.querySelector('#toolboxNav').addEventListener('click', () => {
  showToolboxView().catch(error => toast(error.message, true));
});
document.querySelector('#darkflowNav').addEventListener('click', () => {
  showDarkflowView().catch(error => toast(error.message, true));
});
document.querySelector('#behaviorNav').addEventListener('click', () => {
  showBehaviorView().catch(error => toast(error.message, true));
});
document.querySelector('#topicNav').addEventListener('click', () => {
  showTopicView().catch(error => toast(error.message, true));
});
document.querySelector('#returnToMemories').addEventListener('click', () => showHomeView().catch(error => toast(error.message, true)));
document.querySelectorAll('[data-house-route]').forEach(button => {
  button.addEventListener('click', () => {
    const routes = {
      topics: showTopicView,
      mailbox: showMailboxView,
      thoughts: showMindView,
      resonance: showResonanceView,
      calendar: showCalendarView,
      tasks: showTaskView,
    };
    const route = routes[button.dataset.houseRoute];
    if (route) Promise.resolve(route()).catch(error => toast(error.message, true));
  });
});
document.querySelector('#houseEmotionTicket').addEventListener('click', () => {
  showHormoneView().catch(error => toast(error.message, true));
});
document.querySelector('#houseDarkflowLink').addEventListener('click', () => {
  showDarkflowView().catch(error => toast(error.message, true));
});
document.querySelector('#acknowledgePush').addEventListener('click', acknowledgePush);
document.querySelector('#topicBackToMain').addEventListener('click', returnToTopicMain);
document.querySelector('#topicBackToSubs').addEventListener('click', returnToTopicSubs);
document.querySelector('#refreshTopics').addEventListener('click', () => {
  loadTopics(false).catch(error => toast(error.message, true));
});
document.querySelector('#autoOrganizeTopics').addEventListener('click', () => {
  openTopicPreview().catch(error => toast(error.message, true));
});
document.querySelector('#confirmTopicBulk').addEventListener('click', () => {
  applyTopicPreview().catch(error => toast(error.message, true));
});
document.querySelector('#undoTopicBulk').addEventListener('click', () => {
  undoTopicBulk().catch(error => toast(error.message, true));
});
document.querySelector('#topicMainSelect').addEventListener('change', () => fillTopicSubSelect());
document.querySelector('#saveTopicAssignment').addEventListener('click', async () => {
  const bucketId = document.querySelector('#topicBucketId').value;
  try {
    await api(`/api/topics/buckets/${encodeURIComponent(bucketId)}`, {
      method: 'PUT',
      body: JSON.stringify({
        main_topic: document.querySelector('#topicMainSelect').value,
        subtopic: document.querySelector('#topicSubSelect').value,
      }),
    });
    document.querySelector('#topicDialog').close();
    await loadTopics(false);
    toast('主题位置已更新，记忆正文没有修改。');
  } catch (error) {
    toast(error.message, true);
  }
});
document.querySelector('#removeTopicAssignment').addEventListener('click', async () => {
  const bucketId = document.querySelector('#topicBucketId').value;
  try {
    await api(`/api/topics/buckets/${encodeURIComponent(bucketId)}`, {method: 'DELETE'});
    document.querySelector('#topicDialog').close();
    await loadTopics(false);
    toast('已经移到待分类，记忆正文没有修改。');
  } catch (error) {
    toast(error.message, true);
  }
});
document.querySelector('#returnFromMailbox').addEventListener('click', () => showHomeView().catch(error => toast(error.message, true)));
document.querySelector('#returnFromTasks').addEventListener('click', () => showHomeView().catch(error => toast(error.message, true)));
document.querySelector('#refreshTasks').addEventListener('click', () => loadTasks().catch(error => toast(error.message, true)));
document.querySelector('#taskSearchForm').addEventListener('submit', event => {
  event.preventDefault();
  loadTasks().catch(error => toast(error.message, true));
});
document.querySelector('#clearTaskSearch').addEventListener('click', () => {
  document.querySelector('#taskSearchInput').value = '';
  loadTasks().catch(error => toast(error.message, true));
});
document.querySelectorAll('[data-task-status]').forEach(button => button.addEventListener('click', () => {
  state.taskStatus = button.dataset.taskStatus;
  document.querySelectorAll('[data-task-status]').forEach(item => item.classList.toggle('is-active', item === button));
  loadTasks().catch(error => toast(error.message, true));
}));
document.querySelector('#mailboxSearchForm').addEventListener('submit', event => {
  event.preventDefault();
  loadMailbox(true).catch(error => toast(error.message, true));
});
document.querySelector('#clearMailboxSearch').addEventListener('click', () => {
  document.querySelector('#mailboxSearchInput').value = '';
  loadMailbox(true).catch(error => toast(error.message, true));
});
document.querySelector('#returnFromHormone').addEventListener('click', () => showHomeView().catch(error => toast(error.message, true)));
document.querySelector('#returnFromDarkflow').addEventListener('click', () => showHomeView().catch(error => toast(error.message, true)));
document.querySelector('#returnFromBehavior').addEventListener('click', () => showHomeView().catch(error => toast(error.message, true)));
document.querySelector('#refreshBehavior').addEventListener('click', () => {
  loadBehavior().catch(error => toast(error.message, true));
});
document.querySelector('#refreshHormone').addEventListener('click', () => {
  Promise.all([loadHormone(), loadJudge()]).catch(error => toast(error.message, true));
});
document.querySelector('#refreshDarkflow').addEventListener('click', () => {
  loadDarkflow().catch(error => toast(error.message, true));
});
document.querySelector('#refreshThoughts').addEventListener('click', () => {
  loadThoughts().catch(error => toast(error.message, true));
});
document.querySelector('#refreshResonance').addEventListener('click', () => {
  loadResonance().catch(error => toast(error.message, true));
});
document.querySelector('#calendarDate').addEventListener('change', () => {
  loadCalendar().catch(error => toast(error.message, true));
});
document.querySelectorAll('[data-thought-status]').forEach(button => {
  button.addEventListener('click', () => {
    state.thoughtStatus = button.dataset.thoughtStatus;
    document.querySelectorAll('[data-thought-status]').forEach(item => item.classList.toggle('is-active', item === button));
    loadThoughts().catch(error => toast(error.message, true));
  });
});
document.querySelector('#addJudgeRelation').addEventListener('click', () => {
  const relations = collectJudgeRelations();
  relations.push({name: '', aliases: [], role: '', safety: '', note: '', trigger: {}});
  document.querySelector('#judgeRelations').innerHTML = relations.map(judgeRelationCard).join('');
  document.querySelector('#judgeRelationCount').textContent = `${relations.length} 个人物`;
  icons();
});
document.querySelector('#judgeRelations').addEventListener('click', event => {
  const button = event.target.closest('[data-remove-judge]');
  if (!button) return;
  button.closest('.judge-relation').remove();
  const cards = Array.from(document.querySelectorAll('.judge-relation'));
  cards.forEach((card, index) => {
    card.dataset.judgeIndex = index;
    card.querySelector('.judge-relation-title strong').textContent = `人物 ${index + 1}`;
  });
  document.querySelector('#judgeRelationCount').textContent = `${cards.length} 个人物`;
  if (!cards.length) {
    document.querySelector('#judgeRelations').innerHTML = '<div class="manager-empty judge-empty"><i data-lucide="users"></i><strong>还没有人物关系</strong><span>可以先留空，DeepSeek 仍会按正文判断。</span></div>';
    icons();
  }
});
document.querySelector('#saveJudge').addEventListener('click', saveJudge);
document.querySelector('#mailboxMore').addEventListener('click', () => {
  loadMailbox(false).catch(error => toast(error.message, true));
});
document.querySelector('#editAction').addEventListener('click', () => fillEditor(state.detail));
document.querySelector('#saveEdit').addEventListener('click', saveEditor);
document.querySelector('#editMode').addEventListener('change', event => {
  const editor = document.querySelector('#editContent');
  if (event.target.value === 'append') {
    if (editor.value === state.editorOriginalContent) editor.value = '';
    editor.placeholder = '这里只写要新增的一包，保存时会自动加时间分隔线';
  } else {
    if (!editor.value.trim()) editor.value = state.editorOriginalContent;
    editor.placeholder = '';
  }
});
document.querySelector('#pinAction').addEventListener('click', () => {
  const next = state.detail.pin_level ? '' : 'important';
  quickUpdate({pin_level: next}, next ? '已设为重要钉选。' : '已取消钉选。');
});
document.querySelector('#archiveAction').addEventListener('click', () => {
  const next = !(state.detail.sealed || state.detail.archived);
  quickUpdate({sealed: next}, next ? '已封存，默认检索将不再显示。' : '已解除封存。');
});

const deleteDialog = document.querySelector('#deleteDialog');
document.querySelector('#deleteAction').addEventListener('click', () => {
  document.querySelector('#deleteBucketId').textContent = state.selectedId;
  document.querySelector('#deleteConfirmInput').value = '';
  deleteDialog.showModal();
});
document.querySelector('#confirmDelete').addEventListener('click', async () => {
  const confirmBucketId = document.querySelector('#deleteConfirmInput').value.trim();
  try {
    await api(`/api/buckets/${encodeURIComponent(state.selectedId)}`, {
      method: 'DELETE', body: JSON.stringify({confirm_bucket_id: confirmBucketId}),
    });
    deleteDialog.close();
    state.selectedId = '';
    state.detail = null;
    toast('删除前快照已保存，记忆已删除。');
    await Promise.all([loadStats(), loadList(false)]);
    await loadTasks();
  } catch (error) {
    toast(error.message, true);
  }
});

const permanentDeleteDialog = document.querySelector('#permanentDeleteDialog');
document.querySelector('#openPermanentDelete').addEventListener('click', async () => {
  deleteDialog.close();
  const bucketId = state.selectedId;
  document.querySelector('#permanentDeleteBucketId').textContent = bucketId;
  document.querySelector('#permanentDeleteConfirmInput').value = '';
  document.querySelector('#permanentDeleteAcknowledged').checked = false;
  document.querySelector('#confirmPermanentDelete').disabled = true;
  document.querySelector('#permanentDeleteCounts').textContent = '正在核对关联副本...';
  permanentDeleteDialog.showModal();
  try {
    const result = await api(`/api/buckets/${encodeURIComponent(bucketId)}/permanent-preview`);
    const copyCount = Math.max(0, Number(result.online_copy_count || 1) - 1);
    document.querySelector('#permanentDeleteCounts').textContent =
      `在线正文 1 份，另有 ${copyCount} 条快照、索引或关联记录会一并清理。`;
  } catch (error) {
    document.querySelector('#permanentDeleteCounts').textContent = error.message;
  }
});

function updatePermanentDeleteButton() {
  const matches = document.querySelector('#permanentDeleteConfirmInput').value.trim() === state.selectedId;
  const acknowledged = document.querySelector('#permanentDeleteAcknowledged').checked;
  document.querySelector('#confirmPermanentDelete').disabled = !(matches && acknowledged);
}

document.querySelector('#permanentDeleteConfirmInput').addEventListener('input', updatePermanentDeleteButton);
document.querySelector('#permanentDeleteAcknowledged').addEventListener('change', updatePermanentDeleteButton);
document.querySelector('#confirmPermanentDelete').addEventListener('click', async () => {
  const bucketId = state.selectedId;
  const confirmBucketId = document.querySelector('#permanentDeleteConfirmInput').value.trim();
  try {
    await api(`/api/buckets/${encodeURIComponent(bucketId)}/permanent`, {
      method: 'DELETE',
      body: JSON.stringify({confirm_bucket_id: confirmBucketId, confirm_permanent: true}),
    });
    permanentDeleteDialog.close();
    state.selectedId = '';
    state.detail = null;
    toast('在线正文及其快照、索引和关联记录已永久删除。');
    await Promise.all([loadStats(), loadList(false)]);
  } catch (error) {
    toast(error.message, true);
  }
});

document.querySelectorAll('input[name="treasuryType"]').forEach(input => {
  input.addEventListener('change', () => {
    document.querySelectorAll('.treasury-kind label').forEach(label => {
      label.classList.toggle('is-selected', label.querySelector('input').checked);
    });
  });
});

document.querySelector('#treasuryForm').addEventListener('submit', async event => {
  event.preventDefault();
  const amount = document.querySelector('#treasuryAmount').value.trim();
  const reason = document.querySelector('#treasuryReason').value.trim();
  if (!amount || !reason) {
    toast('金额和原因都要填写。', true);
    return;
  }
  const button = document.querySelector('.treasury-submit');
  button.disabled = true;
  try {
    await api('/api/treasury/entries', {
      method: 'POST',
      body: JSON.stringify({
        entry_type: document.querySelector('input[name="treasuryType"]:checked').value,
        amount,
        reason,
        occurred_at: document.querySelector('#treasuryOccurredAt').value,
      }),
    });
    document.querySelector('#treasuryAmount').value = '';
    document.querySelector('#treasuryReason').value = '';
    document.querySelector('#treasuryOccurredAt').value = datetimeLocalValue();
    toast('这笔账已经记入AI小金库。');
    await loadTreasury();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelectorAll('[data-treasury-filter]').forEach(button => {
  button.addEventListener('click', async () => {
    state.treasuryFilter = button.dataset.treasuryFilter;
    document.querySelectorAll('[data-treasury-filter]').forEach(item => item.classList.toggle('is-active', item === button));
    try {
      await loadTreasury();
    } catch (error) {
      toast(error.message, true);
    }
  });
});

document.querySelector('#taskForm').addEventListener('submit', async event => {
  event.preventDefault();
  const title = document.querySelector('#taskTitle').value.trim();
  if (!title) {
    toast('先写清楚要完成什么。', true);
    return;
  }
  const button = event.submitter;
  button.disabled = true;
  try {
    await api('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({
        title,
        details: document.querySelector('#taskDetails').value.trim(),
        importance: Number(document.querySelector('#taskImportance').value),
      }),
    });
    document.querySelector('#taskTitle').value = '';
    document.querySelector('#taskDetails').value = '';
    document.querySelector('#taskImportance').value = '3';
    state.taskStatus = 'open';
    document.querySelectorAll('[data-task-status]').forEach(item => item.classList.toggle('is-active', item.dataset.taskStatus === 'open'));
    toast('这件事已经记下来了。');
    await loadTasks();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#saveTaskEdit').addEventListener('click', async () => {
  const taskId = state.taskEditingId;
  const title = document.querySelector('#taskEditTitle').value.trim();
  if (!taskId || !title) {
    toast('事项标题不能为空。', true);
    return;
  }
  const button = document.querySelector('#saveTaskEdit');
  button.disabled = true;
  try {
    await api(`/api/tasks/${taskId}`, {
      method: 'PUT',
      body: JSON.stringify({
        title,
        details: document.querySelector('#taskEditDetails').value.trim(),
        importance: Number(document.querySelector('#taskEditImportance').value),
        status: document.querySelector('#taskEditStatus').value,
      }),
    });
    document.querySelector('#taskEditDialog').close();
    toast('未竟事项已经更新。');
    await loadTasks();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#confirmTaskDelete').addEventListener('click', async () => {
  const taskId = state.taskEditingId;
  const confirmed = Number(document.querySelector('#taskDeleteConfirm').value.trim());
  if (!taskId || confirmed !== taskId) {
    toast('事项编号不一致，没有删除。', true);
    return;
  }
  const button = document.querySelector('#confirmTaskDelete');
  button.disabled = true;
  try {
    await api(`/api/tasks/${taskId}`, {
      method: 'DELETE',
      body: JSON.stringify({confirm_task_id: confirmed}),
    });
    document.querySelector('#taskDeleteDialog').close();
    toast('这条错误事项已经删除。');
    await loadTasks();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#saveTreasuryEdit').addEventListener('click', async () => {
  const entryId = state.treasuryEditingId;
  const amount = document.querySelector('#treasuryEditAmount').value.trim();
  const reason = document.querySelector('#treasuryEditReason').value.trim();
  if (!entryId || !amount || !reason) {
    toast('金额和原因都要填写。', true);
    return;
  }
  const button = document.querySelector('#saveTreasuryEdit');
  button.disabled = true;
  try {
    await api(`/api/treasury/entries/${entryId}`, {
      method: 'PUT',
      body: JSON.stringify({
        entry_type: document.querySelector('#treasuryEditType').value,
        amount,
        reason,
        occurred_at: document.querySelector('#treasuryEditTime').value,
      }),
    });
    document.querySelector('#treasuryEditDialog').close();
    toast('修改前记录已保存，账目已更新。');
    await loadTreasury();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#confirmTreasuryDelete').addEventListener('click', async () => {
  const entryId = state.treasuryEditingId;
  const confirmed = Number(document.querySelector('#treasuryDeleteConfirm').value.trim());
  if (!entryId || confirmed !== entryId) {
    toast('账目编号不一致，没有删除。', true);
    return;
  }
  const button = document.querySelector('#confirmTreasuryDelete');
  button.disabled = true;
  try {
    await api(`/api/treasury/entries/${entryId}`, {
      method: 'DELETE',
      body: JSON.stringify({confirm_entry_id: confirmed}),
    });
    document.querySelector('#treasuryDeleteDialog').close();
    toast('删除前记录已保存，账目已删除。');
    await loadTreasury();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#saveMailboxEdit').addEventListener('click', async () => {
  const messageId = state.mailboxEditingId;
  const message = document.querySelector('#mailboxEditMessage').value.trim();
  if (!messageId || !message) {
    toast('留言内容不能为空。', true);
    return;
  }
  const button = document.querySelector('#saveMailboxEdit');
  button.disabled = true;
  try {
    await api(`/api/mailbox/messages/${messageId}`, {
      method: 'PUT',
      body: JSON.stringify({message}),
    });
    document.querySelector('#mailboxEditDialog').close();
    toast('修改前版本已保存，留言已更新。');
    await loadMailbox(true);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#confirmMailboxDelete').addEventListener('click', async () => {
  const messageId = state.mailboxEditingId;
  const confirmed = Number(document.querySelector('#mailboxDeleteConfirm').value.trim());
  if (!messageId || confirmed !== messageId) {
    toast('留言编号不一致，没有删除。', true);
    return;
  }
  const button = document.querySelector('#confirmMailboxDelete');
  button.disabled = true;
  try {
    await api(`/api/mailbox/messages/${messageId}`, {
      method: 'DELETE',
      body: JSON.stringify({confirm_message_id: confirmed}),
    });
    document.querySelector('#mailboxDeleteDialog').close();
    toast('删除前版本已保存，留言已删除。');
    await loadMailbox(true);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

const exportDialog = document.querySelector('#exportDialog');
document.querySelectorAll('#exportMemory, #exportMemoryTop').forEach(button => {
  button.addEventListener('click', () => exportDialog.showModal());
});

const formatHints = {
  migration: '适合迁移到另一台 Clio，保留正文、元数据、向量和所选数据库，并附 SHA-256 校验清单。',
  markdown: '适合自己阅读或打印，把记忆整理成一个 Markdown 文档。',
  json: '适合程序处理或导入其他支持结构化数据的系统。',
};
document.querySelector('#exportFormat').addEventListener('change', event => {
  document.querySelector('#formatHint').textContent = formatHints[event.target.value];
  const canEncrypt = event.target.value === 'migration';
  document.querySelector('#encryptExport').disabled = !canEncrypt;
  if (!canEncrypt) document.querySelector('#encryptExport').checked = false;
  document.querySelector('#exportPasswordField').hidden = !document.querySelector('#encryptExport').checked;
});

const vaultHealthDialog = document.querySelector('#vaultHealthDialog');
document.querySelector('#vaultHealth').addEventListener('click', () => {
  document.querySelector('#advancedMenu').removeAttribute('open');
  vaultHealthDialog.showModal();
});

document.querySelector('#runVaultHealth').addEventListener('click', async () => {
  const button = document.querySelector('#runVaultHealth');
  const summary = document.querySelector('#vaultHealthSummary');
  const issues = document.querySelector('#vaultHealthIssues');
  button.disabled = true;
  summary.innerHTML = '<p>正在逐条检查，不会改动任何内容……</p>';
  issues.innerHTML = '';
  try {
    const result = await api('/api/vault-health');
    const label = result.status === 'ok' ? '全部正常' : result.status === 'warning' ? '基本正常，有待处理项' : '发现需要处理的问题';
    summary.innerHTML = `
      <div class="vault-health-state is-${result.status}"><i data-lucide="${result.status === 'ok' ? 'badge-check' : 'triangle-alert'}"></i><strong>${label}</strong></div>
      <div class="vault-health-grid">
        <div><span>记忆文件</span><b>${result.memory.files}</b></div>
        <div><span>SQLite 数据库</span><b>${result.database_count}</b></div>
        <div><span>已生成向量</span><b>${result.embedding_count}</b></div>
        <div><span>等待生成</span><b>${result.embedding_queue}</b></div>
      </div>
      <p>全库指纹：<code>${escapeHtml(result.memory.fingerprint_sha256)}</code></p>`;
    issues.innerHTML = result.issues.length
      ? result.issues.map(item => `<p class="vault-health-issue is-${item.level === 'error' ? 'error' : 'warning'}"><strong>${escapeHtml(item.item)}</strong><span>${escapeHtml(item.detail || item.kind)}</span></p>`).join('')
      : '<p class="vault-health-empty">没有发现损坏、重复编号或数据库异常。</p>';
    icons();
  } catch (error) {
    summary.innerHTML = `<p>检查没有完成：${escapeHtml(error.message)}</p>`;
  } finally {
    button.disabled = false;
  }
});
document.querySelector('#encryptExport').addEventListener('change', event => {
  document.querySelector('#exportPasswordField').hidden = !event.target.checked;
});

document.querySelector('#createExport').addEventListener('click', async () => {
  const password = document.querySelector('#encryptExport').checked
    ? document.querySelector('#exportPassword').value
    : '';
  const payload = {
    scope: document.querySelector('input[name="exportScope"]:checked').value,
    bucket_id: state.selectedId,
    format: document.querySelector('#exportFormat').value,
    include_history: document.querySelector('#includeHistory').checked,
    include_mailbox: document.querySelector('#includeMailbox').checked,
    include_timeline: document.querySelector('#includeTimeline').checked,
    include_feedback: document.querySelector('#includeFeedback').checked,
    include_treasury: document.querySelector('#includeTreasury').checked,
    password,
  };
  const button = document.querySelector('#createExport');
  button.disabled = true;
  try {
    const response = await fetch('/api/export', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(body.detail || '导出失败。');
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = match?.[1] || 'Clio-export';
    link.click();
    URL.revokeObjectURL(link.href);
    exportDialog.close();
    toast('导出文件已经生成。');
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

async function start() {
  const opening = document.querySelector('#managerOpening');
  const openingMessage = document.querySelector('#openingMessage');
  const startedAt = performance.now();
  let connected = false;
  document.querySelector('#treasuryOccurredAt').value = datetimeLocalValue();
  try {
    if (!await checkManagerAuthentication()) return;
    await api('/api/health');
    setServiceStatus(true);
    await loadList(false);
    await showHomeView();
    connected = true;
  } catch (error) {
    setServiceStatus(false);
    toast(error.message, true);
  }
  icons();
  const minimumOpeningTime = Math.max(0, 900 - (performance.now() - startedAt));
  setTimeout(() => {
    openingMessage.textContent = connected ? '欢迎回来' : '管理页已打开，服务仍在连接';
    opening.classList.add(connected ? 'is-ready' : 'is-warning');
    setTimeout(() => {
      opening.classList.add('is-hidden');
      document.body.classList.remove('is-opening');
    }, connected ? 520 : 1100);
  }, minimumOpeningTime);
}

document.querySelector('#managerLoginForm').addEventListener('submit', submitManagerLogin);
document.querySelector('#managerLogout').addEventListener('click', logoutManager);
start();
