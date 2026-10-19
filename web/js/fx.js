/* ==========================================================================
   fx.js — 外围特效脚本（只做一件事：#bgCanvas 的粒子星野）

   职责边界
   --------
   背景渐变光晕、漫游光斑、扫描带、工具条扫描光全在 css/fx.css 里用 CSS 完成。
   本文件**只**画 canvas 上缓慢漂浮的细光点，零依赖、零构建（原生 <script>）。

   硬约束
   ------
   1. 原生 JS，不引任何库；项目没有打包器。
   2. 用 requestAnimationFrame；标签页隐藏（visibilitychange）即停帧；
      窗口 resize 重新适配，并考虑 devicePixelRatio。
   3. **必须尊重 prefers-reduced-motion: reduce** —— 命中时一个点都不画、不启动，
      留静态渐变底（fx.css 的 .bg::before）即可。
   4. 颜色不许硬编码：一律从 CSS 令牌读（明暗切换 / 换主色都不用改这里）。
   5. canvas 的尺寸与定位由 fx.css 控制，本文件不写任何 px 样式，
      只写 canvas.width/height 这两个**属性**（backing store）。
   ========================================================================== */
(function () {
  'use strict';

  var cv = document.getElementById('bgCanvas');
  if (!cv || typeof cv.getContext !== 'function') return;
  var ctx = cv.getContext('2d');
  if (!ctx || typeof window.requestAnimationFrame !== 'function') return;

  var root = document.documentElement;

  /* ── 参数 ─────────────────────────────────────────────── */
  var COUNT    = 96;       /* 只画 96 个点：够看出星野，不烧帧 */
  var FRAME_MS = 40;       /* ~25fps。星野是慢速漂浮，不需要 60fps */
  var BASE_A   = 0.18;     /* 单点最低透明度 */
  var RAND_A   = 0.44;     /* 透明度随机幅度 → 单点 0.18 ~ 0.62 */
  var TAU      = 6.283185307179586;

  var dots = [];
  var cols = [];
  var dpr = 1, W = 0, H = 0;
  var raf = null, last = 0;

  /* 确定性伪随机（样板同款 LCG），避免用 Math.random 造成每次 resize 抖动 */
  var seed = 20261011;
  function rnd() {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  }

  /* ── 颜色：从 CSS 令牌读，绝不硬编码 ──────────────────── */
  function readCols() {
    var cs = getComputedStyle(root);
    var out = [];
    var names = ['--accent', '--accent2'];
    for (var i = 0; i < names.length; i++) {
      var v = (cs.getPropertyValue(names[i]) || '').trim();
      if (v) out.push(v);
    }
    if (out.length) cols = out;   /* 读不到就沿用上一次的，别用空串去画 */
  }

  /* ── 适配视口 ─────────────────────────────────────────── */
  function build() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth;
    H = window.innerHeight;
    /* 只改 backing store；显示尺寸交给 fx.css 的 #bgCanvas{width/height:100%} */
    cv.width  = Math.max(1, Math.floor(W * dpr));
    cv.height = Math.max(1, Math.floor(H * dpr));
    dots.length = 0;
    for (var n = 0; n < COUNT; n++) {
      dots.push({
        x:  rnd() * W,
        y:  rnd() * H,
        r:  0.6 + rnd() * 1.5,
        vx: (rnd() - 0.5) * 0.16,
        vy: -(0.06 + rnd() * 0.2),
        a:  BASE_A + rnd() * RAND_A
      });
    }
  }

  /* ── 逐帧绘制 ─────────────────────────────────────────── */
  function frame(ts) {
    raf = window.requestAnimationFrame(frame);
    if (ts - last < FRAME_MS) return;    /* 降帧：超出的帧直接跳过，不排队 */
    last = ts;
    if (!cols.length) return;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);   /* 之后按 CSS 像素坐标画 */

    for (var n = 0; n < dots.length; n++) {
      var d = dots[n];
      d.x += d.vx;
      d.y += d.vy;
      if (d.y < -8) { d.y = H + 8; d.x = rnd() * W; }   /* 飘出上边就回到底部 */
      if (d.x < -8) d.x = W + 8;
      if (d.x > W + 8) d.x = -8;
      ctx.globalAlpha = d.a;
      ctx.fillStyle = cols[n % cols.length];
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, TAU);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  /* ── 启停 ─────────────────────────────────────────────── */
  var mq = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : null;

  function reduced() { return !!(mq && mq.matches); }

  function start() {
    if (raf || reduced() || document.hidden || !dots.length) return;
    last = 0;
    raf = window.requestAnimationFrame(frame);
  }

  function stop() {
    if (!raf) return;
    window.cancelAnimationFrame(raf);
    raf = null;
  }

  function clear() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
  }

  function sync() {
    if (reduced() || document.hidden) { stop(); clear(); return; }
    start();
  }

  /* ── 事件 ─────────────────────────────────────────────── */
  window.addEventListener('resize', function () {
    stop();
    build();
    start();
  });

  document.addEventListener('visibilitychange', sync);

  if (mq) {
    if (typeof mq.addEventListener === 'function') mq.addEventListener('change', sync);
    else if (typeof mq.addListener === 'function') mq.addListener(sync);   /* 老 Safari */
  }

  /* 明暗主题切换时重新取色（app.js 改的是 html[data-theme]） */
  if (typeof window.MutationObserver === 'function') {
    new MutationObserver(readCols).observe(root, {
      attributes: true,
      attributeFilter: ['data-theme']
    });
  }

  /* ── 启动 ─────────────────────────────────────────────── */
  readCols();
  build();
  sync();     /* 命中 reduced-motion 时 sync() 不会 start()，一个点都不画 */
})();
