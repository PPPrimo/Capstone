const el = document.getElementById('liveData');

function setText(text) {
  if (!el) return;
  el.textContent = text;
}

// ── WebSocket connection with auto-reconnect ──
// Teleop constraints:
// - reconnect within 1s
// - treat the connection as stale quickly if pings/data stop
const WS_RECONNECT_BASE_MS = 250;
const WS_RECONNECT_MAX_MS = 1000;
const WS_STALE_MS = 2000; // no ping/data for this long => reconnect
let ws = null;
let shouldReconnect = true; // false when page is being unloaded; true on normal display
let lastMessageAt = Date.now();
let staleTimer = null;
let reconnectDelayMs = WS_RECONNECT_BASE_MS;
let reconnectTimer = null;

function startStaleWatchdog() {
  if (staleTimer) return;
  staleTimer = setInterval(() => {
    if (!shouldReconnect) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - lastMessageAt > WS_STALE_MS) {
      try { ws.close(); } catch { /* ignore */ }
    }
  }, 250);
}

function stopStaleWatchdog() {
  if (!staleTimer) return;
  clearInterval(staleTimer);
  staleTimer = null;
}

function scheduleReconnect() {
  if (!shouldReconnect) return;
  if (reconnectTimer) return;
  const delay = Math.min(reconnectDelayMs, WS_RECONNECT_MAX_MS);
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, WS_RECONNECT_MAX_MS);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

function connect() {
  if (!shouldReconnect) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/api/ws`);

  ws.onopen = () => {
    setText('Connected — waiting for data...');
    lastMessageAt = Date.now();
    reconnectDelayMs = WS_RECONNECT_BASE_MS;
    startStaleWatchdog();
  };

  ws.onmessage = (ev) => {
    try {
      const obj = JSON.parse(ev.data);
      lastMessageAt = Date.now();
      if (obj.ping) return;            // keepalive, ignore
      if (obj.payload) {
        setText(JSON.stringify(obj, null, 2));
      }
    } catch { /* ignore malformed */ }
  };

  ws.onclose = (ev) => {
    stopStaleWatchdog();
    if (!shouldReconnect) return;      // don't reconnect if page is going away

    // Auth failed/expired: server uses 4401/4403. Retrying won't help.
    if (ev && (ev.code === 4401 || ev.code === 4403)) {
      shouldReconnect = false;
      setText('Disconnected: Unauthorized. Please refresh and log in again.');
      return;
    }

    setText('Disconnected. Reconnecting...');
    scheduleReconnect();
  };

  ws.onerror = () => {
    ws.close();
  };
}

// Clean up when the iframe is unloaded (tab switch) to prevent zombie connections
function cleanup() {
  shouldReconnect = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (ws) {
    ws.onclose = null;  // prevent reconnect
    ws.close();
    ws = null;
  }
  stopStaleWatchdog();
}
window.addEventListener('beforeunload', cleanup);
window.addEventListener('pagehide', cleanup);

// If the browser restores this document from the back/forward cache (bfcache),
// scripts do NOT re-run. We must explicitly resume and reconnect.
window.addEventListener('pageshow', () => {
  shouldReconnect = true;
  if (!ws || ws.readyState === WebSocket.CLOSED) connect();
});

connect();
