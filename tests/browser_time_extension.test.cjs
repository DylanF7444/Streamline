const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/personal_hub/browser_time/extension/background.js'), 'utf8');
const event = () => ({handlers: [], addListener(f) {this.handlers.push(f);}});
async function harness(enabled = true) {
  const messages = [], intervals = [], state = {enabled}, listeners = {};
  let reads = 0;
  const native = {postMessage: m => messages.push(m), onMessage: event(), onDisconnect: event(), disconnect() {}};
  const chrome = {
    storage: {local: {get: async () => state, set: async value => Object.assign(state, value)}, onChanged: event()},
    permissions: {contains: async () => true, onRemoved: event()},
    runtime: {connectNative: () => native, onStartup: event(), onInstalled: event()},
    alarms: {create() {}, onAlarm: event()},
    windows: {getLastFocused: async () => ({id: 1, focused: true, incognito: false, type: 'normal'}), onFocusChanged: event()},
    tabs: {query: async () => {reads++; return [{url: 'https://example.com/private?q=secret'}];}, onActivated: event(), onUpdated: event(), onRemoved: event()},
  };
  const context = vm.createContext({chrome, navigator: {userAgent: 'Chrome'}, URL, setInterval(f) {intervals.push(f); return 1;}, clearInterval() {}});
  vm.runInContext(source, context);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  return {chrome, native, context, messages, intervals, state, reads: () => reads, flush};
}
test('no URL access until both extension and Hub opt in', async () => {
  const disabled = await harness(false);
  assert.equal(disabled.messages.length, 0);
  assert.equal(disabled.reads(), 0);
  const h = await harness();
  assert.equal(h.messages[0].domain, null);
  assert.equal(h.reads(), 0);
  h.native.onMessage.handlers[0]({recording: true});
  await h.intervals[0]();
  assert.equal(h.messages.at(-1).domain, 'example.com');
  assert.equal(JSON.stringify(h.messages).includes('secret'), false);
  assert.equal(JSON.stringify(h.state).includes('example.com'), false);
  h.chrome.windows.getLastFocused = async () => ({id: 1, focused: false});
  await h.intervals[0]();
  assert.equal(h.messages.at(-1).domain, null);
});
test('private windows and non-web/local addresses excluded', async () => {
  const h = await harness();
  h.native.onMessage.handlers[0]({recording: true});
  h.chrome.windows.getLastFocused = async () => ({id: 1, focused: true, incognito: true, type: 'normal'});
  await h.intervals[0]();
  assert.equal(h.reads(), 0);
  for (const raw of ['chrome://settings', 'file:///secret', 'https://127.0.0.1', 'https://printer.local', 'https://localhost', 'https://[::1]']) {
    assert.equal(vm.runInContext(`domainOf(${JSON.stringify(raw)})`, h.context), null);
  }
  assert.equal(vm.runInContext('domainOf("https://mail.example.co.uk/private")', h.context), 'mail.example.co.uk');
});
test('permission revocation stops collection', async () => {
  const h = await harness();
  h.native.onMessage.handlers[0]({recording: true});
  h.chrome.permissions.contains = async () => false;
  await h.chrome.permissions.onRemoved.handlers[0]();
  const count = h.messages.length;
  await h.intervals[0]();
  assert.equal(h.messages.length, count);
  assert.equal(h.state.enabled, false);
});
test('popup requests permission directly from the click gesture', async () => {
  let click, requested = false;
  const elements = {toggle: {addEventListener: (_name, f) => {click = f;}}, status: {}};
  const chrome = {
    storage: {local: {get: async () => ({enabled: false}), set: async () => {}}, onChanged: event()},
    permissions: {request: () => {requested = true; return Promise.resolve(true);}},
  };
  const popup = fs.readFileSync(path.join(__dirname, '../src/personal_hub/browser_time/extension/popup.js'), 'utf8');
  vm.runInNewContext(popup, {chrome, document: {getElementById: id => elements[id]}});
  await new Promise(resolve => setImmediate(resolve));
  const done = click();
  assert.equal(requested, true);
  await done;
});
