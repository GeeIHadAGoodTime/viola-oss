const statusEl = document.getElementById("status");
const baseUrlInput = document.getElementById("base-url-input");
const connectBtn = document.getElementById("connect-btn");
const disconnectBtn = document.getElementById("disconnect-btn");
const resultEl = document.getElementById("result");

function sendBridgeMessage(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ success: false, message: chrome.runtime.lastError.message });
        return;
      }
      resolve(response || {});
    });
  });
}

function setStatus(connected, baseUrl) {
  if (connected) {
    statusEl.textContent = "Connected to Viola";
    statusEl.className = "status connected";
    baseUrlInput.value = baseUrl || "";
    disconnectBtn.disabled = false;
    resultEl.textContent = "Browser cookies are not imported or synced.";
    resultEl.style.color = "#9ca3af";
    return;
  }
  statusEl.textContent = "Viola is not connected";
  statusEl.className = "status disconnected";
  disconnectBtn.disabled = true;
  resultEl.textContent = "Enter your local Viola URL or https://api.useviola.com.";
  resultEl.style.color = "#9ca3af";
}

async function refreshStatus() {
  const res = await sendBridgeMessage({ action: "status" });
  if (res && res.viola_running) {
    setStatus(true, res.base_url);
  } else {
    setStatus(false, null);
  }
}

connectBtn.addEventListener("click", async () => {
  const baseUrl = baseUrlInput.value.trim();
  if (!baseUrl) {
    resultEl.textContent = "Enter a Viola base URL first.";
    resultEl.style.color = "#f87171";
    return;
  }

  if (!window.confirm(`Connect Viola Browser Bridge to ${baseUrl}?`)) {
    return;
  }

  connectBtn.disabled = true;
  resultEl.textContent = "Checking Viola...";
  resultEl.style.color = "#9ca3af";
  const res = await sendBridgeMessage({ action: "set_base_url", base_url: baseUrl });
  connectBtn.disabled = false;
  if (res && res.success) {
    setStatus(true, res.base_url);
    return;
  }
  resultEl.textContent = res && res.message ? res.message : "Could not connect to Viola.";
  resultEl.style.color = "#f87171";
});

disconnectBtn.addEventListener("click", async () => {
  disconnectBtn.disabled = true;
  await sendBridgeMessage({ action: "clear_base_url" });
  baseUrlInput.value = "";
  setStatus(false, null);
});

refreshStatus();
