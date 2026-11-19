let enabled = false;
let renderRevision = 0;
const popupConnection = chrome.runtime.connect({name: "popup"});
const colors = ["#73d6b5", "#79b6f2", "#b19bea", "#f2bf71", "#ee929b", "#78d5d9", "#b9cf7c", "#dc9fd0", "#839199"];
function duration(seconds) {
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;
}
function renderUsage(usage) {
  const bar = document.getElementById("distribution"), list = document.getElementById("sites");
  bar.replaceChildren();
  list.replaceChildren();
  document.getElementById("current-site").textContent = usage?.current?.domain || "No focused website time available";
  document.getElementById("current-time").textContent = usage?.current ? duration(usage.current.seconds) : "—";
  document.getElementById("total").textContent = usage ? duration(usage.total_seconds) : "—";
  document.getElementById("day").textContent = usage ? `${usage.date} · ${usage.timezone}` : "";
  document.getElementById("empty").hidden = !!usage?.total_seconds;
  document.getElementById("empty").textContent = usage ? "No website time saved today yet." : "Focus a website with Hub recording enabled to see today's saved time.";
  const labels = [];
  const sites = usage ? [...usage.sites, ...(usage.other_seconds ? [{domain: "Other", seconds: usage.other_seconds}] : [])] : [];
  sites.forEach((site, index) => {
    if (!site.seconds || !usage.total_seconds) return;
    const share = site.seconds / usage.total_seconds * 100;
    const label = `${site.domain}: ${share.toFixed(1)}% · ${duration(site.seconds)}`;
    const color = site.domain === "Other" ? colors[8] : colors[index];
    labels.push(label);
    const segment = document.createElement("span");
    segment.className = "segment";
    segment.style.width = `${share}%`;
    segment.style.backgroundColor = color;
    segment.title = label;
    bar.append(segment);
    const row = document.createElement("li"), dot = document.createElement("span");
    dot.className = "dot";
    dot.style.backgroundColor = color;
    row.append(dot);
    for (const [text, className] of [[site.domain, "site"], [`${share.toFixed(1)}%`, "number"], [duration(site.seconds), "number"]]) {
      const cell = document.createElement("span");
      cell.className = className;
      cell.textContent = text;
      cell.title = text;
      row.append(cell);
    }
    list.append(row);
  });
  bar.setAttribute("aria-label", labels.join("; ") || "No saved website time available");
}
async function render() {
  const revision = ++renderRevision;
  const state = await chrome.storage.local.get(["enabled", "status", "browser", "statusAt"]);
  let usage = null;
  if (state.enabled && (!state.statusAt || Date.now() - state.statusAt <= 12000)) {
    try { usage = (await chrome.runtime.sendMessage({type: "getUsage"}))?.usage; }
    catch { /* Worker unavailable; clear all usage details. */ }
  }
  if (revision !== renderRevision) return;
  enabled = state.enabled === true;
  const status = state.statusAt && Date.now() - state.statusAt > 12000
    ? "Status is stale; waiting for a fresh Hub response" : state.status;
  document.getElementById("browser").value = ["chrome", "edge", "brave"].includes(state.browser) ? state.browser : "";
  document.getElementById("toggle").textContent = enabled ? "Pause browser tracking" : "Enable browser tracking";
  document.getElementById("status").textContent = enabled ? (status || "Connecting to Hub…") : "Paused";
  renderUsage(usage);
}
document.getElementById("toggle").addEventListener("click", async () => {
  if (enabled) await chrome.storage.local.set({enabled: false, status: "Paused"});
  else if (await chrome.permissions.request({permissions: ["tabs", "nativeMessaging"]})) {
    await chrome.storage.local.set({enabled: true, status: "Connecting to Hub…"});
  }
  await render();
});
document.getElementById("browser").addEventListener("change", async event => {
  await chrome.storage.local.set({browser: event.target.value});
});
chrome.storage.onChanged.addListener(render);
render();
setInterval(render, 2000);
