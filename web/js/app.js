/* ==========================================================================
   app.js — 前端交互逻辑（发票合并样板 · v2 左侧导航版）
   约定：本文件只负责「界面状态 + 调后端接口 + 渲染」，
   所有解析 / 合并算法都在 Python 后端（server/core/）。
   ========================================================================== */

const API = {
  health:     '/api/health',
  scan:       '/api/merge/scan',
  poolScan:   '/api/pool/scan',      // v0.4 统一解析入口：一次抽全字段（R2）
  poolRun:    '/api/pool/run',       // v0.4 批次 2 流水线：查重→核对→重命名→合并（一次跑完）
  /* v0.7（用户第 ⑥ / ⑦ 条）新增的两个接口键。两者都可能**还没上线**：
     · poolThumb   渲染票面 PNG（成功 200 image/png；失败 404 + JSON）→ <img src>，非 200 自动降级 iframe
     · poolScanStatus 用请求里埋的 job_id 查解析进度（404 / found:false → 退回转圈遮罩）
     契约字段名见任务书，一个字都不能差。 */
  poolThumb:  '/api/pool/thumb',
  poolScanStatus: '/api/pool/scan/status',
  /* 第 ⑩ 条（用户第 ⑦ 条之二）新增：点「开始」后的全屏进度轮询。
     与 poolScanStatus 同一个协议（`?job_id=` → {ok,index,total,current,...}），
     同样可能**还没上线** —— 后端不认就静默退回「遮罩保持到 POST 返回」。 */
  poolRunStatus: '/api/pool/run/status',
  tripRun:    '/api/trip/run',       // v0.5 行程单合并接口：交通费 + 住宿票一次交出去，出一张合一行程单
  upload:     '/api/merge/upload',
  pick:       '/api/merge/pick',
  pickdir:    '/api/merge/pickdir',
  run:        '/api/merge/run',
  defaultOut: '/api/merge/default_out',
  openFolder: '/api/merge/open_folder',
};

/* 模块元数据。工作台是 v0.4 的默认主页；其余五个是 v0.3 旧模块，
   过渡期全部保留入口且功能照旧（批次 2 才会把流水线搬进工作台）。*/
const MODULES = {
  workbench: { title: '发票工作台', sub: '投一次，在同一张列表上做完核对 / 重命名 / 合并', enabled: true },
  merge:  { title: '发票合并',   sub: '把多张发票合并为一个 PDF',      enabled: true },
  train:  { title: '火车票',     sub: '解析车票并生成行程单',          enabled: true },
  hotel:  { title: '住宿票',     sub: '解析住宿发票并生成行程单',      enabled: true },
  check:  { title: '发票核对',   sub: '比对预设抬头与税号',            enabled: true },
  rename: { title: '智能重命名', sub: '按票面内容批量重命名文件',      enabled: true },
  settings: { title: '设置', sub: '抬头 / 输出目录 / 执行偏好 / 外观', enabled: true },
  /* v0.6：发票库。副标题照页头那句写 —— 「库是软件自己留的副本，磁盘原件不动」
     是这一整个视图的语义底线，顶栏切过去时也要能看到同一句。 */
  vault:  { title: '发票库', sub: '库内副本 · 磁盘原件不动',        enabled: true },
};

/* ── 应用状态 ─────────────────────────────────────────── */
const state = {
  mod: 'workbench',    // 当前模块（v0.4 默认进工作台）
  rows: [],            // 全量行数据（发票合并模块）
  filter: '',          // 搜索关键字
  sortKey: 'filename',
  sortDir: 1,          // 1 升序 / -1 降序
  outDir: '',
};

/* ── DOM 引用 ─────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const el = {
  dropzone: $('dropzone'), fileInput: $('fileInput'),
  pickFiles: $('pickFiles'), pickFolder: $('pickFolder'),
  stats: $('stats'), statCount: $('statCount'), statAmount: $('statAmount'),
  statPages: $('statPages'), statTypes: $('statTypes'),
  tableCard: $('tableCard'), grid: $('grid'), gridBody: $('gridBody'),
  searchInput: $('searchInput'), clearBtn: $('clearBtn'),
  outCard: $('outCard'), outDir: $('outDir'), pickOutDir: $('pickOutDir'),
  actionbar: $('actionbar'), actionInfo: $('actionInfo'), runBtn: $('runBtn'),
  overlay: $('overlay'), overlayText: $('overlayText'),
  resultModal: $('resultModal'), resultIcon: $('resultIcon'),
  resultTitle: $('resultTitle'), resultBody: $('resultBody'),
  openFolderBtn: $('openFolderBtn'), closeModalBtn: $('closeModalBtn'),
  toastWrap: $('toastWrap'),
  /* 顶栏右侧：主题切换 + 设置齿轮（v0.5：左侧栏整块下线后，设置入口搬到这里）
     + 发票库图标与「库占用」（v0.6）*/
  themeBtn: $('themeBtn'), settingsBtn: $('btnSettings'),
  vaultStat: $('vaultStat'),
  mainArea: $('mainArea'), pageTitle: $('pageTitle'), pageSub: $('pageSub'),
  content: $('content'),
  badgeMerge: $('badgeMerge'), badgeTrain: $('badgeTrain'), badgeHotel: $('badgeHotel'),
  badgeCheck: $('badgeCheck'), badgeRename: $('badgeRename'),

  // ── 工作台（v0.4 · R1 统一发票列表）──
  viewWorkbench: $('view-workbench'),
  wbEmpty: $('wbEmpty'), wbHero: $('weHero'), wbBar: $('wbBar'), wbTip: $('wbTip'),
  wbFileInput: $('wbFileInput'), wbPickFiles: $('wbPickFiles'),
  wbPickFolder: $('wbPickFolder'), wbClear: $('wbClear'),
  wbVaultImport: $('wbVaultImport'), weVaultCard: $('weVaultCard'),
  wePickFiles: $('wePickFiles'), wePickFolder: $('wePickFolder'),
  wbKpis: $('wbKpis'), wbFilters: $('wbFilters'), wbChips: $('wbChips'),
  wbDateFrom: $('wbDateFrom'), wbDateTo: $('wbDateTo'),
  wbStatus: $('wbStatus'), wbSearch: $('wbSearch'), wbReset: $('wbReset'),
  wbGridWrap: $('wbGridWrap'),
  wbGrid: $('wbGrid'), wbThead: $('wbThead'), wbBody: $('wbBody'), wbPickAll: $('wbPickAll'),
  wbDetailToggle: $('wbDetailToggle'),
  wbActionbar: $('wbActionbar'), wbActionInfo: $('wbActionInfo'),
  // v0.7（用户第 ⑦ 条）解析进度条：默认 hidden，解析时显示 index/total/current
  // 第 ⑩ 条：同一个节点加 `.fs` 类即变成**全屏**遮罩（见 app.css `#wbProg.fs`），
  //   wbProgFile = 当前文件名 / 当前阶段，wbProgPct = 大号百分比。
  wbProg: $('wbProg'), wbProgBar: $('wbProgBar'),
  wbProgText: $('wbProgText'), wbProgPct: $('wbProgPct'), wbProgFile: $('wbProgFile'),
  // ── 工作台底栏流水线（v0.4 批次 2）──
  wbRunBtn: $('wbRunBtn'), wbRunLabel: $('wbRunLabel'), wbRunNote: $('wbRunNote'),
  wbTripBtn: $('wbTripBtn'), wbTripMenu: $('wbTripMenu'),
  wbTglCheck: $('wbTglCheck'), wbTglRename: $('wbTglRename'),
  wbTglDedup: $('wbTglDedup'), wbTglMerge: $('wbTglMerge'),
  detailPanel: $('detailPanel'), detailTitle: $('detailTitle'),
  detailBody: $('detailBody'), detailToggle: $('detailToggle'),

  // 视图容器（工作台 / 合并模块专有 / 其余四模块共用）
  viewMerge: $('view-merge'), viewGeneric: $('view-generic'),
  // 通用模块（火车票 / 住宿票 / 核对 / 重命名）
  gDropzone: $('gDropzone'), gFileInput: $('gFileInput'),
  gPickFiles: $('gPickFiles'), gPickFolder: $('gPickFolder'),
  gDzTitle: $('gDzTitle'), gDzHint: $('gDzHint'),
  gStats: $('gStats'), gTableCard: $('gTableCard'), gTableTitle: $('gTableTitle'),
  gGrid: $('gGrid'), gThead: $('gThead'), gBody: $('gBody'),
  gSearchInput: $('gSearchInput'), gClearBtn: $('gClearBtn'),
  gOutCard: $('gOutCard'), gOutDir: $('gOutDir'), gPickOutDir: $('gPickOutDir'),
  gActionbar: $('gActionbar'), gActionInfo: $('gActionInfo'),
  gRunBtn: $('gRunBtn'), gRunLabel: $('gRunLabel'),
  gCheckPanel: $('gCheckPanel'), gExpName: $('gExpName'), gExpTax: $('gExpTax'),
  gRenamePanel: $('gRenamePanel'),

  // ── 设置视图 ──
  viewSettings: $('view-settings'),
  seName: $('seName'), seTax: $('seTax'), seHintHeader: $('seHintHeader'),
  seOutDir: $('seOutDir'), sePickDir: $('sePickDir'), seOpenDir: $('seOpenDir'),
  seRenameMode: $('seRenameMode'), seRenameCopy: $('seRenameCopy'), seRenameMove: $('seRenameMove'),
  sePolicy: $('sePolicy'), sePolicySkip: $('sePolicySkip'), sePolicyStop: $('sePolicyStop'),
  seTheme: $('seTheme'), seThemeDark: $('seThemeDark'), seThemeLight: $('seThemeLight'),
  seVer: $('seVer'), sePath: $('sePath'), seSaved: $('seSaved'),
  // 首次运行引导（在工作台空状态里）
  seBanner: $('seBanner'), seBannerClose: $('seBannerClose'), seBannerGo: $('seBannerGo'),

  // ── 发票库视图（v0.6）──
  viewVault: $('view-vault'),
  vaultBack: $('vaultBack'), vaultKpis: $('vaultKpis'), vaultBar: $('vaultBar'),
  vaultSearch: $('vaultSearch'), vaultChips: $('vaultChips'),
  vaultFrom: $('vaultFrom'), vaultTo: $('vaultTo'),
  vaultDelSel: $('vaultDelSel'), vaultClearAll: $('vaultClearAll'), vaultImport: $('vaultImport'),
  vaultGridWrap: $('vaultGridWrap'), vaultGrid: $('vaultGrid'),
  vaultThead: $('vaultThead'), vaultBody: $('vaultBody'), vaultPickAll: $('vaultPickAll'),
  vaultFoot: $('vaultFoot'), vaultPageInfo: $('vaultPageInfo'),
  vaultPrev: $('vaultPrev'), vaultNext: $('vaultNext'),
  vaultEmpty: $('vaultEmpty'), vaultUnavailable: $('vaultUnavailable'),
  vaultGoWork: $('vaultGoWork'), vaultRetry: $('vaultRetry'),
};

/* ── 工具函数 ─────────────────────────────────────────── */
const fmtMoney = (n) => '￥' + (Number(n) || 0).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  let data;
  try { data = await res.json(); } catch { data = { ok: false, error: '响应解析失败' }; }
  return { status: res.status, data };
}

function toast(msg, kind = 'info', ms = 2600) {
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  t.innerHTML = `<span class="dot"></span><span>${esc(msg)}</span>`;
  el.toastWrap.appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 260); }, ms);
}

function showOverlay(text) { el.overlayText.textContent = text; el.overlay.hidden = false; }
function hideOverlay() { el.overlay.hidden = true; }

/* ── 主题切换 ─────────────────────────────────────────── */
/** 落一次主题：DOM 标记 + 本地记忆 + 设置页按钮选中态。唯一写主题的地方。 */
function applyTheme(next) {
  document.documentElement.dataset.theme = next;
  localStorage.setItem('fapiao-theme', next);
  seSyncTheme();
}

function initTheme() {
  const saved = localStorage.getItem('fapiao-theme') || 'dark';
  document.documentElement.dataset.theme = saved;
  el.themeBtn.addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
}

/* ── 模块切换 ─────────────────────────────────────────── */
function switchModule(key, { importVaultSelection = true } = {}) {
  const cfg = MODULES[key];
  if (!cfg || !cfg.enabled) {
    toast(`${cfg ? cfg.title : key} 暂未实现`, 'info');
    return;
  }
  if (key === 'workbench' && state.mod === 'vault' && importVaultSelection
      && typeof vault !== 'undefined' && vault.picked.size) {
    return vaultImportToWorkbench();
  }
  state.mod = key;
  document.documentElement.dataset.mod = key;
  el.mainArea.dataset.mod = key;
  el.pageTitle.textContent = cfg.title;
  el.pageSub.textContent = cfg.sub;
  /* 设置入口随当前页面高亮；发票库由顶栏的库占用按钮进入。 */
  if (el.settingsBtn) {
    const on = key === 'settings';
    el.settingsBtn.classList.toggle('active', on);
    el.settingsBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  if (key === 'workbench') {
    showWorkbench();
    if (importVaultSelection && typeof vault !== 'undefined' && vault.workbenchDirty && wb.sourcePaths.size) {
      vault.workbenchDirty = false;
      wbLoad([...wb.sourcePaths]);
    }
    return;
  }

  // 发票库视图（v0.6）：与设置页同族（都不是工作台布局），但表格要占满剩余高度，
  // 所以内容区换成 is-vault（内容区自己不滚，由 .vault-grid-wrap 滚）。
  if (key === 'vault') {
    if (!el.viewVault) return;         // 视图容器还没落地时不动任何东西
    el.content.classList.remove('is-wb');
    el.content.classList.add('is-vault');
    el.detailPanel.hidden = true;
    el.wbActionbar.hidden = true;
    el.viewWorkbench.hidden = true;
    el.viewMerge.hidden = true;
    el.viewGeneric.hidden = true;
    if (el.viewSettings) el.viewSettings.hidden = true;
    el.actionbar.hidden = true;
    el.gActionbar.hidden = true;
    el.viewVault.hidden = false;
    vaultOnEnter();                    // 每次进入都重新拉一遍列表
    return;
  }

  // 设置视图：既不是工作台布局，也不和四个旧模块共用通用布局
  if (key === 'settings') {
    if (!el.viewSettings) return;      // 视图容器还没落地时不动任何东西
    el.content.classList.remove('is-wb');
    el.content.classList.remove('is-vault');
    el.detailPanel.hidden = true;
    el.wbActionbar.hidden = true;
    el.viewWorkbench.hidden = true;
    el.viewMerge.hidden = true;
    el.viewGeneric.hidden = true;
    if (el.viewVault) el.viewVault.hidden = true;
    el.actionbar.hidden = true;
    el.gActionbar.hidden = true;
    el.viewSettings.hidden = false;
    seOnEnter();                       // 把当前真值刷进表单
    return;
  }

  // 旧模块：内容区恢复滚动，收起工作台专属面板
  el.content.classList.remove('is-wb');
  el.content.classList.remove('is-vault');
  el.detailPanel.hidden = true;
  el.wbActionbar.hidden = true;
  el.viewWorkbench.hidden = true;
  if (el.viewSettings) el.viewSettings.hidden = true;
  if (el.viewVault) el.viewVault.hidden = true;

  // 视图切换：合并模块用专有布局，其余四个模块共用通用布局
  const isMerge = key === 'merge';
  el.viewMerge.hidden = !isMerge;
  el.viewGeneric.hidden = isMerge;
  el.actionbar.hidden = true;
  if (isMerge) {
    el.gActionbar.hidden = true;
    render();                    // 合并模块的空/有数据状态由 render 自行决定
  } else {
    gSwitch(key);                // 通用模块：重置状态并渲染
  }
}

/** 切到工作台：显示统一列表，隐藏旧模块的表格与底部栏。 */
function showWorkbench() {
  el.content.classList.add('is-wb');
  el.content.classList.remove('is-vault');
  el.viewMerge.hidden = true;
  el.viewGeneric.hidden = true;
  if (el.viewSettings) el.viewSettings.hidden = true;
  if (el.viewVault) el.viewVault.hidden = true;
  el.actionbar.hidden = true;
  el.gActionbar.hidden = true;
  el.viewWorkbench.hidden = false;
  if (!el.detailPanel.classList.contains('collapsed')) el.detailPanel.hidden = false;
  wbRender();
}

/* ── 健康检查 ─────────────────────────────────────────── */
/* v0.5：左侧栏整块下线，原来挂在 `.nav-foot` 里的状态点（`#healthChip` +
   靠 title 悬停才看得见的 `#healthText`）随之移除 —— 任务书明确要求连 `.nav-foot`
   一起删，不留一个「无处安放」的指示器。这里保留这次探测，但只在**连不上**时出声：
   一个明说的 toast 比一个看不懂的小圆点有用；连上了就静默（版本号仍显示在设置页
   `#seVer`，那才是「看版本」的地方）。 */
async function checkHealth() {
  const { data } = await api(API.health);
  if (!data || !data.ok) toast('后端未连接', 'err');
}

/* ── 数据加载 ─────────────────────────────────────────── */
function mergeRows(newRows) {
  const exist = new Set(state.rows.map((r) => r.path));
  let added = 0;
  for (const r of newRows) {
    if (!exist.has(r.path)) { state.rows.push(r); exist.add(r.path); added++; }
  }
  return added;
}

async function loadFromPaths(paths) {
  if (!paths || !paths.length) { toast('未选择任何发票文件', 'err'); return; }
  showOverlay(`正在解析 ${paths.length} 个文件…`);
  try {
    const { data } = await api(API.scan, { method: 'POST', body: JSON.stringify({ paths }) });
    hideOverlay();
    if (!data.ok) { toast(data.error || '解析失败', 'err'); return; }
    const added = mergeRows(data.rows);
    if (!state.outDir && data.default_out) {
      state.outDir = data.default_out;
      el.outDir.value = data.default_out;
    }
    state.rows.forEach((r, i) => { r.id = i; });
    render();
    const hasErrors = !!(data.errors && data.errors.length);
    if (hasErrors) {
      const issue = (data.vault_failed && data.errors.find((e) => e.filename === '（发票库入库）')) || data.errors[0];
      const summary = data.vault_failed
        ? `${data.vault_failed} 个文件未能入库`
        : `${data.errors.length} 项文件处理异常`;
      toast(`${summary}：${issue.reason || '请核对导入文件'}`, 'err', 6000);
    }
    if (!hasErrors) toast(added ? `已添加 ${added} 张发票` : '文件已在列表中', added ? 'ok' : 'info');
  } catch (e) {
    hideOverlay();
    toast('解析请求失败：' + e.message, 'err');
  }
}

async function uploadFiles(fileList) {
  const files = [...fileList].filter((f) => /\.(pdf|ofd)$/i.test(f.name));
  if (!files.length) { toast('只能上传 PDF / OFD 文件', 'err'); return; }
  showOverlay(`正在上传 ${files.length} 个文件…`);
  try {
    const fd = new FormData();
    files.forEach((f) => fd.append('files', f));
    const res = await fetch(API.upload, { method: 'POST', body: fd });
    const data = await res.json();
    hideOverlay();
    if (!data.ok || !data.paths.length) { toast(data.errors?.[0]?.reason || data.error || '上传失败', 'err'); return; }
    await loadFromPaths(data.paths);
    if (data.failed) toast(`${data.failed} 个文件上传失败：${data.errors?.[0]?.reason || '请检查文件'}`, 'err', 6000);
  } catch (e) {
    hideOverlay();
    toast('上传失败：' + e.message, 'err');
  }
}

/* ── 渲染 ─────────────────────────────────────────────── */
function visibleRows() {
  const kw = state.filter.trim().toLowerCase();
  let rows = state.rows;
  if (kw) {
    rows = rows.filter((r) =>
      (r.filename + r.seller + r.type + r.date + r.invoice_no).toLowerCase().includes(kw));
  }
  const k = state.sortKey, dir = state.sortDir;
  return [...rows].sort((a, b) => {
    let va = a[k], vb = b[k];
    if (k === 'amount') { va = Number(va) || 0; vb = Number(vb) || 0; return (va - vb) * dir; }
    return String(va).localeCompare(String(vb), 'zh') * dir;
  });
}

function render() {
  const has = state.rows.length > 0;
  el.stats.hidden = !has;
  el.tableCard.hidden = !has;
  el.outCard.hidden = !has;
  el.actionbar.hidden = !has;
  el.dropzone.classList.toggle('compact', has);
  if (has) el.dropzone.querySelector('.dz-title').textContent = '继续添加发票';

  setBadge(el.badgeMerge, state.rows.length);

  if (!has) { el.gridBody.innerHTML = ''; updateStats(); return; }

  updateStats();

  const rows = visibleRows();
  if (!rows.length) {
    el.gridBody.innerHTML =
      `<tr><td colspan="7"><div class="empty">没有匹配「${esc(state.filter)}」的发票</div></td></tr>`;
    updateActionInfo();
    return;
  }

  el.gridBody.innerHTML = rows.map((r, i) => `
    <tr data-id="${r.id}">
      <td class="col-idx">${i + 1}</td>
      <td>
        <div class="cell-name" title="${esc(r.filename)}">
          <span class="ext">${esc(r.ext)}</span>
          <span class="fname">${esc(r.filename)}</span>
        </div>
      </td>
      <td class="col-type"><span class="tag" data-kind="${esc(r.kind)}">${esc(r.type)}</span></td>
      <td class="col-date">${r.date ? esc(r.date) : '<span class="muted">—</span>'}</td>
      <td class="col-seller"><span class="truncate" title="${esc(r.seller)}">${r.seller ? esc(r.seller) : '<span class="muted">—</span>'}</span></td>
      <td class="col-amount"><span class="amount${r.amount ? '' : ' zero'}">${r.amount ? fmtMoney(r.amount) : '—'}</span></td>
      <td class="col-del">
        <button class="del-btn" data-del="${r.id}" title="移出列表">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 6L6 18"/><path d="M6 6l12 12"/>
          </svg>
        </button>
      </td>
    </tr>`).join('');

  updateActionInfo();

  el.grid.querySelectorAll('th.sortable').forEach((th) => {
    th.classList.remove('asc', 'desc');
    if (th.dataset.sort === state.sortKey) th.classList.add(state.sortDir === 1 ? 'asc' : 'desc');
  });
}

function updateStats() {
  const rows = state.rows;
  el.statCount.textContent = rows.length;
  el.statAmount.textContent = fmtMoney(rows.reduce((s, r) => s + (Number(r.amount) || 0), 0));
  el.statPages.textContent = rows.length;

  const byType = {};
  rows.forEach((r) => { byType[r.type] = (byType[r.type] || 0) + 1; });
  const parts = Object.entries(byType).sort((a, b) => b[1] - a[1]).slice(0, 3)
    .map(([t, n]) => `${t}×${n}`);
  el.statTypes.textContent = parts.length ? parts.join(' · ') : '—';
}

function updateActionInfo() {
  const n = state.rows.length;
  const total = state.rows.reduce((s, r) => s + (Number(r.amount) || 0), 0);
  el.actionInfo.innerHTML =
    `已选 <b>${n}</b> 张发票 · 金额合计 <span class="hl">${fmtMoney(total)}</span>`;
  el.runBtn.disabled = n === 0;
}

/* ── 事件绑定 ─────────────────────────────────────────── */
function bindEvents() {
  /* 顶栏右上角「设置」齿轮（v0.5：左侧栏整块下线后，设置入口搬到这里）。
     ⚠️ 它同时是**唯一的「回工作台」出口**：原来那个 data-key=workbench 的侧栏入口
     随左侧栏一起没了，而 #view-settings 里本来就没有返回按钮 —— 齿轮若只单向进设置，
     用户进去就出不来。所以做成开关：不在设置里就进设置，已经在设置里就回工作台；
     `aria-pressed` 与 `.active` 一起表明它是一个可切换的按钮。 */
  if (el.settingsBtn) {
    el.settingsBtn.addEventListener('click', () => {
      switchModule(state.mod === 'settings' ? 'workbench' : 'settings');
    });
  }

  // 点击投放区 → 原生文件对话框
  el.dropzone.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;
    el.fileInput.click();
  });
  el.fileInput.addEventListener('change', (e) => {
    if (e.target.files.length) uploadFiles(e.target.files);
    e.target.value = '';
  });

  // 「选择文件」→ 后端 Tk 对话框
  el.pickFiles.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const { data } = await api(API.pick, { method: 'POST', body: JSON.stringify({ mode: 'files' }) });
      if (data.ok && data.paths.length) await loadFromPaths(data.paths);
      else if (data.ok) toast('未选择文件', 'info');
      else toast(data.error || '对话框不可用', 'err');
    } catch (err) { toast('调用失败：' + err.message, 'err'); }
  });

  // 「选择文件夹」
  el.pickFolder.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const { data } = await api(API.pick, { method: 'POST', body: JSON.stringify({ mode: 'folder' }) });
      if (data.ok && data.paths.length) await loadFromPaths(data.paths);
      else if (data.ok) toast('该文件夹内没有 PDF/OFD', 'info');
      else toast(data.error || '对话框不可用', 'err');
    } catch (err) { toast('调用失败：' + err.message, 'err'); }
  });

  // 拖拽
  ['dragenter', 'dragover'].forEach((ev) =>
    el.dropzone.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      el.dropzone.classList.add('dragging');
    }));
  ['dragleave', 'drop'].forEach((ev) =>
    el.dropzone.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      if (ev === 'dragleave' && el.dropzone.contains(e.relatedTarget)) return;
      el.dropzone.classList.remove('dragging');
    }));
  el.dropzone.addEventListener('drop', (e) => {
    const files = e.dataTransfer?.files;
    if (files && files.length) uploadFiles(files);
  });
  ['dragover', 'drop'].forEach((ev) =>
    window.addEventListener(ev, (e) => e.preventDefault()));

  // 搜索 / 排序 / 删除
  el.searchInput.addEventListener('input', (e) => { state.filter = e.target.value; render(); });

  el.grid.querySelectorAll('th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (state.sortKey === k) state.sortDir *= -1;
      else { state.sortKey = k; state.sortDir = 1; }
      render();
    });
  });

  el.gridBody.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-del]');
    if (!btn) return;
    const id = Number(btn.dataset.del);
    const tr = btn.closest('tr');
    tr.classList.add('removing');
    setTimeout(() => {
      state.rows = state.rows.filter((r) => r.id !== id);
      state.rows.forEach((r, i) => { r.id = i; });
      render();
    }, 200);
  });

  el.clearBtn.addEventListener('click', () => {
    if (!state.rows.length) return;
    state.rows = [];
    state.filter = '';
    el.searchInput.value = '';
    render();
    toast('已清空列表', 'info');
  });

  // 输出目录
  el.pickOutDir.addEventListener('click', async () => {
    try {
      const { data } = await api(API.pickdir, { method: 'POST', body: '{}' });
      if (data.ok && data.dir) {
        state.outDir = data.dir;
        el.outDir.value = data.dir;
        toast('输出目录已更新', 'ok');
      } else if (data.ok) toast('未选择目录', 'info');
      else toast(data.error || '对话框不可用', 'err');
    } catch (err) { toast('选择目录失败：' + err.message, 'err'); }
  });
  el.outDir.addEventListener('input', (e) => { state.outDir = e.target.value.trim(); });

  // 执行合并
  el.runBtn.addEventListener('click', runMerge);

  /* ── 通用模块（火车票 / 住宿票 / 核对 / 重命名）─────────── */
  el.gDropzone.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;
    el.gFileInput.click();
  });
  el.gFileInput.addEventListener('change', (e) => {
    if (e.target.files.length) gUpload(e.target.files);
    e.target.value = '';
  });
  el.gPickFiles.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const { data } = await api(API.pick, { method: 'POST', body: JSON.stringify({ mode: 'files' }) });
      if (data.ok && data.paths.length) await gLoad(data.paths);
      else if (data.ok) toast('未选择文件', 'info');
      else toast(data.error || '对话框不可用', 'err');
    } catch (err) { toast('调用失败：' + err.message, 'err'); }
  });
  el.gPickFolder.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const { data } = await api(API.pick, { method: 'POST', body: JSON.stringify({ mode: 'folder' }) });
      if (data.ok && data.paths.length) await gLoad(data.paths);
      else if (data.ok) toast('该文件夹内没有 PDF/OFD', 'info');
      else toast(data.error || '对话框不可用', 'err');
    } catch (err) { toast('调用失败：' + err.message, 'err'); }
  });

  ['dragenter', 'dragover'].forEach((ev) =>
    el.gDropzone.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      el.gDropzone.classList.add('dragging');
    }));
  ['dragleave', 'drop'].forEach((ev) =>
    el.gDropzone.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      if (ev === 'dragleave' && el.gDropzone.contains(e.relatedTarget)) return;
      el.gDropzone.classList.remove('dragging');
    }));
  el.gDropzone.addEventListener('drop', (e) => {
    const files = e.dataTransfer?.files;
    if (files && files.length) gUpload(files);
  });

  el.gSearchInput.addEventListener('input', (e) => { gstate.filter = e.target.value; gRender(); });

  // 通用模块表头排序 —— 表头是每次 gRender 重建的，所以用事件委托
  el.gThead.addEventListener('click', (e) => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const k = th.dataset.sort;
    if (gstate.sortKey === k) gstate.sortDir *= -1;
    else { gstate.sortKey = k; gstate.sortDir = 1; }
    gRender();
  });

  el.gClearBtn.addEventListener('click', () => {
    if (!gstate.rows.length && !gstate.srcRows.length) return;
    gstate.rows = []; gstate.srcRows = []; gstate.filter = '';
    gstate.phase = 'idle'; gstate.sortKey = ''; gstate.sortDir = 1;
    el.gSearchInput.value = '';
    gSyncPhase();
    gRender();
    toast('已清空列表', 'info');
  });
  el.gPickOutDir.addEventListener('click', async () => {
    try {
      const { data } = await api(API.pickdir, { method: 'POST', body: '{}' });
      if (data.ok && data.dir) {
        gstate.outDir = data.dir; el.gOutDir.value = data.dir;
        toast('输出目录已更新', 'ok');
      } else if (data.ok) toast('未选择目录', 'info');
      else toast(data.error || '对话框不可用', 'err');
    } catch (err) { toast('选择目录失败：' + err.message, 'err'); }
  });
  el.gOutDir.addEventListener('input', (e) => { gstate.outDir = e.target.value.trim(); });
  el.gRunBtn.addEventListener('click', gRun);

  // 核对预设抬头 / 税号：改过就记住
  el.gExpName.addEventListener('input', saveCheckExpected);
  el.gExpTax.addEventListener('input', saveCheckExpected);

  // 工作台（v0.4 主界面）
  bindWorkbench();

  // 设置视图 + 首次运行引导（用户第 8 条）
  bindSettings();
  bindFirstRun();

  // 发票库视图（v0.6）
  bindVault();

  // 结果对话框
  el.closeModalBtn.addEventListener('click', () => { el.resultModal.hidden = true; });
  el.openFolderBtn.addEventListener('click', async () => {
    // 合并模块用 state.outDir，其余模块用 gstate.outDir
    const dir = state.mod === 'merge' ? state.outDir : gstate.outDir;
    const { data } = await api(API.openFolder, { method: 'POST', body: JSON.stringify({ dir }) });
    if (!data.ok) toast(data.error || '打开失败', 'err');
  });

  // 快捷键
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'o') {
      e.preventDefault();
      if (state.mod === 'workbench') wbPick('files');
      else if (state.mod === 'merge') el.pickFiles.click();
      else el.gPickFiles.click();
    } else if (e.key === 'Escape') {
      if (!el.resultModal.hidden) el.resultModal.hidden = true;
    }
  });
}

/* ── 执行合并 ─────────────────────────────────────────── */
async function runMerge() {
  if (!state.rows.length) return;
  const paths = state.rows.map((r) => r.path);
  el.runBtn.disabled = true;
  showOverlay(`正在合并 ${paths.length} 张发票…`);
  try {
    const { data } = await api(API.run, {
      method: 'POST',
      body: JSON.stringify({ paths, out_dir: state.outDir }),
    });
    hideOverlay();
    if (!data.ok) { showResult(false, data.error || '合并失败'); return; }
    showResult(true, '', data);
  } catch (e) {
    hideOverlay();
    showResult(false, '请求失败：' + e.message);
  } finally {
    el.runBtn.disabled = state.rows.length === 0;
  }
}

function showResult(ok, errMsg, data) {
  el.resultIcon.className = 'modal-icon ' + (ok ? 'ok' : 'err');
  el.resultIcon.innerHTML = ok
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>';
  el.resultTitle.textContent = ok ? '合并完成' : '合并失败';

  if (ok) {
    const failed = (data.failed_files || []).length;
    el.resultBody.innerHTML = `
      <div class="kv"><span>输出文件</span><b>${esc(data.filename)}</b></div>
      <div class="kv"><span>合并张数</span><b>${data.merged_files} 张</b></div>
      <div class="kv"><span>总页数</span><b>${data.page_count} 页</b></div>
      ${failed ? `<div class="kv"><span>跳过</span><b style="color:var(--err)">${failed} 张</b></div>` : ''}
      <div class="path">${esc(data.merged_path)}</div>`;
    el.openFolderBtn.style.display = '';
    toast('合并成功', 'ok');
  } else {
    el.resultBody.innerHTML = `<div class="path">${esc(errMsg)}</div>`;
    el.openFolderBtn.style.display = 'none';
    toast(errMsg, 'err');
  }
  el.resultModal.hidden = false;
}

/* ── 启动 ─────────────────────────────────────────────── */
function initApp() {
  initTheme();
  loadCheckExpected();                                // 核对预设抬头 / 税号（含本地记忆）
  // 本地偏好（输出目录 / 重命名方式 / 失败策略）：必须在 loadCheckExpected() 之后、
  // 首次运行横幅与视图切换之前 —— 首屏就要用上记忆值（含 :700 的 default_out 判断）。
  loadPrefs();
  if (sePrefs.outDir) seApplyOutDir(sePrefs.outDir);  // 记忆值写回 gOutDir + gstate
  seSyncFirstRun();                                   // 没填抬头/税号 → 亮首次运行引导
  checkHealth();
  bindEvents();
  render();                                           // 先让合并模块自己的状态归位
  gUpdateBadge();                                     // 清掉四个通用模块的硬编码「0」残留
  vaultRefreshStat();                                 // 顶栏「库占用」：拉一次 /api/vault/stats
                                                      // （404 时自己落成「发票库 不可用」，不抛错）
  switchModule('workbench');                          // v0.4：默认进工作台
}

document.addEventListener('DOMContentLoaded', initApp, { once: true });
