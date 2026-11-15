let enabled = false;
async function render() {
  const state = await chrome.storage.local.get(["enabled", "status", "browser", "statusAt"]);
  enabled = state.enabled === true;
  const status = state.statusAt && Date.now() - state.statusAt > 12000
    ? "Status is stale; waiting for a fresh Hub response" : state.status;
  document.getElementById("browser").value = ["chrome", "edge", "brave"].includes(state.browser) ? state.browser : "";
  document.getElementById("toggle").textContent = enabled ? "Pause browser tracking" : "Enable browser tracking";
  document.getElementById("status").textContent = enabled ? (status || "Connecting to Hub…") : "Paused";
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
