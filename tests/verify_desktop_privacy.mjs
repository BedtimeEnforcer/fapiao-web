// 只连接已用独立数据目录启动的便携版；必须显式提供沙盒地址。
// node tests/verify_desktop_privacy.mjs http://127.0.0.1:8815
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const base = process.argv[2];
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base || '') || /:(8765|8766)$/.test(base)) {
  throw Error('必须显式指定独立沙盒地址，禁止连接默认用户实例');
}
const health = await (await fetch(base + '/api/health')).json();
assert.equal(health.edition, 'desktop', '需要实际 exe，而非源码服务');
const profile = mkdtempSync(join(tmpdir(), 'baoxiao-privacy-'));
const edge = spawn('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-proxy-server',
  '--remote-debugging-port=9254', `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore', windowsHide: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws;
try {
  let version;
  for (let i = 0; i < 100; i++) {
    try { version = await (await fetch('http://127.0.0.1:9254/json/version')).json(); break; }
    catch { await sleep(200); }
  }
  assert.ok(version, '独立 Edge 启动');
  ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let seq = 0;
  const pending = new Map(), errors = [];
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.text);
    if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const send = (method, params = {}, sessionId) => new Promise((r, j) => {
    const id = ++seq;
    pending.set(id, m => m.error ? j(Error(JSON.stringify(m.error))) : r(m.result));
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  const s = (method, params = {}) => send(method, params, sessionId);
  await s('Runtime.enable');
  await s('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  const ev = async expression => {
    const r = await s('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw Error(r.exceptionDetails.text);
    return r.result.value;
  };
  async function ready() {
    for (let i = 0; i < 100; i++) {
      if (await ev('document.readyState === "complete" && typeof sePrefs !== "undefined" && typeof el !== "undefined" && !!el.seName')) return;
      await sleep(100);
    }
    throw Error('界面未就绪');
  }
  await s('Page.navigate', { url: base });
  await ready();
  await ev('document.getElementById("btnSettings").click()');
  assert.equal(await ev('el.seName.value'), '');
  assert.equal(await ev('el.seTax.value'), '');
  console.log('PASS: 全新浏览器首次打开，抬头与税号为空');
  const screenshot = await s('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(profile, 'first-run-settings.png'), Buffer.from(screenshot.data, 'base64'));

  await ev(`localStorage.setItem('fapiao-check-expected', JSON.stringify({name:'示例企业',tax:'DEMO'}));
    localStorage.setItem('fapiao-prefs-v1', JSON.stringify({outDir:'Z:/demo-old-output'}));`);
  await s('Page.reload');
  await sleep(200);
  await ready();
  assert.equal(await ev('el.gExpName.value'), '');
  assert.equal(await ev('el.gExpTax.value'), '');
  assert.equal(await ev('sePrefs.outDir'), '');
  console.log('PASS: 同一地址存在源码版旧抬头和目录，便携版仍为空白');
  await ev(`document.getElementById('btnSettings').click();
    el.seName.value='用户填写的示例';el.seName.dispatchEvent(new Event('input',{bubbles:true}));`);
  await s('Page.reload');
  await sleep(200);
  await ready();
  assert.equal(await ev('el.gExpName.value'), '用户填写的示例');
  assert.equal(await ev('JSON.parse(localStorage.getItem("fapiao-check-expected")).name'), '示例企业');
  assert.deepEqual(errors, []);
  console.log('PASS: 主动保存后可恢复，源码版旧设置未被改动，无脚本异常');
  console.log('SCREENSHOT=' + join(profile, 'first-run-settings.png'));
} finally {
  if (ws) ws.close();
  try { execFileSync('taskkill', ['/PID', String(edge.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true }); }
  catch { /* 只清理本脚本启动的进程。 */ }
}
