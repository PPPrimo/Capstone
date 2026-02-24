const el = document.getElementById('liveData');

function setText(text) {
  if (!el) return;
  el.textContent = text;
}

// ── WebSocket connection with auto-reconnect ──
const WS_RECONNECT_MS = 1000;
let ws = null;
let alive = true;  // false when page is being unloaded / hidden

function connect() {
  if (!alive) return;

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/api/ws`);

  ws.onopen = () => {
    setText('Connected — waiting for data...');
  };

  ws.onmessage = (ev) => {
    try {
      const obj = JSON.parse(ev.data);
      if (obj.ping) return;            // keepalive, ignore
      if (obj.payload) {
        setText(JSON.stringify(obj, null, 2));
      }
    } catch { /* ignore malformed */ }
  };

  ws.onclose = () => {
    if (!alive) return;                // don't reconnect if page is going away
    setText('Disconnected. Reconnecting...');
    setTimeout(connect, WS_RECONNECT_MS);
  };

  ws.onerror = () => {
    ws.close();
  };
}

// Clean up when the iframe is unloaded (tab switch) to prevent zombie connections
function cleanup() {
  alive = false;
  if (ws) {
    ws.onclose = null;  // prevent reconnect
    ws.close();
    ws = null;
  }
}
window.addEventListener('beforeunload', cleanup);
window.addEventListener('pagehide', cleanup);

connect();
