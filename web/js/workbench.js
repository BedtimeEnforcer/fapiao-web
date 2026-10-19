// workbench 视图；依赖先加载的 app.js 中的通用状态和界面工具。
/* ══════════════════════════════════════════════════════════════
   发票工作台（v0.4 · R1 统一发票列表 + R2 统一解析入口）
   ──────────────────────────────────────────────────────────────
   与旧模块的差别：旧模块「一个模块一张表、各自 scan 一遍」，工作台是
   **一张跨票种主列表**，数据只来自 /api/pool/scan 的一次解析。
   批次 2 起底栏是**流水线栏**：一次 `POST /api/pool/run` 按刚性顺序跑完
   「查重 → 核对 → 重命名 → 合并」，「生成行程单」是旁边的第二条独立路径。
   ══════════════════════════════════════════════════════════════ */

const wb = {
  rows: [],          // 当前扫描快照的全部行；行 id 始终与服务端快照一致
  sourcePaths: new Set(), // 历次导入的原始输入，含被归并的副本和附属材料
  /* ── 选中语义（用户 2026-10-11 第 6 条）──
     「已选」不再是「用户点过的那些」，而是 **当前筛选结果减去用户手动取消的行**：
     可见即已选。所以这里存的是**反选集**（unsel）—— 空集 = 当前筛选结果全选，
     而不是「一张都没选」。
     ⚠️ 用 path 做键，**不用 id**：id 是 wb.rows 的下标，每次 wbLoad 都会
     `wb.rows.forEach((r, i) => { r.id = i })` 重排，用 id 记录的话重载后
     就指向了别的行。wb.proc 早就是这个口径。 */
  unsel: new Set(),  // 被用户**手动取消勾选**的行的 r.path
  scanId: '',        // pool/scan 给的快照 id —— run 必须带上它（后端靠它做白名单校验 + 复用解析结果）
  /* 票种筛选（用户第 2 条改成**多选**：点一下选中、再点别的=多选、点已选中的=取消）。
     **空集 = 「全部」**（不筛票种）；非空 = 只留集合里这些票种。 */
  cats: new Set(),   // train / air / pax / hotel / food / other / noninvoice
  /* 品名中类筛选：选中「其他」时显示并生效（见 wbHasOther），仅过滤其他票种。
     另外几类第一层已经说清楚「这是什么票」，再按品名细分没有业务含义。
     取值是后端 `mid` 字段（电子配件 / 办公用品 / 日用百货 / 食品饮料 /
     医药保健 / 五金工具 / 其他）或 'all'。 */
  mid: 'all',
  status: 'all',     // 核对状态筛选：pass / review / fail / all
  /* 起止日期筛选（用户第 3 条，**替换掉原来的「近 30 天 / 近 90 天 / 今年」轮盘**）：
     两端都是 'YYYY-MM-DD'，**空串 = 不设该端**。两者都空 = 不按日期筛。 */
  from: '',
  to: '',
  q: '',             // 关键字
  sortKey: 'date',
  sortDir: -1,       // 默认日期倒序：新票在最上面
  activeId: null,    // 详情侧栏当前展示的行
  /* ── 「处理」列逐行状态（批次 2）──
     **按 path 存，不按 id 存**：`routes/pool.py::_dedup_copies` 会把同票副本整行移出，
     行 id 是数组下标、去重后会整体重排；path 才是全项目通用的身份（去重、预览、执行都用它）。
     proc 是**唯一真源**：`wbProcHtml` 从它读，不从行对象的 `.proc` 读 ——
     只在行上挂 `r.proc` 会变成两处状态、迟早不同步（前面那版就踩过）。 */
  proc: new Map(),   // path -> 'todo' | 'running' | 'done' | 'skip' | 'fail'
};

/** 「处理」列 5 态的文案。`todo` = 未执行（列表刚载入时的默认态）。 */
const PROC_NAME = {
  todo: '待处理', running: '处理中', done: '成功', skip: '跳过', fail: '失败',
};

/** 执行顺序：**刚性固定**，与底栏勾选框的排列顺序无关（需求明确要求）。
    后端 `routes/pipeline.py` 的时序也是 dedup → check → rename → merge，两边一致。 */
const PIPELINE_ORDER = ['dedup', 'check', 'rename', 'merge'];

/** 步骤 key → 底栏执行中的进度文案（后端无流式接口，只能按阶段顺序提示）。 */
const PIPELINE_DOING = {
  dedup: '正在查重…', check: '正在核对…', rename: '正在重命名…', merge: '正在合并…',
};

/** 步骤 key → 短名。底栏说明（wbRenderRunNote）与全屏进度的标题共用这一份，
    免得同一件事在两个函数里各写一遍、越写越不一样。 */
const STAGE_NAME = { dedup: '查重', check: '核对', rename: '重命名', merge: '合并',
                     finalize: '收尾' };

/** 流水线是否正在跑。**不能用 `el.wbRunBtn.disabled` 反推**：那个属性还在别处被
    「没勾选行」控制，两者语义不同（正在跑 vs 没得跑）。 */
let wbRunBusy = false;

/** 废弃标记：非电子发票（行程单 / 报销单据 / 报销辅助材料）匹配不上任何发票时为真。
    ⚠️ 与核对的 pass / review / fail 是**两套语义**，不可混用：
    那三态描述「抬头税号对不对」，这个描述「这文件压根不是可报销的电子发票」。
    行对象（`r.discarded`）与详情对象（`r.detail.discarded`）结构不同，两处都要判。 */
function isDiscarded(r) {
  return !!(r && (r.discarded || (r.detail && r.detail.discarded)));
}

/** 废弃原因（后端 discard_reason）：'' | 'noninvoice'。
    行对象与详情对象都可能带，取到哪个用哪个。
    注：同票重复副本已由后端**整行移出列表**（副本不再进前端），
    所以这里只剩「非电子发票材料且匹配不上同批发票」一种废弃。 */
function deadReason(r) {
  return (r && (r.discard_reason || (r.detail && r.detail.discard_reason))) || '';
}

/** 废弃徽章文案：它不是发票（行程单/报销单据/报销辅助材料），且匹配不上同批任何发票。 */
function deadLabel() {
  return '废弃·非发票';
}

/** 徽章悬停说明：说清「为什么废弃」+「金额不进合计」。 */
function deadTip() {
  return '废弃原因：它不是发票（行程单/报销单据/报销辅助材料），且匹配不上同批里的任何发票；金额不进合计';
}

/** 路径 → 文件名（详情里只显示文件名，不暴露目录）。 */
const baseName = (p) => String(p || '').split(/[\\/]/).pop() || '';

/** 票种归类 —— 只决定「色点颜色 + 筛选 chip」，不改后端 kind。
    注（批次 2 订正，旧注释已过期）：core **已经**产出 `air` / `taxi`
    （`core/trainmap.py::resolve_mode` 与 `routes/pool.py` 都会落这两个值），
    下面的关键词归类只是**给没有 kind 的行兜底**，不再存在「提前改坏后端字段」的顾虑。 */
function wbCat(r) {
  /* 非电子发票单独成一类，**且必须排在关键词匹配之前**：
     它压根不是发票，往下走「机票 / 打车」那套关键词只会把它染成错的票种。
     挂上的那些已经并进宿主发票、不独立成行，所以能进这里的都是挂不上的（discarded）。 */
  if (r.kind === 'noninvoice') return 'noninvoice';
  if (r.kind === 'train') return 'train';
  if (r.kind === 'air') return 'air';
  if (r.kind === 'hotel') return 'hotel';
  /* ── 2026-10-12（用户第 ⑩ 条第三款）：打车票 + 客车票 **合并成「客运服务」一类**。
     用户原话（转述）：上方的票据标签把打车票和客车票都直接归为「客运服务」，
     这样交通费里面就只有火车票 / 机票 / 客运服务三项。
     后端冻结契约：这两类行的 `type` 都会写成「客运服务」，`kind` 仍分别是 taxi / bus。
     所以这里的判定顺序是：**先认 kind，再认 type 文案，最后才走关键词兜底**
     （kind 缺失的老数据、手工构造的臆造行都要能落进 pax）。 */
  if (r.kind === 'taxi' || r.kind === 'bus') return 'pax';
  /* 用 `|` 分隔而不是直接拼接：label 尾部 + type 头部可能在边界上拼出假关键词
     （实测：label='汽车出租' 拼 type='汽车出租' → 中间出现「出租汽车」，被误判成打车）。 */
  const t = `${r.label || ''}|${r.type || ''}`;
  /* 后端的「客运服务」是冻结文案，必须排在所有关键词之前 —— 否则
     `type='客运服务'` 里带「客运」二字会被下面的客车正则抢先（结果一样是 pax，
     但顺序写死才能保证以后有人往 pax 里加别的关键词时不出意外）。 */
  if (/客运服务/.test(t)) return 'pax';
  if (/机票|航空|航班/.test(t)) return 'air';
  /* ⚠️ 这里绝不能写裸「出租」：core/rename.py 的规则表里有 `['租赁','租金','出租'] → 租赁费`，
     「房屋出租 / 汽车出租」是租赁费不是打车费。裸词会把它们全染成打车票的橙色点。
     只认明确指向出租车的写法：出租车 / 出租汽车 / 网约车 / 打车 / 滴滴 / 代驾 / 顺风车。
     （旧注释里「客运 48 张被算进打车票」那个 bug 已随本次合并消失：现在两者
      本来就是同一类，不会再出现「打车票 45」这种票种名不对的计数。） */
  if (/出租车|出租汽车|网约|打车|滴滴|代驾|顺风车/.test(t)) return 'pax';
  /* 客车 / 大巴 / 班车 / 长途汽车 / 汽车票 —— 同样归「客运服务」。 */
  if (/客运|客车|大巴|班车|长途汽车|汽车票/.test(t)) return 'pax';
  /* 餐饮票（批次 2 新增第 7 类）。判定只认**明确指向餐饮**的写法，
     与 `core/rename.py` 的 `['餐饮','食品','餐费','伙食','食材','外卖'] → 餐饮费` 对齐。
     ⚠️ 必须放在交通类之后、`return 'other'` 之前，且**不写裸「餐」**
     —— 「餐」单字会误伤「工作餐补」「餐补」这类非餐饮发票项目。 */
  if (/餐饮服务|餐饮费|餐费/.test(t)) return 'food';
  return 'other';
}

/* 票种 chip / 色点的显示名。口径（用户 2026-10-10 拍板）：交通与住宿类一律加「票」，
   与后端标签（火车票 / 机票 / 住宿票）保持一致，消除「票」「费」混用。
   批次 2 新增 `food`（餐饮票）。
   2026-10-12（第 ⑩ 条）：`taxi`(打车票) 与 `bus`(客车票) 合并为 `pax`（客运服务）。
   保留 taxi / bus 两个**别名键**只为老数据兜底显示（`wbCat` 已经不会返回它们）。 */
const CAT_NAME = { train: '火车票', air: '机票', pax: '客运服务',
                   taxi: '打车票', bus: '客车票',
                   hotel: '住宿票', food: '餐饮票', other: '其他', noninvoice: '非电子发票' };

/* chip 的**展示顺序**（用户第 1 条 + 第 ⑩ 条）：交通费三个成员排最前
   （火车票 → 机票 → 客运服务），后面依次住宿 / 餐饮 / 其他 / 非电子发票。
   `CAT_ORDER` 决定顺序，`TRAVEL_CATS` 决定哪几个要被包进「交通费」分组标签里。 */
const CAT_ORDER = ['train', 'air', 'pax', 'hotel', 'food', 'other', 'noninvoice'];

/* 交通费分组（用户第 1 条：跟出行有关的票都归进来）。
   组内成员**保留各自独立分类**，只在筛选栏里视觉上归到「交通费」标签下
   （用户 2026-10-10 拍板：**不做成合成 chip**，每一项仍可单独点选）。
   2026-10-11 从 [air, taxi] 扩到 [train, air, taxi, bus] —— 火车票原先被漏在组外。
   2026-10-12（第 ⑩ 条）：打车票 + 客车票 → 客运服务，交通费组只剩三项。 */
const TRAVEL_CATS = ['train', 'air', 'pax'];

/* chip 色点令牌：`pax` 是前端合成类，theme.css（只读，本轮不许改）里没有 --k-pax，
   直接拼会得到一个空 background（色点整个消失，比没颜色更糟）。退回它两个成员
   合并前用的 --k-taxi 青绿，视觉上是连续的。 */
const catToken = (c) => (c === 'pax' ? 'var(--k-taxi)' : `var(--k-${c})`);

/* 品名中类的**展示顺序**（用户第 7 条）。取值与后端 `core/rename.py::_MID_CLASS`
   的 9 个中类一一对应 —— 后端是唯一真源，这里只负责排序，
   **不在这里重写映射表**（否则就是第三份镜像实现）。 */
const MID_ORDER = ['电子配件', '办公用品', '日用百货', '食品饮料', '医药保健', '五金工具',
                   '汽车用品', '服务费', '其他'];

const CHECK_NAME = { pass: '通过', fail: '不符', review: '待确认', unknown: '未核对' };

/** 起止日期筛选（用户第 3 条）。from/to 形如 2026-04-13，空串表示不设该端。
    行上没有可解析的日期、而用户又设了任一端 ⇒ 排除（不能因为解析不出就当它落在区间内）。 */
function wbInRange(date) {
  if (!wb.from && !wb.to) return true;
  const d = String(date || '').slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return false;
  if (wb.from && d < wb.from) return false;   // YYYY-MM-DD 定长，字符串比较等价于按日期比较
  if (wb.to && d > wb.to) return false;
  return true;
}

/** 二级品名筛选仅作用于已选中的「其他」票种，允许与其他票种并选。 */
function wbHasOther() {
  return wb.cats.has('other');
}

/** 模糊搜索的检索面（用户第 4 条）：把所有**确实是解析出来的**字段都算上。
    `r.detail` 是后端为各票种产出的专属字段（车次 / 出发站 / 到达站 / 乘客 /
    酒店名 / 入住退房…），`Object.values` 取全文即可，不必穷举键名 ——
    穷举会漏掉以后新加的字段。金额/日期也一起进去，方便按「334」或「2026-09」找。
    过滤掉对象/数组：detail 里若有嵌套结构，`[object Object]` 只会污染检索面。 */
function wbHaystack(r) {
  const parts = [r.filename, r.ext, r.seller, r.buyer, r.type, r.label, r.mid,
                 r.item_name, r.amount, r.date];
  const d = r.detail || {};
  for (const k of Object.keys(d)) {
    const v = d[k];
    if (v != null && typeof v !== 'object') parts.push(v);
  }
  return parts.filter((x) => x != null && x !== '').join(' ').toLowerCase();
}

/** 当前筛选 + 排序后的行。KPI 与表格都基于它，保证数字永远对得上。 */
function wbVisible() {
  const q = wb.q.trim().toLowerCase();
  let rows = wb.rows.filter((r) => {
    /* 票种多选（用户第 2 条）：空集 = 「全部」，不挡任何行。 */
    if (wb.cats.size && !wb.cats.has(wbCat(r))) return false;
    if (wbHasOther() && wbCat(r) === 'other' && wb.mid !== 'all'
        && (r.mid || '') !== wb.mid) return false;
    if (wb.status !== 'all' && r.completeness !== wb.status) return false;
    if (!wbInRange(r.date)) return false;
    if (q && !wbHaystack(r).includes(q)) return false;
    return true;
  });
  const k = wb.sortKey, dir = wb.sortDir;
  return rows.sort((a, b) => {
    if (k === 'amount') return ((Number(a.amount) || 0) - (Number(b.amount) || 0)) * dir;
    /* 解析状态（用户第 ⑨ 条）：这一列存在的意义就是「方便找需要确认的发票」。
       直接按字符串排会得到 failed < todo < done 之外的怪序（'done' < 'failed' < 'todo'），
       把要处理的两档拆在两头。所以按语义权重排：失败(0) → 待补全(1) → 完成(2)，
       升序箭头 ⇒ 待处理的永远在最上面。 */
    if (k === 'parse_status') {
      return ((PARSE_RANK[wbParseStatus(a)] ?? 3) - (PARSE_RANK[wbParseStatus(b)] ?? 3)) * dir;
    }
    return String(a[k] ?? '').localeCompare(String(b[k] ?? ''), 'zh') * dir;
  });
}

/* 交通费分组（TRAVEL_CATS）已在上面的 `CAT_ORDER` 旁一起声明，此处不再重复定义。 */

/** facet 计数的底座（用户第 ⑧① 条）—— 「除票种 / 品名中类这两维之外」的全部
    筛选条件：搜索 + 起止日期 + 核对状态。
    ⚠️ 这里是**真 bug 的修复点**：原来 chips 的计数全部取自 `wb.rows`（全量），
    所以用户选了日期或搜了关键字以后，下面列表只剩几条，「全部 180 张」
    「火车票 49 张」却一点不变；而 KPI 早就是按筛选结果算的（wbRenderKpis(rows)），
    于是同屏两套口径打架。facet 的标准做法：算某一维的计数时把它自己那一维放开
    —— 票种维放开票种、品名中类维放开中类，其余条件照收。 */
function wbFacetBase(r) {
  if (wb.status !== 'all' && r.completeness !== wb.status) return false;
  if (!wbInRange(r.date)) return false;
  const q = wb.q.trim().toLowerCase();
  if (q && !wbHaystack(r).includes(q)) return false;
  return true;
}

/** 票种 chips：列出当前日期/搜索范围内的票种；已选中但暂为 0 的标签仍保留，方便取消。
    计数口径：`wbFacetBase`（见上）—— 搜索 / 日期 / 状态照收，**票种自己那一维放开**。 */
function wbRenderChips() {
  const base = wb.rows.filter(wbFacetBase);
  const count = {};
  base.forEach((r) => { const c = wbCat(r); count[c] = (count[c] || 0) + 1; });
  // 单个 chip 的 DOM 结构保持不变（.chip / data-cat / .cdot / .cn），从 map 里提出来复用
  // `.on` = 这个票种在 `wb.cats` 里（多选，用户第 2 条）
  const chipHtml = (c) => `<button class="chip${wb.cats.has(c) ? ' on' : ''}" data-cat="${c}">`
    + `<span class="cdot" style="background:${catToken(c)}"></span>${CAT_NAME[c]}`
    + `<span class="cn">${count[c] || 0}</span></button>`;
  const showCat = (c) => count[c] || wb.cats.has(c);
  const travelChips = TRAVEL_CATS.filter(showCat);
  const parts = [];
  let travelDone = false;
  CAT_ORDER.filter(showCat).forEach((c) => {
    if (TRAVEL_CATS.includes(c)) {
      if (travelDone) return;                 // 「交通费」分组只插一次
      travelDone = true;
      // 分组标签是不可点的 <span>，不是 button；组内票种各自仍可点选
      parts.push(`<div class="chip-group" data-group="travel">`
        + `<span class="cg-label">交通费</span>`
        + `<div class="cg-items">${travelChips.map(chipHtml).join('')}</div></div>`);
      return;
    }
    parts.push(chipHtml(c));
  });
  /* 品名中类第二行：只要选中了「其他」就显示。
     另外六类第一层已经说清了「这是什么票」，再按品名细分没有业务含义。
     只列本批真实出现的中类（实测 4–5 个），不会撑爆；
     默认视图里这段 DOM 根本不生成，所以常驻筛选栏高度零影响。
     计数同样走 facet 口径：在 `base` 之上再放开「品名中类」这一维。 */
  let midRow = '';
  if (wbHasOther()) {
    const midCount = {};
    let midTotal = 0;
    base.forEach((r) => {
      if (wbCat(r) !== 'other') return;
      const m = r.mid || '';
      if (!m) return;
      midCount[m] = (midCount[m] || 0) + 1;
      midTotal += 1;
    });
    const mids = MID_ORDER.filter((m) => midCount[m] || wb.mid === m);
    if (mids.length) {
      midRow = `<div class="chips-mid">`
        + `<span class="cg-label">品名中类</span>`
        + `<button class="chip${wb.mid === 'all' ? ' on' : ''}" data-mid="all">全部`
        + `<span class="cn">${midTotal}</span></button>`
        + mids.map((m) => `<button class="chip${wb.mid === m ? ' on' : ''}" `
            + `data-mid="${esc(m)}">${esc(m)}<span class="cn">${midCount[m] || 0}</span></button>`)
          .join('')
        + `</div>`;
    }
  }
  el.wbChips.innerHTML =
    `<button class="chip${wb.cats.size === 0 ? ' on' : ''}" data-cat="all">全部`
    + `<span class="cn">${base.length}</span></button>`
    + parts.join('') + midRow;
}

/** KPI 条与信息完整度列共用后端完整度结果，按当前筛选结果重算。 */
function wbRenderKpis(rows) {
  // 废弃材料不进合计（后端已源头清零，这里是显示层的第二道闸）
  const total = rows.filter((r) => !isDiscarded(r))
    .reduce((s, r) => s + (Number(r.amount) || 0), 0);
  const n = (st) => rows.filter((r) => r.completeness === st).length;
  const items = [
    { label: '张数', value: rows.length },
    { label: '合计', value: fmtMoney(total), cls: 'accent' },
    { label: '信息完整', value: n('complete'), cls: 'ok' },
    { label: '信息待补充', value: n('missing'), cls: 'warn' },
  ];
  el.wbKpis.innerHTML = items.map((it) =>
    `<div class="kpi ${it.cls || ''}"><span class="kpi-label">${esc(it.label)}</span>`
    + `<span class="kpi-value">${esc(String(it.value))}</span></div>`).join('');
}

/* ── 解析状态（用户 2026-10-12 第 ⑨ 条）───────────────────────────────────
   这一列以前叫「核对」、显示的是**抬头比对**的结论（通过 / 不符 / 待确认）。
   用户原话的意思是：那不对，这一列应该叫「解析状态」，只有完成 / 待补全 / 失败三档。
   真源是后端行字段 `parse_status`（'done' | 'todo' | 'failed'）。
   `parse_status` 可能还没上线 → 回退**客户端猜测**：票面文字层读不出来
   （`row.unreadable`，与日期列那个「不可读」标记同源）就是「待补全」，
   其余能正常解析出票面字段的当「完成」。**有字段一定优先用字段**，不覆盖后端口径。 */
const PARSE_NAME = { done: '完成', todo: '待补全', failed: '解析失败' };
/* 排序权重：失败 → 待补全 → 完成。配 `wbVisible()` 里的 parse_status 分支使用。 */
const PARSE_RANK = { failed: 0, todo: 1, done: 2 };

function wbParseStatus(r) {
  const raw = r.parse_status || (r.detail || {}).parse_status;
  if (raw === 'done' || raw === 'todo' || raw === 'failed') return raw;
  if (r.unreadable) return 'todo';
  return 'done';
}

/** 「发票类型」列的取值链（用户第 ⑨ 条第 7 款）：**品名中类**（`r.mid`，只有
    「其他」票种的行才有）→ **票种**（`r.type`：火车票 / 住宿票 / 客运服务 / 机票…）
    → 票面标签（`r.label`）→ 后端 kind 的显示名 → 「发票」。
    **每一行都必须落到一个有内容的字符串** —— 用户原话「现在很多火车票、住宿票等
    都不显示标签，要求全部显示标签」，所以这里没有留空的分支，兜到最后一定写「发票」。

    ⚠️ 两个顺序都是 2026-10-12 定死的，别对调：
    ① `r.mid` 必须排在 `r.label` 前面 —— `mid` 才是品名中类，`label` 是**票面原文**
       （「蓄电池 / 美容护肤品 / 纸制品」这种商品名），顺序反了会把商品名当票种
       打进这一列（`tests/verify_v04_batch1.mjs:778-779` 的两条断言专盯这个）。
    ② `r.type` 必须排在 `r.label` 前面 —— 242 份真实样本实测，两者**只对出租车票
       有差别**（`type`=客运服务，`label`=客运票 / 打车票），其余票种逐字相同。
       用户第 ③ 条已把「打车票 + 客车票」合并成「客运服务」，这一列若显示 `label`
       就会冒出「打车票」这种与上方 chip 打架的旧名。 */
function wbTypeLabel(r) {
  return String(r.mid || r.type || r.label || CAT_NAME[r.kind] || '').trim() || '发票';
}

/** 主表格。行高与列宽由 CSS 控制（32px），这里只管内容。 */
function wbRenderTable(rows) {
  if (!rows.length) {
    el.wbBody.innerHTML = `<tr><td colspan="10"><div class="empty">`
      + `${wb.rows.length ? '没有符合当前筛选条件的发票' : '还没有发票'}</div></td></tr>`;
    return;
  }
  el.wbBody.innerHTML = rows.map((r) => {
    const cat = wbCat(r);
    const ty = wbTypeLabel(r);
    const ps = wbParseStatus(r);
    /* 解析状态的悬停说明：后端口径（`parse_status` 真的存在）与客户端猜测分开措辞，
       免得把「后端还没上这个字段」说成「后端说这行有问题」。 */
    const psTip = r.parse_error || (ps === 'failed' ? '票面解析失败'
      : ps === 'todo'
        ? (r.unreadable
            ? '票面文字层里没有发票号码和开票日期，可能需要人工核对'
            : '还有票面字段没解析出来，可能需要补全')
        : '票面字段已解析完整');
    const dead = isDiscarded(r);       // 挂不上的非电子发票：压暗 + 「废弃」徽章
    /* 勾选态 = 「没在反选集里」（用户第 6 条：可见即已选），默认就是勾上的。
       `data-pick` 存的是 **path** 而不是 id —— 见 wb.unsel 的说明。 */
    const picked = !wb.unsel.has(r.path);
    const cls = [picked ? 'sel' : '', wb.activeId === r.id ? 'active' : '',
                 dead ? 'discarded' : ''].filter(Boolean).join(' ');
    return `<tr data-id="${r.id}" class="${cls}">`
      + `<td class="col-pick"><input type="checkbox" data-pick="${esc(r.path)}"`
      + `${picked ? ' checked' : ''}></td>`
      + `<td class="col-dot"><span class="kdot" data-cat="${esc(cat)}" `
      + `title="${esc(CAT_NAME[cat])}${r.label ? ' · ' + esc(r.label) : ''}"></span></td>`
      /* 文件名单元格（用户第 ⑨ 条第 7 款 d 项）：**扩展名徽章整块删掉** ——
         用户原话「导入进来的发票文件名前面全都带一个 PDF 或者 OFD，不要这个」。
         是删除不是隐藏：留一个 display:none 的 PDF 前缀，早晚会被谁摸回来。 */
      + `<td class="col-fname" title="${esc(r.filename)}">`
      + `<span class="fname">${esc(r.filename)}</span>`
      /* 废弃原因直接写在标注上（用户 2026-10-10：「标注废弃原因为非发票」）——
         只写「废弃」两个字，用户得悬停才知道为什么，等于没标。 */
      + (dead ? `<span class="badge-dead" title="${esc(deadTip(r))}">${esc(deadLabel(r))}</span>` : '')
      + `</td>`
      /* ★ 新增「发票类型」列（用户第 ⑨ 条第 7 款 c 项）—— 原来挂在文件名后面的
         那枚开放标签（`.lbl`）搬到这里，成为**每一行都有**的独立列。
         取值链见 `wbTypeLabel(r)`：`r.mid`（品名中类）→ `r.type`（票种）→
         `r.label`（票面标签）→ kind 显示名 → 「发票」；两个「谁排前面」的由来与坑
         都写在 `wbTypeLabel` 的注释里，改这一列之前先看那段。
         用户原话「现在很多火车票、住宿票等都不显示标签，要求全部显示标签」，
         所以这里没有留空的分支。**显示到哪一层（用户第 7 条）**：2026-10-11 前这里
         原样打印票面明细名，于是「蓄电池 / 计算机配套产品」这种**商品名**跟
         「火车票 / 住宿票」并排站一列，用户会以为「蓄电池」是一种票。现在优先显示
         **品名中类**（`r.mid`），没有中类的行才退到票种，**票面原文一点没丢**：
         悬停 title 是「中类 · 原文」，点行进详情栏看「票面明细」（`renderDetail`）。
         ⚠️ 元素类名是 `ty` 与 `lbl` **两个**：`#wbGrid td.col-type .ty` 负责新样式，
         `.lbl` 是给 `tests/verify_v04_batch1.mjs:778-779` 兜底的 —— 那条冻结的测试
         用 `#wbBody .lbl` 的文本断言「只显示中类」，本轮口径未变、测试没改。 */
      + `<td class="col-type"><span class="ty lbl" `
      + `title="${esc(r.mid ? `${r.mid} · ${r.label || r.type || ''}` : ty)}">`
      + `${esc(ty)}</span></td>`
      /* ② 用户 2026-10-11：票面文字层里既没有发票号码、也没有开票日期时
         （后端新字段 `row.unreadable`），日期列**不再留一个空格子** ——
         空格子跟「解析失败」分不清。用 --warn 色的小标记 + title 说清原因。
         真的解析出了日期就照旧显示日期（两种情形互斥，标记只在没日期时出现）。 */
      + `<td class="col-date">${r.date ? esc(r.date) : (r.unreadable
          ? '<span class="wb-unread" title="票面文字层里没有发票号码和开票日期，可能需要人工核对">不可读</span>'
          : '<span class="muted">—</span>')}</td>`
      + `<td class="col-seller" title="${esc(r.seller)}">`
      + `${r.seller ? esc(r.seller) : '<span class="muted">—</span>'}</td>`
      /* 废弃行显式写 ¥0.00 —— 它跟「没解析出金额」不是一回事：
         这个 0 是后端判废后主动清零的结果，写清楚才不会被人当成解析失败。 */
      + `<td class="col-amount"><span class="amount${r.amount ? '' : ' zero'}">`
      + `${dead ? fmtMoney(0) : (r.amount ? fmtMoney(r.amount) : '—')}</span></td>`
      /* ★「解析状态」列（用户第 ⑨ 条）：完成 / 待补全 / 失败。
         **不再出现「未核对 / 通过 / 不符」** —— 那是抬头比对的结论（后端 `check`），
         跟「这张票解析干净没有」是两件事，它留在详情面板的「核对结论」块里。
         用 `.pst` 前缀而不是复用 `.st`：`.st.todo` 已经被「处理」列占用为中性灰
         （待处理），而这里的 todo 是「待补全」、要点琥珀色 —— 同名一定串色。 */
      + `<td class="col-check"><span class="pst ${esc(ps)}" `
      + `title="${esc(psTip)}">${esc(PARSE_NAME[ps] || '—')}</span></td>`
      + `<td class="col-info">${journeyStatusHtml(r)}</td>`
      + '<td class="col-detail-toggle" aria-hidden="true"></td>'
      + `</tr>`;
  }).join('');

  el.wbThead.querySelectorAll('th.sortable').forEach((th) => {
    th.classList.remove('asc', 'desc');
    if (th.dataset.sort === wb.sortKey) th.classList.add(wb.sortDir === 1 ? 'asc' : 'desc');
  });
}

/** 「处理」列的单行状态。未执行过 = `todo`（不是留空，也不是 `—`）：
    空着会让人以为「这列坏了」，写「待处理」才是可读的默认值。 */
function wbProcHtml(r) {
  const p = wb.proc.get(r.path) || 'todo';
  const tip = p === 'skip' ? '本步按策略跳过（同票重复 / 目标已存在 / 缺前置条件）' : '';
  return `<span class="st ${esc(p)}" title="${esc(tip)}">${esc(PROC_NAME[p] || PROC_NAME.todo)}</span>`;
}

/** 只重画「处理」列的 5 态，**不重建整表**。
    与 `wbRefreshSel` 同一个理由：`wbRenderTable` 会替换 `#wbBody.innerHTML`，
    把用户手里的复选框节点销毁、还会把滚动位置弹回顶部。
    流水线在跑的时候每一行都要闪一次状态，整表重建会闪得没法用。
    行内 `tr[data-id]` 与 `wb.rows` 通过 id 对应（id = 数组下标，见 wbLoad）。 */
function wbRefreshProc() {
  el.wbBody.querySelectorAll('tr[data-id]').forEach((tr) => {
    const td = tr.lastElementChild;
    if (!td || !td.classList.contains('col-proc')) return;
    const r = wb.rows.find((x) => x.id === Number(tr.dataset.id));
    if (r) td.innerHTML = wbProcHtml(r);
  });
}

/** 「开始 (N)」的计数 + 按钮可用性。文案由**同一处**算，避免按钮上的数字和左边统计打架。 */
function wbRenderBar() {
  const sel = wbSelRows();
  const total = sel.filter((r) => !isDiscarded(r))
    .reduce((s, r) => s + (Number(r.amount) || 0), 0);
  el.wbActionInfo.innerHTML = `已选 <b>${sel.length}</b> 张 · 合计 `
    + `<span class="hl">${fmtMoney(total)}</span>`
    + (sel.length < wb.rows.length ? ` <span class="muted">（共 ${wb.rows.length} 张）</span>` : '');

  // `(N)` = 当前勾选行数，动态更新（wbRefreshSel → wbRenderBar，勾一个变一次）
  el.wbRunLabel.textContent = `开始 (${sel.length})`;
  el.wbRunBtn.disabled = wbRunBusy || !sel.length;
  el.wbTripBtn.disabled = !wbTripRows('all').length;
  if (el.wbTripBtn.disabled) wbSetTripMenu(false);
  else if (!el.wbTripMenu.hidden) wbRenderTripMenu();
}

/* ══════════════════════════════════════════════════════════════
   底栏流水线执行（v0.4 批次 2）
   ──────────────────────────────────────────────────────────────
   一次 `POST /api/pool/run` 跑完，顺序由**后端**刚性决定：
     查重 → 核对 → 重命名 → 合并
   前端只负责：① 请求前把选中行置 running；② 请求中在底栏按阶段滚提示；
   ③ 请求后把 stages/rows/skipped 翻译成逐行状态 + 汇总框。
   ⚠️ 后端**没有流式接口**（只有 /api/pool/run/status 轮询，本批不用），
   所以「正在核对…」这类文案只是**按阶段顺序的乐观提示**，不代表服务端真的走到那一步。
   ══════════════════════════════════════════════════════════════ */

/** 底栏右侧的说明文字：执行中就显示阶段文案，空闲就说明将要跑哪几步。 */
function wbRenderRunNote() {
  if (wbRunBusy) return;                       // 执行中由 wbSetStage 接管，别覆盖它
  const on = wbPipelineActions();
  const names = PIPELINE_ORDER.filter((k) => on[k]).map((k) => STAGE_NAME[k]);
  /* ⑩ 没选输出目录不再拦人执行 —— 那就得**明说**会落到哪：后端默认目录 = 当前用户真实桌面下的
     「发票处理输出」（保存文件时才创建）。填了目录才显示路径，避免这行字太长挤爆底栏。 */
  const dirTip = (el.gOutDir.value || '').trim() ? '' : ' · 输出目录：默认（桌面\\发票处理输出）';
  el.wbRunNote.textContent = (names.length
    ? `将按固定顺序执行：${names.join(' → ')}`
    : '一个核对待办都没勾，开始前请至少勾一项') + dirTip;
}

/** 读底栏四个开关 → `actions` 对象（字段名与后端契约一致）。 */
function wbPipelineActions() {
  const on = (node) => !!node && node.getAttribute('aria-pressed') === 'true';
  return {
    check: on(el.wbTglCheck),
    rename: on(el.wbTglRename),
    dedup: on(el.wbTglDedup),
    merge: on(el.wbTglMerge),
  };
}

/** 阶段文案 + 「当前处理 N/共 M」进度。点「开始」后**同步**先调一次
    （不能只靠 `setTimeout` 滚：后端在本地机器上有可能几十毫秒就返回了，
    那样用户一个阶段文案都看不到，会以为按钮没反应）。 */
function wbSetStage(step, cur, total) {
  el.wbRunNote.textContent = (PIPELINE_DOING[step] || '正在处理…')
    + (total ? `（${cur || 0}/${total}）` : '');
}

/** 「已选」= 当前筛选结果减去用户手动取消的那些行（用户第 6 条）。
    ⚠️ 用 path 做键，**不用 id**：id 是 wb.rows 的下标，每次 wbLoad 都会重排。 */
function wbSelRows() {
  return wbVisible().filter((r) => !wb.unsel.has(r.path));
}

function wbSetProc(paths, state) {
  (paths || []).forEach((p) => wb.proc.set(p, state));
}

/** 把后端回写的 `run_status` 翻成「处理」列的状态。
    后端取值见 `routes/pipeline.py`（dedup / rename 两步各自写自己的牌）。
    ⚠️ 用 path 匹配，**不用 id**：去重会把同票副本整行移出，id 会整体重排。 */
function wbRowProcFromRun(r) {
  const st = String(r.run_status || '');
  if (st === 'dup_skipped') return 'skip';
  if (st === 'rename_same' || st === 'rename_skipped') return 'skip';
  if (st === 'rename_failed') return 'fail';
  const checked = (r.check || {}).status;
  if (checked === 'fail') return 'fail';
  return 'done';                               // 核对 unknown / review 也算「跑过了」，不是失败
}

/** 「开始」后的全屏进度（用户第 ⑤ 条第 1 款）：与导入共用同一层皮
    （`wbProgFs` + `#wbProg.fs`）。

    2026-10-12（第 7 轮 ①）接上**真进度**：`GET /api/pool/run/status` 现在可以不传
    `run_id`（回最近注册的那一次，见 `server\routes\pipeline.py::run_status` 的说明），
    响应体带 `ok:true / total / index / current / stage`，所以这里能画与导入同款的
    真百分比。这条链上原来有两个断点，都已修：① 后端只回裸的 `{stage,index,...}`，
    没有 `ok` ⇒ 前端 `data.ok !== true` 把真数据当失败；② 前端省略 `run_id` ⇒ 400。

    兜底不变：404 / ok!==true / found:false 连续两次（且还没画过真进度）就放弃轮询、
    停在不定量扫光态，不会卡死。**能拿到真数字就用真的，拿不到就不假装有。** */
function wbRunProgStart(n, label, runId) {
  let stopped = false;
  let timer = 0;
  let miss = 0;
  let live = false;                     // 已经拿到真实进度 → 停掉不定量态
  let sawLive = false;                  // 见过**本次**运行「还没跑完」的快照
  if (!el.wbProg) {                     // 进度条 DOM 没就位就别硬撑，退回旧遮罩
    showOverlay(label || '正在执行…');
    return { stop() { hideOverlay(); } };
  }
  /* `#wbProg` 是底栏的子节点，祖先 `display:none` 连 `position:fixed` 的后代一起吃掉。 */
  if (el.wbActionbar) el.wbActionbar.hidden = false;
  wbProgFs(true);
  el.wbProg.classList.add('indet');
  if (el.wbProgBar) el.wbProgBar.style.width = '';   // 不定量态的宽度由 CSS 动画接管
  if (el.wbProgPct) el.wbProgPct.textContent = `${n} 张`;
  if (el.wbProgFile) { el.wbProgFile.textContent = ''; el.wbProgFile.hidden = true; }
  if (el.wbProgText) el.wbProgText.textContent = label || '正在执行…';

  /* ⚠️ 「已经拿到真进度」和「停止轮询」是两件事（2026-10-12 第 7 轮 ①修）：
     拿到真进度之后必须**接着**轮询，否则进度条只跳一下就把那一刻冻住
     （用户要的是「进度条真的随进度动」）。唯一的停止条件就是 `stopped` ——
     由流水线 POST 结束时调的 `stop()` 置位。 */
  const tick = async () => {
    if (stopped) return;
    let status = 0;
    let data = null;
    try {
      const r = await api(`${API.poolRunStatus}?run_id=${encodeURIComponent(runId)}`);
      status = r.status;
      data = r.data;
    } catch (e) { return; }             // 网络层拿不到 → 静默放弃轮询，不定量态照旧
    if (stopped) return;
    if (status === 404 || !data || data.ok !== true || data.found === false) {
      if (live) {                       // 已经在画真进度了 → 别因为一次抖动就退回去
        timer = setTimeout(tick, WB_PROG_POLL_MS);
        return;
      }
      miss++;
      if (miss >= 2) return;
      timer = setTimeout(tick, WB_PROG_POLL_MS);
      return;
    }
    miss = 0;
    const total = Number(data.total) || 0;
    /* 第一次轮询很可能赶在本次 run `register()` 之前，落到**上一次**运行留下的
       `done:true` 快照上。那不是本次的进度，跳过它、继续等本次注册 ——
       否则会把上一轮的终值当成这一次的起点画出来。 */
    if (total > 0 && !(data.done && !sawLive)) {
      live = true;
      sawLive = true;
      el.wbProg.classList.remove('indet');
      /* 阶段名走 STAGE_NAME（与底栏 idle 文案同一份）—— 直接打印后端的英文
         `merge/check` 用户看不懂。 */
      wbProgPaint(data.index, total, data.current || '',
                  STAGE_NAME[data.stage] || data.stage || '正在执行');
    } else if (data.stage && el.wbProgText) {
      el.wbProgText.textContent = data.stage;   // 只给阶段名也行，照实显示
    }
    if (!data.done || !sawLive) timer = setTimeout(tick, WB_PROG_POLL_MS);
  };
  timer = setTimeout(tick, WB_PROG_POLL_MS);

  return {
    stop() {
      if (stopped) return;
      stopped = true;
      if (timer) clearTimeout(timer);
      el.wbProg.classList.remove('indet');
      if (el.wbProgBar) el.wbProgBar.style.width = '0%';   // 不定量态的内联宽度不残留
      wbProgFs(false);
      el.wbProg.hidden = true;
    },
  };
}

/** 真正点「开始 (N)」：全屏进度 → 逐行状态 → **新标签页里的 HTML 核对报告**。 */
async function wbRunPipeline() {
  if (wbRunBusy) return;
  const rows = wbSelRows();
  if (!rows.length) { toast('先勾选要处理的发票', 'info'); return; }

  const actions = wbPipelineActions();
  if (!PIPELINE_ORDER.some((k) => actions[k])) {
    toast('请至少勾选一项处理（查重 / 核对抬头 / 重命名 / 合并 PDF）', 'info');
    return;
  }

  /* 输出目录取 #gOutDir —— 旧模块的「输出目录」输入框。它可以**空着**：
     ⑩ 的新口径是「空则用后端默认目录」，也就是**当前用户真实桌面下的 `发票处理输出`**
     （保存文件时才创建，并在 scan 响应里通过 `default_out` 把真实路径回给前端）。
     这里**不往 gstate.outDir 写回**：gstate 是四个旧模块的共享状态，工作台的一次执行
     不该改动「智能重命名」模块的输出目录。 */
  const outDir = (el.gOutDir.value || '').trim();

  /* ★ 第 ⑤ 条第 3 款：结果不再是桌面 PDF、也不再是核对结果弹窗，而是**新标签页里的
     HTML 核对报告**。浏览器会拦截「异步之后」才发起的 window.open（旧 gRun 的注释里
     踩过这个坑），所以在点击的**同步阶段**先占一个空标签，拿到 URL 后再导航过去。
     只有前面两道校验都过了才占位：被 toast 拦下的点击不该凭空多出一个空白标签页。 */
  const tab = window.open('', '_blank');
  /* 全屏进度里写清真要跑哪几步（与底栏 idle 文案同一份 STAGE_NAME）。 */
  const stageNames = PIPELINE_ORDER.filter((k) => actions[k]).map((k) => STAGE_NAME[k]);
  const runId = crypto.randomUUID();

  wbRunBusy = true;
  const paths = rows.map((r) => r.path);
  wbSetProc(paths, 'running');                 // 立刻置「处理中」，不等后端
  wbRefreshProc();
  wbSetStage(PIPELINE_ORDER.find((k) => actions[k]), 0, 0);
  el.wbRunBtn.disabled = true;
  /* ★ 第 ⑤ 条第 1 款：与导入同款的全屏进度遮罩（`#wbProg.fs`），盖住整个视口。 */
  const prog = wbRunProgStart(rows.length, `正在执行：${stageNames.join(' → ')}`, runId);

  try {
    const { data } = await api(API.poolRun, {
      method: 'POST',
      body: JSON.stringify({
        scan_id: wb.scanId,
        run_id: runId,
        selected_ids: rows.map((r) => r.id),
        // 空字符串 = 让后端用它自己的默认目录（当前用户真实桌面下的「发票处理输出」）
        out_dir: outDir,
        actions,
        check: {
          expected_name: el.gExpName.value.trim(),
          expected_tax_id: el.gExpTax.value.trim(),
        },
        /* ⑪ 重命名方式跟着设置页的分段控件走：
           'copy'    = 复制原件（原件不动，副本进输出目录）—— 默认值
           'inplace' = 修改原件（直接改磁盘上原件自己的文件名，内容不动）
           默认仍是 copy：没进过设置页时这里与改前完全一致，不会因为放开而改变既有行为。 */
        rename: { mode: sePrefs.renameMode === 'inplace' ? 'inplace' : 'copy' },
        failure_policy: 'skip',
        dry_run: false,
      }),
    });

    if (!data.ok) {
      /* ── 优雅降级 ──
         /api/pool/run 与 /api/pool/scan 是同批两位工程师并行开发的：
         它可能还没上线（404 / 405 / 后端异常）。这里**必须**退回可读提示，
         绝不能把整页抛崩，也不能把行永远留在「处理中」。 */
      wbSetProc(paths, 'todo');
      wbRefreshProc();
      wbRenderRunNote();
      closeTab(tab);                             // 失败了就别把人扔在一个空白标签页上
      wbShowRunFail(data.error || '后端未返回处理结果', outDir, paths.length);
      return;
    }

    /* ── 逐行状态回写 ──
       先按 path 把本次涉及的行全部对出来并**默认置 fail**，再用后端数据覆盖：
       契约说 `rows` 会回写「全部选中行（含被跳过的）」，万一某行没回来，
       默认 fail 是更安全的读法 —— 成功得由后端明确说，不能由缺失默认成功。 */
    const byPath = new Map();
    rows.forEach((r) => { byPath.set(r.path, r); wb.proc.set(r.path, 'fail'); });

    let hit = 0;
    for (const rr of (data.rows || [])) {
      /* ★ 第 ⑥ 条修正（「点票面预览提示文件不可用」的根因就在这三行）：
         `rename.mode='inplace'`（修改原件）时后端 `os.rename` 挪了文件，
         `rr.path` 是**改名之后**的新路径，而本地行里存的还是扫描时的旧路径 ——
         直接拿 `rr.path` 查 `byPath` 必然落空 ⇒ `continue`，
         行状态不更新、**旧路径继续钉在行上**；于是点预览时
         `/api/pool/file?path=<旧路径>` 命中 `os.path.isfile` 失败分支返回 404，
         前端就把 `{"ok":false,"error":"文件不可用…"}` 这坨 JSON 显示给人看。
         后端会回 `original_path`（= 扫描期路径，copy 模式下与 path 相同），
         用它来认行；认到之后**把本地 path 换成磁盘现状**，预览才打得开。
         两个模式都成立，且不再"猜"哪个键优先。 */
      const key = rr.original_path || rr.path || rr.filepath;
      const orig = key ? byPath.get(key) : null;
      if (!orig) continue;
      const oldPath = orig.path;
      Object.assign(orig, rr, { id: orig.id, path: rr.path || orig.path });   // id 以本地为准，path 以磁盘现状为准
      if (orig.path !== oldPath) {
        wb.proc.delete(oldPath);                     // 旧路径的默认 fail 别留在表里
        if (wb.sourcePaths.delete(oldPath)) wb.sourcePaths.add(orig.path);
        if (wb.unsel.delete(oldPath)) wb.unsel.add(orig.path);
        const overrides = tripOvAll();
        if (Object.prototype.hasOwnProperty.call(overrides, oldPath)) {
          overrides[orig.path] = overrides[oldPath];
          delete overrides[oldPath];
          tripOvWrite(overrides);
        }
      }
      wb.proc.set(orig.path, wbRowProcFromRun(rr));
      hit++;
    }
    if (!hit) {
      /* 后端只回了 stages 没回 rows（契约漂移）：不假装逐行成功，
         退化成「跑完了」而不是逐行编状态。 */
      wbSetProc(paths, 'done');
    }
    wbRefreshProc();

    // 逐行状态变了，顺手把详情侧栏刷一遍（引用的是同一个行对象）
    if (wb.activeId != null) renderDetail(wb.rows.find((r) => r.id === wb.activeId));
    if (data.vault_failed === null) {
      toast('发票库入库结果未确认，请查看报告中的警告', 'err', 6000);
    } else if (Number(data.vault_failed) > 0) {
      toast(`${data.vault_failed} 个文件未能保存到发票库，请查看报告中的警告`, 'err', 6000);
    }

    /* ★ 第 ⑤ 条第 2、3 款：**不再弹 `#resultModal`、不再下载 PDF**，
       改成在新标签页里看新的 HTML 核对报告。
       冻结契约：成功负载带 `report_url`（新 HTML 报告）与 `kind:"html"`；
       缺失时回退既有的 `preview_url`；两者都没有才退回旧行为（弹窗汇总）。
       ⚠️ 这里必须 `return`：`#resultModal` 一旦被打开，用户说的
       「不要再弹出核对结果的弹窗」就白改了。 */
    const resultActions = data.actions || actions;
    const resultTitle = PIPELINE_ORDER.filter((k) => resultActions[k])
      .map((k) => STAGE_NAME[k]).join('、') + '结果';
    const reportUrl = data.report_url || data.preview_url;
    if (reportUrl) {
      gotoPreview(tab, reportUrl);
      if (data.vault_failed !== null && !Number(data.vault_failed)) {
        toast(`${resultTitle}已在新标签页打开`, 'ok');
      }
      return;
    }
    closeTab(tab);
    wbShowRunSummary(data, outDir, rows.length, resultActions);
  } catch (e) {
    /* 网络层失败（接口还没上线时多半就是这里：fetch 到 404 也可能拿到非 JSON） */
    wbSetProc(paths, 'todo');
    wbRefreshProc();
    wbRenderRunNote();
    closeTab(tab);
    wbShowRunFail('请求失败：' + e.message, outDir, paths.length);
  } finally {
    prog.stop();                               // 全屏进度：成功 / 失败 / 异常三条路都收
    wbRunBusy = false;
    wbRenderBar();                             // 还原按钮可用性与 (N) 计数
  }
}

/** 失败提示：沿用 #resultModal（和 gShowResult 同一套外壳），
    失败时隐藏「打开输出目录」——没有产物，点开只会看到一个空目录。 */
function wbShowRunFail(msg, outDir, n) {
  el.resultIcon.className = 'modal-icon err';
  el.resultIcon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>';
  el.resultTitle.textContent = '流水线未执行';
  el.resultBody.innerHTML = `
    <div class="kv"><span>选中张数</span><b>${n} 张</b></div>
    <div class="muted" style="line-height:1.7;margin:10px 0 4px">${esc(msg)}</div>
    <div class="path">可能原因：后端 <code>POST /api/pool/run</code> 还没部署，或服务未重启。
      这一步已安全退出，发票文件、列表与勾选都没有被改动。</div>`;
  el.openFolderBtn.style.display = 'none';
  el.resultModal.hidden = false;
  toast('流水线未执行：' + msg, 'err');
}

/** 汇总框：各步通过/失败/跳过数、合并产物、跳过清单、最终合计。
    复用 #resultModal 的同一套 DOM（`#resultIcon/#resultTitle/#resultBody/#openFolderBtn`）。 */
function wbShowRunSummary(data, outDir, selectedCount, actions) {
  const st = data.stages || {};
  const ck = st.check || {}, dd = st.dedup || {}, rn = st.rename || {}, mg = st.merge || {};
  const skipped = data.skipped || [];
  const warnings = (data.warnings || []).filter((w) =>
    !PIPELINE_ORDER.some((k) => !actions[k] && String(w).startsWith(`${STAGE_NAME[k]}：`)));
  const resultTitle = PIPELINE_ORDER.filter((k) => actions[k])
    .map((k) => STAGE_NAME[k]).join('、') + '结果';
  /* ⑩ `outDir` 可以是空字符串 —— 空 = 本次用的是**后端的默认目录**
     （当前用户真实桌面下的「发票处理输出」）。后端若在响应里回了真实路径就优先用它，
     否则至少要写清「默认目录」，不能在汇总框里印一行空白、也不能给一个点不动的按钮。 */
  const dir = outDir || data.out_dir || data.default_out || '';

  const stageRow = (name, okN, failN, skipN) =>
    `<div class="kv"><span>${name}</span><b>`
    + `<span style="color:var(--ok)">${okN}</span> / `
    + `<span style="color:var(--err)">${failN}</span> / `
    + `<span style="color:var(--text-3)">${skipN}</span>`
    + `</b></div>`;

  const checkRow = stageRow('① 核对结果（通过 / 不符 / 待确认）',
    ck.pass || 0, ck.fail || 0, ck.review || 0);
  const renameRow = stageRow('③ 重命名（成功 / 失败 / 跳过）',
    rn.renamed || 0, rn.failed || 0, rn.skipped || 0);
  const dedupRow = `<div class="kv"><span>② 查重</span><b>`
    + `代表件 ${dd.representatives || 0} 张 · 剔除重复 <span style="color:var(--warn)">${dd.dups_skipped || 0}</span> 张</b></div>`;

  const mergedPath = mg.merged_path || '';
  const mergeRow = `<div class="kv"><span>④ 合并</span><b>`
    + (mergedPath
        ? `${mg.page_count || 0} 页 · 失败 ${(mg.failed || []).length} 张`
        : '<span class="muted">没有生成合并文件</span>')
    + `</b></div>`;

  // 跳过清单：**原因 + 是否需人工**都要写出来，否则「跳过 N 张」等于没说
  const skipList = skipped.length
    ? `<div class="detail-sec"><h4>跳过清单（${skipped.length}）</h4><ul class="dup-list">`
      + skipped.map((s) => {
          const who = s.filename ? baseName(s.filename) : (s.id != null ? `行 ${s.id}` : '—');
          const human = s.need_human
            ? ' <b style="color:var(--warn)">需人工</b>' : '';
          return `<li><b>${esc(who)}</b>：${esc(s.step || '')} · ${esc(s.reason || '')}${human}</li>`;
        }).join('')
      + `</ul></div>`
    : '';

  const warnList = warnings.length
    ? `<div class="detail-sec"><h4>提示（${warnings.length}）</h4><ul class="dup-list">`
      + warnings.map((w) => `<li>${esc(w)}</li>`).join('') + `</ul></div>`
    : '';

  el.resultIcon.className = 'modal-icon ok';
  el.resultIcon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';
  el.resultTitle.textContent = resultTitle;
  el.resultBody.innerHTML = `
    <div class="kv"><span>本次选中</span><b>${selectedCount} 张${
      (data.elapsed_ms || 0) ? ` · 耗时 ${(Number(data.elapsed_ms) / 1000).toFixed(1)}s` : ''}</b></div>
    ${actions.dedup ? dedupRow : ''}${actions.check ? checkRow : ''}${actions.rename ? renameRow : ''}${actions.merge ? mergeRow : ''}
    <div class="kv"><span>最终合计金额</span><b class="hl">${fmtMoney(data.total_amount)}</b></div>
    ${mergedPath ? `<div class="path">${esc(mergedPath)}</div>`
      : `<div class="path">${esc(dir || '默认输出目录：当前用户桌面\\发票处理输出')}</div>`}
    ${skipList}${warnList}`;

  /* 「打开输出目录」按钮：bindEvents 里那条处理的是旧模块（用 state/gstate 的 outDir），
     工作台用自己这次真正落盘的目录。这里临时换掉点击行为，按钮用完不 restore —— 
     因为旧模块每次进 switchModule 都会重绑。
     ⚠️ 连目录都不知道（空 outDir 且后端没回真实路径）时**隐藏**按钮：
     点开一个猜出来的目录比不给按钮更糟。 */
  el.openFolderBtn.style.display = dir ? '' : 'none';
  el.openFolderBtn.onclick = async () => {
    if (!dir) return;
    const { data: d } = await api(API.openFolder, { method: 'POST', body: JSON.stringify({ dir }) });
    if (!d.ok) toast(d.error || '打开失败', 'err');
  };

  el.resultModal.hidden = false;
  toast(`${resultTitle}已生成`, 'ok');
}

/* ── 第二条独立路径：生成行程单 ────────────────────────────────
   「工作台全部」忽略筛选与勾选；「当前勾选」沿用 wbSelRows 的可见勾选语义。
   两种范围都只包含交通与住宿票，不改变工作台已有筛选。
   v0.5：交通票与住宿票**不再分两个接口、开两个标签页** —— 统一走一个合并接口
   `POST /api/trip/run`（一份 paths 进去，一张合一的行程单出来），因此占位标签页
   也从「每个接口一个」收敛成「只占一个」。
   复用 gRun() 的模式：点击的同步阶段先占一个空标签 —— 浏览器会拦截
   「异步之后」才发起的 window.open，不先占位就看不到行程单。 */
function wbTripRows(scope) {
  const source = scope === 'all' ? wb.rows : wbSelRows();
  return source.filter((r) => JOURNEY_KINDS.has(String(r.kind || '')) && !isDiscarded(r));
}

function wbRenderTripMenu() {
  for (const [scope, label] of [
    ['all', '工作台全部交通与住宿票'],
    ['selected', '仅当前勾选的行程票'],
  ]) {
    const count = wbTripRows(scope).length;
    const button = el.wbTripMenu.querySelector(`[data-trip-scope="${scope}"]`);
    button.disabled = count === 0;
    button.querySelector('strong').textContent = `${label}（${count} 张）`;
  }
}

function wbSetTripMenu(open) {
  if (open) wbRenderTripMenu();
  el.wbTripMenu.hidden = !open;
  el.wbTripBtn.setAttribute('aria-expanded', String(open));
}

async function wbRunTrip(scope) {
  wbSetTripMenu(false);
  if (scope !== 'all' && scope !== 'selected') return;
  const rows = wbTripRows(scope);
  if (!rows.length) {
    toast(scope === 'all' ? '工作台内没有交通或住宿票' : '当前勾选的发票中没有交通或住宿票', 'info');
    return;
  }

  const paths = rows.map((r) => r.path);
  const outDir = (el.gOutDir.value || '').trim();

  /* 标签页必须**同步**开，见上面的说明。只有一个接口 → 只需要占一个位置；
     请求失败就把它关掉，不给用户留一个空白页。 */
  const tab = window.open('', '_blank');
  el.wbTripBtn.disabled = true;
  showOverlay('正在生成行程单…');
  try {
    const { data } = await api(API.tripRun, {
      method: 'POST',
      body: JSON.stringify({
        paths, out_dir: outDir,
        include_map: true, dedup: true,
        trip_overrides: collectTripOverrides(paths),
      }),
    });
    hideOverlay();
    /* 成功：把占位标签页直接导到 preview_url（`preview_url` 就是给
       window.open 用的现成 URL）。失败：关掉占位页 + 出声，**不**回退去调
       /api/train/run 或 /api/hotel/run —— 那两个接口是旧模块的单据线，
       回退只会让同一批票生成两张不一样的行程单。 */
    if (data && data.ok) {
      gotoPreview(tab, data.preview_url);
      toast(`行程单已生成：${data.filename || ''}`, 'ok');
      return;
    }
    closeTab(tab);
    toast((data && data.error) || '生成行程单失败', 'err');
  } catch (e) {
    hideOverlay();
    closeTab(tab);
    toast('请求失败：' + e.message, 'err');
  } finally {
    el.wbTripBtn.disabled = !wbTripRows('all').length;
  }
}

function wbSetDetailCollapsed(collapsed) {
  el.detailPanel.classList.toggle('collapsed', collapsed);
  el.detailPanel.hidden = collapsed || !wb.rows.length || state.mod !== 'workbench';
  const label = collapsed ? '展开详情页' : '折叠详情页';
  el.wbDetailToggle.title = label;
  el.wbDetailToggle.setAttribute('aria-label', label);
  el.wbDetailToggle.setAttribute('aria-expanded', String(!collapsed));
  el.wbDetailToggle.classList.toggle('is-collapsed', collapsed);
}

function wbRender() {
  const has = wb.rows.length > 0;
  el.wbEmpty.hidden = has;
  el.wbBar.hidden = !has;
  el.wbKpis.hidden = false;         // KPI 条常驻：无数据时也显示 0 张 / ￥0.00 / 通过 0 / 待确认 0
  el.wbFilters.hidden = !has;
  el.wbGridWrap.hidden = !has;      // 无数据时隐藏表格：只有表头、0 行的是死区
  el.wbActionbar.hidden = !has;
  /* v0.5：原来这里给左侧栏的「工作台」入口刷张数角标
     （`setBadge(el.badgeWorkbench, wb.rows.length)`）。左侧栏整块下线后没有角标可刷，
     `setBadge()` 本身保留 —— 其余四个旧模块的角标还在用。 */

  // 详情侧栏：无数据时整列收起（避免一整列黑）；有数据时尊重用户的折叠选择。
  wbSetDetailCollapsed(el.detailPanel.classList.contains('collapsed'));

  if (!has) {
    wbRenderKpis([]);               // 常驻 KPI：数值 0 就是 0，不造假
    el.wbChips.innerHTML = '';
    el.wbBody.innerHTML = '';
    renderDetail(null);
    return;
  }

  el.wbTip.textContent = `共 ${wb.rows.length} 张 · 拖入或点右侧按钮继续添加`;
  wbRenderChips();
  const rows = wbVisible();
  wbRenderKpis(rows);
  wbRenderTable(rows);
  /* 表头「全选」必须跟着筛选结果一起刷新 —— 用户第 6 条报的
     「点了『全部』下方打勾的还是那 85 条」，真凶就是这里：
     表头这个复选框从来没人重算过，标签切换后它一直停在上一次的状态上。 */
  el.wbPickAll.checked = rows.length > 0 && rows.every((r) => !wb.unsel.has(r.path));
  wbRenderBar();
  wbRenderRunNote();
}

/* ── ⑦ 解析进度：真进度条 + 轮询（用户 2026-10-11 上午第 ⑦ 条）──────────
   改前：导入 100 张只有一个转圈，用户不知道到第几张了。
   改后：请求体里先埋一个 `job_id`（`scan_<时间戳>_<随机>`），**并发地**每 400ms 问一次
   `GET /api/pool/scan/status?job_id=…`，把 index/total/current 画到底栏进度条上；
   POST 一返回（或请求失败）就停轮询、收进度条。
   降级（必须做）：status 接口现在**可能还没上线**（契约：404；或 `found:false`
   表示没见过这个 job_id）→ 静默退回原来的转圈遮罩，不卡死、不弹错误框。 */

/* 150ms：一次 3 秒的 run 能采到 20 个点，进度条才是「动」而不是「跳」。
   本机回环 + 内存快照，这个频率的开销可以忽略。 */
const WB_PROG_POLL_MS = 150;

/** 全屏进度开关（用户第 ① 条 / 第 ⑤ 条共用一层皮）。
    用户原话：「我想要的效果是全屏显示，即整个屏幕被盖住，只能显示进度条等待。」
    `#wbProg` 平时是底栏里那条 6px 细进度；`.fs` 一加上就 `position:fixed` 铺满视口
    （见 app.css `#wbProg.fs`）—— 冻结的四个 id 与内部 DOM 结构一个都不用改。
    ⚠️ `#wbProg` 是 `#wbActionbar` 的子节点，祖先 `display:none` 连 `position:fixed`
    的后代一起吃掉，所以底层调用方必须先把底栏放出来（见 wbProgStart 的 forcedBar）。 */
function wbProgFs(on) {
  if (!el.wbProg) return;
  el.wbProg.classList.toggle('fs', !!on);
  if (on) el.wbProg.hidden = false;
}

/** 画进度：宽度 / 百分比 / 文件名 / 计数器四处取自同一组数字，永远不会互相打架。 */
function wbProgPaint(index, total, current, label) {
  const i = Math.max(0, Number(index) || 0);
  const t = Math.max(0, Number(total) || 0);
  const pct = t > 0 ? Math.min(100, Math.round((i / t) * 100)) : 0;
  if (el.wbProgBar) el.wbProgBar.style.width = pct + '%';
  if (el.wbProgPct) el.wbProgPct.textContent = pct + '%';
  /* 当前文件名单独占一行、字号比计数器大：用户第 ① 条要的是
     「大号百分比 + 当前文件名 + 进度条」，文件名塞在计数器后面当小尾巴不算。 */
  if (el.wbProgFile) {
    const name = current ? String(current).split(/[\\/]/).pop() : '';
    el.wbProgFile.textContent = name;
    el.wbProgFile.hidden = !name;          // 没有文件名（如流水线阶段）就不占位
  }
  if (el.wbProgText) {
    const act = label || '正在解析';
    el.wbProgText.textContent = t > 0 ? `${act} 第 ${i} / ${t} 张` : `${act}…`;
  }
}

/** 开一次进度。返回 `{ stop() }`：停轮询 + 收进度条（若回退过，连遮罩一起收）。 */
function wbProgStart(jobId, fileCount) {
  let stopped = false;
  let timer = 0;
  let miss = 0;
  let fellBack = false;
  /* 首次导入时底栏还是 hidden（`wbRender` 里 `wbActionbar.hidden = !has`），
     `#wbProg` 就在底栏里 —— 临时把底栏让出来，收进度时再还回去。 */
  const forcedBar = !!(el.wbProg && el.wbActionbar && el.wbActionbar.hidden && !wb.rows.length);
  if (el.wbProg) {
    if (forcedBar) el.wbActionbar.hidden = false;
    wbProgFs(true);
    wbProgPaint(0, fileCount, '', '正在解析');
  } else {
    showOverlay(`正在解析 ${fileCount} 个文件…`);   // 进度条 DOM 没就位就别硬撑
    fellBack = true;
  }

  const fallback = () => {
    if (stopped || fellBack) return;
    fellBack = true;
    wbProgFs(false);
    if (el.wbProg) el.wbProg.hidden = true;
    showOverlay(`正在解析 ${fileCount} 个文件…`);   // 退回改前的加载态
  };

  const tick = async () => {
    if (stopped) return;
    let status = 0;
    let data = null;
    try {
      const r = await api(`${API.poolScanStatus}?job_id=${encodeURIComponent(jobId)}`);
      status = r.status;
      data = r.data;
    } catch (e) { fallback(); return; }             // 网络层拿不到 → 降级
    if (stopped) return;
    if (status === 404 || !data || data.ok !== true) { fallback(); return; }
    if (data.found === false) {
      /* 「没见过这个 job_id」只**连续**算数：POST 还在路上时第一次问多半就是 false，
         忍两次（约 800ms）再回退 —— 既不误判，也不会卡死。 */
      miss++;
      if (miss >= 2) { fallback(); return; }
    } else {
      miss = 0;
      wbProgPaint(data.index, data.total || fileCount, data.current || '', '正在解析');
    }
    timer = setTimeout(tick, WB_PROG_POLL_MS);
  };
  tick();

  return {
    stop() {
      if (stopped) return;
      stopped = true;
      if (timer) clearTimeout(timer);
      wbProgFs(false);
      if (el.wbProg) el.wbProg.hidden = true;
      if (fellBack) hideOverlay();                   // 回退时开的那层遮罩由这里收
      if (forcedBar && !wb.rows.length) el.wbActionbar.hidden = true;
    },
  };
}

/* ── 加载：一次解析抽全字段（R2）─────────────────────────────
   核对预设沿用「发票核对」模块里用户填的那份（同一份 localStorage），
   这样工作台的列和核对模块的结论口径一致。 */
let wbScanQueue = Promise.resolve();
function wbLoad(paths) {
  // 同时到达的导入请求按顺序重扫；后一批会看到前一批保存的 sourcePaths。
  const next = wbScanQueue.then(() => wbLoadNow(paths));
  wbScanQueue = next.catch(() => {});
  return next;
}
async function wbLoadNow(paths) {
  if (!paths || !paths.length) { toast('未选择任何发票文件', 'err'); return false; }
  // /run 只接受同一个 scan_id 对应的行下标。每次追加导入都重扫历次输入，
  // 使界面中的全部行和最新服务端快照保持同一顺序，包括被隐藏的同票副本。
  const allPaths = [...new Set([...wb.sourcePaths, ...paths])];
  /* ⑦ 先埋 job_id，再「并发」做两件事：POST 解析 + 每 400ms 轮询进度。 */
  const jobId = `scan_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  const prog = wbProgStart(jobId, allPaths.length);
  try {
    const { data } = await api(API.poolScan, {
      method: 'POST',
      body: JSON.stringify({
        paths: allPaths,
        expected_name: el.gExpName.value.trim(),
        expected_tax_id: el.gExpTax.value.trim(),
        job_id: jobId,                    // 契约新增的**可选**字段（后端不认识就当没给）
      }),
    });
    prog.stop();                        // POST 一回来就停轮询、收进度条
    hideOverlay();
    if (!data.ok) { toast(data.error || '解析失败', 'err'); return false; }

    /* 默认是当前用户真实桌面下的 `发票处理输出`，实际保存文件时才创建。
       后端通过 `default_out` 把真实路径回给前端（旧模块在 gLoad / loadFromPaths 里
       早就在读这个字段），这里补上工作台这条通路 —— 只在框还空着时填，
       并且**不写 localStorage**：它不是用户选的目录，只是默认值。 */
    if (data.default_out && !(el.gOutDir.value || '').trim()) {
      el.gOutDir.value = data.default_out;
      gstate.outDir = data.default_out;
    }

    if (!data.scan_id || !Array.isArray(data.rows)) {
      toast('扫描结果不完整，请重新导入', 'err');
      return false;
    }
    const previous = new Set(wb.rows.map((r) => r.path));
    const activePath = wb.rows.find((r) => r.id === wb.activeId)?.path;
    wb.sourcePaths = new Set(allPaths);
    wb.scanId = String(data.scan_id);
    wb.rows = data.rows;
    wb.rows.forEach((r, i) => { r.id = i; tripOvApplyTo(r); });
    wb.activeId = wb.rows.find((r) => r.path === activePath)?.id ?? null;
    const added = wb.rows.filter((r) => !previous.has(r.path)).length;
    wbRender();
    const hasErrors = !!(data.errors && data.errors.length);
    const hasWarnings = !!(data.warnings && data.warnings.length);
    if (hasErrors) {
      const issue = (data.vault_failed && data.errors.find((e) => e.filename === '（发票库入库）')) || data.errors[0];
      const summary = data.vault_failed
        ? `${data.vault_failed} 个文件未能入库`
        : `${data.errors.length} 项文件处理异常`;
      toast(`${summary}：${issue.reason || '请核对导入文件'}`, 'err', 6000);
    }
    if (hasWarnings) {
      toast(`${data.warnings.length} 组同号文件信息不一致，请在重复副本详情中核对`, 'err', 5000);
    }
    if (!hasErrors && !hasWarnings) {
      toast(added ? `已添加 ${added} 张发票` : '工作台已更新', added ? 'ok' : 'info');
    }
    return true;
  } catch (e) {
    prog.stop();                        // 失败也要停轮询，别让定时器一直转
    hideOverlay();
    toast('解析请求失败：' + e.message, 'err');
    return false;
  }
}

async function wbUpload(fileList) {
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
    await wbLoad(data.paths);
    if (data.failed) toast(`${data.failed} 个文件上传失败：${data.errors?.[0]?.reason || '请检查文件'}`, 'err', 6000);
  } catch (e) {
    hideOverlay();
    toast('上传失败：' + e.message, 'err');
  }
}

/* ── 选中态 / 激活态的「就地刷新」───────────────────────────
   这两个状态原本是靠 wbRender() 整表重建来体现的。整表重建会替换 #wbBody.innerHTML，
   **把用户手里正在点的复选框节点直接销毁**：实测连点 5 个勾选框只有第 1 个生效
   （后 4 个节点已经脱离文档，click 打空）。同时还会把滚动位置弹回顶部。
   所以改成只改属性、不换节点。 */
function wbRefreshSel() {
  const rows = wbVisible();
  el.wbBody.querySelectorAll('input[data-pick]').forEach((box) => {
    /* `box.dataset.pick` 是 `esc(r.path)` 写进 HTML 属性、再由浏览器解析回来的原文，
       所以可以直接和 `r.path` / `wb.unsel` 里的 key 比。 */
    const on = !wb.unsel.has(box.dataset.pick);
    box.checked = on;
    const tr = box.closest('tr');
    if (tr) tr.classList.toggle('sel', on);
  });
  // 表头「全选」= 当前筛选结果**全部没被手动取消**（不是「用户点过全选」）
  el.wbPickAll.checked = rows.length > 0 && rows.every((r) => !wb.unsel.has(r.path));
  wbRenderBar();
}

function wbRefreshActive() {
  el.wbBody.querySelectorAll('tr[data-id]').forEach((tr) => {
    tr.classList.toggle('active', Number(tr.dataset.id) === wb.activeId);
  });
}

/* ── 右侧详情侧栏 ───────────────────────────────────────────
   点任意行 → 显示票面预览 + 解析明细 + 核对结论。 */
const DETAIL_FIELDS = [
  ['type', '票种'], ['label', '标签'], ['item_name', '票面明细'], ['amount', '金额'], ['date', '开票日期'],
  ['seller', '销售方'], ['buyer', '购买方'], ['invoice_no', '发票号码'],
  ['invoice_date', '票面日期'], ['invoice_code', '发票代码'],
  ['buyer_tax_id', '购买方税号'], ['seller_tax_id', '销售方税号'],
  ['tax_rate', '税率'], ['tax_amount', '税额'], ['amount_without_tax', '不含税金额'],
  ['drawer', '开票人'],
];

const DETAIL_TRAIN = [
  ['train', '车次'], ['from_', '出发站'], ['to_', '到达站'],
  ['train_date', '乘车时间'], ['seat_class', '席位'], ['seat_no', '座位号'],
  ['passenger', '乘客'],
];

const DETAIL_HOTEL = [
  ['hotel_name', '酒店'], ['checkin', '入住'], ['checkout', '退房'],
];

/* ── 机票行程手动补全（用户口径：解析不到就给用户一个入口，不填也没关系）───────
   持久化用 localStorage，key 用 **现成的 r.path** —— 全项目的去重、预览、执行
   一律以 path 为身份（wbLoad/gstate 都是 `new Set(rows.map(r => r.path))`）。
   不另发明指纹规则：自己造的指纹最容易跟后端对不上。

   ⚠️ 本轮**不接后端**：数据只存在本机，等航班解析落地后再随执行请求一起发给后端
   （届时在 GENERIC.train.body 那处带上即可）。 */
const TRIP_OV_KEY = 'fapiao.tripOverrides.v1';

/* 交通工具判定 —— `core/trainmap.py::resolve_mode` 的 JS 副本。
   前端不能 import Python，所以同一套规则链在两边各写一份；**Python 那份是真源**，
   改规则必须两边同步，否则列表里画的和轨迹图里画的可能不是同一个交通工具。
   规则链（与 Python 一致，命中即返回）：
     显式 mode > 机票类标签（label/type/item_name）> 地名含机场 > 航班号形态 > 默认 train */
const TRIP_AIR_KW = ['机票', '航空', '航班', '民航', '客票'];
const TRIP_AIR_PLACE = ['机场', '航站楼', '空港'];
/* 航班号 = 2 位航司码 + 3~4 位数字（MU5100）；车次是 1 位字母（G101），不会撞形 */
const TRIP_AIR_NO = /^[A-Z]{2}\s?\d{3,4}$/;

function tripMode(r, fallback) {
  const inv = r || {};
  const m = String(inv.mode || '').toLowerCase();       // 后端已判定过就直接用
  if (m === 'air' || m === 'train') return m;
  // 与 wbCat 同一处取词：label|type（用 | 分隔，避免边界拼出假关键词）
  const label = `${inv.label || ''}|${inv.type || ''}|${inv.item_name || ''}`;
  if (TRIP_AIR_KW.some((k) => label.indexOf(k) >= 0)) return 'air';
  const t = inv.trip || {};
  const place = `${t.from_ || inv.from_ || ''} ${t.to_ || inv.to_ || ''}`;
  if (TRIP_AIR_PLACE.some((k) => place.indexOf(k) >= 0)) return 'air';
  if (TRIP_AIR_NO.test(String(t.no || inv.train || '').trim())) return 'air';
  return fallback || 'train';
}

function tripOvAll() {
  try { return JSON.parse(localStorage.getItem(TRIP_OV_KEY) || '{}') || {}; }
  catch (e) { return {}; }        // 存储被禁用/损坏时退化成「不持久化」，不影响主流程
}

function tripOvWrite(all) {
  try { localStorage.setItem(TRIP_OV_KEY, JSON.stringify(all)); }
  catch (e) { /* 写不进去也不阻断界面 */ }
}

/** 把本机保存的补全值套用到一行上（载入列表 / 保存后都走这里）。 */
function tripOvApplyTo(r) {
  if (r.journey_edited) return;
  const ov = tripOvAll()[r.path];
  if (!ov) return;
  r.trip = Object.assign({}, r.trip, {
    from_: ov.from_ || '', to_: ov.to_ || '', date: ov.date || '', no: ov.no || '',
  });
  r.trip.available = !!(r.trip.from_ && r.trip.to_);
  // mode 逐条判定（镜像 core/trainmap.py::resolve_mode），不再手写 'transit' 默认值：
  // 有出发+到达就是交通，再判是航班还是火车；住宿没有 from_/to_，
  // 服务端给的 'stay' 原样保留（以前这里会被无脑刷成 'transit'，住宿行会显示成交通）。
  if (r.trip.available) r.trip.mode = tripMode(r);
  r.trip.manual = true;
}

function tripOvSave(r, patch) {
  const all = tripOvAll();
  const cur = Object.assign({}, all[r.path] || {}, patch);
  // 四个字段全空 = 等于没补，直接删掉，别在存储里留垃圾
  if (!cur.from_ && !cur.to_ && !cur.date && !cur.no) delete all[r.path];
  else all[r.path] = cur;
  tripOvWrite(all);
  tripOvApplyTo(r);
  // 把逐条判定的交通工具一并存下 —— 后端画轨迹时按它出图标，飞机不能画成火车。
  // （本表单只在机票行出现，正常就是 'air'；后端把它当**该条记录自己的显式 mode**，
  //  不是整页 mode，混排页的火车半页不受影响。）
  if (all[r.path]) { all[r.path].mode = r.trip.mode || ''; tripOvWrite(all); }
}

function tripOvClear(r) {
  const all = tripOvAll();
  delete all[r.path];
  tripOvWrite(all);
  r.trip = Object.assign({}, r.trip, { available: false, mode: '', manual: false });
}

/** 取本批要处理的路径对应的补全值，供后端生成行程单时套用。
    全空的视为没补；缺 from_/to_ 的也照传 —— 让后端统一判定「不够就静默跳过」，
    前端不替后端做决定，也绝不因补全不全而拦下生成动作。 */
function collectTripOverrides(paths) {
  const all = tripOvAll();
  const out = {};
  for (const p of (paths || [])) {
    const ov = all[p];
    if (!ov) continue;
    const o = {
      from_: String(ov.from_ || ''), to_: String(ov.to_ || ''),
      date: String(ov.date || ''), no: String(ov.no || ''),
      mode: String(ov.mode || ''),
    };
    if (!o.from_ && !o.to_ && !o.date && !o.no) continue;
    out[p] = o;
  }
  return out;
}

/** 附属材料：挂到本张发票上的行程单（行程单自己不再独立成行，只在这里呈现）。
    契约见 core/attach.py::build_attachment —— {type, source, trip_count,
    trip_amount, start_date, trips}；`detail.attachment_note` 是那句人类可读的
    说明（「行程单（某渠道）· N 笔 · 合计 ¥X」），是本块的主角。
    ⚠️ 这里的 trip_amount **只用于展示**，不进任何合计。 */
function detailAttachHtml(r) {
  const d = r.detail || {};
  const att = r.attachment || d.attachment || null;
  const note = d.attachment_note || '';
  if (!att && !note) return '';
  const trips = (att && att.trips) || [];
  return `<div class="detail-sec"><h4>附属材料</h4>`
    + (note ? `<p class="detail-note">${esc(note)}</p>` : '')
    + kvBlock('行程单', [
        ['类型', att && att.type], ['来源', att && att.source],
        ['行程笔数', att && att.trip_count],
        ['行程合计', att && att.trip_amount ? fmtMoney(att.trip_amount) : ''],
        ['起始日期', att && att.start_date],
      ])
    + (trips.length ? `<p class="detail-note">共 ${trips.length} 笔行程明细，`
        + `见原行程单文件。</p>` : '')
    + `</div>`;
}

/** 废弃说明：为什么这一行被标废、它还在不在列表里。用户要的是「标注」不是「删除」。
    注：同票重复副本已整行移出列表，所以这里只剩「它不是发票」一种废弃原因。 */
function detailDiscardHtml(r) {
  if (!isDiscarded(r)) return '';
  return `<div class="detail-sec"><h4>废弃标记</h4>`
    + `<p class="detail-note"><b>废弃原因：它不是发票</b>（行程单 / 报销单据 / 报销辅助材料这一类），`
    + `在同批里也匹配不上任何宿主发票 —— 因此标注<b>废弃</b>：`
    + `金额不进合计，但<b>仍保留在列表里</b>供你核对，不会自动删除。</p>`
    + `</div>`;
}

/** 代表件的「重复副本」区块：同票的其它副本已去重、不单独成行，只在这里以「+N 副本」呈现。
    没有它，用户只知道「这张票还在」，不知道另外几份文件去哪了。 */
function detailDupHtml(r) {
  const ps = (r && r.dup_paths) || [];
  if (!ps.length) return '';
  const warnings = (r && r.dup_warnings) || [];
  /* ② 用户 2026-10-11：光写「+N 副本」等于没解释 —— 用户只看到票还在，
     不知道另外几份文件是什么、为什么被剔掉。按副本的真实扩展名如实写原因
     （同一张发票常常同时有 PDF 与 OFD 两份，去重后只留代表件）。 */
  const exts = [...new Set(ps.map((p) => {
    const m = /\.([a-z0-9]+)$/i.exec(baseName(p) || '');
    return m ? m[1].toUpperCase() : '';
  }).filter(Boolean))];
  const fmt = exts.length ? `${exts.join(' / ')} 格式` : '同票副本';
  return `<div class="detail-sec"><h4>重复副本</h4>`
    + warnings.map((message) => `<p class="detail-note"><b>需核对：</b>${esc(message)}</p>`).join('')
    + `<p class="detail-note">同一张发票另有 <b>+${ps.length} 副本</b>：`
    + `已剔除 ${ps.length} 份同票副本（同一张发票的 ${esc(fmt)}），`
    + `已去重、不单独成行（不计入合计，也不重复报销）：</p>`
    + `<ul class="dup-list">`
    + ps.map((p) => `<li title="${esc(p)}">${esc(baseName(p))}</li>`).join('')
    + `</ul></div>`;
}

function kvBlock(title, pairs) {
  const rows = pairs.filter(([, v]) => v !== '' && v != null && v !== 0);
  if (!rows.length) return '';
  return `<div class="detail-sec"><h4>${esc(title)}</h4><dl class="detail-kv">`
    + rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join('')
    + `</dl></div>`;
}

function renderDetail(r) {
  if (!r) {
    el.detailTitle.textContent = '详情';
    el.detailBody.innerHTML =
      `<div class="detail-blank">`
      + `<span class="db-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"`
      + ` stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">`
      + `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>`
      + `<path d="M14 2v6h6"/></svg></span>`
      + `<p>未选择发票</p>`
      + `<span class="db-sub">点左侧任意一行，这里显示票面预览与解析明细</span>`
      + `</div>`;
    return;
  }
  el.detailTitle.textContent = r.filename;

  /* ── 票面预览（用户第 ⑥ 条）────────────────────────────────────────────
     改前：PDF 内嵌 `<iframe>`、OFD 只给一句「无法在页面内渲染」+ 新标签链接；
     `.detail-preview` 是固定 240px 高 —— 侧栏只有 260px 宽，PDF 竖版放进来，
     下方那一大块全是留白。
     改后：主体是一张 `<img class="pv-img" id="pvImg">`（后端新接口
     `/api/pool/thumb` 直接返回 PNG），**按发票真实比例**由图片自己撑开高度，
     `.detail-preview` 不再固定高度，下面不留白；点图开全屏 lightbox（懒创建）。
     后端新接口**可能还没上线**，所以非 200 / 解码失败一律静默降级：
       · PDF → 换回原来的 iframe
       · OFD → 保留原来的「无法在页面内直接渲染」说明 + 新标签链接
     降级见 pvWireFallback()；契约：失败是 404 + JSON，`<img>` 的 error 事件一定到。 */
  const url = `/api/pool/file?path=${encodeURIComponent(r.path)}`;
  const thumb = `${API.poolThumb}?path=${encodeURIComponent(r.path)}&max=1400`;
  /* ⚠️ **只有 pdf 才去要缩略图**（2026-10-11 修）：后端 `/api/pool/thumb` 用
     pypdfium2 渲 PDF 第 1 页，**根本渲不了 OFD** —— 对 .ofd 无条件挂 `<img>`
     等于每次点开 OFD 都必然发一次 404（控制台一片红、再靠 error 事件兜回来）。
     真实样本里就有 OFD（住宿/机票的 OFD 与 PDF 同号成对）。非 pdf 直接走
     「说明 + 新标签打开」，一个多余请求都不发（`#pvImg` 缺席时
     `pvWireFallback()` 会自己 early-return，见它的第一行）。 */
  const isPdf = r.ext === 'pdf';
  const canTryPreview = isPdf || r.ext === 'ofd';
  const preview = `<div class="detail-preview" data-ext="${esc(r.ext || '')}" `
    + `data-file-url="${esc(url)}">`
    + (canTryPreview ? `<img class="pv-img" id="pvImg" src="${esc(thumb)}" alt="票面预览" `
        + `title="点击放大" loading="lazy">` : '')
    + `</div>`
    /* OFD 的说明 + 新标签入口**照旧留在下面**：图能渲染出来它是补充入口，
       图渲染不出来它就是唯一的入口（此时把坏图摘掉，见 pvWireFallback）。 */
    + (isPdf ? '' : `<p class="detail-note">有同名 PDF 时显示页内预览；否则可`
      + `<a href="${esc(url)}" target="_blank" rel="noreferrer">点这里在新标签页打开</a>`
      + `（需要系统已装 OFD 阅读器）。</p>`);

  // ── 解析明细：按票种给不同字段集，空字段一律不显示 ──
  const d = r.detail || {};
  const base = DETAIL_FIELDS.map(([k, label]) => {
    let v = d[k] != null ? d[k] : r[k];
    if (k === 'amount') v = v ? fmtMoney(v) : '';
    return [label, v];
  });
  const extra = d.kind === 'train' ? DETAIL_TRAIN
    : d.kind === 'hotel' ? DETAIL_HOTEL : [];
  const extraRows = extra.map(([k, label]) => [label, d[k]]);

  // ── 核对结论 ──
  const st = (r.check || {}).status || 'unknown';
  const checkBlock = `<div class="detail-sec"><h4>核对结论</h4>`
    + `<div><span class="st ${esc(st)}">${esc(CHECK_NAME[st] || '—')}</span></div>`
    + ((r.check || {}).reason
        ? `<p class="detail-note">${esc(r.check.reason)}</p>` : '')
    + kvBlock('比对对象', [['购买方', d.buyer || r.buyer || ''],
                           ['购买方税号', d.buyer_tax_id || '']])
    + `</div>`;

  // 可编辑的行程信息置顶，核对结论和票面查阅内容随后。
  el.detailBody.innerHTML =
    journeyEditorHtml(r, r.journey_edited ? {} : (tripOvAll()[r.path] || {}))
    + checkBlock
    + detailDiscardHtml(r)
    + detailDupHtml(r)
    + detailAttachHtml(r)
    + `<div class="detail-sec"><h4>票面预览</h4>${preview}</div>`
    + kvBlock('票面信息', base)
    + (extraRows.length ? kvBlock(d.kind === 'train' ? '车票信息' : '住宿信息', extraRows) : '');

  el.detailBody.querySelectorAll('.journey-date').forEach(dtAttach);
  journeyAttachAirports(el.detailBody).catch((error) => toast(error.message, 'err'));

  /* ⑥ 预览图挂完就得立刻接线：降级（非 200 → iframe / OFD 说明）+ 点击放大。
     必须在 innerHTML 之后调用 —— 图是个新节点，error 事件可能马上就到。 */
  pvWireFallback();
}

function wbApplySavedJourney(r, saved) {
  const j = saved.journey || {};
  r.vault_id = saved.id;
  r.journey = j;
  r.missing_info = saved.missing_info || [];
  r.completeness = saved.completeness;
  r.journey_edited = true;
  if (r.kind === 'hotel') {
    Object.assign(r.detail, { checkin: j.checkin || '', checkout: j.checkout || '' });
    Object.assign(r.trip, { checkin: j.checkin || '', checkout: j.checkout || '',
                            available: !!(j.checkin || j.checkout), mode: 'stay' });
  } else {
    Object.assign(r.detail, { from_: j.from_ || '', to_: j.to_ || '',
      train_date: j.travel_date || '', passenger: j.passenger || '', train: j.train || '' });
    Object.assign(r.trip, { from_: j.from_ || '', to_: j.to_ || '', date: j.travel_date || '',
      passenger: j.passenger || '', no: j.train || '', available: !!(j.from_ && j.to_),
      mode: tripMode(r), manual: true });
  }
  const old = tripOvAll();
  if (old[r.path]) { delete old[r.path]; tripOvWrite(old); }
  if (state.mod === 'workbench') {
    wbRender();
    if (wb.activeId === r.id) renderDetail(r);
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   v0.7 新增的两个浮层（用户 2026-10-11）
     · ⑥ 票面放大 lightbox
     · ⑧② 自建日期日历（原生 `<input type="date">` 的弹出面板改不了样式）
   两个都是**单例 + 懒创建**，DOM 都挂在 `document.body`（index.html 里没有它们）。
   类名 / id 是任务书的冻结契约（`#pvOverlay` `#pvOverlayImg` `#pvClose` `.on`
   `.pv-open`、`.dt-pop` `.dt-head` `.dt-nav` `.dt-nav-btn` `.dt-title` `.dt-grid`
   `.dt-dow` `.dt-day`(+`.on`/`.today`/`.off`) `.dt-foot` `.dt-quick` `.dt-ok`
   `.dt-clear`），改名之前先把任务书翻出来看一遍。
   ══════════════════════════════════════════════════════════════════════════ */

/* ── ⑥ 票面预览：降级 + 点击放大 ─────────────────────────────────────── */

/** 预览图没加载出来时**静默回退到改前的样子**。
    后端的 `/api/pool/thumb` 此时很可能还没上线（契约说失败是 404 + JSON），
    所以这里不弹 toast、不弹对话框 —— 它就是一张图，加载不出来就退回老样子。
    三条触发路径都要覆盖：img 的 error 事件、同步就失败（错误图已在缓存里 →
    `complete && naturalWidth === 0`）、以及每次换发票重新挂上的那张新图。 */
function pvWireFallback() {
  const img = document.getElementById('pvImg');
  if (!img) return;                       // 已经降级过 / 这次没有预览区
  const box = img.closest('.detail-preview');
  if (!box) return;
  const fileUrl = box.dataset.fileUrl || '';
  const isPdf = box.dataset.ext === 'pdf';
  let swapped = false;
  const swap = () => {
    if (swapped || !img.isConnected) return;
    swapped = true;
    if (isPdf) {
      /* PDF：换成原来的内嵌 iframe（高度由 CSS 的 `.detail-preview iframe` 给） */
      const f = document.createElement('iframe');
      f.src = fileUrl;
      f.title = '票面预览';
      box.replaceChild(f, img);
    } else {
      /* OFD：下面那行「无法在页面内直接渲染 + 新标签打开」已经在渲染时就写好了，
         这里只需把坏图摘掉，不要让它占着一块空白 */
      img.remove();
    }
  };
  img.addEventListener('error', swap, { once: true });
  if (img.complete && img.naturalWidth === 0) swap();   // 同步补一次兜底
  /* 点击放大：lightbox 里的 src 跟这张图一致（契约要求） */
  img.addEventListener('click', () => pvOpen(img.getAttribute('src') || fileUrl));
}

/** lightbox 的 DOM：第一次点图才建（懒创建），挂 document.body。 */
function pvEnsure() {
  let ov = document.getElementById('pvOverlay');
  if (ov) return ov;
  ov = document.createElement('div');
  ov.id = 'pvOverlay';
  ov.innerHTML = `<button type="button" id="pvClose" aria-label="关闭">×</button>`
    + `<img id="pvOverlayImg" alt="票面放大预览">`;
  ov.addEventListener('click', (e) => {
    /* 点遮罩本身、或点右上角关闭按钮 → 关；点图片不关
       （放大看细节时误点图片就关掉会很烦）。 */
    if (e.target === ov || e.target.closest('#pvClose')) pvClose();
  });
  document.body.appendChild(ov);
  return ov;
}

function pvOpen(src) {
  if (!src) return;
  const ov = pvEnsure();
  const im = ov.querySelector('#pvOverlayImg');
  if (im) im.src = src;
  ov.classList.add('on');                 // 显隐只认这个类（契约）
  document.body.classList.add('pv-open'); // 禁背景滚动（app.css 里 body.pv-open{overflow:hidden}）
}

function pvClose() {
  const ov = document.getElementById('pvOverlay');
  if (!ov) return;
  ov.classList.remove('on');
  document.body.classList.remove('pv-open');
  const im = ov.querySelector('#pvOverlayImg');
  if (im) im.setAttribute('src', '');     // 别再占着解码后的位图
}

/* ── ⑧② 自建日期日历浮层 ───────────────────────────────────────────────
   为什么自建：原生 `<input type="date">` 的弹出面板由浏览器自己画，CSS 够不着
   （用户原话「弹出框 UI 风格不符」）。
   四个日期输入框（工作台的 起/止 + 发票库的 起/止）都已经是
   `readonly` + `inputmode="none"`：**`.value` 的读写语义一个字没变**，
   主理人的脚本照旧 `el.value = 'YYYY-MM-DD'` + 派发 change 就能用；
   点日期框 → 弹下面这个浮层。
   单例、懒创建、挂 `document.body`；起止两个框**共用同一个实例**。 */

/** 星期表头：与 JS 的 getDay() 对齐（getDay 里 0=周日，表头以周一开头）。 */
const DT_DOW = ['一', '二', '三', '四', '五', '六', '日'];
/** 可选范围下限。上限是「今天 + 2 年」（见 dtMax）：火车票/机票本来就可能是
    未来日期，卡死在今天反而挡了正常用法。 */
const DT_MIN = '2001-01-01';
/** 起止两框的配对表：快捷项（本月 / 近 30 天）要一次填一对。 */
const DT_PAIRS = { wbDateFrom: 'wbDateTo', wbDateTo: 'wbDateFrom',
                   vaultFrom: 'vaultTo', vaultTo: 'vaultFrom' };

const dtState = { node: null, target: null, y: 0, m: 0 };

const dtPad = (n) => String(n).padStart(2, '0');
/** 0 基月份 → 'YYYY-MM-DD'（与 input.value 的格式一致） */
function dtYmd(y, m, d) { return `${y}-${dtPad(m + 1)}-${dtPad(d)}`; }
function dtToday() { const n = new Date(); return dtYmd(n.getFullYear(), n.getMonth(), n.getDate()); }
function dtMax() { const n = new Date(); return dtYmd(n.getFullYear() + 2, n.getMonth(), n.getDate()); }

/** 浮层 DOM：懒创建。点任何一个日期格 / 翻页 / 快捷项都在这里派发。 */
function dtEnsure() {
  if (dtState.node && dtState.node.isConnected) return dtState.node;
  const pop = document.createElement('div');
  pop.className = 'dt-pop';
  pop.hidden = true;
  pop.innerHTML =
    `<div class="dt-head">`
    + `<div class="dt-nav">`
    + `<button type="button" class="dt-nav-btn" data-nav="-1" aria-label="上个月">‹</button>`
    + `<button type="button" class="dt-nav-btn" data-nav="1" aria-label="下个月">›</button>`
    + `</div>`
    + `<div class="dt-title"></div>`
    + `</div>`
    + `<div class="dt-grid"></div>`
    + `<div class="dt-foot">`
    + `<div class="dt-quick">`
    + `<button type="button" data-quick="month">本月</button>`
    + `<button type="button" data-quick="30">近 30 天</button>`
    + `<button type="button" data-quick="clear">清空</button>`
    + `</div>`
    + `<button type="button" class="dt-ok">确定</button>`
    + `<button type="button" class="dt-clear">清空</button>`
    + `</div>`;
  pop.addEventListener('click', (e) => {
    const nav = e.target.closest('.dt-nav-btn');
    if (nav) { dtShift(Number(nav.dataset.nav) || 0); return; }
    const day = e.target.closest('.dt-day');
    /* `.off` = 超出可选范围 / 不在本月，点了不响应 */
    if (day) { if (!day.classList.contains('off')) dtWrite(day.dataset.d); return; }
    const q = e.target.closest('[data-quick]');
    if (q) { dtQuick(q.dataset.quick); return; }
    if (e.target.closest('.dt-ok')) { dtClose(); return; }
    /* 底部的「清空」只清当前这个框（快捷行的「清空」清一对，见 dtQuick） */
    if (e.target.closest('.dt-clear')) { dtWrite(''); return; }
  });
  document.body.appendChild(pop);
  dtState.node = pop;

  /* ── 浮层的三条全局行为（只在第一次创建时绑一次）── */
  // 1) 点外部关闭。用捕获阶段：不等别人 stopPropagation，避免浮层赖着不走
  document.addEventListener('click', (e) => {
    if (pop.hidden) return;
    if (e.target.closest('.dt-pop') || e.target === dtState.target) return;
    dtClose();
  }, true);
  // 2) Esc 关闭（lightbox 开着时优先关 lightbox，见 v0.7 的 pvClose）
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const ov = document.getElementById('pvOverlay');
    if (ov && ov.classList.contains('on')) { pvClose(); return; }
    if (!pop.hidden) dtClose();
  });
  // 3) 视口变了就重新贴一次，别让它悬在半空
  window.addEventListener('resize', () => { if (!pop.hidden && dtState.target) dtPlace(dtState.target, pop); });
  return pop;
}

function dtOpen(input) {
  if (!input) return;
  const pop = dtEnsure();
  dtState.target = input;
  const m = /^(\d{4})-(\d{1,2})/.exec((input.value || '').trim());
  const n = new Date();
  dtState.y = m ? Number(m[1]) : n.getFullYear();
  dtState.m = m ? Number(m[2]) - 1 : n.getMonth();
  dtRender();
  pop.hidden = false;
  dtPlace(input, pop);
}

function dtClose() {
  const pop = dtState.node;
  if (pop) pop.hidden = true;
  dtState.target = null;
}

/** 把浮层贴在输入框下方；下面装不下就上翻，左右夹在视口里。 */
function dtPlace(input, pop) {
  const r = input.getBoundingClientRect();
  const sx = window.pageXOffset || document.documentElement.scrollLeft || 0;
  const sy = window.pageYOffset || document.documentElement.scrollTop || 0;
  const w = pop.offsetWidth || 234;
  const h = pop.offsetHeight || 300;
  let left = r.left + sx;
  let top = r.bottom + sy + 6;
  if (r.bottom + 6 + h > window.innerHeight && r.top - 6 - h > 0) top = r.top + sy - h - 6;  // 上翻
  const maxLeft = sx + window.innerWidth - w - 8;
  if (left > maxLeft) left = maxLeft;
  if (left < sx + 8) left = sx + 8;
  pop.style.left = `${Math.round(left)}px`;
  pop.style.top = `${Math.round(top)}px`;
}

function dtShift(n) {
  if (!n) return;
  let m = dtState.m + n;
  let y = dtState.y;
  while (m < 0) { m += 12; y -= 1; }
  while (m > 11) { m -= 12; y += 1; }
  dtState.m = m;
  dtState.y = y;
  dtRender();
}

function dtRender() {
  const pop = dtState.node;
  if (!pop) return;
  const cur = dtState.target ? (dtState.target.value || '') : '';
  const today = dtToday();
  const max = dtMax();
  pop.querySelector('.dt-title').textContent = `${dtState.y} 年 ${dtState.m + 1} 月`;

  const first = new Date(dtState.y, dtState.m, 1);
  const startIdx = (first.getDay() + 6) % 7;               // 0 = 周一
  const days = new Date(dtState.y, dtState.m + 1, 0).getDate();
  const prevDays = new Date(dtState.y, dtState.m, 0).getDate();
  const mk = (ds, off) => {
    const cls = ['dt-day'];
    if (off || ds < DT_MIN || ds > max) cls.push('off');
    if (ds === cur) cls.push('on');
    if (ds === today) cls.push('today');
    return `<button type="button" class="${cls.join(' ')}" data-d="${ds}">`
      + `${Number(ds.slice(8, 10))}</button>`;
  };
  const cells = DT_DOW.map((d) => `<span class="dt-dow">${d}</span>`);
  for (let i = 0; i < startIdx; i++) {                     // 上月尾巴（.off）
    const pm = dtState.m === 0 ? 11 : dtState.m - 1;
    const py = dtState.m === 0 ? dtState.y - 1 : dtState.y;
    cells.push(mk(dtYmd(py, pm, prevDays - startIdx + 1 + i), true));
  }
  for (let d = 1; d <= days; d++) cells.push(mk(dtYmd(dtState.y, dtState.m, d), false));
  const used = startIdx + days;
  const rest = (7 - (used % 7)) % 7;
  for (let i = 1; i <= rest; i++) {                        // 下月开头（.off）
    const nm = dtState.m === 11 ? 0 : dtState.m + 1;
    const ny = dtState.m === 11 ? dtState.y + 1 : dtState.y;
    cells.push(mk(dtYmd(ny, nm, i), true));
  }
  pop.querySelector('.dt-grid').innerHTML = cells.join('');
}

/** 写回输入框并关浮层。**这一行的 change 是 (bubbles:true) 的**：
    工作台 / 发票库的筛选监听都挂在自己的框上（listener 直接挂在元素上，
    bubbles 只是为了跟「脚本派发」那条路径完全一致）。 */
function dtWrite(v) {
  const t = dtState.target;
  if (t) {
    t.value = v;                                          // '' = 清空
    t.dispatchEvent(new Event('change', { bubbles: true }));
  }
  dtClose();
}

/** 快捷项：本月 / 近 30 天 一次填「起 + 止」一对，清空清一对。 */
function dtQuick(kind) {
  const t = dtState.target;
  if (!t) { dtClose(); return; }
  if (kind === 'clear') {
    [t, document.getElementById(DT_PAIRS[t.id] || '')].forEach((x) => {
      if (x) { x.value = ''; x.dispatchEvent(new Event('change', { bubbles: true })); }
    });
    dtClose();
    return;
  }
  const n = new Date();
  let from, to;
  if (kind === 'month') {
    from = dtYmd(n.getFullYear(), n.getMonth(), 1);
    to = dtYmd(n.getFullYear(), n.getMonth() + 1, 0);
  } else {
    const s = new Date(n.getFullYear(), n.getMonth(), n.getDate() - 29);
    from = dtYmd(s.getFullYear(), s.getMonth(), s.getDate());
    to = dtToday();
  }
  /* 起止各自写回自己的框（from 先写：工作台的 onDate 会在 from > to 时给提示） */
  const a = t.id === 'wbDateTo' || t.id === 'vaultTo' ? document.getElementById(DT_PAIRS[t.id]) : t;
  const b = a === t ? document.getElementById(DT_PAIRS[t.id]) : t;
  if (a) { a.value = from; a.dispatchEvent(new Event('change', { bubbles: true })); }
  if (b) { b.value = to; b.dispatchEvent(new Event('change', { bubbles: true })); }
  dtClose();
}

/** 把四个日期框接到浮层上：点（或键盘 Enter / 空格）弹，再点收起。
    readonly 已经挡住了原生面板，`preventDefault()` 是第二道保险。 */
function dtAttach(input) {
  if (!input) return;
  input.addEventListener('click', (e) => {
    e.preventDefault();
    const pop = dtState.node;
    if (pop && !pop.hidden && dtState.target === input) { dtClose(); return; }
    dtOpen(input);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); dtOpen(input); }
  });
}

/* ── 工作台事件 ─────────────────────────────────────────── */
function bindWorkbench() {
  // 空状态投放条：点投放条本身（不点里面的按钮）→ 选文件
  el.wbHero.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;
    el.wbFileInput.click();
  });
  el.wbFileInput.addEventListener('change', (e) => {
    if (e.target.files.length) wbUpload(e.target.files);
    e.target.value = '';
  });

  el.wePickFiles.addEventListener('click', (e) => { e.stopPropagation(); wbPick('files'); });
  el.wePickFolder.addEventListener('click', (e) => { e.stopPropagation(); wbPick('folder'); });
  el.wbPickFiles.addEventListener('click', () => wbPick('files'));
  el.wbPickFolder.addEventListener('click', () => wbPick('folder'));

  /* ── 底栏流水线（批次 2，样式改版见用户第 ② 条）────────────
     开关是**按钮**不是 checkbox：点一下就翻转 `aria-pressed`，
     `aria-pressed` 是**唯一真相源**（读取用 wbPipelineActions()、样式用
     `.wb-toggle[aria-pressed="true"]`）—— ☐/☑ 那对方框和 1/2/3/4 序号已按
     用户第 ② 条删除，选中态改成整张卡片的强调色底 + 强调色文字。
     `.on` 类只是同一状态的**样式别名**（CSS 里 `.wb-toggle.on` 与
     `[aria-pressed="true"]` 同一条规则），两个一起维护，别让它们分叉。
     四个开关的排列顺序**不影响**执行顺序 —— 执行顺序由 PIPELINE_ORDER 刚性决定。 */
  el.wbActionbar.querySelectorAll('.wb-toggle').forEach((b) => {
    b.classList.toggle('on', b.getAttribute('aria-pressed') === 'true');
  });
  el.wbActionbar.addEventListener('click', (e) => {
    const tgl = e.target.closest('.wb-toggle');
    if (!tgl || wbRunBusy) return;
    const on = tgl.getAttribute('aria-pressed') !== 'true';
    tgl.setAttribute('aria-pressed', on ? 'true' : 'false');
    tgl.classList.toggle('on', on);
    wbRenderRunNote();
  });

  el.wbRunBtn.addEventListener('click', wbRunPipeline);
  el.wbTripBtn.addEventListener('click', () => wbSetTripMenu(el.wbTripMenu.hidden));
  el.wbTripMenu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-trip-scope]');
    if (button && !button.disabled) wbRunTrip(button.dataset.tripScope);
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.wb-trip-control')) wbSetTripMenu(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !el.wbTripMenu.hidden) {
      wbSetTripMenu(false);
      el.wbTripBtn.focus();
    }
  });

  // 行程字段保存到发票库；表格和当前详情随接口结果立即刷新。
  el.detailBody.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-journey-save]');
    if (!btn) return;
    const r = wb.rows.find((x) => x.id === wb.activeId);
    if (!r) return;
    btn.disabled = true;
    try {
      const saved = await journeySave(r, btn.closest('.journey-editor'));
      wbApplySavedJourney(r, saved);
      toast(r.completeness === 'complete' ? '已保存，行程信息完整' : '已保存，仍有信息待补充',
        r.completeness === 'complete' ? 'ok' : 'info');
    } catch (err) {
      toast('保存失败：' + err.message, 'err', 5000);
    } finally {
      btn.disabled = false;
    }
  });

  el.wbClear.addEventListener('click', () => {
    if (!wb.rows.length) return;
    wb.rows = []; wb.sourcePaths.clear(); wb.unsel.clear();
    wb.cats = new Set(); wb.status = 'all'; wb.from = ''; wb.to = ''; wb.q = ''; wb.mid = 'all';
    wb.activeId = null;
    // 逐行状态与快照 id 一并作废：scan_id 对应的是**刚被清空的这批行**，
    // 留着它下次 run 会让后端按旧快照解析下标，指向完全不相干的文件。
    wb.proc.clear();
    wb.scanId = '';
    el.wbSearch.value = ''; el.wbDateFrom.value = ''; el.wbDateTo.value = '';
    el.wbStatus.value = 'all';
    wbRender();
    toast('已清空工作台', 'info');
  });

  // 拖拽：空状态投放条与工具条都能接住
  [el.wbHero, el.wbBar].forEach((zone) => {
    ['dragenter', 'dragover'].forEach((ev) =>
      zone.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        zone.classList.add('dragging');
      }));
    ['dragleave', 'drop'].forEach((ev) =>
      zone.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        if (ev === 'dragleave' && zone.contains(e.relatedTarget)) return;
        zone.classList.remove('dragging');
      }));
    zone.addEventListener('drop', (e) => {
      const files = e.dataTransfer?.files;
      if (files && files.length) wbUpload(files);
    });
  });

  // 票种 chips（**多选**，用户第 2 条）+ 品名中类 chips（用户第 7 条）。
  // 中类只过滤「其他」；并选其他票种时保留中类，取消「其他」时才复位。
  el.wbChips.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    if (chip.dataset.mid !== undefined) {   // 中类 chip：只切 mid，不动票种
      wb.mid = chip.dataset.mid;
    } else if (chip.dataset.cat === 'all') {
      wb.cats = new Set();                  // 「全部」= 清空多选（空集就是这个语义）
      wb.mid = 'all';
    } else {
      /* 票种多选：点一下选中 / 点已选中的取消 / 点其他的是多选。
         ⚠️ **空集代表「全部」而不是「没选」**，所以第一次点某个票种时不能直接
         `add`（那只会在「全部」上再叠一层，语义没变）——必须先把集合换成只有它。
         集合只有一项时再点它 → 归零 → 又回到「全部」，这也是用户要的「再点一下取消」。 */
      const c = chip.dataset.cat;
      if (!wb.cats.size) wb.cats = new Set([c]);
      else if (wb.cats.has(c)) wb.cats.delete(c);
      else wb.cats.add(c);
      if (!wbHasOther()) wb.mid = 'all';
    }
    wbRender();
  });

  /* 起止日期（用户第 3 条）：两个 input 共用一个处理函数。
     原来是单个 select（近 30 天 / 近 90 天 / 今年），已按第 3 条删掉。 */
  const onDate = () => {
    wb.from = el.wbDateFrom.value || '';
    wb.to = el.wbDateTo.value || '';
    /* 兜底：开始日期被设得比结束日期晚时**不要静默给一张空表** ——
       两个独立日期框不做跨字段校验，用户只会看到筛选"坏了"。
       直接把结束日期拉到开始日期，并明说改了什么。 */
    if (wb.from && wb.to && wb.from > wb.to) {
      wb.to = wb.from;
      el.wbDateTo.value = wb.to;
      toast('起止日期已调整：结束日期不能早于开始日期', 'info');
    }
    wbRender();
  };
  el.wbDateFrom.addEventListener('change', onDate);
  el.wbDateTo.addEventListener('change', onDate);
  /* ⑧②：两个日期框都接上自建日历（readonly + 点弹浮层）。
     `change` 那条原样留着 —— 脚本赋值 + 派发 change 的老路径一个字没变。 */
  [el.wbDateFrom, el.wbDateTo].forEach(dtAttach);
  el.wbStatus.addEventListener('change', (e) => { wb.status = e.target.value; wbRender(); });
  el.wbSearch.addEventListener('input', (e) => { wb.q = e.target.value; wbRender(); });
  el.wbReset.addEventListener('click', () => {
    wb.cats = new Set(); wb.status = 'all'; wb.from = ''; wb.to = ''; wb.q = ''; wb.mid = 'all';
    el.wbSearch.value = ''; el.wbDateFrom.value = ''; el.wbDateTo.value = '';
    el.wbStatus.value = 'all';
    wbRender();
  });

  // 表头排序
  el.wbThead.addEventListener('click', (e) => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const k = th.dataset.sort;
    if (wb.sortKey === k) wb.sortDir *= -1;
    else { wb.sortKey = k; wb.sortDir = 1; }
    wbRender();
  });

  // 全选 / 取消全选（只作用于当前筛选出来的行；写的是**反选集** wb.unsel）
  el.wbPickAll.addEventListener('change', (e) => {
    const rows = wbVisible();
    rows.forEach((r) => { if (e.target.checked) wb.unsel.delete(r.path); else wb.unsel.add(r.path); });
    wbRefreshSel();
  });

  // 行内勾选 / 点行看详情
  el.wbBody.addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-id]');
    if (!tr) return;
    const id = Number(tr.dataset.id);
    if (e.target.closest('input[data-pick]')) return;   // 勾选框自己有 change 事件
    wb.activeId = id;
    renderDetail(wb.rows.find((r) => r.id === id));
    wbRefreshActive();
  });
  el.wbBody.addEventListener('change', (e) => {
    const box = e.target.closest('input[data-pick]');
    if (!box) return;
    // `dataset.pick` 存的是 r.path（见 wbRenderTable）；取消 = 加进反选集
    if (box.checked) wb.unsel.delete(box.dataset.pick); else wb.unsel.add(box.dataset.pick);
    wbRefreshSel();
  });

  // 详情侧栏折叠 / 展开
  el.detailToggle.addEventListener('click', () => {
    wbSetDetailCollapsed(true);
  });
  el.wbDetailToggle.addEventListener('click', () =>
    wbSetDetailCollapsed(!el.detailPanel.classList.contains('collapsed')));

  // ── 发票库入口（v0.6）──────────────────────────────────────────────
  // 两处入口都只是「切到发票库」。**不在这里弹自定义弹窗**：库里有没有东西、
  // 勾哪几张，都由发票库那一页自己说清楚，入口只负责把人送过去。
  if (el.wbVaultImport) {
    el.wbVaultImport.addEventListener('click', () => switchModule('vault'));
  }
  if (el.weVaultCard) {
    el.weVaultCard.addEventListener('click', () => switchModule('vault'));
    // 卡片是 role="button" 的 section，键盘要能进（Enter / Space）
    el.weVaultCard.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();          // Space 不拦会把容器再滚一下
      switchModule('vault');
    });
  }
}

/** 系统文件对话框（服务在本机，Tk 窗口可直接弹出）。 */
async function wbPick(mode) {
  try {
    const { data } = await api(API.pick, { method: 'POST', body: JSON.stringify({ mode }) });
    if (data.ok && data.paths.length) await wbLoad(data.paths);
    else if (data.ok) toast(mode === 'folder' ? '该文件夹内没有 PDF/OFD' : '未选择文件', 'info');
    else toast(data.error || '对话框不可用', 'err');
  } catch (err) { toast('调用失败：' + err.message, 'err'); }
}

