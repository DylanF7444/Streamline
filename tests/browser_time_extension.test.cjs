const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/personal_hub/browser_time/extension/background.js'), 'utf8');
const event = () => ({handlers: [], addListener(f) {this.handlers.push(f);}});
async function harness(enabled = true, options = {}) {
  const messages = [], intervals = [], state = {enabled, browser: options.browser};
  let reads = 0;
  const native = {postMessage: m => messages.push(m), onMessage: event(), onDisconnect: event(), disconnect() {}};
  const badge = {};
  const chrome = {
    action: {setBadgeText: v => Object.assign(badge, v), setBadgeBackgroundColor: v => Object.assign(badge, v), setTitle: v => Object.assign(badge, v)},
    storage: {local: {get: async () => state, set: async value => Object.assign(state, value)}, onChanged: event()},
    permissions: {contains: async () => true, onRemoved: event()},
    runtime: {connectNative: () => native, onStartup: event(), onInstalled: event()},
    alarms: {create() {}, onAlarm: event()},
    windows: {getLastFocused: async () => ({id: 1, focused: true, incognito: false, type: 'normal'}), onFocusChanged: event()},
    tabs: {query: async () => {reads++; return [{url: 'https://example.com/private?q=secret'}];}, onActivated: event(), onUpdated: event(), onRemoved: event()},
  };
  const context = vm.createContext({chrome, navigator: options.navigator || {userAgent: 'Chrome'}, URL, setInterval(f) {intervals.push(f); return 1;}, clearInterval() {}});
  vm.runInContext(source, context);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  return {chrome, native, context, messages, intervals, state, badge, reads: () => reads, flush};
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
  const elements = {browser: {addEventListener() {}}, toggle: {addEventListener: (_name, f) => {click = f;}}, status: {}};
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


test('Brave detection works with a Chrome user agent and has an explicit fallback', async () => {
  const brave = await harness(true, {navigator: {userAgent: 'Chrome', brave: {isBrave: async () => true}}});
  assert.equal(brave.messages[0].browser, 'brave');
  const choice = await harness(true, {browser: 'brave'});
  assert.equal(choice.messages[0].browser, 'brave');
  const unavailable = await harness(true, {browser: 'brave', navigator: {userAgent: 'Chrome', brave: {isBrave: async () => {throw new Error('blocked');}}}});
  assert.equal(unavailable.messages[0].browser, 'brave');
  const rejected = await harness(true, {navigator: {userAgent: 'Chrome', brave: {isBrave: async () => {throw new Error('unavailable');}}}});
  assert.equal(rejected.messages[0].browser, 'chrome');
  const edge = await harness(true, {navigator: {userAgent: 'Chrome Edg/123'}});
  assert.equal(edge.messages[0].browser, 'edge');
  const denied = await harness(false, {navigator: {userAgent: 'Chrome', brave: {isBrave: async () => {throw new Error('must not probe before consent');}}}});
  assert.equal(denied.messages.length, 0);
});

test('changing the browser choice reconnects with the new identity', async () => {
  const h = await harness();
  h.state.browser = 'brave';
  h.chrome.storage.onChanged.handlers[0]({browser: {newValue: 'brave'}}, 'local');
  await h.flush();
  assert.equal(h.messages.at(-1).browser, 'brave');
  assert.equal(h.messages.at(-1).domain, null);
});
test('popup saves the explicit Brave choice when automatic detection is unavailable', async () => {
  let change;
  const state = {enabled: false};
  const elements = {browser: {value: '', addEventListener: (_name, f) => {change = f;}}, toggle: {addEventListener() {}}, status: {}};
  const chrome = {storage: {local: {get: async () => state, set: async value => Object.assign(state, value)}, onChanged: event()}, permissions: {request: async () => true}};
  const popup = fs.readFileSync(path.join(__dirname, '../src/personal_hub/browser_time/extension/popup.js'), 'utf8');
  vm.runInNewContext(popup, {chrome, document: {getElementById: id => elements[id]}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.browser.value, '');
  await change({target: {value: 'brave'}});
  assert.equal(state.browser, 'brave');
});
test('late disconnect from the previous browser choice does not close its replacement', async () => {
  const h = await harness();
  const next = {postMessage: message => h.messages.push(message), onMessage: event(), onDisconnect: event(), disconnect() {}};
  h.chrome.runtime.connectNative = () => next;
  h.state.browser = 'brave';
  h.chrome.storage.onChanged.handlers[0]({browser: {newValue: 'brave'}}, 'local');
  await h.flush();
  h.native.onDisconnect.handlers[0]();
  next.onMessage.handlers[0]({recording: true});
  await h.intervals.at(-1)();
  assert.equal(h.messages.at(-1).browser, 'brave');
  assert.equal(h.messages.at(-1).domain, 'example.com');
});


test('native connection errors have useful fixed descriptions without leaking paths', async () => {
  const h = await harness();
  h.chrome.runtime.lastError = {message: 'Specified native messaging host not found.'};
  h.native.onDisconnect.handlers[0]();
  assert.match(h.state.status, /not registered/);
  for (const [message, expected] of [
    ['Access to the specified native messaging host is forbidden.', /not authorized/],
    ['Native host has exited. C:/private/name.exe', /could not stay running/],
    ['Unknown error involving C:/private/name.exe', /connection failed/],
  ]) {
    const text = vm.runInContext(`connectionStatus(${JSON.stringify(message)})`, h.context);
    assert.match(text, expected);
    assert.equal(text.includes('private'), false);
  }
});

test('badge requires a saved interval acknowledgement and clears on tab change, pause and disconnect', async () => {
  const h = await harness();
  assert.equal(h.badge.text, 'WAIT');
  h.native.onMessage.handlers[0]({recording: true, state: 'waiting'});
  await h.intervals[0]();
  h.native.onMessage.handlers[0]({recording: true, state: 'recording'});
  assert.equal(h.badge.text, 'REC');
  assert.equal(h.badge.title.includes('example.com'), false);
  h.chrome.tabs.query = async () => [{url: 'https://other.org'}];
  await h.intervals[0]();
  assert.equal(h.badge.text, 'WAIT');
  h.native.onMessage.handlers[0]({recording: true, state: 'waiting'});
  h.native.onDisconnect.handlers[0]();
  assert.equal(h.badge.text, '!');
  h.state.enabled = false;
  h.chrome.storage.onChanged.handlers[0]({enabled: {newValue: false}}, 'local');
  await h.flush();
  assert.equal(h.badge.text, 'OFF');
});
test('badge distinguishes idle, focus and excluded pages; older helper cannot claim recording', async () => {
  const h = await harness();
  for (const state of ['idle', 'focus', 'ineligible']) {
    h.native.onMessage.handlers[0]({recording: state === 'ineligible', state});
    assert.equal(h.badge.text, 'WAIT');
  }
  h.native.onMessage.handlers[0]({recording: true});
  assert.notEqual(h.badge.text, 'REC');
});
test('a silent helper clears recording within the response deadline', async () => {
  const h = await harness();
  vm.runInContext('lastResponse = Date.now() - 13000', h.context);
  await h.intervals[0]();
  assert.equal(h.badge.text, '!');
  assert.equal(h.badge.title.includes('stopped responding'), true);
});
