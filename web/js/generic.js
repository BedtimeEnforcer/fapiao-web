// generic 视图；依赖先加载的 app.js 中的通用状态和界面工具。
/* ══════════════════════════════════════════════════════════════
   通用模块（火车票 / 住宿票 / 发票核对 / 智能重命名）
   ──────────────────────────────────────────────────────────────
   这四个模块的交互骨架完全一致 —— 投放文件 → 表格预览 → 底部执行，
   差异只有三处：表格列（columns）、统计指标（kpis）、执行接口（run+body）。
   所以共用同一套 DOM（#view-generic），用下面的配置驱动渲染，
   避免把几乎相同的逻辑手抄四遍（抄四遍必然有一处会漏改）。
   ══════════════════════════════════════════════════════════════ */

const fmtSize = (n) => {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
};

const GENERIC = {
  /* ── 火车票 ─────────────────────────────────────────── */
  train: {
    scan: '/api/train/scan', run: '/api/train/run',
    preview: true,                    // 行程单在浏览器新标签页打开，页面里再点保存
    dzTitle: '把火车票拖到这里，或点击选择',
    dzHint: '支持 PDF / OFD，可多选；直接选整个文件夹也行',
    tableTitle: '车票行程明细',
    searchHint: '搜索车次 / 车站 / 乘客',
    runLabel: '生成行程单',
    running: '正在生成行程单…',
    columns: [
      { key: 'train', label: '车次', cls: 'col-train' },
      { key: 'route', label: '行程', cls: 'col-route', sortKey: 'from_',
        render: (r) => `${esc(r.from_ || '?')} <span class="muted">→</span> ${esc(r.to_ || '?')}` },
      { key: 'date', label: '乘车时间', cls: 'col-date' },
      { key: 'passenger', label: '乘客', cls: 'col-passenger' },
      { key: 'seat_class', label: '席位', cls: 'col-seat' },
      { key: 'amount', label: '金额', cls: 'col-amount', money: true },
    ],
    kpis: (d) => [
      { label: '车票张数', value: d.count },
      { label: '票面合计', value: fmtMoney(d.total_amount), accent: true },
      { label: '待确认', value: d.review_count || 0 },
      { label: '乘客', value: (d.passengers || []).join('、') || '—', small: true },
    ],
    // 统计一律由「当前行」重算 —— 追加导入后数字依然准确
    stat: (rows) => ({
      count: rows.length,
      /* ⚠️ 废弃材料（挂不上的非电子发票）**不进合计** —— 后端已在出口把它的
         amount 清零（core/attach.py::_clear_amount），这里是第二道闸：
         只靠后端一道，将来换个数据源就会漏。 */
      total_amount: rows.filter((r) => !isDiscarded(r))
        .reduce((s, r) => s + (Number(r.amount) || 0), 0),
      review_count: rows.filter((r) => r.needs_review).length,
      passengers: [...new Set(rows.map((r) => r.passenger).filter(Boolean))],
    }),
    body: (st) => ({
      paths: st.srcRows.map((r) => r.path), out_dir: st.outDir,
      include_map: true, dedup: true,
      // 手动补全的行程随生成请求发给后端，按 filepath 逐条套用（不填的条目不出现）
      trip_overrides: collectTripOverrides(st.srcRows.map((r) => r.path)),
    }),
  },

  /* ── 住宿票 ─────────────────────────────────────────── */
  hotel: {
    scan: '/api/hotel/scan', run: '/api/hotel/run',
    preview: true,                    // 住宿行程单同样在浏览器新标签页打开
    dzTitle: '把住宿发票拖到这里，或点击选择',
    dzHint: '支持 PDF / OFD，可多选；直接选整个文件夹也行',
    tableTitle: '住宿明细',
    searchHint: '搜索酒店 / 购买方',
    runLabel: '生成行程单',
    running: '正在生成住宿行程单…',
    columns: [
      { key: 'hotel_name', label: '酒店', cls: 'col-seller' },
      // 住宿发票票面没有「入住人」，这里取的本来就是购买方
      { key: 'guest_name', label: '购买方', cls: 'col-passenger' },
      { key: 'date', label: '入住', cls: 'col-date' },
      { key: 'checkout', label: '退房', cls: 'col-date' },
      { key: 'nights', label: '晚数', cls: 'col-nights' },
      { key: 'amount', label: '金额', cls: 'col-amount', money: true },
    ],
    kpis: (d) => [
      { label: '发票张数', value: d.count },
      { label: '住宿合计', value: fmtMoney(d.total_amount), accent: true },
      { label: '总晚数', value: d.total_nights || 0 },
      { label: '待确认', value: d.review_count || 0 },
    ],
    stat: (rows) => ({
      count: rows.length,
      total_amount: rows.filter((r) => !isDiscarded(r))
        .reduce((s, r) => s + (Number(r.amount) || 0), 0),
      total_nights: rows.reduce((s, r) => s + (Number(r.nights) || 0), 0),
      review_count: rows.filter((r) => r.needs_review).length,
    }),
    body: (st) => ({ paths: st.srcRows.map((r) => r.path), out_dir: st.outDir, dedup: true }),
  },

  /* ── 发票核对 ─────────────────────────────────────────
     两阶段：先登记文件 → 填预设抬头/税号 → 核对后表格换成「结果列」。 */
  check: {
    scan: '/api/check/scan', run: '/api/check/run',
    preview: true,                    // 核对结论也在新标签页出结果页
    dzTitle: '把要核对的发票拖到这里，或点击选择',
    dzHint: '支持 PDF / OFD；在上方填好预设抬头与税号，再点右下角「开始核对」',
    tableTitle: '待核对的发票',
    resultTitle: '核对结果',           // 执行后表格标题 / 按钮文案跟着变
    resultLabel: '重新核对',
    searchHint: '搜索文件名',
    runLabel: '开始核对',
    running: '正在核对发票…',
    extra: 'check',
    columns: [
      { key: 'filename', label: '文件名' },
      { key: 'size', label: '大小', cls: 'col-size', render: (r) => esc(fmtSize(r.size)) },
    ],
    kpis: (d) => [{ label: '待核对', value: d.count }],
    stat: (rows) => ({ count: rows.length }),
    body: (st) => ({
      // 核对完成后 rows 会被换成「结果行」（不含 path），路径一律取自 scan 阶段的原始行 ——
      // 否则再次点「重新核对」会传空路径。
      paths: st.srcRows.map((r) => r.path),
      expected_name: el.gExpName.value.trim(),
      expected_tax_id: el.gExpTax.value.trim(),
    }),
    resultColumns: [
      { key: 'filename', label: '文件名' },
      { key: 'buyer_name', label: '购买方', cls: 'col-seller' },
      { key: 'tax_id', label: '税号', cls: 'col-tax' },
      { key: 'amount', label: '金额', cls: 'col-amount', money: true },
      { key: 'verdict', label: '结果', cls: 'col-verdict', sortKey: 'passed', render: (r) => (
        r.passed ? '<span class="tag ok-mark">通过</span>'
          : (r.needs_review ? '<span class="tag warn-mark">待确认</span>'
            : '<span class="tag err-mark">不符</span>')) },
    ],
    resultStat: (rows) => ({
      total: rows.length,
      passed: rows.filter((r) => r.passed).length,
      failed: rows.filter((r) => !r.passed).length,          // 与后端口径一致（不符含待确认）
      review_count: rows.filter((r) => r.needs_review).length,
    }),
    resultKpis: (d) => [
      { label: '总计', value: d.total },
      { label: '通过', value: d.passed, accent: true },
      { label: '不符', value: d.failed },
      { label: '待确认', value: d.review_count || 0 },
    ],
  },

  /* ── 智能重命名 ───────────────────────────────────────
     先预览新名字，用户确认后才执行（绝不静默改名）。 */
  rename: {
    scan: '/api/rename/scan', run: '/api/rename/run',
    dzTitle: '把要重命名的发票拖到这里，或点击选择',
    dzHint: '先解析出规范文件名供你过目，点右下角「执行重命名」才会真正改名',
    tableTitle: '重命名预览',
    searchHint: '搜索文件名',
    runLabel: '执行重命名',
    running: '正在重命名…',
    extra: 'rename',
    columns: [
      { key: 'filename', label: '原文件名' },
      { key: 'newname', label: '新文件名', sortKey: 'new_name',
        render: (r) => (r.error
          ? `<span class="err-mark">解析失败：${esc(r.error)}</span>`
          : `<span class="new">${esc(r.new_name || '—')}</span>`) },
      { key: 'type', label: '类型', cls: 'col-type' },
      { key: 'amount', label: '金额', cls: 'col-amount', money: true },
    ],
    kpis: (d) => [
      { label: '文件总数', value: d.count },
      { label: '可重命名', value: d.changed, accent: true },
      { label: '解析失败', value: d.count - d.parsed },
    ],
    stat: (rows) => ({
      count: rows.length,
      parsed: rows.filter((r) => !r.error).length,
      changed: rows.filter((r) => !r.error && r.new_name && r.new_name !== r.filename).length,
    }),
    body: (st) => ({
      items: st.srcRows.filter((r) => !r.error && r.new_name)
        .map((r) => ({ path: r.path, new_name: r.new_name })),
      mode: (document.querySelector('input[name="renameMode"]:checked') || {}).value || 'inplace',
      out_dir: st.outDir,
    }),
  },
};

/* ── 通用模块状态：**每个模块各留一份** ──────────────────────────
   切模块只换「当前指针」，不重置数据 —— 这样跟「发票合并」一样，
   切走再切回来，已经导入的文件还在，不用重新导入。 */
function blankGState(mod) {
  return {
    mod,
    rows: [],        // 当前要渲染的行（核对执行后会换成结果行）
    srcRows: [],     // 原始「待处理」行（含 path）—— 结果行没有 path，重复执行要靠它
    filter: '',
    outDir: '',
    phase: 'idle',   // 'idle' 待处理 / 'done' 已出结果（只有核对用得到）
    sortKey: '', sortDir: 1,
  };
}
const gstates = {
  train:  blankGState('train'),
  hotel:  blankGState('hotel'),
  check:  blankGState('check'),
  rename: blankGState('rename'),
};
/** 当前模块的状态。切模块时整体换引用，所有 `gstate.xxx` 自动指向新对象。 */
let gstate = gstates.train;

function gVisibleRows() {
  const kw = (gstate.filter || '').trim().toLowerCase();
  let rows = gstate.rows;
  if (kw) {
    const cfg = GENERIC[gstate.mod];
    rows = rows.filter((r) =>
      String(r.filename || '').toLowerCase().includes(kw)
      || (cfg.columns || []).some((c) => String(r[c.key] ?? '').toLowerCase().includes(kw)));
  }
  const k = gstate.sortKey;
  if (!k) return rows;
  const dir = gstate.sortDir;
  return [...rows].sort((a, b) => {
    const va = a[k], vb = b[k];
    if (typeof va === 'number' || typeof vb === 'number') {   // 金额 / 晚数等按数值比
      return ((Number(va) || 0) - (Number(vb) || 0)) * dir;
    }
    return String(va ?? '').localeCompare(String(vb ?? ''), 'zh') * dir;
  });
}

function gCell(r, col) {
  if (col.render) return `<td class="${col.cls || ''}">${col.render(r)}</td>`;
  const v = r[col.key];
  if (col.money) {
    const n = Number(v) || 0;
    return `<td class="col-amount"><span class="amount${n ? '' : ' zero'}">${n ? fmtMoney(n) : '—'}</span></td>`;
  }
  if (v === 0 || v === '' || v == null) return `<td class="${col.cls || ''}"><span class="muted">—</span></td>`;
  return `<td class="${col.cls || ''}"><span class="truncate" title="${esc(String(v))}">${esc(String(v))}</span></td>`;
}

/** 侧栏角标：图标导航下用「有数才显示」的小圆点，0 时留空（CSS 的 :empty 会隐藏）。 */
function setBadge(node, n) {
  if (node) node.textContent = n ? String(n) : '';
}

function gUpdateBadge() {
  /* 四个通用模块的角标一起更新：首屏调用一次即可把硬编码残留清零，
     之后任一模块导入 / 清空也会同步，不会出现「切过去才刷新」的脏角标。 */
  const map = {
    train: el.badgeTrain, hotel: el.badgeHotel,
    check: el.badgeCheck, rename: el.badgeRename,
  };
  Object.keys(map).forEach((mod) => setBadge(map[mod], gstates[mod].rows.length));
}

/** 按当前阶段同步「表格标题 / 执行按钮文案」（核对模块有「结果态」）。 */
function gSyncPhase() {
  const cfg = GENERIC[gstate.mod];
  const done = gstate.phase === 'done';
  el.gTableTitle.textContent = (done && cfg.resultTitle) ? cfg.resultTitle : cfg.tableTitle;
  el.gRunLabel.textContent = (done && cfg.resultLabel) ? cfg.resultLabel : cfg.runLabel;
}

/** 切到某个通用模块：换状态指针 + 套用该模块的文案与面板（**保留已导入的数据**）。 */
function gSwitch(mod) {
  const cfg = GENERIC[mod];
  gstate = gstates[mod];              // 换到该模块自己那份状态，数据不丢
  gstate.mod = mod;

  el.gDzTitle.textContent = cfg.dzTitle;
  el.gDzHint.textContent = cfg.dzHint;
  el.gSearchInput.placeholder = cfg.searchHint;
  el.gSearchInput.value = gstate.filter || '';
  el.gCheckPanel.hidden = cfg.extra !== 'check';
  el.gRenamePanel.hidden = cfg.extra !== 'rename';
  if (gstate.outDir) el.gOutDir.value = gstate.outDir;
  gSyncPhase();
  gRender();                          // 有数据就恢复表格，没有就显示投放区
}

function gRender() {
  const cfg = GENERIC[gstate.mod];
  const done = gstate.phase === 'done';        // 核对出结果后换一套列 / 指标
  const cols = (done && cfg.resultColumns) ? cfg.resultColumns : cfg.columns;
  const kpiFn = (done && cfg.resultKpis) ? cfg.resultKpis : cfg.kpis;
  const statFn = (done && cfg.resultStat) ? cfg.resultStat : cfg.stat;
  const rows = gstate.rows;
  const shown = gVisibleRows();

  // 统计卡：指标一律由**当前行**重算 —— 这样「追加导入」之后数字依然准确
  // （若直接用后端某一次的返回值，追加后就对不上了）。
  const items = rows.length ? kpiFn(statFn(rows)) : [];
  el.gStats.innerHTML = items.map((it) => `
    <div class="stat"${it.accent ? ' style="--k-c: var(--acc)"' : ''}>
      <span class="stat-label">${esc(it.label)}</span>
      <span class="stat-value${it.accent ? ' accent' : (it.small ? ' small' : '')}">${esc(String(it.value))}</span>
    </div>`).join('');
  el.gStats.hidden = items.length === 0;

  const has = rows.length > 0;
  el.gTableCard.hidden = !has;
  el.gDropzone.classList.toggle('compact', has);
  el.gDzTitle.textContent = has ? '继续添加' : cfg.dzTitle;
  el.gActionbar.hidden = !has || !cfg.body;
  el.gOutCard.hidden = !has || gstate.mod === 'check';

  if (!has) { gUpdateBadge(); return; }

  // 表头：除显式 `sortable:false` 的列外，都能点表头排序
  el.gThead.innerHTML = '<tr><th class="col-idx">#</th>'
    + cols.map((c) => {
        if (c.sortable === false) return `<th class="${c.cls || ''}">${esc(c.label)}</th>`;
        const sk = c.sortKey || c.key;
        const on = gstate.sortKey === sk;
        const dirCls = on ? (gstate.sortDir === 1 ? ' asc' : ' desc') : '';
        return `<th class="${c.cls || ''} sortable${dirCls}" data-sort="${esc(sk)}">`
          + `${esc(c.label)}</th>`;
      }).join('')
    + '</tr>';

  el.gBody.innerHTML = shown.length
    ? shown.map((r, i) => `<tr data-id="${r.id}" class="${r.needs_review ? 'needs-review' : ''}">`
        + `<td class="col-idx">${i + 1}</td>`
        + cols.map((c) => gCell(r, c)).join('') + '</tr>').join('')
    : `<tr><td colspan="${cols.length + 1}"><div class="empty">没有匹配「${esc(gstate.filter)}」的记录</div></td></tr>`;

  const spent = rows.reduce((s, r) => s + (Number(r.amount) || 0), 0);
  el.gActionInfo.innerHTML = `已选 <b>${rows.length}</b> 项`
    + (spent ? ` · 金额合计 <span class="hl">${fmtMoney(spent)}</span>` : '');
  el.gRunBtn.disabled = rows.length === 0 || !cfg.body;
  gUpdateBadge();
}

/** 选择/拖入文件后调对应模块的 scan 接口。新导入的会**追加**到已有列表。 */
async function gLoad(paths) {
  const cfg = GENERIC[gstate.mod];
  if (!paths || !paths.length) { toast('未选择任何文件', 'err'); return; }
  showOverlay(`正在解析 ${paths.length} 个文件…`);
  try {
    const { data } = await api(cfg.scan, { method: 'POST', body: JSON.stringify({ paths }) });
    hideOverlay();
    if (!data.ok) { toast(data.error || '解析失败', 'err'); return; }

    // 与「发票合并」保持一致：追加 + 按路径去重，而不是清空重来
    const exist = new Set(gstate.srcRows.map((r) => r.path));
    let added = 0;
    for (const r of (data.rows || [])) {
      if (r.path && exist.has(r.path)) continue;
      if (r.path) exist.add(r.path);
      gstate.srcRows.push({ ...r });
      added++;
    }
    if (!gstate.srcRows.length) { toast('没有解析到可用的发票', 'err'); return; }

    gstate.rows = gstate.srcRows.map((r) => ({ ...r }));   // 渲染行独立一份
    gstate.rows.forEach((r, i) => { r.id = i; });
    gstate.srcRows.forEach((r, i) => { r.id = i; });
    gstate.phase = 'idle';                                 // 新一批文件 → 回到待处理态
    gstate.filter = '';
    el.gSearchInput.value = '';
    // 输出目录：设置页存下的**本地记忆优先** —— 只有还没记过任何目录时，
    // 才采用后端查到的默认值（最小侵入：只多一个判空，不改后端契约）。
    if (data.default_out && !sePrefs.outDir) { gstate.outDir = data.default_out; el.gOutDir.value = data.default_out; }
    gSyncPhase();
    gRender();
    toast(added ? `已添加 ${added} 项` : '文件已在列表中', added ? 'ok' : 'info');
  } catch (e) {
    hideOverlay();
    toast('解析请求失败：' + e.message, 'err');
  }
}

async function gUpload(fileList) {
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
    await gLoad(data.paths);
    if (data.failed) toast(`${data.failed} 个文件上传失败：${data.errors?.[0]?.reason || '请检查文件'}`, 'err', 6000);
  } catch (e) {
    hideOverlay();
    toast('上传失败：' + e.message, 'err');
  }
}

/** 执行当前模块的主操作。 */
async function gRun() {
  const cfg = GENERIC[gstate.mod];
  if (!gstate.rows.length || !cfg.body) return;
  const body = cfg.body(gstate);

  // 行程单 / 核对结果都在**新标签页**里看。浏览器会拦截「异步之后」才发起的
  // window.open，所以在点击的同步阶段先占一个空标签，拿到 URL 后再导航过去。
  const tab = cfg.preview ? window.open('', '_blank') : null;

  el.gRunBtn.disabled = true;
  showOverlay(cfg.running || '正在处理…');
  try {
    const { data } = await api(cfg.run, { method: 'POST', body: JSON.stringify(body) });
    hideOverlay();
    if (!data.ok) { closeTab(tab); gShowResult(false, data.error || '处理失败'); return; }

    if (gstate.mod === 'check') {
      gstate.phase = 'done';                 // 表格换成结果列，直接看逐张判定
      gstate.rows = (data.rows || []).map((r, i) => ({ ...r, id: i }));
      gstate.filter = ''; el.gSearchInput.value = '';
      gSyncPhase();
      gRender();
      gotoPreview(tab, data.preview_url);
      gShowResult(true, `核对完成：通过 ${data.passed} / 不符 ${data.failed}`);
    } else if (gstate.mod === 'rename') {
      gShowResult(true, `重命名完成：成功 ${data.success} / 失败 ${data.failed}`,
                  data.mode === 'copy');
    } else {
      // 火车票 / 住宿票：行程单在浏览器新标签页里看，页面里再点「保存到本地」
      gotoPreview(tab, data.preview_url);
      gShowResult(true, `行程单已生成：${data.filename}`);
    }
  } catch (e) {
    hideOverlay();
    closeTab(tab);
    gShowResult(false, '请求失败：' + e.message);
  } finally {
    el.gRunBtn.disabled = gstate.rows.length === 0;
  }
}

/** 把预先占好的标签导航到预览页；没有预览页就把它关掉。 */
function gotoPreview(tab, url) {
  if (!tab) { if (url) window.open(url, '_blank'); return; }
  if (url) tab.location = url;
  else { try { tab.close(); } catch (e) { /* ignore */ } }
}

function closeTab(tab) {
  try { if (tab) tab.close(); } catch (e) { /* ignore */ }
}

/** 通用模块的结果弹窗（复用同一个 modal，标题与内容按模块填）。
    showOutDir：只有真的往磁盘写了产物时才展示输出目录（行程单/核对结果现在走浏览器预览）。 */
function gShowResult(ok, msg, showOutDir = false) {
  el.resultIcon.className = 'modal-icon ' + (ok ? 'ok' : 'err');
  el.resultIcon.innerHTML = ok
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>';
  el.resultTitle.textContent = ok ? '处理完成' : '处理失败';
  const withPath = ok && showOutDir && gstate.outDir;
  el.resultBody.innerHTML = ok
    ? `<div class="kv"><span>结果</span><b>${esc(msg)}</b></div>`
      + (withPath ? `<div class="path">${esc(gstate.outDir)}</div>` : '')
    : `<div class="path">${esc(msg)}</div>`;
  el.openFolderBtn.style.display = withPath ? '' : 'none';
  el.resultModal.hidden = false;
  toast(msg, ok ? 'ok' : 'err');
}
