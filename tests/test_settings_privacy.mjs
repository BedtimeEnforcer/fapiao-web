// 无浏览器、无发票库：复现同一 origin 中旧设置被便携版读取的问题。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const code = readFileSync(new URL('../web/js/settings.js', import.meta.url), 'utf8');
function context(edition, entries = {}) {
  const store = new Map(Object.entries(entries));
  const ctx = vm.createContext({
    document: { documentElement: { dataset: { edition } } },
    el: { gExpName: { value: '' }, gExpTax: { value: '' } },
    localStorage: { getItem: key => store.get(key) ?? null,
      setItem: (key, value) => store.set(key, value) },
    console,
  });
  vm.runInContext(code, ctx);
  return { ctx, store, run: js => vm.runInContext(js, ctx) };
}

const old = {
  'fapiao-check-expected': JSON.stringify({ name: '示例企业', tax: 'DEMO' }),
  'fapiao-prefs-v1': JSON.stringify({ outDir: 'Z:/old-output', renameMode: 'inplace' }),
};
const desktop = context('desktop', old);
desktop.run('loadCheckExpected(); loadPrefs();');
assert.equal(desktop.ctx.el.gExpName.value, '');
assert.equal(desktop.ctx.el.gExpTax.value, '');
assert.equal(desktop.run('sePrefs.outDir'), '');
assert.equal(desktop.run('sePrefs.renameMode'), 'copy');
assert.equal(desktop.store.get('fapiao-check-expected'), old['fapiao-check-expected']);
desktop.ctx.el.gExpName.value = '用户主动填写的示例';
desktop.run('saveCheckExpected(); loadCheckExpected();');
assert.equal(desktop.ctx.el.gExpName.value, '用户主动填写的示例');
assert.ok(desktop.store.has('fapiao-desktop-check-expected'));

const source = context('source', old);
source.run('loadCheckExpected(); loadPrefs();');
assert.equal(source.ctx.el.gExpName.value, '示例企业');
assert.equal(source.run('sePrefs.outDir'), 'Z:/old-output');
const fresh = context('desktop');
fresh.run('loadCheckExpected();');
assert.equal(fresh.ctx.el.gExpName.value, '');
const invalid = context('desktop', { 'fapiao-desktop-check-expected': '{invalid' });
invalid.run('loadCheckExpected();');
assert.equal(invalid.ctx.el.gExpName.value, '');
console.log('PASS: 便携版空白默认、隔离旧抬头/目录、保留源码版设置、主动保存及异常设置回退');
