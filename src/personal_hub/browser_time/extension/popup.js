let enabled = false;
async function render() {
  const state = await chrome.storage.local.get(["enabled", "status"]);
  enabled = state.enabled === true;
  const status = state.status;
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
chrome.storage.onChanged.addListener(render);
render();
