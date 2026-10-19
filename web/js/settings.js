// settings 视图；依赖先加载的 app.js 中的通用状态和界面工具。
/* ── 核对预设条件（默认值 + 本地记忆）─────────────────────────
   不硬编码真实公司信息，首次运行由设置页引导填写；
   用户改过就存进 localStorage，下次打开还在。 */
// 便携版不读取源码版浏览器中的抬头和输出目录；用户主动保存后仍正常记忆。
const SETTINGS_STORE_PREFIX = document.documentElement.dataset.edition === 'desktop'
  ? 'fapiao-desktop-' : 'fapiao-';
const CHECK_STORE_KEY = SETTINGS_STORE_PREFIX + 'check-expected';
const CHECK_DEFAULTS = {
  name: '',
  tax: '',
};

function loadCheckExpected() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(CHECK_STORE_KEY) || 'null'); } catch (e) { saved = null; }
  const v = (saved && typeof saved === 'object') ? saved : {};
  el.gExpName.value = (v.name != null) ? v.name : CHECK_DEFAULTS.name;
  el.gExpTax.value = (v.tax != null) ? v.tax : CHECK_DEFAULTS.tax;
}

function saveCheckExpected() {
  try {
    localStorage.setItem(CHECK_STORE_KEY, JSON.stringify({
      name: el.gExpName.value.trim(),
      tax: el.gExpTax.value.trim(),
    }));
  } catch (e) { /* localStorage 不可用时静默降级 */ }
}

/* ── 设置视图（执行偏好 + 首次运行引导）─────────────────────────
   两个存储键分得很清楚：
     · `fapiao-check-expected`（沿用旧键）→ 抬头 / 税号，真源仍是
       `el.gExpName` / `el.gExpTax` 两个 input + `saveCheckExpected()`；
     · `fapiao-prefs-v1`（新增）→ 输出目录 / 重命名方式 / 失败策略。
   主题沿用 `fapiao-theme` + `applyTheme()`，不另立一套。
   HTML 结构由并行同事提交、ID 已冻结：若某个节点还没落地，本块整体跳过并告警，
   宁可是设置页不响应，也不能让 init() 抛错拖垮整站。 */
const PREF_STORE_KEY = SETTINGS_STORE_PREFIX + 'prefs-v1';
const PREF_DEFAULTS = { outDir: '', renameMode: 'copy', policy: 'skip' };
let sePrefs = { ...PREF_DEFAULTS };    // 设置页的本地记忆（启动时 loadPrefs() 读回）
let seBannerDismissed = false;         // 本次会话点过「关闭」就不再弹（不写 localStorage）
let seVerCache = '';                   // /api/health 拿到的版本号（不硬编码）
let seSavedTimer = 0;

const SE_IDS = [
  'viewSettings', 'seName', 'seTax', 'seHintHeader',
  'seOutDir', 'sePickDir', 'seOpenDir',
  'seRenameMode', 'seRenameCopy', 'seRenameMove',
  'sePolicy', 'sePolicySkip', 'sePolicyStop',
  'seTheme', 'seThemeDark', 'seThemeLight',
  'seVer', 'sePath', 'seSaved',
];

/** 设置视图的 DOM 是否齐备；缺一个就整块跳过（避免只接了一半的按钮）并告警。 */
function seReady() {
  const missing = SE_IDS.filter((k) => !el[k]);
  if (missing.length) {
    console.warn('[设置视图] 缺少 DOM 节点，已跳过接线：' + missing.join(', '));
    return false;
  }
  return true;
}

/** 读回 `fapiao-prefs-v1`；非法值一律退回默认。 */
function loadPrefs() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(PREF_STORE_KEY) || 'null'); } catch (e) { saved = null; }
  const v = (saved && typeof saved === 'object') ? saved : {};
  sePrefs = {
    outDir:     (typeof v.outDir === 'string') ? v.outDir : PREF_DEFAULTS.outDir,
    /* ⑪（用户 2026-10-11）：「移动原件」这个叫法不对，也不叫移动 —— 它是
       **修改原件**：直接改磁盘上原件自己的文件名，内容与位置都不动，是被允许的。
       data-v 由 'move' 改成 'inplace'（POST /api/pool/run 的 rename.mode 新取值）；
       老记忆里可能还留着 'move'，落到默认 'copy' 是安全的读法。 */
    renameMode: v.renameMode === 'inplace' ? 'inplace' : PREF_DEFAULTS.renameMode,
    policy:     v.policy === 'stop' ? 'stop' : PREF_DEFAULTS.policy,
  };
  return sePrefs;
}

function savePrefs() {
  try { localStorage.setItem(PREF_STORE_KEY, JSON.stringify(sePrefs)); }
  catch (e) { /* localStorage 不可用时静默降级 */ }
}

/** 输出目录写回：工作台流水线读的是 `el.gOutDir.value`，旧模块读 `gstate.outDir`，
    两边都写才算真生效（只改「当前」那份 gstate，不碰别的模块状态）。 */
function seApplyOutDir(dir) {
  const v = (dir || '').trim();
  sePrefs.outDir = v;
  el.gOutDir.value = v;
  gstate.outDir = v;
  savePrefs();
}

/** 「已保存」提示：约 1.2 秒后自己收回去。 */
function seFlashSaved() {
  if (!el.seSaved) return;
  el.seSaved.hidden = false;
  clearTimeout(seSavedTimer);
  seSavedTimer = setTimeout(() => { el.seSaved.hidden = true; }, 1200);
}

/** 分段按钮组：只给选中项挂 `.on`（CSS 侧配 `.se-seg.on`），null 自动忽略。 */
function seSyncSeg(buttons, value) {
  buttons.forEach((b) => { if (b) b.classList.toggle('on', b.dataset.v === value); });
}

/** 同步两组分段按钮的选中态。 */
function seSyncSegs() {
  seSyncSeg([el.seRenameCopy, el.seRenameMove], sePrefs.renameMode);
  seSyncSeg([el.sePolicySkip, el.sePolicyStop], sePrefs.policy);
}

/** 同步外观按钮的选中态（供 applyTheme 复用）。 */
function seSyncTheme() {
  seSyncSeg([el.seThemeDark, el.seThemeLight], document.documentElement.dataset.theme);
}

/** 首次运行引导（用户第 8 条）：抬头与税号都还空着就提示去设置页填。
    只翻转 hidden —— #seBanner 在工作台空状态块里，绝不能给它加默认可见状态。 */
function seSyncFirstRun() {
  const empty = !el.gExpName.value.trim() && !el.gExpTax.value.trim();
  if (el.seBanner) el.seBanner.hidden = seBannerDismissed || !empty;
  if (el.seHintHeader) el.seHintHeader.hidden = !empty;   // 设置页里那句「抬头还没填」
}

/** 进入设置视图：把当前真值刷进表单。 */
function seOnEnter() {
  if (!seReady()) return;
  el.seName.value = el.gExpName.value;
  el.seTax.value = el.gExpTax.value;
  el.seOutDir.value = sePrefs.outDir || el.gOutDir.value;
  seSyncSegs();
  seSyncTheme();
  el.sePath.textContent = location.origin;   // 「关于」里的服务地址
  seLoadVersion();                           // 版本异步填，拿不到就留「—」
  seSyncFirstRun();
}

/** 版本号取 `GET /api/health` —— 不硬编码版本，后端没起来就显示「—」。 */
async function seLoadVersion() {
  if (!seVerCache) {
    try {
      const { data } = await api(API.health);
      if (data && data.ok && data.version) seVerCache = 'v' + data.version;
    } catch (e) { /* 拿不到就留空 */ }
  }
  if (!el.seVer) return;
  el.seVer.textContent = seVerCache || '—';
}

/** 设置视图的事件接线。DOM 不齐备时整块跳过（见 seReady 注释）。 */
function bindSettings() {
  if (!seReady()) return;

  // 返回工作台：顶栏齿轮是开关（再点一次也能回），这里给第二条更显眼的出口
  const back = $('seBack');
  if (back) back.addEventListener('click', () => switchModule('workbench'));

  // 抬头 / 税号：真源仍是核对模块那两个 input，这里只是同一份数据的另一个入口
  el.seName.addEventListener('input', () => {
    el.gExpName.value = el.seName.value;
    saveCheckExpected();
    seFlashSaved();
    seSyncFirstRun();
  });
  el.seTax.addEventListener('input', () => {
    el.gExpTax.value = el.seTax.value;
    saveCheckExpected();
    seFlashSaved();
    seSyncFirstRun();
  });

  // 输出目录：输入即生效（写回 gOutDir + gstate），打开目录用当前值
  el.seOutDir.addEventListener('input', () => { seApplyOutDir(el.seOutDir.value); seFlashSaved(); });
  el.sePickDir.addEventListener('click', async () => {
    try {
      const { data } = await api(API.pickdir, { method: 'POST', body: '{}' });
      if (data.ok && data.dir) {
        el.seOutDir.value = data.dir;
        seApplyOutDir(data.dir);
        seFlashSaved();
        toast('输出目录已更新', 'ok');
      } else if (data.ok) toast('未选择目录', 'info');
      else toast(data.error || '对话框不可用', 'err');
    } catch (err) { toast('选择目录失败：' + err.message, 'err'); }
  });
  el.seOpenDir.addEventListener('click', async () => {
    const dir = (el.seOutDir.value || '').trim();
    if (!dir) { toast('请先选择输出目录', 'err'); return; }
    const { data } = await api(API.openFolder, { method: 'POST', body: JSON.stringify({ dir }) });
    if (!data.ok) toast(data.error || '打开失败', 'err');
  });

  /* 重命名方式（copy / inplace）—— ⑪ 起两项都可用：
     copy = 原件不动、副本进输出目录；inplace = 直接改磁盘上原件的文件名。
     老的 'move' 已从 DOM 里去掉，`btn.disabled` 这层保护留着（失败策略还在用）。 */
  el.seRenameMode.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-v]');
    if (!btn || btn.disabled) return;
    sePrefs.renameMode = btn.dataset.v;
    savePrefs();
    seSyncSegs();
    seFlashSaved();
  });

  // 失败策略（skip / stop）—— stop 仍是 disabled，点了不响应
  el.sePolicy.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-v]');
    if (!btn || btn.disabled) return;
    sePrefs.policy = btn.dataset.v;
    savePrefs();
    seSyncSegs();
    seFlashSaved();
  });

  // 外观：和右上角主题按钮走同一个 applyTheme()
  el.seThemeDark.addEventListener('click', () => applyTheme('dark'));
  el.seThemeLight.addEventListener('click', () => applyTheme('light'));
}

/** 首次运行引导的两个按钮（#seBanner 在工作台空状态里，与设置视图分开接线）。 */
function bindFirstRun() {
  if (el.seBannerGo) el.seBannerGo.addEventListener('click', () => switchModule('settings'));
  if (el.seBannerClose) el.seBannerClose.addEventListener('click', () => {
    seBannerDismissed = true;        // 只在本次会话隐藏，不写 localStorage
    el.seBanner.hidden = true;
  });
}

