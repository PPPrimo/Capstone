const el = document.getElementById('liveData');

function setText(text) {
  if (!el) return;
  el.textContent = text;
}

// ── WebSocket connection with auto-reconnect ──
const WS_RECONNECT_MS = 1000;
const WS_STALE_MS = 3000; // if no ping/data for this long, force reconnect
let ws = null;
let shouldReconnect = true; // false when page is being unloaded; true on normal display
let lastMessageAt = Date.now();
let staleTimer = null;

function startStaleWatchdog() {
  if (staleTimer) return;
  staleTimer = setInterval(() => {
    if (!shouldReconnect) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - lastMessageAt > WS_STALE_MS) {
      try { ws.close(); } catch { /* ignore */ }
    }
  }, 1000);
}

function stopStaleWatchdog() {
  if (!staleTimer) return;
  clearInterval(staleTimer);
  staleTimer = null;
}

function connect() {
  if (!shouldReconnect) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/api/ws`);

  ws.onopen = () => {
    setText('Connected — waiting for data...');
    lastMessageAt = Date.now();
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

  ws.onclose = () => {
    stopStaleWatchdog();
    if (!shouldReconnect) return;      // don't reconnect if page is going away
    setText('Disconnected. Reconnecting...');
    setTimeout(connect, WS_RECONNECT_MS);
  };

  ws.onerror = () => {
    ws.close();
  };
}

// Clean up when the iframe is unloaded (tab switch) to prevent zombie connections
function cleanup() {
  shouldReconnect = false;
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
