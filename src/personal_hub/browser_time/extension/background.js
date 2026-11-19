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
let popupVisible = false;
// Display data stays in worker memory, never extension storage.
let usage = null;
function validUsage(value) {
  const seconds = n => Number.isSafeInteger(n) && n >= 0 && n <= 7 * 86400;
  const site = s => s && typeof s.domain === "string" && domainOf(`https://${s.domain}`) === s.domain && seconds(s.seconds);
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value.date) ||
      typeof value.timezone !== "string" || value.timezone.length > 80 ||
      !seconds(value.total_seconds) || !seconds(value.other_seconds) ||
      !Array.isArray(value.sites) || value.sites.length > 8 || !value.sites.every(site) ||
      (value.current !== null && !site(value.current))) return null;
  if (new Set(value.sites.map(s => s.domain)).size !== value.sites.length ||
      value.sites.reduce((sum, s) => sum + s.seconds, value.other_seconds) !== value.total_seconds ||
      (value.current && value.current.seconds > value.total_seconds)) return null;
  const cleanSite = s => ({domain: s.domain, seconds: s.seconds});
  return {date: value.date, timezone: value.timezone, total_seconds: value.total_seconds,
    other_seconds: value.other_seconds, sites: value.sites.map(cleanSite),
    current: value.current ? cleanSite(value.current) : null};
}
function timeBadge(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 36000) return `${(Math.floor(seconds / 360) / 10).toFixed(1)}h`;
  return `${Math.floor(seconds / 3600)}h`;
}
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
let iconState = null;
function updateToolbarIcon(state, color) {
  if (iconState === state || typeof OffscreenCanvas === "undefined") return;
  const imageData = {};
  for (const size of [16, 32]) {
    const canvas = new OffscreenCanvas(size, size);
    const ctx = canvas.getContext("2d");
    ctx.scale(size / 16, size / 16);
    // A browser window with a state symbol stays legible at toolbar size.
    ctx.fillStyle = "#22282d";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.roundRect(1, 1, 14, 14, 3);
    ctx.fill();
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(1, 5);
    ctx.lineTo(15, 5);
    ctx.stroke();
    ctx.fillStyle = color;
    if (state === "recording") {
      ctx.beginPath();
      ctx.arc(7, 9.5, 2.5, 0, 2 * Math.PI);
      ctx.fill();
    } else if (state === "paused") {
      ctx.fillRect(4, 7, 2, 5);
      ctx.fillRect(8, 7, 2, 5);
    } else if (state === "error") {
      ctx.fillRect(6, 6.5, 2, 4);
      ctx.fillRect(6, 12, 2, 1.5);
    } else {
      ctx.beginPath();
      ctx.arc(7, 9.5, 3, 0, 2 * Math.PI);
      ctx.moveTo(7, 7.5);
      ctx.lineTo(7, 9.5);
      ctx.lineTo(9, 10.5);
      ctx.stroke();
    }
    imageData[size] = ctx.getImageData(0, 0, size, size);
  }
  chrome.action.setIcon({imageData});
  iconState = state;
}
function indicate(state, description) {
  const [text, color, fallback] = indicators[state] || indicators.waiting;
  const status = description || fallback;
  updateToolbarIcon(state, color);
  const current = state === "recording" && usage?.current?.domain === activeDomain ? usage.current : null;
  chrome.action.setBadgeText({text: current ? timeBadge(current.seconds) : text});
  chrome.action.setBadgeBackgroundColor({color});
  chrome.action.setTitle({title: `Personal Hub Browser Time: ${status}${current ? ` · ${timeBadge(current.seconds)} saved today on the focused site` : ""}`});
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
      const focusedWindow = await chrome.windows.getLastFocused();
      let tabWindow = focusedWindow?.focused && !focusedWindow.incognito ? focusedWindow : null;
      if (popupVisible || tabWindow?.type === "popup") {
        // The action popup can make the browser window report unfocused while it is visible.
        // Its live extension port marks that case so the selected normal tab stays current.
        tabWindow = await chrome.windows.getLastFocused({windowTypes: ["normal"]});
      }
      if (tabWindow && !tabWindow.incognito && tabWindow.type === "normal") {
        const [tab] = await chrome.tabs.query({active: true, windowId: tabWindow.id});
        if (tab && !tab.incognito) domain = domainOf(tab.url);
      }
    } catch { domain = null; }
  }
  if (stamp !== revision || !port) return;
  if (domain !== activeDomain) {
    usage = null;
    indicate(domain ? "waiting" : "ineligible");
  }
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
  usage = null;
  pending = [];
  if (timer) clearInterval(timer);
  timer = null;
  const old = port;
  port = null;
  if (old) old.disconnect();
}
async function connect() {
  const attempt = ++connectionRevision;
  const state = await chrome.storage.local.get(["enabled", "browser", "nativeApiReloadPending"]);
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
  // Chromium may grant optional nativeMessaging without exposing its API to
  // the already-running worker. Recreate that context once, preserving consent.
  if (typeof chrome.runtime.connectNative !== "function") {
    if (!state.nativeApiReloadPending) {
      await chrome.storage.local.set({nativeApiReloadPending: true});
      indicate("connecting", "Refreshing extension after permission grant…");
      chrome.runtime.reload();
    } else {
      indicate("error", "Browser connection API unavailable. Reload this extension on the extensions page or restart the browser.");
    }
    return;
  }
  if (state.nativeApiReloadPending) {
    await chrome.storage.local.set({nativeApiReloadPending: false});
    if (attempt !== connectionRevision || port) return;
  }
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
      usage = allowed && activeDomain && sentDomain === activeDomain ? validUsage(message.usage) : null;
      if (usage?.current?.domain !== activeDomain && usage) usage = {...usage, current: null};
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
chrome.runtime.onConnect.addListener(connection => {
  if (connection.name !== "popup" || connection.sender?.url !== chrome.runtime.getURL("popup.html")) return;
  popupVisible = true;
  void snapshot();
  connection.onDisconnect.addListener(() => {
    popupVisible = false;
    void snapshot();
  });
});
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
chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message?.type !== "getUsage") return;
  reply({usage: port && Date.now() - lastResponse <= 12000 ? usage : null});
});
connect();
