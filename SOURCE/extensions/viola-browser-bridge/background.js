const VIOLA_HEALTH_PATH = "/health";
const VIOLA_RUNTIME_CACHE = "viola-browser-bridge-runtime";
const VIOLA_RUNTIME_CACHE_KEY = "https://viola.invalid/runtime-base-url";
const TRUSTED_LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);
const TRUSTED_VIOLA_ORIGINS = new Set([
  "https://api.useviola.com",
  "https://useviola.com",
  "https://www.useviola.com",
]);
const HEALTHY_STATUS_VALUES = new Set(["ok", "healthy", "ready", "live", "operational"]);

let resolvedViolaBaseUrl = null;

function isTrustedLoopbackOrigin(url) {
  return url.protocol === "http:" && TRUSTED_LOOPBACK_HOSTS.has(url.hostname);
}

function isTrustedViolaOrigin(url) {
  return url.protocol === "https:" && TRUSTED_VIOLA_ORIGINS.has(url.origin);
}

function normalizeBaseUrl(value) {
  if (!value || typeof value !== "string") {
    return null;
  }

  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return null;
    }
    if (!isTrustedLoopbackOrigin(url) && !isTrustedViolaOrigin(url)) {
      return null;
    }
    return url.origin;
  } catch (_error) {
    return null;
  }
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

async function loadCachedBaseUrl() {
  try {
    const cache = await caches.open(VIOLA_RUNTIME_CACHE);
    const response = await cache.match(VIOLA_RUNTIME_CACHE_KEY);
    if (!response) {
      return null;
    }
    return normalizeBaseUrl(await response.text());
  } catch (_error) {
    return null;
  }
}

async function saveCachedBaseUrl(baseUrl) {
  const normalized = normalizeBaseUrl(baseUrl);
  if (!normalized) {
    return;
  }

  const cache = await caches.open(VIOLA_RUNTIME_CACHE);
  await cache.put(VIOLA_RUNTIME_CACHE_KEY, new Response(normalized));
}

async function clearCachedBaseUrl() {
  const cache = await caches.open(VIOLA_RUNTIME_CACHE);
  await cache.delete(VIOLA_RUNTIME_CACHE_KEY);
}

async function getTabBaseUrlCandidates() {
  const tabs = [];

  try {
    tabs.push(...await chrome.tabs.query({ active: true, currentWindow: true }));
  } catch (_error) {
    // Best effort only; continue with any cached runtime base URL.
  }

  try {
    tabs.push(...await chrome.tabs.query({ url: [
      "http://localhost/*",
      "http://127.0.0.1/*",
      "http://[::1]/*",
      "https://api.useviola.com/*",
      "https://useviola.com/*",
      "https://www.useviola.com/*",
    ] }));
  } catch (_error) {
    // Host filtering below handles missing or unreadable tab URLs.
  }

  const candidates = [];
  for (const tab of tabs) {
    const baseUrl = normalizeBaseUrl(tab && tab.url);
    if (baseUrl) {
      candidates.push(baseUrl);
    }
  }

  return unique(candidates);
}

function isViolaHealthPayload(payload) {
  if (!payload || typeof payload !== "object") {
    return false;
  }
  const service = typeof payload.service === "string" ? payload.service.toLowerCase() : "";
  const status = typeof payload.status === "string" ? payload.status.toLowerCase() : "";
  return (
    service.startsWith("viola") ||
    payload.ok === true ||
    payload.ready === true ||
    HEALTHY_STATUS_VALUES.has(status) ||
    typeof payload.uptime_s === "number"
  );
}

async function isViolaBaseUrl(baseUrl) {
  if (!baseUrl) {
    return false;
  }

  try {
    const response = await fetch(`${baseUrl}${VIOLA_HEALTH_PATH}`, { method: "GET" });
    if (!response.ok) {
      return false;
    }

    const payload = await response.json();
    return isViolaHealthPayload(payload);
  } catch (_error) {
    return false;
  }
}

async function resolveViolaBaseUrl() {
  const candidates = unique([
    resolvedViolaBaseUrl,
    await loadCachedBaseUrl(),
    ...(await getTabBaseUrlCandidates()),
  ]);

  for (const candidate of candidates) {
    if (await isViolaBaseUrl(candidate)) {
      resolvedViolaBaseUrl = candidate;
      await saveCachedBaseUrl(candidate);
      return candidate;
    }
  }

  resolvedViolaBaseUrl = null;
  await clearCachedBaseUrl();
  return null;
}

async function setResolvedBaseUrl(baseUrl) {
  const normalized = normalizeBaseUrl(baseUrl);
  if (!normalized) {
    return { success: false, message: "Unsupported Viola base URL" };
  }

  if (!(await isViolaBaseUrl(normalized))) {
    return {
      success: false,
      message: "Viola backend was not reachable at the provided base URL",
    };
  }

  resolvedViolaBaseUrl = normalized;
  await saveCachedBaseUrl(normalized);
  return { success: true, base_url: normalized };
}

async function clearResolvedBaseUrl() {
  resolvedViolaBaseUrl = null;
  await clearCachedBaseUrl();
  return { success: true };
}

async function syncCookies() {
  return {
    success: false,
    count: 0,
    message: "Browser session sync is disabled in this public build.",
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "sync") {
    syncCookies().then(sendResponse);
    return true;
  }
  if (msg.action === "status") {
    resolveViolaBaseUrl()
      .then((baseUrl) => sendResponse({ viola_running: Boolean(baseUrl), base_url: baseUrl }))
      .catch(() => sendResponse({ viola_running: false, base_url: null }));
    return true;
  }
  if (msg.action === "set_base_url") {
    setResolvedBaseUrl(msg.base_url).then(sendResponse);
    return true;
  }
  if (msg.action === "clear_base_url") {
    clearResolvedBaseUrl().then(sendResponse);
    return true;
  }
});
