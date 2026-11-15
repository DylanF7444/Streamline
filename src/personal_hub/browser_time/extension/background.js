/* Metadata only. No content scripts, network requests, URLs in storage, or history API. */
let browser = "chrome";
async function detectBrowser(preference) {
  if (["chrome", "edge", "brave"].includes(preference)) return preference;
  try {
    if (typeof navigator.brave?.isBrave === "function" && await navigator.brave.isBrave()) return "brave";
  } catch { /* Explicit popup choice handles unavailable detection. */ }
  return /Edg\//.test(navigator.userAgent) ? "edge" : "chrome";
}
let port = null, timer = null, allowed = false, revision = 0, connectionRevision = 0;
let lastResponse = 0, activeDomain = null, pending = [];
const indicators = {
  paused: ["OFF", "#b0b5bc", "Browser recording paused"],
  connecting: ["WAIT", "#f4bd50", "Connecting to Hub…"],
  waiting: ["WAIT", "#f4bd50", "Waiting for Hub to save a fresh site interval"],
  focus: ["WAIT", "#f4bd50", "Waiting for browser focus"],
  idle: ["WAIT", "#f4bd50", "Paused — no recent input"],
  ineligible: ["WAIT", "#f4bd50", "No eligible site — private, excluded or internal page"],
  recording: ["REC", "#53bda4", "Recording — Hub confirmed saved site time"],
  error: ["!", "#ff7373", "Hub connection unavailable; check setup"],
};
function indicate(state, description) {
  const [text, color, fallback] = indicators[state] || indicators.waiting;
  const status = description || fallback;
  chrome.action.setBadgeText({text});
  chrome.action.setBadgeBackgroundColor({color});
  chrome.action.setTitle({title: `Personal Hub Browser Time: ${status}`});
  chrome.storage.local.set({status, indicator: state, statusAt: Date.now()});
}
const host = "com.personalhub.browser_time";

function connectionStatus(message) {
  const error = String(message || "").toLowerCase();
  if (error.includes("not found") || error.includes("not registered")) return "Hub helper not registered for this browser. Run Set up browser extension in Hub.";
  if (error.includes("forbidden") || error.includes("not allowed")) return "Extension ID is not authorized. Repeat Hub setup with this extension's ID.";
  if (error.includes("exited") || error.includes("failed to start")) return "Hub helper could not stay running. Check the helper executable in Hub setup.";
  return "Hub helper connection failed. Repeat setup in Hub and check the helper executable.";
}

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
  if (Date.now() - lastResponse > 12000) {
    stop();
    indicate("error", "Hub helper stopped responding; reconnecting shortly");
    return;
  }
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
  if (domain !== activeDomain) indicate(domain ? "waiting" : "ineligible");
  activeDomain = domain;
  if (pending.length >= 32) {
    stop();
    indicate("error", "Hub helper is not keeping up; reconnecting shortly");
    return;
  }
  pending.push(domain);
  try { port.postMessage({version: 1, browser, domain}); }
  catch { stop(); indicate("error"); }
}
function stop() {
  connectionRevision++;
  revision++;
  allowed = false;
  activeDomain = null;
  pending = [];
  if (timer) clearInterval(timer);
  timer = null;
  const old = port;
  port = null;
  if (old) old.disconnect();
}
async function connect() {
  const attempt = ++connectionRevision;
  const state = await chrome.storage.local.get(["enabled", "browser"]);
  if (!state.enabled) { indicate("paused"); return; }
  if (port) return;
  indicate("connecting");
  const detected = await detectBrowser(state.browser);
  const permissions = await chrome.permissions.contains({permissions: ["tabs", "nativeMessaging"]});
  if (!permissions) {
    indicate("error", "Permission required — enable browser tracking");
    await chrome.storage.local.set({enabled: false});
    return;
  }
  if (attempt !== connectionRevision || port) return;
  browser = detected;
  try {
    const connection = chrome.runtime.connectNative(host);
    port = connection;
    lastResponse = Date.now();
    connection.onMessage.addListener(message => {
      if (port !== connection) return;
      lastResponse = Date.now();
      const sentDomain = pending.shift();
      allowed = message.recording === true;
      if (!allowed) {
        indicate(["idle", "focus"].includes(message.state) ? message.state : "waiting",
          ["idle", "focus"].includes(message.state) ? undefined : "Waiting for Start browser recording in Hub");
      } else if (sentDomain !== activeDomain) {
        indicate("waiting");
      } else if (message.state === "recording" && activeDomain) {
        indicate("recording");
      } else {
        indicate(message.state === "ineligible" ? "ineligible" : "waiting",
          message.state ? undefined : "Connected; update Hub and its helper for recording confirmation");
      }
    });
    connection.onDisconnect.addListener(() => {
      // Classify locally; never store or display raw errors, which may contain paths.
      const status = connectionStatus(chrome.runtime.lastError?.message);
      if (port !== connection) return;
      port = null;
      stop();
      indicate("error", status);
    });
    timer = setInterval(snapshot, 2000);
    await snapshot();
  } catch {
    stop();
    indicate("error", "Hub helper unavailable. Complete setup in Hub.");
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
  if (area === "local" && (changes.enabled || changes.browser)) {
    stop();
    connect();
  }
});
chrome.permissions.onRemoved.addListener(async () => {
  if (!await chrome.permissions.contains({permissions: ["tabs", "nativeMessaging"]})) {
    stop();
    indicate("error", "Paused: permissions removed");
    await chrome.storage.local.set({enabled: false});
  }
});
connect();
