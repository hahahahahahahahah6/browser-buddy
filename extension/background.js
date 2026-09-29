// Browser Buddy — background service worker (Manifest V3).
//
// Owns a persistent Native Messaging port to the local host process
// (com.browserbuddy.host). The host relays tool calls from the MCP server;
// this worker executes them against the user's REAL browser profile
// (cookies, logins, sessions included) and posts the results back.
//
// Protocol (host -> extension):
//   { "id": <number>, "cmd": "read_active_tab" }
//   { "id": <number>, "cmd": "open_and_read_url", "url": "<https url>" }
//   { "type": "hello_ack" }
// Protocol (extension -> host):
//   { "type": "hello" }
//   { "id": <number>, "ok": true, "url": ..., "title": ..., "text": ..., "truncated": <bool> }
//   { "id": <number>, "ok": false, "error": "..." }

const HOST_NAME = "com.browserbuddy.host";
const LOAD_TIMEOUT_MS = 45000;
const MAX_TEXT_CHARS = 60000;

let port = null;
let reconnectDelayMs = 1000;

function log(...args) {
  console.log("[browser-buddy]", ...args);
}

function safePost(msg) {
  if (!port) {
    log("no native port, dropping message", msg && (msg.cmd || msg.type));
    return false;
  }
  try {
    port.postMessage(msg);
    return true;
  } catch (e) {
    log("postMessage failed:", e);
    return false;
  }
}

function scheduleReconnect() {
  const delay = reconnectDelayMs;
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000);
  log(`reconnecting native host in ${delay}ms`);
  setTimeout(connectNative, delay);
}

function connectNative() {
  let p;
  try {
    p = chrome.runtime.connectNative(HOST_NAME);
  } catch (e) {
    log("connectNative threw:", e);
    scheduleReconnect();
    return;
  }
  port = p;
  reconnectDelayMs = 1000;
  p.onMessage.addListener(onHostMessage);
  p.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    log("native port disconnected", err ? err.message : "");
    port = null;
    scheduleReconnect();
  });
  safePost({ type: "hello" });
  log("native host connected");
}

// Runs inside the target tab. Returns { title, text, truncated }.
function extractPage() {
  const title = document.title || "";
  const root = document.body || document.documentElement;
  if (!root) return { title, text: "", truncated: false };
  const clone = root.cloneNode(true);
  clone.querySelectorAll(
    "script, style, noscript, svg, canvas, video, audio, iframe, template"
  ).forEach((el) => el.remove());
  let text = (clone.innerText || "").replace(/[ \t\u00a0]+/g, " ");
  text = text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .join("\n")
    .trim();
  const truncated = text.length > MAX_TEXT_CHARS;
  return { title, text: text.slice(0, MAX_TEXT_CHARS), truncated };
}

async function extractFromTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: extractPage,
  });
  const extracted = (results && results[0] && results[0].result) || {
    title: "",
    text: "",
    truncated: false,
  };
  return {
    url: tab.url || "",
    title: extracted.title,
    text: extracted.text,
    truncated: extracted.truncated,
  };
}

async function readActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tabs || tabs.length === 0) throw new Error("no active tab found");
  return extractFromTab(tabs[0].id);
}

function waitForTabLoad(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("page load timed out"));
    }, timeoutMs);
    function onUpdated(updatedId, info) {
      if (updatedId === tabId && info.status === "complete") {
        cleanup();
        resolve();
      }
    }
    function cleanup() {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
    }
    chrome.tabs.onUpdated.addListener(onUpdated);
  });
}

function isHttpUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

async function openAndReadUrl(url) {
  if (!isHttpUrl(url)) throw new Error("only http(s) URLs are allowed");
  const tab = await chrome.tabs.create({ url, active: false });
  try {
    await waitForTabLoad(tab.id, LOAD_TIMEOUT_MS);
    // Give client-rendered pages a beat to settle.
    await new Promise((r) => setTimeout(r, 800));
    return await extractFromTab(tab.id);
  } finally {
    try {
      await chrome.tabs.remove(tab.id);
    } catch (e) {
      // tab already gone; ignore
    }
  }
}

async function onHostMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "hello_ack") {
    log("handshake complete");
    return;
  }
  if (typeof msg.id === "undefined" || typeof msg.cmd !== "string") {
    log("ignoring malformed host message", msg);
    return;
  }
  const { id, cmd } = msg;
  try {
    let data;
    if (cmd === "read_active_tab") {
      data = await readActiveTab();
    } else if (cmd === "open_and_read_url") {
      data = await openAndReadUrl(msg.url);
    } else {
      throw new Error(`unknown cmd: ${cmd}`);
    }
    safePost({ id, ok: true, ...data });
  } catch (e) {
    safePost({ id, ok: false, error: String((e && e.message) || e) });
  }
}

chrome.runtime.onInstalled.addListener(() => {
  log("installed, connecting native host");
  connectNative();
});

chrome.runtime.onStartup.addListener(() => {
  log("browser startup, connecting native host");
  connectNative();
});

// Service workers can start without onInstalled/onStartup firing in some
// flows (e.g. extension reloaded); connect eagerly on evaluation too.
connectNative();
