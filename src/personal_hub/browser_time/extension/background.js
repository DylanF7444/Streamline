/* Metadata only. No content scripts, network requests, URLs in storage, or history API. */
const browser = /Edg\//.test(navigator.userAgent) ? "edge" : "chrome";
let port = null, timer = null, allowed = false, revision = 0;
const host = "com.personalhub.browser_time";

function domainOf(raw) {
  try {
    const url = new URL(raw);
    if (!["https:", "http:"].includes(url.protocol)) return null;
    const name = url.hostname.toLowerCase().replace(/\.$/, "");
    if (name.length > 253 || !name.includes(".") || /^\d+(\.\d+){3}$/.test(name)) return null;
    if (/\.(local|localhost|internal|test|invalid|example)$/.test(name)) return null;
    if (!name.split(".").every(p => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(p))) return null;
    return name;
  } catch { return null; }
}
async function snapshot() {
  const stamp = ++revision;
  let domain = null;
  if (!port) return;
  if (allowed) {
    try {
      const window = await chrome.windows.getLastFocused();
      if (window.focused && !window.incognito && window.type === "normal") {
        const [tab] = await chrome.tabs.query({active: true, windowId: window.id});
        if (tab && !tab.incognito) domain = domainOf(tab.url);
      }
    } catch { domain = null; }
  }
  if (stamp !== revision || !port) return;
  try { port.postMessage({version: 1, browser, domain}); } catch { stop(); }
}
function stop() {
  revision++;
  allowed = false;
  if (timer) clearInterval(timer);
  timer = null;
  const old = port;
  port = null;
  if (old) old.disconnect();
}
async function connect() {
  const state = await chrome.storage.local.get("enabled");
  if (!state.enabled || port) return;
  const permissions = await chrome.permissions.contains({permissions: ["tabs", "nativeMessaging"]});
  if (!permissions) {
    await chrome.storage.local.set({enabled: false, status: "Permission required"});
    return;
  }
  try {
    port = chrome.runtime.connectNative(host);
    port.onMessage.addListener(message => {
      allowed = message.recording === true;
      chrome.storage.local.set({status: allowed ? "Recording while Hub is running and unlocked" : "Waiting for Start browser recording in Hub"});
    });
    port.onDisconnect.addListener(() => {
      // Read lastError to suppress browser diagnostics; never serialize its raw text.
      void chrome.runtime.lastError;
      port = null;
      stop();
      chrome.storage.local.set({status: "Hub helper unavailable. Complete setup in Hub."});
    });
    timer = setInterval(snapshot, 2000);
    await snapshot();
  } catch {
    stop();
    await chrome.storage.local.set({status: "Hub helper unavailable. Complete setup in Hub."});
  }
}
chrome.tabs.onActivated.addListener(snapshot);
chrome.tabs.onUpdated.addListener(snapshot);
chrome.tabs.onRemoved.addListener(snapshot);
chrome.windows.onFocusChanged.addListener(snapshot);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("reconnect", {periodInMinutes: 0.5});
  connect();
});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === "reconnect") connect(); });
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.enabled) {
    if (changes.enabled.newValue) connect(); else stop();
  }
});
chrome.permissions.onRemoved.addListener(async () => {
  if (!await chrome.permissions.contains({permissions: ["tabs", "nativeMessaging"]})) {
    stop();
    await chrome.storage.local.set({enabled: false, status: "Paused: permissions removed"});
  }
});
connect();
