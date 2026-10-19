// 发票库视图；依赖先加载的 app.js 中的通用状态和界面工具。
/* ══════════════════════════════════════════════════════════════════════
   发票库（v0.6）
   库是**软件自己留的副本**：删除只删库内副本，磁盘原件一律不动 —— 这句话在
   页头、空态、以及「删除选中 / 一键清空」的二次确认里都要出现，是本视图的
   语义底线，不是文案装饰。
   后端前缀 /api/vault 若不可用，每个入口应显示「发票库还不可用」。
   ══════════════════════════════════════════════════════════════════════ */

const VAULT_API = '/api/vault';
/* 每页行数与后端 limit 同源；超过一页时显示翻页按钮。 */
const VAULT_LIMIT = 200;

/* 发票库保留入库时的原始 kind；这里只把历史 VAT、unknown 等标签显示为中文。 */
const VAULT_KIND_NAME = {
  train: '火车票', air: '机票', taxi: '打车票', bus: '客车票',
  hotel: '住宿票', food: '餐饮票', other: '其他', noninvoice: '非发票',
  vat: '增值税发票', unknown: '票种待识别',
};
const VAULT_KIND_ORDER = ['train', 'air', 'taxi', 'bus', 'hotel', 'food', 'other', 'noninvoice'];
/* 只有这些 kind 才拼 `var(--k-xxx)` —— 令牌清单是固定的，拼一个不存在的令牌
   会得到一个空 background（色点直接消失），不如显式退回 --k-other。 */
const VAULT_KIND_TOKEN = new Set(VAULT_KIND_ORDER);

const vault = {
  rows: [],          // 当前页行（最多 VAULT_LIMIT 条）
  total: 0,          // 后端 total（表尾「共 N 张」用这个，不是 rows.length）
  count: 0,          // 库内总张数
  bytes: 0,          // 库内总字节
  byKind: {},        // { kind: n }
  facetByKind: {},   // 当前搜索/日期范围内各票种张数（不受票种多选限制）
  facetTotal: 0,
  latest: '',        // 最近一次入库时间（est 自当前页行的最大值）
  q: '', kinds: new Set(), from: '', to: '',
  page: 0,
  picked: new Set(), // **行 id**（不是下标）—— 见 vaultRenderTable 与删除逻辑
  down: false,       // 上一次请求失败（404 / 连不上）→ 亮「不可用」态
  reqSeq: 0,         // 请求序号，只认最后一次的结果（连打搜索时防串台）
  workbenchDirty: false, // 在库内修正后，下次回工作台重新读取同一份缓存
};

const vaultKindName = (k) => VAULT_KIND_NAME[String(k || 'unknown').toLowerCase()] || k || '票种待识别';
const vaultRowKindName = (r) => {
  const label = r.mid || r.label;
  return !label || /^(VAT|unknown)$/i.test(label) ? vaultKindName(r.kind) : label;
};
const vaultKindToken = (k) => (VAULT_KIND_TOKEN.has(k) ? `var(--k-${k})` : 'var(--k-other)');
const vaultStr = (s) => (s === null || s === undefined ? '' : String(s));

/** 入库时间：后端给 ISO 串，表格只要到分钟。 */
function vaultTimeHtml(s) {
  const t = vaultStr(s).replace('T', ' ');
  return t ? esc(t.slice(0, 16)) : '<span class="muted">—</span>';
}

/** 金额：取不到就是「—」，**不是 ￥0.00** —— 0.00 是「解析出来真的是零」，
    两者在报销口径里不是一回事（同 wbRenderTable 里那个 ￥0.00 的注释）。 */
function vaultAmountHtml(a) {
  if (a === null || a === undefined || a === '') return '<span class="muted">—</span>';
  const n = Number(a);
  if (!Number.isFinite(n)) return '<span class="muted">—</span>';
  return `<span class="amount">${esc(fmtMoney(n))}</span>`;
}

/** 顶栏「库占用」。三种状态：有数 / 空 / 不可用 —— 不留一个算不出来的假数字。 */
function vaultPaintStat() {
  if (!el.vaultStat) return;
  if (vault.down) {
    el.vaultStat.textContent = '发票库 不可用';
    el.vaultStat.title = '发票库接口还没就位（/api/vault/stats 返回 404）';
    return;
  }
  if (!vault.count) {
    el.vaultStat.textContent = '发票库 空';
    el.vaultStat.title = '发票库是空的 —— 点这里进去看看';
    return;
  }
  el.vaultStat.textContent = `发票库 ${vault.count} 张 · ${fmtSize(vault.bytes)}`;
  el.vaultStat.title = `发票库 ${vault.count} 张，占用 ${fmtSize(vault.bytes)} —— 点这里进去`;
}

/* 一次性把 /api/vault/stats 的结果灌进 vault 的计数字段。
   返回 true = 拿到了可信数据，false = 404 / 连不上（调用方据此决定是否保留旧值）。 */
function vaultApplyStats(data) {
  if (!data || data.ok !== true) return false;
  vault.count = Number(data.count) || 0;
  vault.bytes = Number(data.bytes) || 0;
  vault.byKind = data.by_kind || {};
  return true;
}

/** 页面初始化 / 入库删除后，只刷顶栏那一行（列表页自己的刷新走 vaultFetchList）。 */
async function vaultRefreshStat() {
  try {
    const { status, data } = await api(`${VAULT_API}/stats`);
    if (status === 404 || !vaultApplyStats(data)) {
      vault.down = true;
      vault.count = 0; vault.bytes = 0; vault.byKind = {};
    } else {
      vault.down = false;
    }
  } catch (err) {
    vault.down = true;   // 连不上 = 一样不可用，不留假数字
  }
  vaultPaintStat();
  /* KPI 卡里的「库内 N 张 / 占用 X」和顶栏是同一份数据，别只刷一个。 */
  if (el.vaultKpis && !el.vaultKpis.hidden) vaultRenderKpis();
}

/** /api/vault/list 的查询串。筛选一变就**重新请求后端**（不在前端过滤）——
    「看不见的行仍被删」是这个页面最危险的失败模式，列表与后端的口径必须是同一份。 */
function vaultQuery({ facet = false } = {}) {
  const p = new URLSearchParams();
  if (vault.q) p.set('q', vault.q);
  if (!facet && vault.kinds.size) p.set('kind', [...vault.kinds].join(','));
  if (vault.from) p.set('from', vault.from);
  if (vault.to) p.set('to', vault.to);
  p.set('limit', String(facet ? 1 : VAULT_LIMIT));
  p.set('offset', String(facet ? 0 : vault.page * VAULT_LIMIT));
  return p.toString();
}

/** 拉当前列表；选了票种时另取一次不带票种条件的聚合，供标签显示日期范围内的张数。 */
async function vaultFetchList() {
  const seq = ++vault.reqSeq;
  let ok = false;
  try {
    const needFacet = vault.kinds.size > 0;
    const [listResult, facetResult] = await Promise.all([
      api(`${VAULT_API}/list?${vaultQuery()}`),
      needFacet ? api(`${VAULT_API}/list?${vaultQuery({ facet: true })}`).catch(() => null) : null,
    ]);
    const { status, data } = listResult;
    if (seq !== vault.reqSeq) return;              // 已有更新的请求在路上，丢弃本次
    if (status !== 404 && data && data.ok === true) {
      ok = true;
      vault.rows = Array.isArray(data.rows) ? data.rows : [];
      const facets = needFacet ? (facetResult?.data?.ok === true ? facetResult.data : null) : data;
      vault.facetByKind = facets?.by_kind || {};
      vault.facetTotal = facets ? Number(facets.total) || 0 : null;
      /* ⚠️ 两个坑，是主理人用无头验收（tests/verify_vault_ui.mjs）跑出来的，
         写在这儿免得被"顺手简化"回去：
         ① **不能写 `Number(x) || 旧值`** —— 「一键清空」/删空之后 `count` 与
            `bytes` 就是 0，而 0 是 falsy ⇒ 顶栏会一直显示清空前的张数
            （实测：清空后顶栏仍写「发票库 4 张 · 203.0 KB」），
            空库也切不到空态卡片。必须显式判空。
         ② **有筛选时不能采纳 list 的聚合** —— `count/bytes/by_kind` 都是
            **筛选后**的子集口径，拿来刷顶栏会让「库占用」少报；而「一键清空」
            确认框里的「全部 N 张」用的就是这个数，少报很危险。故只在无筛选时采纳。 */
      const n = (x) => (x === null || x === undefined || x === '' || Number.isNaN(Number(x)) ? null : Number(x));
      const nTotal = n(data.total);
      vault.total = nTotal === null ? vault.rows.length : nTotal;
      const filtered = !!(vault.q || vault.kinds.size || vault.from || vault.to);
      if (!filtered) {
        if (nTotal !== null) vault.count = nTotal;
        const b = n(data.bytes); if (b !== null) vault.bytes = b;
        vault.byKind = data.by_kind || vault.byKind;
      }
      if (vault.page === 0 && !filtered) {
        vault.latest = vault.rows.reduce((m, r) => (vaultStr(r.created_at) > m ? vaultStr(r.created_at) : m), '');
      }
    }
  } catch (err) {
    ok = false;
  }
  if (seq !== vault.reqSeq) return;
  if (ok && vault.page > 0 && vault.total <= vault.page * VAULT_LIMIT) {
    vault.page = Math.max(0, Math.ceil(vault.total / VAULT_LIMIT) - 1);
    vault.picked.clear();
    vaultFetchList();
    return;
  }
  vault.down = !ok;
  if (!ok) { vault.rows = []; vault.total = 0; vault.facetByKind = {}; vault.facetTotal = null; }
  vaultPaintStat();
  vaultRender();
}

/** 筛选变化：**先清空勾选**再重新请求。勾选按行 id 记，而筛选后的列表是另一份
    行集合，留着旧勾选就会出现「看不见的行仍被删」这种危险状态。 */
function vaultRefilter() {
  vault.picked.clear();
  vault.page = 0;
  vaultFetchList();
}

/** 概览条（.wb-kpis / .kpi 的结构与类全部复用工作台那一套）。 */
function vaultRenderKpis() {
  if (!el.vaultKpis) return;
  const bk = vault.byKind || {};
  const keys = VAULT_KIND_ORDER.filter((k) => Number(bk[k]) > 0);
  Object.keys(bk).forEach((k) => { if (Number(bk[k]) > 0 && !VAULT_KIND_ORDER.includes(k)) keys.push(k); });
  const dist = keys.map((k) => `<span class="vk">`
    + `<span class="kdot" data-cat="${esc(k)}" style="background:${vaultKindToken(k)}"></span>`
    + `${esc(vaultKindName(k))} <b>${Number(bk[k]) || 0}</b></span>`).join('');
  const items = [
    { label: '库内', value: `${vault.count} 张` },
    { label: '占用', value: fmtSize(vault.bytes), cls: 'accent' },
    { label: '票种分布', html: dist, cls: 'kpi-text' },
    { label: '最近入库', html: vault.latest ? esc(vaultStr(vault.latest).replace('T', ' ').slice(0, 16)) : '<span class="muted">—</span>', cls: 'kpi-text' },
  ];
  el.vaultKpis.innerHTML = items.map((it) =>
    `<div class="kpi ${it.cls || ''}"><span class="kpi-label">${esc(it.label)}</span>`
    + `<span class="kpi-value">${it.html !== undefined ? it.html : esc(String(it.value))}</span></div>`
  ).join('');
}

/** 票种 chips（含「全部」）。复用工作台 .chip / .cdot / .cn 的视觉。 */
function vaultRenderChips() {
  if (!el.vaultChips) return;
  const bk = vault.facetByKind || {};
  const present = (k) => Number(bk[k]) > 0 || vault.kinds.has(k);
  const keys = VAULT_KIND_ORDER.filter(present);
  [...Object.keys(bk), ...vault.kinds].forEach((k) => {
    if (present(k) && !keys.includes(k)) keys.push(k);
  });
  const chip = (k, label, n) => `<button class="chip${(k === 'all' ? !vault.kinds.size : vault.kinds.has(k)) ? ' on' : ''}" data-vkind="${esc(k)}" aria-pressed="${k === 'all' ? !vault.kinds.size : vault.kinds.has(k)}">`
    + `<span class="cdot" style="background:${vaultKindToken(k)}"></span>${esc(label)}`
    + `<span class="cn">${n === null ? '—' : n}</span></button>`;
  el.vaultChips.innerHTML = chip('all', '全部', vault.facetTotal)
    + keys.map((k) => chip(k, vaultKindName(k), vault.facetTotal === null ? null : Number(bk[k]) || 0)).join('');
}

/** 勾选/操作按钮的联动状态。每个节点都判空 —— 少一个节点不该让整页崩掉。 */
function vaultSyncActions() {
  const n = vault.picked.size;
  if (el.vaultDelSel) {
    el.vaultDelSel.disabled = n === 0;
    el.vaultDelSel.textContent = `删除选中 (${n})`;
  }
  if (el.vaultImport) {
    el.vaultImport.disabled = n === 0;
    el.vaultImport.textContent = `导入到工作台 (${n})`;
  }
  if (el.vaultPickAll) {
    const all = vault.rows.length > 0 && vault.rows.every((r) => vault.picked.has(Number(r.id)));
    el.vaultPickAll.checked = all;
    el.vaultPickAll.indeterminate = !all && n > 0;   // 半选态：别谎报「全选」
  }
}

/** 主表格。行高/字号/列宽全部由全局 table.grid 与 --row-h / --fs-cell 给，
    这里只管内容；**不加任何发光、阴影、filter**（表格本体必须密集可读）。 */
function vaultRenderTable() {
  if (!vault.rows.length) {
    el.vaultBody.innerHTML = `<tr><td colspan="9"><div class="empty">`
      + `${vault.down ? '发票库还不可用' : '没有符合当前筛选条件的发票'}</div></td></tr>`;
    return;
  }
  el.vaultBody.innerHTML = vault.rows.map((r) => {
    const id = Number(r.id);
    const picked = vault.picked.has(id);
    const name = r.file_name || baseName(r.stored_path) || '(未命名)';
    const kind = r.kind || 'unknown';
    return `<tr data-id="${id}" class="${picked ? 'sel' : ''}">`
      + `<td class="col-pick"><input type="checkbox" data-pickv="${id}"${picked ? ' checked' : ''}></td>`
      /* 票种列：色点 + `mid || label`（两者都空时退回票种名，不留空格子） */
      + `<td class="col-kind"><span class="kdot" data-cat="${esc(kind)}" `
      + `style="background:${vaultKindToken(kind)}"></span>`
      + `<span class="lbl" title="${esc(vaultKindName(kind))}${r.label ? ' · ' + esc(r.label) : ''}">`
      + `${esc(vaultRowKindName(r))}</span></td>`
      + `<td title="${esc(name)}"><span class="truncate">${esc(name)}</span></td>`
      + `<td class="col-date">${r.invoice_date ? esc(r.invoice_date) : '<span class="muted">—</span>'}</td>`
      + `<td class="col-seller" title="${esc(vaultStr(r.seller))}">`
      + `${r.seller ? esc(r.seller) : '<span class="muted">—</span>'}</td>`
      + `<td class="col-amount">${vaultAmountHtml(r.amount)}</td>`
      + `<td class="col-info">${journeyStatusHtml(r)}</td>`
      + `<td class="col-date">${vaultTimeHtml(r.created_at)}</td>`
      + `<td class="col-op"><span class="vault-op">`
      + `<button type="button" class="vault-mini" data-vact="detail" data-id="${id}">详情</button>`
      + `<button type="button" class="vault-mini" data-vact="preview" data-id="${id}">预览</button>`
      + `<button type="button" class="vault-mini danger" data-vact="del" data-id="${id}" `
      + `title="删除库内副本（磁盘原件不动）">删除</button>`
      + `</span></td></tr>`;
  }).join('');
}

/** 整页渲染：不可用态 / 空态 / 正常态三选一，互斥。 */
function vaultRender() {
  if (!el.vaultGridWrap) return;
  const filterOn = !!(vault.q || vault.kinds.size || vault.from || vault.to);
  const emptyLib = !vault.down && vault.count === 0 && !filterOn;
  const showTable = !vault.down && !emptyLib;

  if (el.vaultUnavailable) el.vaultUnavailable.hidden = !vault.down;
  if (el.vaultEmpty) el.vaultEmpty.hidden = !emptyLib;
  if (el.vaultKpis) el.vaultKpis.hidden = vault.down;
  if (el.vaultBar) el.vaultBar.hidden = vault.down || emptyLib;
  el.vaultGridWrap.hidden = !showTable;
  if (el.vaultGrid) el.vaultGrid.hidden = !showTable;
  if (el.vaultFoot) {
    const paged = showTable && vault.total > VAULT_LIMIT;
    el.vaultFoot.hidden = !paged;
    if (paged) {
      const start = vault.page * VAULT_LIMIT + 1;
      const end = Math.min(vault.total, start + vault.rows.length - 1);
      el.vaultPageInfo.textContent = `第 ${start}–${end} 张，共 ${vault.total} 张`;
      el.vaultPrev.disabled = vault.page === 0;
      el.vaultNext.disabled = (vault.page + 1) * VAULT_LIMIT >= vault.total;
    }
  }

  if (showTable) { vaultRenderKpis(); vaultRenderChips(); vaultRenderTable(); }
  vaultSyncActions();
}

/** 进发票库：每次进入都重新拉一次。入库/删除都可能发生在别处（工作台跑完一批），
    缓存一份旧列表只会让「刚跑完那批」的人在这里看不到新票。 */
function vaultOnEnter() {
  vault.picked.clear();     // 每次进库都是一次独立的新增选择。
  vaultRender();            // 先用上一次的 by_kind 画一版，避免空白闪一下
  vaultFetchList();
}

/** 发票库侧栏：购买方明确标作开票抬头，不出现“入住人”。 */
function vaultOpenDetail(row) {
  let drawer = document.getElementById('vaultJourneyDrawer');
  if (!drawer) {
    drawer = document.createElement('div');
    drawer.id = 'vaultJourneyDrawer';
    drawer.className = 'vault-journey-drawer';
    drawer.innerHTML = '<div class="vault-drawer-shade" data-vclose></div>'
      + '<aside class="vault-drawer-panel"><div class="vault-drawer-head">'
      + '<b>发票详情</b><button class="icon-btn" type="button" data-vclose aria-label="关闭详情">×</button>'
      + '</div><div class="vault-drawer-body"></div></aside>';
    drawer.addEventListener('click', async (event) => {
      if (event.target.closest('[data-vclose]')) { drawer.remove(); return; }
      const button = event.target.closest('[data-journey-save]');
      if (!button) return;
      const current = vault.rows.find((item) => Number(item.id) === Number(drawer.dataset.id));
      if (!current) { toast('这张票已不在当前列表，请重新打开详情', 'err'); return; }
      button.disabled = true;
      try {
        Object.assign(current, await journeySave(current, button.closest('.journey-editor')));
        vault.workbenchDirty = wb.sourcePaths.size > 0;
        vaultRenderTable();
        vaultOpenDetail(current);
        toast(current.completeness === 'complete' ? '已保存，行程信息完整' : '已保存，仍有信息待补充',
          current.completeness === 'complete' ? 'ok' : 'info');
      } catch (error) {
        toast('保存失败：' + error.message, 'err', 5000);
      } finally {
        button.disabled = false;
      }
    });
    document.body.appendChild(drawer);
  }
  drawer.dataset.id = String(row.id);
  drawer.querySelector('.vault-drawer-body').innerHTML =
    `<h3>${esc(row.file_name || '发票')}</h3>`
    + `<p class="detail-note">票种：${esc(vaultRowKindName(row))}</p>`
    + (row.buyer ? `<p class="detail-note">购买方抬头：${esc(row.buyer)}</p>` : '')
    + journeyEditorHtml(row)
    + `<p class="detail-note">手工补充只保存到发票库的解析记录，原始票据不会被改写。</p>`;
  drawer.querySelectorAll('.journey-date').forEach(dtAttach);
  journeyAttachAirports(drawer).catch((error) => toast(error.message, 'err'));
}

/* ── 发票库的三个写操作（全部要二次确认）──────────────────────────────
   项目里**没有**通用的确认弹窗组件：index.html 只有 #resultModal，而那是「合并
   结果」专用的（标题写死「合并完成/合并失败」）。所以按任务书允许的兜底走
   window.confirm —— 不自己造第三套弹窗。 */

/** 删除库内副本。二次确认里必须写明**磁盘原件不动**。 */
async function vaultDeleteIds(ids, what) {
  if (!ids.length) return;
  if (!window.confirm(`${what}？\n只删发票库里的副本，磁盘原件不会动。`)) return;
  try {
    const { status, data } = await api(`${VAULT_API}/items`, {
      method: 'DELETE',
      body: JSON.stringify({ ids }),
    });
    if (status === 404 || !data || data.ok !== true) {
      toast('删除失败：发票库还不可用', 'err');
      return;
    }
    const leftovers = Number(data.failed || 0) + Number(data.cleanup_failed || 0);
    if (leftovers) toast(`已删除 ${Number(data.deleted) || 0} 张，另有 ${leftovers} 项未完成；请检查磁盘权限后重试`, 'err', 6000);
    else toast(`已删除 ${Number(data.deleted) || 0} 张，释放 ${fmtSize(Number(data.bytes_freed) || 0)}`, 'ok');
    ids.forEach((id) => vault.picked.delete(id));
    await vaultFetchList();          // 列表 + 顶栏「库占用」一起刷新
    await vaultRefreshStat();        // 顶栏以 /stats 为准（有筛选时 list 的 count 只是子集口径）
  } catch (err) {
    toast('删除失败：' + err.message, 'err');
  }
}

async function vaultClearAll() {
  if (!vault.count) { toast('发票库本来就是空的', 'info'); return; }
  if (!window.confirm(`一键清空发票库里的全部 ${vault.count} 张？\n`
    + '只删发票库里的副本，磁盘原件不会动；这个动作不能撤销。')) return;
  try {
    const { status, data } = await api(`${VAULT_API}/clear`, {
      method: 'POST',
      body: JSON.stringify({ confirm: '清空发票库' }),
    });
    if (status === 404 || !data || data.ok !== true) {
      toast('清空失败：发票库还不可用', 'err');
      return;
    }
    const leftovers = Number(data.failed || 0) + Number(data.cleanup_failed || 0);
    if (leftovers) toast(`已处理 ${Number(data.deleted) || 0} 张，另有 ${leftovers} 项未完成；请检查磁盘权限后重试`, 'err', 6000);
    else toast(`已清空 ${Number(data.deleted) || 0} 张，释放 ${fmtSize(Number(data.bytes_freed) || 0)}`, 'ok');
    vault.picked.clear();
    await vaultFetchList();
    await vaultRefreshStat();        // 清空后顶栏必须立刻归零（这里以前踩过 falsy 0 的坑）
  } catch (err) {
    toast('清空失败：' + err.message, 'err');
  }
}

/** 导入到工作台：把选中行的 `stored_path` 交给工作台的加载入口 `wbLoad(paths)`
    （服务在本机，wbLoad 会把路径发给 /api/pool/scan 去解析，所以必须是**服务端
    能直接打开**的库内路径，而不是 rel_path），然后切回工作台。 */
async function vaultImportToWorkbench() {
  const ids = [...vault.picked];
  if (!ids.length) { toast('先勾选要导入的发票', 'info'); return; }
  const picked = vault.rows.filter((r) => ids.includes(Number(r.id)));
  const paths = picked.map((r) => r.stored_path).filter(Boolean);
  const missing = picked.length - paths.length;
  if (!paths.length) { toast('选中的行没有可用的库内路径', 'err'); return; }
  switchModule('workbench', { importVaultSelection: false });
  const loaded = await wbLoad(paths);
  if (loaded) {
    vault.picked.clear();
    vaultSyncActions();
    vault.workbenchDirty = false;
  }
  if (missing) toast(`${missing} 张没有库内路径，已跳过`, 'info');
}

/* 发票库视图的 DOM 契约。少一个节点就整体跳过接线（照 seReady 的写法），
   而不是在某个 addEventListener 上抛 TypeError 把 bindEvents 整条打断。 */
const VAULT_IDS = [
  'viewVault', 'vaultBack', 'vaultKpis', 'vaultBar', 'vaultSearch', 'vaultChips',
  'vaultFrom', 'vaultTo', 'vaultDelSel', 'vaultClearAll', 'vaultImport',
  'vaultGridWrap', 'vaultBody', 'vaultPickAll', 'vaultFoot',
  'vaultPageInfo', 'vaultPrev', 'vaultNext',
  'vaultEmpty', 'vaultUnavailable', 'vaultGoWork', 'vaultRetry',
];
function vaultReady() {
  const miss = VAULT_IDS.filter((k) => !el[k]);
  if (miss.length) {
    console.warn('[发票库] 缺少 DOM 节点，已跳过接线：', miss.join(', '));
    return false;
  }
  return true;
}

function bindVault() {
  if (!vaultReady()) return;

  // 两个返回出口 + 空态里去工作台的按钮，全部回工作台
  el.vaultBack.addEventListener('click', () => switchModule('workbench'));
  el.vaultGoWork.addEventListener('click', () => switchModule('workbench'));
  /* 不可用态的重试：stats 与 list 一起重来（前者管顶栏那一行，后者管整页）。
     故意**不**在命中 404 后自动重试 —— 接口没上线时那会变成后台轮询。 */
  el.vaultRetry.addEventListener('click', () => { vaultRefreshStat(); vaultFetchList(); });
  const turnPage = (delta) => {
    const next = vault.page + delta;
    if (next < 0 || next * VAULT_LIMIT >= vault.total) return;
    vault.page = next;
    vault.picked.clear();
    vaultFetchList();
    el.vaultGridWrap.scrollTop = 0;
  };
  el.vaultPrev.addEventListener('click', () => turnPage(-1));
  el.vaultNext.addEventListener('click', () => turnPage(1));

  /* 顶栏「库占用」：它自己就是入口。已经库里就不动（不跟齿轮一样做开关）——
     它是「看占用」，用户点它是想看数字对应的那批票，不是想切走。 */
  if (el.vaultStat) el.vaultStat.addEventListener('click', () => switchModule('vault'));

  /* 搜索框：输入即搜，220ms 防抖（打字过程中每敲一下都打一次 /api/vault/list
     会把后端打成筛子）。 */
  let qTimer = 0;
  el.vaultSearch.addEventListener('input', (e) => {
    vault.q = e.target.value.trim();
    clearTimeout(qTimer);
    qTimer = setTimeout(vaultRefilter, 220);
  });

  // 日期用 change（不是 input）：date 控件在选完那一天才该发请求
  el.vaultFrom.addEventListener('change', (e) => { vault.from = e.target.value || ''; vaultRefilter(); });
  el.vaultTo.addEventListener('change', (e) => { vault.to = e.target.value || ''; vaultRefilter(); });
  /* ⑧②：同一套日历也用在发票库的起止日期上（共用同一个浮层实例） */
  [el.vaultFrom, el.vaultTo].forEach(dtAttach);

  // chips 是每次渲染重建的，所以用事件委托
  el.vaultChips.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-vkind]');
    if (!b) return;
    const kind = b.dataset.vkind;
    if (kind === 'all') vault.kinds.clear();
    else if (vault.kinds.has(kind)) vault.kinds.delete(kind);
    else vault.kinds.add(kind);
    vaultRenderChips();
    vaultRefilter();
  });

  // 行勾选：**每一行自己的勾**，键是 data-id（行 id）
  el.vaultBody.addEventListener('change', (e) => {
    const box = e.target.closest('input[data-pickv]');
    if (!box) return;
    const id = Number(box.dataset.pickv);
    if (box.checked) vault.picked.add(id); else vault.picked.delete(id);
    const tr = box.closest('tr');
    if (tr) tr.classList.toggle('sel', box.checked);
    vaultSyncActions();
  });
  // 行内「详情 / 预览 / 删除」
  el.vaultBody.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-vact]');
    if (!b && e.target.closest('input[data-pickv]')) return;
    const id = Number(b?.dataset.id || e.target.closest('tr[data-id]')?.dataset.id);
    if (!Number.isInteger(id)) return;
    if (!b || b.dataset.vact === 'detail') {
      const row = vault.rows.find((item) => Number(item.id) === id);
      if (row) vaultOpenDetail(row);
      return;
    }
    // 预览走库内副本的现成接口，和新标签页打开 PDF 是同一件事
    if (b.dataset.vact === 'preview') { window.open(`${VAULT_API}/file?id=${id}`, '_blank'); return; }
    vaultDeleteIds([id], '删除这张票');
  });

  el.vaultPickAll.addEventListener('change', (e) => {
    const on = e.target.checked;
    vault.rows.forEach((r) => {
      const id = Number(r.id);
      if (on) vault.picked.add(id); else vault.picked.delete(id);
    });
    el.vaultBody.querySelectorAll('input[data-pickv]').forEach((b) => { b.checked = on; });
    el.vaultBody.querySelectorAll('tr[data-id]').forEach((tr) => tr.classList.toggle('sel', on));
    vaultSyncActions();
  });

  el.vaultDelSel.addEventListener('click', () => {
    const ids = [...vault.picked];
    if (!ids.length) return;
    vaultDeleteIds(ids, `删除选中的 ${ids.length} 张`);
  });
  el.vaultClearAll.addEventListener('click', vaultClearAll);
  el.vaultImport.addEventListener('click', vaultImportToWorkbench);
}

